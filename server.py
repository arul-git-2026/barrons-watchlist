"""
Streetwise Terminal — server.py
──────────────────────────────────────────────────────────────────────────────
Data sources
  Live quotes  →  Yahoo Finance via yfinance (cached in memory, TTL 5 min)
  Price history→  price_history.db (SQLite) — built by history_manager.py
                  Falls back to Yahoo Finance when DB rows are missing,
                  then saves the new rows to DB for next time.
  Static data  →  streetwise_data.json

Install
-------
  pip install flask flask-cors yfinance

Section index (grep for the ── marker to jump to any section)
──────────────────────────────────────────────────────────────────────────────
  ── .env loader          line ~47   reads .env and /etc/streetwise.env
  ── Logging              line ~72   ColourFormatter + werkzeug silence
  ── Token pricing        line ~138  _TOKEN_PRICES dict — update when rates change
  ── Cost ledger          line ~156  log_cost() → costs_log.json
  ── Token auth           line ~188  _check_token before_request hook
  ── Config               line ~325  DATA_FILE, CACHE_TTL, RANGES, MIN_ROWS, YAHOO_MAP
  ── In-memory cache      line ~370  cache_get / cache_set / cache_expire
  ── Static data          line ~385  load_data / load_sources / save_sources / save_tickers
  ── SQLite helpers       line ~445  get_db / db_get_history
  ── /v2 route            line ~745  Jinja2 render_template with cache headers
  ── /api/data            line ~774  quote refresh + static data endpoint
  ── __main__             line ~4543 startup banner + Flask run
"""

from __future__ import annotations

from flask import Flask, jsonify, send_file, render_template, request, session, make_response
import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import time
import logging
from datetime import datetime, date, timedelta
from flask_cors import CORS

# ── .env loader ───────────────────────────────────────────────────────────────
# Reads KEY=VALUE pairs and injects them into os.environ (existing vars win).
# Checks, in order: .env next to server.py, then /etc/streetwise.env (Linux).
# EDIT: To add a new search path, append it to the _load_dotenv() call below.
# DEBUG: If "API key not set" on startup but key is in .env:
#   1. Confirm .env is in the same folder as server.py (not the repo root)
#   2. Check for trailing spaces or stray quotes around the value in .env
#   3. Verify the shell environment doesn't already have a different value set
#      (shell env always wins — unset it with: del os.environ['KEY'] for testing)
# WHY PermissionError catch: on Linux the service user may lack read access to /etc/streetwise.env
def _load_dotenv(*paths):
    for path in paths:
        try:
            with open(path, encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k, _, _v = _line.partition("=")
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
        except (FileNotFoundError, PermissionError, OSError):
            pass

_load_dotenv(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    "/etc/streetwise.env",
)

# ── Logging ───────────────────────────────────────────────────────────────────
import sys

class _ColourFormatter(logging.Formatter):
    """Coloured, timestamped terminal output."""
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    CYAN   = "\033[36m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    BLUE   = "\033[34m"
    MAGENTA= "\033[35m"

    LEVEL_COLOURS = {
        "DEBUG":    "\033[2m",          # dim
        "INFO":     "\033[36m",          # cyan
        "WARNING":  "\033[33m",          # yellow
        "ERROR":    "\033[31m",          # red
        "CRITICAL": "\033[1m\033[31m",  # bold red
    }
    LEVEL_ICONS = {
        "DEBUG":    "·",
        "INFO":     "●",
        "WARNING":  "⚠",
        "ERROR":    "✗",
        "CRITICAL": "✗✗",
    }

    def format(self, record: logging.LogRecord) -> str:
        ts      = self.formatTime(record, "%H:%M:%S")
        level   = record.levelname
        colour  = self.LEVEL_COLOURS.get(level, "")
        icon    = self.LEVEL_ICONS.get(level, "·")
        msg     = record.getMessage()

        # Dim timestamp, coloured icon+level, normal message
        return (
            f"{self.DIM}{ts}{self.RESET}  "
            f"{colour}{icon} {level:<8}{self.RESET} "
            f"{msg}"
        )

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_ColourFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger(__name__)

# Silence Flask's noisy default request logger — we print our own
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)
app.secret_key = os.environ.get("STREETWISE_TOKEN", "dev-secret-key-local")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
CORS(app, supports_credentials=True)

@app.errorhandler(Exception)
def _handle_exception(e):
    import traceback
    log.error(f"Unhandled {type(e).__name__}: {e} | {traceback.format_exc()[:400]}")
    return jsonify({"ok": False, "error": str(e), "type": type(e).__name__}), 500

@app.errorhandler(404)
def _handle_404(e):
    return jsonify({"ok": False, "error": "Not found"}), 404

# ── Token pricing (per million tokens, March 2026) ────────────────────────────
_TOKEN_PRICES = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-haiku-4-5":          {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-5":         {"input": 3.00,  "output": 15.00},
    "claude-opus-4-5":           {"input": 15.00, "output": 75.00},
}

def calc_cost(response) -> tuple[int, int, float]:
    """Return (input_tokens, output_tokens, cost_usd) from an API response."""
    usage  = getattr(response, "usage", None)
    inp    = getattr(usage, "input_tokens",  0) if usage else 0
    out    = getattr(usage, "output_tokens", 0) if usage else 0
    model  = getattr(response, "model", "claude-haiku-4-5-20251001")
    prices = _TOKEN_PRICES.get(model, _TOKEN_PRICES["claude-haiku-4-5-20251001"])
    cost   = (inp * prices["input"] + out * prices["output"]) / 1_000_000
    return inp, out, cost


# ── Cost ledger ───────────────────────────────────────────────────────────────
COSTS_FILE = "costs_log.json"
_costs_lock = __import__("threading").Lock()

def log_cost(service: str, ticker: str, inp: int, out: int, cost: float, model: str = ""):
    """Append one cost event to costs_log.json atomically."""
    entry = {
        "ts":      datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "date":    datetime.utcnow().strftime("%Y-%m-%d"),
        "service": service,
        "ticker":  ticker,
        "model":   model,
        "inp":     inp,
        "out":     out,
        "cost":    round(cost, 6),
    }
    with _costs_lock:
        try:
            if os.path.exists(COSTS_FILE):
                with open(COSTS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = []
            data.append(entry)
            tmp = COSTS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, COSTS_FILE)
        except Exception as e:
            log.warning(f"log_cost failed: {e}")

# ── Token auth (Oracle Cloud deployment) ─────────────────────────────────────
# Set STREETWISE_TOKEN in /etc/streetwise.env on the server.
# When set, every request must include ?token=<value> or header X-Streetwise-Token.
# When not set (local dev), auth is skipped entirely.
_AUTH_TOKEN: str = os.environ.get("STREETWISE_TOKEN", "").strip()

_AUTH_EXEMPT = {"/favicon.ico", "/health", "/api/costs", "/api/crisis-monitor", "/login", "/logout"}

@app.before_request
def _check_token():
    """Reject requests that don't carry valid auth.

    Auth passes if ANY of these are true:
      1. STREETWISE_TOKEN env var is not set    → local dev, no auth
      2. Path is in _AUTH_EXEMPT               → public endpoints
      3. session['auth'] == True               → 30-day browser session cookie
      4. X-Streetwise-Token header matches     → Chrome extension
    Browser requests that fail redirect to /login.
    API/AJAX requests that fail return 403 JSON.

    DEBUG: If the Chrome extension gets 403/CORS errors:
      - OPTIONS preflight is allowed through unconditionally (see comment below)
      - The extension sends X-Streetwise-Token on every request via authHeaders()
      - If token is wrong/missing, check chrome.storage.local → serverToken
    DEBUG: If browser sessions expire unexpectedly, check PERMANENT_SESSION_LIFETIME
      and that app.secret_key is stable across restarts (it uses STREETWISE_TOKEN).
    EDIT: To make a new endpoint public (no auth), add its path to _AUTH_EXEMPT above.
    """
    if not _AUTH_TOKEN:
        return
    # CORS preflights carry no credentials — let Flask-CORS handle them
    if request.method == "OPTIONS":
        return
    if request.path in _AUTH_EXEMPT:
        return
    if session.get("auth"):
        return
    # Chrome extension — API key in header
    if request.headers.get("X-Streetwise-Token", "") == _AUTH_TOKEN:
        return
    # Not authenticated — redirect browsers to /login, return 403 for API calls
    log.warning(f"  auth: rejected {request.remote_addr} → {request.path}")
    wants_html = "text/html" in request.headers.get("Accept", "")
    if wants_html and request.method == "GET":
        from flask import redirect
        return redirect(f"/login?next={request.path}")
    return jsonify({"ok": False, "error": "Unauthorized"}), 403


_LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Streetwise \u2014 Login</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
  .card{{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:36px 40px;
        width:100%;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.4)}}
  .logo{{font-size:13px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;
        gap:8px;margin-bottom:28px}}
  .logo span{{background:#3b82f6;color:#fff;padding:3px 9px;border-radius:5px;font-size:12px}}
  label{{display:block;font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;
        letter-spacing:.06em;margin-bottom:6px}}
  input[type=password]{{width:100%;padding:10px 12px;background:#0f172a;border:1px solid #334155;
        border-radius:7px;color:#e2e8f0;font-size:14px;outline:none;transition:border-color .15s}}
  input[type=password]:focus{{border-color:#3b82f6}}
  button{{width:100%;padding:11px;background:#3b82f6;color:#fff;border:none;border-radius:7px;
         font-size:13px;font-weight:600;cursor:pointer;margin-top:16px;transition:background .15s}}
  button:hover{{background:#2563eb}}
  .err{{background:#2d0f0f;color:#fca5a5;border:1px solid #7f1d1d;border-radius:6px;
       padding:9px 12px;font-size:12px;margin-bottom:16px}}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><span>STW</span> Streetwise</div>
  {error}
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{next}">
    <label>Passphrase</label>
    <input type="password" name="passphrase" autofocus placeholder="Enter passphrase">
    <button type="submit">Sign in</button>
  </form>
</div>
</body>
</html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    from flask import redirect, make_response as _mr
    if request.method == "POST":
        pw   = request.form.get("passphrase", "").strip()
        next_url = request.form.get("next", "/v2")
        if pw == _AUTH_TOKEN:
            session.permanent = True
            session["auth"]   = True
            return redirect(next_url or "/v2")
        html = _LOGIN_PAGE.format(
            error='<div class="err">⚠ Incorrect passphrase</div>',
            next=next_url or "/v2"
        )
        return html, 401
    next_url = request.args.get("next", "/v2")
    return _LOGIN_PAGE.format(error="", next=next_url)


@app.route("/logout")
def logout():
    from flask import redirect
    session.clear()
    return redirect("/login")


@app.before_request
def _log_request():
    """Print every incoming request to the terminal."""
    import flask
    flask.g._req_start = time.time()
    # Don't clutter the terminal with the auto-refresh polling
    if request.path not in ("/api/data",) or request.args.get("tickers"):
        log.info(f"→ {request.method} {request.full_path.rstrip('?')}")

@app.after_request
def _log_response(response):
    """Print response status + elapsed time."""
    import flask
    elapsed = (time.time() - getattr(flask.g, "_req_start", time.time())) * 1000
    path    = request.path
    # Skip noisy silent auto-refreshes
    if path == "/api/data" and not request.args.get("tickers"):
        return response
    status  = response.status_code
    colour  = "\033[32m" if status < 300 else "\033[33m" if status < 400 else "\033[31m"
    reset   = "\033[0m"
    log.info(f"← {colour}{status}{reset}  {path}  {elapsed:.0f}ms")
    return response

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE = "streetwise_data.json"
SOURCES_FILE = "sources.json"
DB_FILE   = "price_history.db"
CACHE_TTL = 300     # seconds before a cached live quote is considered stale

# All supported history ranges
# key → (days_back_for_db_query, yf_period, yf_interval)
# days_back=None means YTD (Jan 1 of current year)
# 1D and 5D use intraday intervals — never stored in DB, always live from Yahoo
RANGES = {
    "1D":  (1,    "1d",   "5m"),
    "5D":  (5,    "5d",   "1h"),
    "1W":  (7,    "5d",   "1d"),
    "1M":  (30,   "1mo",  "1d"),
    "3M":  (90,   "3mo",  "1d"),
    "6M":  (180,  "6mo",  "1d"),
    "YTD": (None, "ytd",  "1d"),
    "1Y":  (365,  "1y",   "1d"),
    "2Y":  (730,  "2y",   "1wk"),
    "3Y":  (1095, "3y",   "1wk"),
}

# Minimum DB rows expected per range before trusting the DB
# 1D and 5D are always fetched live from Yahoo (intraday)
MIN_ROWS = {
    "1D":  0, "5D":  0,
    "1W":  3, "1M":  15,
    "3M":  45, "6M": 90,
    "YTD": 5, "1Y":  200,
    "2Y":  400, "3Y": 600,
}

# Tickers whose Yahoo Finance symbol differs from the stored key
YAHOO_MAP = {
    "WALMEX":  "WALMEX.MX",
    "000660":  "000660.KS",
    "SSNLF":   "005930.KS",
    "2282.HK": "2282.HK",
    "FRFHF":   "FRFHF",
}

# ── In-memory live quote cache ────────────────────────────────────────────────
# { "AAPL": { ...quote fields..., "_ts": epoch_float } }
_cache: dict = {}

def cache_get(ticker: str) -> dict | None:
    entry = _cache.get(ticker)
    if entry and (time.time() - entry.get("_ts", 0)) < CACHE_TTL:
        return {k: v for k, v in entry.items() if k != "_ts"}
    return None

def cache_set(ticker: str, data: dict):
    _cache[ticker] = {**data, "_ts": time.time()}

def cache_expire(tickers: list[str]):
    """Set _ts to 0 so the next fetch goes to Yahoo Finance."""
    for t in tickers:
        if t in _cache:
            _cache[t]["_ts"] = 0
    log.info(f"Cache expired: {tickers}")

# ── Static data ───────────────────────────────────────────────────────────────

def load_data_raw() -> list[dict]:
    """Load ALL records including __meta__ entries."""
    if not os.path.exists(DATA_FILE):
        log.warning(f"{DATA_FILE} not found")
        return []
    try:
        with open(DATA_FILE, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed to load {DATA_FILE}: {e}")
        return []


def load_data() -> list[dict]:
    """Load ticker records only — skips __meta__ entries."""
    return [r for r in load_data_raw() if not r.get("__meta__")]


def load_sources() -> dict:
    """Read __sources__ meta-record from streetwise_data.json."""
    for rec in load_data_raw():
        if rec.get("t") == "__sources__":
            return rec.get("sources") or {}
    return {}


def save_sources(registry: dict):
    """Write __sources__ meta-record back into streetwise_data.json atomically."""
    db    = load_data_raw()
    found = False
    for rec in db:
        if rec.get("t") == "__sources__":
            rec["sources"] = registry
            found = True
            break
    if not found:
        db.insert(0, {"t": "__sources__", "__meta__": True, "sources": registry})
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def save_tickers(ticker_records: list):
    """Atomically save ticker records, preserving __sources__ meta-record."""
    raw   = load_data_raw()
    meta  = [r for r in raw if r.get("__meta__")]
    tmap  = {r["t"]: r for r in ticker_records if r.get("t")}
    final = meta + list(tmap.values())
    tmp   = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def load_data_compat() -> list[dict]:
    """Alias kept for any code that still calls load_data() expecting raw list."""
    return load_data()

# ── SQLite helpers ────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection | None:
    if not os.path.exists(DB_FILE):
        return None
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def db_get_history(ticker: str, start: str, end: str) -> list[tuple]:
    """
    Read (date, close) rows from the DB for a date range.
    Returns list sorted ascending, or [] if DB is missing / no rows found.
    """
    conn = get_db()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT date, close FROM prices "
            "WHERE ticker = ? AND date >= ? AND date <= ? "
            "ORDER BY date ASC",
            (ticker.upper(), start, end)
        ).fetchall()
        return [(r["date"], r["close"]) for r in rows]
    except Exception as e:
        log.warning(f"DB read failed for {ticker}: {e}")
        return []
    finally:
        conn.close()


def db_upsert(ticker: str, rows: list[tuple]):
    """Write (date, close) rows to the DB, creating the table if needed."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            close  REAL NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ticker_date ON prices (ticker, date)"
    )
    data = [(ticker.upper(), d, c) for d, c in rows if d and c and c > 0]
    if data:
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?,?,?)",
            data
        )
        conn.commit()
    conn.close()

# ── Yahoo Finance helpers ─────────────────────────────────────────────────────

def currency_symbol(cur: str) -> str:
    return {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩"}.get(cur, cur + " ")


# Yahoo Finance quoteType codes that are NOT meaningful sector names
_QUOTE_TYPE_NOISE = {
    "ECNQQUOTE", "EQUITY", "ETF", "MUTUALFUND", "INDEX",
    "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION",
}

# Map quoteType → human-readable asset type (stored as "y" in JSON)
_QT_TO_TYPE = {
    "EQUITY":         "Stock",
    "ETF":            "ETF",
    "MUTUALFUND":     "Mutual Fund",
    "INDEX":          "Index",
    "CURRENCY":       "Currency",
    "CRYPTOCURRENCY": "Crypto",
    "FUTURE":         "Future",
    "OPTION":         "Option",
}

# Map quoteType → sector label (overrides Yahoo sector for non-equity types)
_QT_TO_SECTOR = {
    "ETF":        "ETF",
    "MUTUALFUND": "Mutual Fund",
    "INDEX":      "Index",
    "CURRENCY":   "Currency",
    "CRYPTOCURRENCY": "Crypto",
}

def _qt_to_type(info: dict) -> str:
    """Return asset type string derived from Yahoo quoteType."""
    qt = (info.get("quoteType") or "").upper()
    return _QT_TO_TYPE.get(qt, "Stock")

def _clean_sector(info: dict) -> str:
    """
    Return a clean sector string from a Yahoo Finance info dict.
    - For non-equity types (ETF, MutualFund…) returns a readable label
      instead of N/A so they group properly in the sector filter.
    - For equities uses Yahoo sector; falls back to N/A.
    """
    qt = (info.get("quoteType") or "").upper()

    # Non-equity types get their own sector label
    if qt in _QT_TO_SECTOR:
        return _QT_TO_SECTOR[qt]

    sector = info.get("sector", "") or ""
    if sector and sector.upper() not in _QUOTE_TYPE_NOISE:
        return sector

    return "N/A"


def fetch_quote(ticker: str) -> dict:
    """Return live quote for one ticker. Uses in-memory cache first."""
    hit = cache_get(ticker)
    if hit:
        log.debug(f"  cache hit: {ticker}")
        return hit
    log.info(f"  fetching live quote: {ticker}")

    y_sym = YAHOO_MAP.get(ticker, ticker)
    try:
        info  = yf.Ticker(y_sym).info
        cur   = info.get("currency", "USD")
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        prev  = info.get("regularMarketPreviousClose") or price
        diff  = price - prev
        pct   = (diff / prev * 100) if prev else 0
        rec_r = (info.get("recommendationKey") or "n/a").lower()
        rec   = rec_r.replace("_", " ").title() if rec_r != "n/a" else "N/A"
        data  = {
            "price_fmt":  f"{currency_symbol(cur)}{price:,.2f}",
            "price_raw":  price,
            "currency":   cur,
            "diff_raw":   round(diff, 3),
            "diff_fmt":   f"{'+' if diff >= 0 else ''}{diff:,.2f}",
            "pct_fmt":    f"{'+' if pct >= 0 else ''}{pct:.2f}%",
            "pct_raw":    round(pct, 3),
            "is_up":      diff >= 0,
            "y":          _qt_to_type(info),
            "sector":     _clean_sector(info),
            "industry":   info.get("industry", "") or "",
            "rec":        rec,
            "mktcap":       info.get("marketCap"),
            "pe":           info.get("trailingPE") or info.get("forwardPE"),
            "div_yield":    info.get("dividendYield"),   # Yahoo returns e.g. 0.92 meaning 0.92%
            "52w_high":     info.get("fiftyTwoWeekHigh"),
            "52w_low":      info.get("fiftyTwoWeekLow"),
            # ── Yahoo summary table fields ─────────────────────────────────────
            "prev_close":   info.get("regularMarketPreviousClose") or info.get("previousClose"),
            "open_price":   info.get("regularMarketOpen"),
            "bid":          info.get("bid"),
            "bid_size":     info.get("bidSize"),
            "ask":          info.get("ask"),
            "ask_size":     info.get("askSize"),
            "day_low":      info.get("regularMarketDayLow"),
            "day_high":     info.get("regularMarketDayHigh"),
            "volume":       info.get("regularMarketVolume"),
            "avg_volume":   info.get("averageVolume"),
            "beta":         info.get("beta"),
            "eps_ttm":      info.get("trailingEps"),
            "earnings_date": (
                datetime.utcfromtimestamp(info["earningsTimestamp"]).strftime("%b %d, %Y")
                if info.get("earningsTimestamp") else None
            ),
            "forward_div":  info.get("dividendRate"),           # annual $ amount
            "forward_yield": info.get("dividendYield"),          # decimal e.g. 0.031
            "ex_div_date": (
                datetime.utcfromtimestamp(info["exDividendDate"]).strftime("%b %d, %Y")
                if info.get("exDividendDate") else None
            ),
            "target_price": info.get("targetMeanPrice"),
            # ──────────────────────────────────────────────────────────────────
            "fetched_at":   datetime.utcnow().isoformat() + "Z",
        }
    except Exception as e:
        log.warning(f"Quote failed for {y_sym}: {e}")
        data = {
            "price_fmt": "N/A", "price_raw": 0,
            "diff_fmt":  "—",   "pct_fmt":  "—",
            "pct_raw":   0,     "is_up":    True,
            "sector":    "N/A", "rec":      "N/A",
            "fetched_at": None,
        }
    cache_set(ticker, data)
    return data


def fetch_yf_history(ticker: str, range_key: str) -> list[tuple] | None:
    """
    Fetch history from Yahoo Finance and save daily rows to DB.
    Returns [(date_str, close), ...] or None on failure.

    For 1D: prepends the previous close as the first data point so the
    chart baseline matches Google/Yahoo Finance (% change from prev close).
    """
    _, period, interval = RANGES[range_key]
    y_sym = YAHOO_MAP.get(ticker, ticker)
    try:
        tk   = yf.Ticker(y_sym)
        hist = tk.history(period=period, interval=interval, auto_adjust=True)
        closes = hist["Close"].dropna()
        if closes.empty:
            return None

        # For intraday (1D/5D) keep the full timestamp so the widget can show HH:MM
        # For daily/weekly use date-only strings (what the DB stores)
        if interval in ("1d", "1wk"):
            rows = [(str(d.date()), round(float(v), 4)) for d, v in zip(closes.index, closes)]
            db_upsert(ticker, rows)
            log.info(f"Saved {len(rows)} rows to DB for {ticker} ({range_key})")
        else:
            rows = [(str(d), round(float(v), 4)) for d, v in zip(closes.index, closes)]

        # ── 1D baseline fix ───────────────────────────────────────────────────
        # build_series() normalises everything relative to rows[0].
        # For 1D we want rows[0] = previous close so % change matches
        # Google Finance / Yahoo Finance exactly.
        if range_key == "1D":
            try:
                info       = tk.info
                prev_close = (
                    info.get("regularMarketPreviousClose") or
                    info.get("previousClose")
                )
                if prev_close and prev_close > 0:
                    # Synthesise a label just before market open (09:29)
                    # using the date of the first real bar
                    first_ts   = rows[0][0] if rows else ""
                    date_part  = first_ts.split(" ")[0] if " " in first_ts else first_ts[:10]
                    anchor_lbl = f"{date_part} 09:29:00-05:00"
                    rows.insert(0, (anchor_lbl, round(float(prev_close), 4)))
                    log.info(f"1D baseline for {ticker}: prev_close={prev_close}")
            except Exception as be:
                log.warning(f"Could not fetch prev_close for {ticker}: {be}")

        return rows
    except Exception as e:
        log.warning(f"YF history failed for {y_sym} ({range_key}): {e}")
        return None

# ── Series builder ────────────────────────────────────────────────────────────

def date_range_for(range_key: str) -> tuple[str, str]:
    today = date.today()
    if range_key == "YTD":
        start = date(today.year, 1, 1)
    else:
        days  = RANGES[range_key][0] or 365
        start = today - timedelta(days=days)
    return start.isoformat(), today.isoformat()


def build_series(rows: list[tuple]) -> dict:
    """
    Normalise a [(date_str, close), ...] list into a % return series.
    Returns {"labels", "values", "abs_values"} or {} if invalid.
    """
    if not rows or rows[0][1] == 0:
        return {}
    base = rows[0][1]
    return {
        "labels":     [r[0] for r in rows],
        "values":     [round((r[1] / base - 1) * 100, 3) for r in rows],
        "abs_values": [r[1] for r in rows],
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/sender")
def sender():
    """Standalone sender page — works from any browser, no extension needed."""
    resp = send_file("sender.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/")
def home():
    resp = send_file("widget.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@app.route("/v2")
def home_v2():
    """Jinja2 template — feature files live in templates/features/.

    Template structure:
      templates/widget_v2.html          ← shell: global CSS + JS in {% raw %} blocks
      templates/features/table_view.html
      templates/features/add_ticker.html
      templates/features/csv_import.html
      templates/features/watchlist.html
      templates/features/episode_brief.html
      templates/features/chart.html
      templates/features/crisis_monitor.html
      templates/features/heatmap.html

    DEBUG: If you see Jinja2 TemplateSyntaxError on startup:
      - CSS keyframes (e.g. @keyframes pulse{0%,100%{...}}) contain "}}" which Jinja2
        interprets as a template expression. Wrap the style block in {% raw %}...{% endraw %}.
      - Same applies to JS object literals containing "}},".
    DEBUG: If /v2 serves a stale page, these headers force a full reload — check browser DevTools
      → Network → Response Headers to confirm Cache-Control: no-store is present.
    """
    resp = make_response(render_template("widget_v2.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@app.route("/api/data")
def get_all_data():
    """
    Returns ticker data with optional live quotes.

    ?tickers=AAPL,MSFT  — fetch live quotes for those tickers only (refresh)
    no params           — return static data immediately, no Yahoo calls
                          (used on page load so the table appears instantly)

    The widget calls this once on load (no params) to paint the table fast,
    then the user manually refreshes selected tickers to get live prices.
    """
    raw = load_data()
    if not raw:
        return jsonify([])

    subset = request.args.get("tickers", "")
    if subset:
        # Selective refresh — only fetch quotes for the requested tickers
        wanted = {t.strip().upper() for t in subset.split(",") if t.strip()}
        log.info(f"  quote refresh: {sorted(wanted)}")
        t0 = time.time()
        changed = False
        for item in raw:
            if item.get("t", "").upper() in wanted:
                q = fetch_quote(item["t"])
                item.update(q)
                # Persist sector, rec, div_yield back to JSON so they survive restarts
                if q.get("sector") and q["sector"] != "N/A":
                    item["sector"] = q["sector"]
                    changed = True
                if q.get("rec") and q["rec"] != "N/A":
                    item["rec"] = q["rec"]
                    changed = True
                if q.get("div_yield") is not None:
                    item["div_yield"] = q["div_yield"]
                    changed = True
        elapsed = time.time() - t0
        log.info(f"  quotes done ({len(wanted)} tickers · {elapsed:.1f}s)")
        # Write sector/rec updates back to JSON so they appear on next startup
        if changed:
            try:
                save_tickers(raw)
                log.info("  persisted sector/rec/yield to JSON")
            except Exception as e:
                log.warning(f"  could not persist fields: {e}")
    else:
        # Startup load — serve static data immediately, no Yahoo calls
        # Return any cached quotes we already have from previous refreshes
        log.info(f"  startup load: {len(raw)} tickers (static, no Yahoo calls)")
        for item in raw:
            cached = cache_get(item["t"])
            if cached:
                item.update(cached)

    raw.sort(key=lambda x: abs(x.get("pct_raw", 0)), reverse=True)
    return jsonify(raw)


@app.route("/api/history")
def get_history():
    """
    Normalised % return series for charting.

    Query params
      tickers  comma-separated, e.g. SCHD,EUFN,EWY
      range    1D | 5D | 1W | 1M | 3M | 6M | YTD | 1Y | 2Y | 3Y  (default 1M)

    Strategy
      1D (5-min)   always Yahoo Finance — intraday, not stored in DB
      5D (hourly)  always Yahoo Finance — intraday, not stored in DB
      everything else:
        1. Check DB has MIN_ROWS and data is fresh (<= 5 days old)
        2. If sufficient  → serve from DB (zero cost)
        3. If not         → fetch from Yahoo, save to DB for next time
    """
    raw_tickers = request.args.get("tickers", "")
    range_key   = request.args.get("range", "1M").upper()

    if not raw_tickers:
        return jsonify({"error": "tickers param required"}), 400
    if range_key not in RANGES:
        return jsonify({"error": f"unknown range. valid: {list(RANGES.keys())}"}), 400

    tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]
    results: dict = {}

    for ticker in tickers:

        # ── Intraday (1D, 5D): always Yahoo — never stored in DB ──────────────
        if range_key in ("1D", "5D"):
            rows = fetch_yf_history(ticker, range_key)
            if rows:
                s = build_series(rows)
                if s:
                    s["source"] = "yahoo"
                    results[ticker] = s
            else:
                log.warning(f"No intraday history for {ticker} ({range_key})")
            continue

        # ── Daily / weekly ranges: DB first, Yahoo as fallback ────────────────
        start, end = date_range_for(range_key)
        db_rows    = db_get_history(ticker, start, end)
        min_needed = MIN_ROWS.get(range_key, 3)

        # Freshness check: latest DB row must be within 5 calendar days
        db_fresh = False
        if len(db_rows) >= min_needed and db_rows:
            days_old = (date.today() - date.fromisoformat(db_rows[-1][0])).days
            db_fresh = days_old <= 5
            if not db_fresh:
                log.info(f"{ticker} ({range_key}): DB stale by {days_old} days — refreshing")

        if db_fresh:
            s = build_series(db_rows)
            if s:
                s["source"] = "db"
                results[ticker] = s
                log.info(f"History {ticker} ({range_key}): DB ({len(db_rows)} rows)")
            continue

        # DB insufficient or stale → fetch from Yahoo and save
        log.info(f"History {ticker} ({range_key}): DB insufficient ({len(db_rows)}/{min_needed}) — Yahoo")
        rows = fetch_yf_history(ticker, range_key)
        if rows:
            s = build_series(rows)
            if s:
                s["source"] = "yahoo"
                results[ticker] = s
        else:
            log.warning(f"No history for {ticker} ({range_key})")

    sources = {t: results[t].get("source","?") for t in results}
    log.info(f"  history done  range={range_key}  {sources}")
    return jsonify(results)


@app.route("/api/episodes")
def get_episodes():
    """
    Returns all known episodes in order.
    Reads streetwise_episodes.json if it exists, otherwise derives
    episode tags from streetwise_data.json (e key values).

    Response: { "episodes": [{"key": "2/27", "label": "2/27 — Anything-But-AI Rally"}, ...],
                "latest": {"key": "2/27", "label": "..."} }
    """
    EPISODES_FILE = "streetwise_episodes.json"

    if os.path.exists(EPISODES_FILE):
        try:
            with open(EPISODES_FILE) as f:
                registry = json.load(f)
            # registry is { "2/27": "2/27 — Anything-But-AI Rally", ... }
            episodes = [
                {"key": k, "label": v}
                for k, v in registry.items()
            ]
        except Exception as e:
            log.warning(f"Failed to read {EPISODES_FILE}: {e}")
            episodes = []
    else:
        # Derive from streetwise_data.json episode tags
        data = load_data()
        seen = {}
        for d in data:
            for ep in d.get("e", []):
                if ep not in seen:
                    seen[ep] = ep   # key = label when no registry
        episodes = [{"key": k, "label": k} for k in seen]

    # Sort chronologically.
    # Streetwise key format: "2026/2/27"
    # Ian key format:        "ian:2026/3/6"
    def ep_sort_key(e):
        k = e["key"]
        is_ian = k.startswith("ian:")
        bare   = k[4:] if is_ian else k      # strip "ian:" prefix
        try:
            parts = bare.split("/")
            if len(parts) == 3:
                # year/month/day
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            elif len(parts) == 2:
                # legacy month/day (no year)
                year, month, day = 0, int(parts[0]), int(parts[1])
            else:
                return (is_ian, 0, 0, 0)
            return (is_ian, year, month, day)
        except Exception:
            return (is_ian, 0, 0, 0)

    episodes.sort(key=ep_sort_key)
    latest = episodes[-1] if episodes else None

    return jsonify({"episodes": episodes, "latest": latest})


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    """
    Expire the live quote cache.
    ?tickers=AAPL,MSFT  →  expire only those tickers  (selective refresh)
    No param            →  expire everything
    """
    param = request.args.get("tickers", "")
    if param:
        tickers = [t.strip().upper() for t in param.split(",") if t.strip()]
        cache_expire(tickers)
        return jsonify({"ok": True, "expired": tickers})
    _cache.clear()
    log.info("  cache cleared — all tickers will refresh from Yahoo Finance")
    return jsonify({"ok": True, "expired": "all"})


@app.route("/api/db/status")
def db_status():
    """Summary of what's in the price history database."""
    conn = get_db()
    if not conn:
        return jsonify({
            "ok":    False,
            "error": f"{DB_FILE} not found — run: python history_manager.py --init"
        })
    try:
        total   = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        tickers = conn.execute(
            "SELECT ticker, COUNT(*) AS rows, MIN(date) AS first, MAX(date) AS last "
            "FROM prices GROUP BY ticker ORDER BY ticker"
        ).fetchall()
        return jsonify({
            "ok":         True,
            "total_rows": total,
            "tickers":    [dict(r) for r in tickers],
        })
    finally:
        conn.close()


@app.route("/api/status")
def status():
    """General health check."""
    raw  = load_data()
    live = sum(1 for v in _cache.values() if (time.time() - v.get("_ts", 0)) < CACHE_TTL)
    return jsonify({
        "status":            "ok",
        "tickers_in_json":   len(raw),
        "cache_live_entries": live,
        "cache_ttl_seconds": CACHE_TTL,
        "db_found":          os.path.exists(DB_FILE),
        "server_time_utc":   datetime.utcnow().isoformat() + "Z",
        "supported_ranges":  list(RANGES.keys()),
    })


@app.route("/api/research", methods=["POST"])
def research():
    """
    Research a ticker using Claude (with web search) or Gemini.
    Falls back to Claude without web search if the tool is unavailable.
    Body: { ticker, name, query, model }
    Returns SSE stream: data: {"text":"..."} ... data: [DONE]
    """
    import anthropic as _anthropic

    body   = request.get_json(force=True)
    ticker = body.get("ticker", "").upper().strip()
    name   = body.get("name", "")
    query  = body.get("query", "").strip()
    model  = body.get("model", "claude")

    if not ticker:
        return jsonify({"error": "ticker required"}), 400

    if not query:
        query = f"What is the latest news, earnings, and analyst views on {ticker} ({name})?"

    system_prompt = (
        f"You are a financial research assistant. "
        f"The user is researching {ticker} ({name}). "
        f"Format your response as 6-10 bullet points using this exact format — "
        f"each bullet on its own line:\n"
        f"• **Label:** one concise sentence\n\n"
        f"Labels to use (pick the most relevant): Recent news, Earnings, "
        f"Analyst rating, Price target, EPS forecast, Catalyst, Risk, "
        f"Valuation, Macro tailwind, Sector context, Key development.\n\n"
        f"Rules: bold (**) every label before the colon; one sentence per bullet; "
        f"include specific numbers and dates where available; cite sources inline e.g. (Goldman, Reuters)."
    )

    log.info(f"  research: model={model} ticker={ticker} query={query[:80]}")

    def generate():
        # Send an immediate keepalive so the browser knows the stream is open
        # This prevents the "No results" flash while waiting for Claude
        yield ": keepalive\n\n"

        try:
            # ── Gemini ────────────────────────────────────────────────────────
            if model == "gemini":
                import urllib.request, urllib.error as _ue
                gemini_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
                if not gemini_key:
                    yield "data: " + json.dumps({"error": "GEMINI_API_KEY not set. Run: set GEMINI_API_KEY=your-key"}) + "\n\n"
                    yield "data: [DONE]\n\n"
                    return
                log.info(f"  research: Gemini key len={len(gemini_key)}")

                # Select model tier from request
                model_id = "gemini-2.5-pro" if model == "gemini-pro" else "gemini-2.5-flash"
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model_id}:generateContent?key={gemini_key}"
                )
                payload = json.dumps({
                    "contents": [{"parts": [{"text": f"{system_prompt}\n\n{query}"}]}],
                    "tools":    [{"google_search": {}}],
                    "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
                }).encode()
                req = urllib.request.Request(url, data=payload,
                        headers={"Content-Type": "application/json"})
                for _attempt in range(2):
                    try:
                        resp_raw = urllib.request.urlopen(req, timeout=60)
                        break
                    except _ue.HTTPError as he:
                        err_body = he.read().decode("utf-8", errors="replace")
                        log.error(f"  research: Gemini HTTP {he.code} — {err_body[:300]}")
                        if he.code == 429 and _attempt == 0:
                            log.warning("  research: Gemini 429 — retrying in 3s")
                            yield ": retrying\n\n"
                            import time as _t; _t.sleep(3)
                        else:
                            yield "data: " + json.dumps({"error": f"Gemini API error {he.code}: {err_body[:200]}"}) + "\n\n"
                            yield "data: [DONE]\n\n"
                            return
                data     = json.loads(resp_raw.read())
                # Extract text — may be spread across multiple parts
                parts    = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                text     = "".join(p.get("text", "") for p in parts).strip()
                if not text:
                    text = "Gemini returned no text — check your API key has billing enabled and grounding/search is available in your region."
                log.info(f"  research: Gemini {model_id} done {len(text)} chars")
                chunk_size = 40
                for i in range(0, len(text), chunk_size):
                    yield "data: " + json.dumps({"text": text[i:i+chunk_size]}) + "\n\n"
                yield "data: [DONE]\n\n"
                return

            # ── Claude ────────────────────────────────────────────────────────
            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                yield "data: " + json.dumps({"error": "ANTHROPIC_API_KEY not set. Run: set ANTHROPIC_API_KEY=sk-ant-..."}) + "\n\n"
                yield "data: [DONE]\n\n"
                return

            # Log masked key so you can verify which key the server is using
            masked = api_key[:12] + "…" + api_key[-6:] if len(api_key) > 20 else "too short"
            log.info(f"  research: using key {masked} (len={len(api_key)})")

            # Strip any accidental quotes or whitespace that Windows set/setx can add
            api_key = api_key.strip('"').strip("'").strip()

            client = _anthropic.Anthropic(api_key=api_key)

            # Map model value to actual model ID
            _claude_model_map = {
                "claude":        "claude-haiku-4-5-20251001",
                "claude-haiku":  "claude-haiku-4-5-20251001",
                "claude-sonnet": "claude-sonnet-4-6",
            }
            claude_model_id = _claude_model_map.get(model, "claude-haiku-4-5-20251001")
            # Call Claude with web_search tool
            log.info(f"  research: calling {claude_model_id} + web_search…")
            yield "data: " + json.dumps({"text": "🔍 Searching the web…\n\n"}) + "\n\n"
            resp = client.messages.create(
                model      = claude_model_id,
                max_tokens = 2000,
                system     = system_prompt,
                tools      = [{"type": "web_search_20250305", "name": "web_search"}],
                messages   = [{"role": "user", "content": query}],
            )
            inp, out, cost = calc_cost(resp)

            # Collect text from all blocks — web_search responses come back as
            # multiple separate text blocks (one per sentence/paragraph)
            parts = []
            for blk in resp.content:
                if getattr(blk, "text", ""):
                    parts.append(blk.text)

            full_text = "".join(parts).strip()

            # Single clean cost summary line
            log.info(
                f"  ✓ research done  {ticker}  "
                f"{len(full_text):,} chars  "
                f"in={inp:,} out={out:,}  "
                f"\033[32mcost=${cost:.4f}\033[0m"
            )
            log_cost("claude-research", ticker, inp, out, cost, model=getattr(resp, "model", ""))

            if not full_text:
                full_text = "No text returned by Claude. Check terminal for block details."

            # Yield each chunk directly — no nested generator
            chunk_size = 40
            for i in range(0, len(full_text), chunk_size):
                yield "data: " + json.dumps({"text": full_text[i:i+chunk_size]}) + "\n\n"

            yield "data: [DONE]\n\n"
            return

        except Exception as e:
            log.error(f"  research: error — {type(e).__name__}: {e}", exc_info=True)
            yield "data: " + json.dumps({"error": f"{type(e).__name__}: {e}"}) + "\n\n"
            yield "data: [DONE]\n\n"

    from flask import stream_with_context, Response

    def flushing_generate():
        """Encode every SSE chunk to bytes so Werkzeug is happy."""
        for chunk in generate():
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    return Response(
        stream_with_context(flushing_generate()),
        mimetype = "text/event-stream",
        headers  = {
            "X-Accel-Buffering": "no",
            "Cache-Control":     "no-cache",
            "Connection":        "keep-alive",
        },
    )


@app.route("/api/ingest-page", methods=["POST"])
def ingest_page():
    """
    Receive article text from Chrome extension, extract tickers via Claude or Gemini.
    Body: { text, date, year, source, prefix, title, model? }
    model: "claude-haiku" | "claude-sonnet" | "gemini-flash" | "gemini-pro"
    Returns: { ok, added, updated, tickers, cost, model, log }
    """
    body       = request.get_json(force=True)
    text       = body.get("text", "").strip()
    date       = body.get("date", "").strip()
    year       = str(body.get("year", datetime.now().year)).strip()
    source     = body.get("source", "ian").strip().lower()
    title      = body.get("title", "").strip()
    prefix_arg = body.get("prefix", "").strip().lower()
    model_req  = body.get("model", "claude-haiku").strip().lower()
    # anchors: [{text, href}] from blue-underlined company links in the article
    anchors    = body.get("anchors", [])

    if not text:
        return jsonify({"ok": False, "error": "no text provided"}), 400
    if not date:
        return jsonify({"ok": False, "error": "date required"}), 400

    # ── Resolve model ─────────────────────────────────────────────────────────
    use_gemini   = model_req.startswith("gemini")
    _claude_map  = {"claude-haiku": "claude-haiku-4-5-20251001",
                    "claude-sonnet": "claude-sonnet-4-6",
                    "claude": "claude-haiku-4-5-20251001"}
    claude_model = _claude_map.get(model_req, "claude-haiku-4-5-20251001")
    gemini_model = "gemini-2.5-pro" if model_req == "gemini-pro" else "gemini-2.5-flash"

    if use_gemini:
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
        if not gemini_key:
            return jsonify({"ok": False, "error": "GEMINI_API_KEY not set in environment"}), 400
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
        if not api_key:
            return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set in environment"}), 400

    # ── Source metadata ───────────────────────────────────────────────────────
    # prefix comes explicitly from extension — never derive from source name
    # source is the free-text display label sent by the extension
    prefix = prefix_arg if prefix_arg else "src"

    # Use the raw source string as the label (it's already the display name)
    # Fall back to built-in labels only if source looks like a prefix
    builtin_labels = {
        "stw": "Barron's Streetwise",
        "ian": "Barron's Ian Salisbury",
        "div": "Dividends",
        "bl":  "Barrons Live",
    }
    source_label = builtin_labels.get(prefix, body.get("source", source).strip())
    if not source_label:
        source_label = prefix.upper()
    is_ian_style = prefix != "stw"

    label  = f"{date}/{year}"
    if title:
        label = f"{label} — {title}"
    ep_key = f"{prefix}:{year}/{date}"

    srv_log = []   # collect log lines to return to extension
    def ilog(msg, level="info"):
        getattr(log, level)(f"  ingest-page: {msg}")
        srv_log.append(msg)

    ilog(f"source={source_label}  prefix={prefix}  ep={ep_key}  chars={len(text):,}")

    # ── Duplicate / already-processed check ───────────────────────────────────
    import hashlib as _hl
    force     = bool(body.get("force", False))
    text_hash = _hl.md5(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    sources_reg_early = load_sources()
    ep_entry_early = (sources_reg_early.get(prefix) or {}).get("episodes", {}).get(ep_key, {})
    if not force and ep_entry_early.get("text_hash") == text_hash:
        ilog("Article already processed (same content hash) — skipping", "warn")
        return jsonify({
            "ok": True, "skipped": True, "added": 0, "updated": 0,
            "tickers": list((ep_entry_early.get("tickers") or [])),
            "cost": "0.0000", "model": "cached", "log": srv_log,
            "article_summary": ep_entry_early.get("article_summary", ""),
            "message": "Article already in DB — skipping extraction",
        })

    # ── Parse tickers / company names from anchors ────────────────────────────
    # Barron's links companies like /market-data/stocks/AAPL or /quote/AAPL
    import re as _re
    anchor_tickers = {}   # ticker -> company_name (from URL)
    anchor_names   = []   # company names without a resolvable ticker
    for a in (anchors or []):
        href = (a.get("href") or "").strip()
        name = (a.get("text") or "").strip()
        if not name:
            continue
        # Try to pull ticker from URL path segment
        m = _re.search(r'/(?:stocks?|quote|symbol)/([A-Z0-9\.\-]{1,10})(?:[/?]|$)', href, _re.I)
        if m:
            sym = m.group(1).upper()
            anchor_tickers[sym] = name
        else:
            anchor_names.append(name)

    # Build hint block for the AI prompt
    hints_block = ""
    if anchor_tickers or anchor_names:
        lines = []
        if anchor_tickers:
            lines.append("The following company names and their tickers were found as hyperlinks in the article:")
            for sym, nm in anchor_tickers.items():
                lines.append(f"  {nm} → {sym}")
        if anchor_names:
            lines.append("The following company names were hyperlinked but their ticker could not be determined from the URL — please resolve them:")
            for nm in anchor_names:
                lines.append(f"  {nm}")
        hints_block = "\n\nCOMPANY HINTS (from article hyperlinks):\n" + "\n".join(lines)

    ilog(f"anchors: {len(anchor_tickers)} with ticker, {len(anchor_names)} name-only")

    # ── Extract tickers via Claude ─────────────────────────────────────────────
    extract_prompt = f"""You are a financial research assistant reading content from {source_label}.
Content label: {label}

Extract EVERY stock, ETF, or mutual fund mentioned — including brief references,
comparisons, and cautionary examples.

IMPORTANT: Articles often mention companies by name only, without a ticker symbol.
You MUST still include those companies and supply the correct US exchange ticker yourself.
Examples: "Exxon Mobil" → XOM, "Chevron" → CVX, "ConocoPhillips" → COP,
"Devon Energy" → DVN, "Diamondback Energy" → FANG, "Permian Resources" → PR,
"Occidental Petroleum" → OXY, "Ovintiv" → OVV, "California Resources" → CRC.
Never skip a company just because no ticker was printed in the article.{hints_block}

For EACH one return a JSON object with exactly these fields:
{{
  "ticker":   "BKU",
  "name":     "BankUnited",
  "type":     "Stock",
  "sector":   "Financial Services",
  "rec":      "Buy",
  "price":    "~$46",
  "status":   "rot",
  "discussion": "2-3 sentence prose narrative summarising how this stock was discussed in the article. Written in third person, past tense. No bullet points — this is a short readable paragraph.",
  "summary":  "6-8 bullet points, each on its own line, format: \'• **Label:** explanation\'. Cover: why mentioned, analyst thesis, key metrics, valuation, price targets/EPS, risks, macro tailwind. Bold (**) the label before each colon.",
  "base":     "one sentence base-case outcome",
  "bear":     "one sentence bear-case risk",
  "bull":     "one sentence bull-case upside"
}}

status = hot | rot | pull | press | dip | rec | caut | flat
sector = GICS sector e.g. "Financial Services", "Technology", "N/A" if unknown
rec    = analyst consensus if mentioned: "Strong Buy", "Buy", "Hold", "Sell", "N/A"

Return ONLY a valid JSON array. No markdown, no prose, no backticks.

CONTENT:
{text[:40000]}
"""

    ilog(f"model={gemini_model if use_gemini else claude_model}")
    cost       = 0.0
    model_used = gemini_model if use_gemini else claude_model

    try:
        if use_gemini:
            import urllib.request, urllib.error as _ue
            max_tokens = 16000 if gemini_model == "gemini-2.5-pro" else 16384
            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{gemini_model}:generateContent?key={gemini_key}"
            )
            gen_cfg = {
                "temperature":     0.2,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            }
            # Disable thinking for Flash — ticker extraction doesn't need it
            # and thinking tokens eat into the output budget
            if "flash" in gemini_model:
                gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            payload = json.dumps({
                "contents": [{"parts": [{"text": extract_prompt}]}],
                "generationConfig": gen_cfg,
            }).encode()
            req = urllib.request.Request(url, data=payload,
                    headers={"Content-Type": "application/json"})
            for _attempt in range(2):
                try:
                    resp_raw = urllib.request.urlopen(req, timeout=120)
                    break
                except _ue.HTTPError as he:
                    err_body = he.read().decode("utf-8", errors="replace")
                    if he.code == 429 and _attempt == 0:
                        import time as _t; _t.sleep(3)
                    else:
                        ilog(f"Gemini HTTP {he.code}: {err_body[:200]}", "error")
                        return jsonify({"ok": False, "error": f"Gemini {he.code}: {err_body[:200]}",
                                        "log": srv_log}), 500
            gdata  = json.loads(resp_raw.read())
            finish = (gdata.get("candidates") or [{}])[0].get("finishReason", "")
            if finish == "MAX_TOKENS":
                thoughts = (gdata.get("usageMetadata") or {}).get("thoughtsTokenCount", 0)
                ilog(f"Gemini hit MAX_TOKENS (thinking={thoughts})", "error")
                return jsonify({"ok": False,
                                "error": f"Gemini hit token limit (thinking={thoughts}). Try Flash.",
                                "log": srv_log}), 500
            raw = ""
            for cand in (gdata.get("candidates") or []):
                for part in (cand.get("content", {}).get("parts") or []):
                    t = part.get("text", "")
                    if t and not part.get("thought"):
                        raw += t
            raw = raw.strip()
            usage  = gdata.get("usageMetadata", {})
            inp    = usage.get("promptTokenCount", 0)
            out    = usage.get("candidatesTokenCount", 0)
            rates  = {"gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0)}
            r_in, r_out = rates.get(gemini_model, (0.30, 2.50))
            cost   = (inp * r_in + out * r_out) / 1_000_000
            model_used = gemini_model
            ilog(f"extraction done  in={inp:,} out={out:,} cost=${cost:.4f}")
        else:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=api_key)
            msg    = client.messages.create(
                model      = claude_model,
                max_tokens = 8000,
                messages   = [{"role": "user", "content": extract_prompt}],
            )
            inp, out, cost = calc_cost(msg)
            raw        = msg.content[0].text.strip()
            model_used = claude_model
            ilog(f"extraction done  in={inp:,} out={out:,} cost=${cost:.4f}")
            log_cost("claude-ingest", "batch", inp, out, cost, model=claude_model)

        if raw.startswith("```"):
            parts = raw.split("```")
            raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

        entries = json.loads(raw)
        ilog(f"extracted {len(entries)} entries")

    except json.JSONDecodeError as e:
        log.error(f"  ingest-page JSON error: {e}"); ilog(f"JSON parse error: {e}", "error")
        return jsonify({"ok": False, "error": "Model returned invalid JSON", "log": srv_log}), 500
    except Exception as e:
        log.error(f"  ingest-page extraction error: {e}"); ilog(f"extraction error: {e}", "error")
        return jsonify({"ok": False, "error": str(e), "log": srv_log}), 500

    # ── Merge into database ────────────────────────────────────────────────────
    db     = load_data()
    lookup = {d["t"].upper(): d for d in db}
    added = updated = 0
    tickers_touched = []

    _NOISE = {"ECNQQUOTE","EQUITY","ETF","MUTUALFUND","INDEX","CURRENCY","CRYPTOCURRENCY"}

    for e in entries:
        ticker = e.get("ticker", "").strip().upper()
        if not ticker or ticker == "N/A":
            continue

        new_sum  = e.get("summary", "")
        new_disc = e.get("discussion", "").strip()
        tickers_touched.append(ticker)

        # Clean sector
        raw_sector = e.get("sector", "") or ""
        sector = raw_sector if raw_sector and raw_sector.upper() not in _NOISE else "N/A"

        if ticker in lookup:
            rec = lookup[ticker]
            # Episode tag
            ep_tags = rec.get("e", [])
            if ep_key not in ep_tags:
                rec["e"] = ep_tags + [ep_key]
            # Source
            rec.setdefault("src", [source])
            if source not in rec["src"]:
                rec["src"].append(source)
            # Summary — always use === heading === for all sources (enables per-episode separation + dedup)
            heading  = f"=== {ep_key} | {source_label} · {label} ==="
            existing = rec.get("sum", "")
            if heading in existing:
                # Replace this episode's section in-place
                before = existing.split(heading)[0].rstrip()
                rec["sum"] = f"{before}\n\n{heading}\n{new_sum}" if before else f"{heading}\n{new_sum}"
            else:
                rec["sum"] = f"{existing.rstrip()}\n\n{heading}\n{new_sum}" if existing.strip() else f"{heading}\n{new_sum}"
            # Discussion narrative (per-episode dict)
            if new_disc:
                disc_map = rec.setdefault("disc", {})
                disc_map[ep_key] = new_disc
            # Fields
            rec["s"] = e.get("status", rec.get("s", "flat"))
            if e.get("price", "N/A") != "N/A":
                rec["p"] = e["price"]
            if sector != "N/A":
                rec["sector"] = sector
            if e.get("rec") and e["rec"] != "N/A":
                rec["rec_analyst"] = e["rec"]
            updated += 1
        else:
            summary = f"=== {ep_key} | {source_label} · {label} ===\n{new_sum}"
            new_rec = {
                "t": ticker, "n": e.get("name", ticker),
                "y": e.get("type", "Stock"), "e": [ep_key],
                "p": e.get("price", "N/A"), "s": e.get("status", "flat"),
                "src": [source], "sum": summary,
                "base": e.get("base", ""), "bear": e.get("bear", ""),
                "bull": e.get("bull", ""),
            }
            if new_disc:
                new_rec["disc"] = {ep_key: new_disc}
            if sector != "N/A":       new_rec["sector"]     = sector
            if e.get("rec") and e["rec"] != "N/A": new_rec["rec_analyst"] = e["rec"]
            lookup[ticker] = new_rec
            added += 1

    merged = list(lookup.values())
    try:
        # Atomic write to DB
        save_tickers(merged)

        # Register episode in sources.json
        sources_reg = load_sources()
        if prefix not in sources_reg:
            sources_reg[prefix] = {
                "label":    source_label,
                "color":    "#64748b",
                "episodes": {}
            }
        ep_entry = sources_reg[prefix]["episodes"].setdefault(ep_key, {})
        # Always update title/date if we have it
        if title:
            ep_title = title if title else label.split(" — ", 1)[-1] if " — " in label else label
            _dp = date.split("/") if "/" in date else [date]
            if len(_dp) == 2:
                ep_entry["date"] = f"{year}-{int(_dp[0]):02d}-{int(_dp[1]):02d}"
            else:
                ep_entry["date"] = f"{year}-{date}"
            ep_entry["title"] = ep_title
        # Save article text + hash + tickers for dedup and future re-processing
        ep_entry["text"]      = text
        ep_entry["text_hash"] = text_hash
        ep_entry["tickers"]   = tickers_touched
        save_sources(sources_reg)
        ilog(f"registered episode {ep_key} with {len(tickers_touched)} tickers in sources.json")

        ilog(f"saved {added} new · {updated} updated")

        # ── Auto-generate episode summary (runs in background after response) ──
        # Only generate if not already present
        article_summary = ep_entry.get("article_summary", "")
        if not article_summary:
            try:
                sum_key = (gemini_key if use_gemini
                           else os.environ.get("GEMINI_API_KEY","").strip().strip('"').strip("'"))
                if sum_key:
                    import urllib.request as _ur2
                    sum_prompt = (
                        f"You are a financial analyst. Summarise this podcast or article for an investor.\n\n"
                        f"{text[:40000]}"
                    )
                    sum_payload = json.dumps({
                        "contents": [{"parts": [{"text": sum_prompt}]}],
                        "generationConfig": {
                            "temperature": 1.0,
                            "maxOutputTokens": 16000,
                        },
                    }).encode()
                    sum_url = (
                        "https://generativelanguage.googleapis.com/v1beta/"
                        "models/gemini-2.5-flash:generateContent?key=" + sum_key
                    )
                    sum_req  = _ur2.Request(sum_url, data=sum_payload,
                                            headers={"Content-Type": "application/json"})
                    sum_resp = _ur2.urlopen(sum_req, timeout=90)
                    sum_data = json.loads(sum_resp.read())
                    raw_sum  = ""
                    for cand in (sum_data.get("candidates") or []):
                        for part in (cand.get("content", {}).get("parts") or []):
                            t = part.get("text", "")
                            if t and not part.get("thought"):
                                raw_sum += t
                    raw_sum = raw_sum.strip()
                    if raw_sum:
                        ep_entry["article_summary"] = raw_sum
                        save_sources(sources_reg)
                        ilog(f"episode summary saved  chars={len(raw_sum)}")
            except Exception as se:
                ilog(f"episode summary failed (non-fatal): {se}", "warn")
                log.warning(f"  ingest-page summary: {se}")

        return jsonify({"ok": True, "added": added, "updated": updated,
                        "tickers": tickers_touched, "cost": f"{cost:.4f}",
                        "model": model_used, "log": srv_log})
    except Exception as e:
        log.error(f"  ingest-page save error: {e}"); srv_log.append(f"save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/regen-episode-summary", methods=["POST"])
def regen_episode_summary():
    """
    Re-run Gemini summary for an episode using the saved article text.
    Body: { "ep_key": "stw:2026/4/10" }
    """
    body   = request.get_json(force=True)
    ep_key = (body.get("ep_key") or "").strip()
    if not ep_key:
        return jsonify({"ok": False, "error": "ep_key required"}), 400

    prefix = ep_key.split(":")[0]
    sources_reg = load_sources()
    ep_entry = (sources_reg.get(prefix) or {}).get("episodes", {}).get(ep_key)
    if not ep_entry:
        return jsonify({"ok": False, "error": f"Episode '{ep_key}' not found"}), 404

    text = ep_entry.get("text", "")
    if not text:
        return jsonify({"ok": False, "error": "No article text saved for this episode — re-extract first"}), 400

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
    if not gemini_key:
        return jsonify({"ok": False, "error": "GEMINI_API_KEY not set"}), 400

    try:
        import urllib.request as _ur2
        sum_prompt = (
            "You are a financial analyst. Summarise this podcast or article for an investor.\n\n"
            + text[:40000]
        )
        sum_payload = json.dumps({
            "contents": [{"parts": [{"text": sum_prompt}]}],
            "generationConfig": {"temperature": 1.0, "maxOutputTokens": 16000},
        }).encode()
        sum_url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-2.5-flash:generateContent?key=" + gemini_key
        )
        resp = _ur2.urlopen(
            _ur2.Request(sum_url, data=sum_payload,
                         headers={"Content-Type": "application/json"}), timeout=120)
        data = json.loads(resp.read())
        raw = ""
        for cand in (data.get("candidates") or []):
            for part in (cand.get("content", {}).get("parts") or []):
                t = part.get("text", "")
                if t and not part.get("thought"):
                    raw += t
        raw = raw.strip()
        if not raw:
            return jsonify({"ok": False, "error": "Gemini returned empty output"}), 500

        ep_entry["article_summary"] = raw
        save_sources(sources_reg)
        log.info(f"  regen-episode-summary: {ep_key}  chars={len(raw)}")
        return jsonify({"ok": True, "ep_key": ep_key, "summary": raw, "chars": len(raw)})
    except Exception as e:
        log.error(f"  regen-episode-summary: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/save-episode-summary", methods=["POST"])
def save_episode_summary():
    """
    Save a manually pasted summary for an episode.
    Body: { "ep_key": "stw:2026/4/10", "summary": "...raw text..." }
    """
    body    = request.get_json(force=True)
    ep_key  = (body.get("ep_key") or "").strip()
    summary = (body.get("summary") or "").strip()
    if not ep_key:
        return jsonify({"ok": False, "error": "ep_key required"}), 400

    prefix = ep_key.split(":")[0]
    sources_reg = load_sources()
    if prefix not in sources_reg:
        return jsonify({"ok": False, "error": f"Source '{prefix}' not found"}), 404
    ep_entry = sources_reg[prefix]["episodes"].get(ep_key)
    if ep_entry is None:
        return jsonify({"ok": False, "error": f"Episode '{ep_key}' not found"}), 404

    if summary:
        ep_entry["article_summary"] = summary
    else:
        ep_entry.pop("article_summary", None)

    save_sources(sources_reg)
    log.info(f"  save-episode-summary: {ep_key}  chars={len(summary)}")
    return jsonify({"ok": True, "ep_key": ep_key, "chars": len(summary)})


@app.route("/api/regen-cases", methods=["POST"])
def regen_cases():
    """
    Regenerate bull/bear/base cases for a ticker using web search.
    Supports both Claude (Anthropic) and Gemini (Google) models.
    Appends with a date stamp so history is preserved.

    Body: { ticker, name, model }
      model: "claude" (default) | "gemini-flash" | "gemini-pro"
    Returns: { ok, ticker, base, bear, bull, date, model_used }
    """

    body   = request.get_json(force=True)
    ticker = body.get("ticker", "").strip().upper()
    name   = body.get("name", ticker).strip()
    model_raw = body.get("model", "claude-haiku").strip().lower()
    _model_map = {
        "claude-haiku":  "claude-haiku",  "claude-sonnet": "claude-sonnet",
        "claude":        "claude-haiku",  "gemini":        "gemini",
        "gemini-flash":  "gemini",        "gemini-pro":    "gemini-pro",
    }
    model = _model_map.get(model_raw, "claude-haiku")

    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400

    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""You are an equity research analyst. Research {ticker} ({name}) using current market data.

Write three concise investment scenario sentences for today ({today}):

1. BASE CASE (1 sentence): The most likely 12-month outcome given current fundamentals, valuation, and macro backdrop.
2. BEAR CASE (1 sentence): The key downside risk that could cause meaningful underperformance.
3. BULL CASE (1 sentence): The key catalyst or upside scenario that could drive outperformance.

Each sentence should be specific — mention actual metrics, price targets, or catalysts if available.

Respond ONLY in this exact JSON format, nothing else:
{{
  "base": "one sentence base case",
  "bear": "one sentence bear case",
  "bull": "one sentence bull case"
}}"""

    raw = ""
    model_used = model

    try:
        # ── Gemini path — raw HTTP, no SDK ────────────────────────────────────
        # IMPORTANT: 2.5 Pro/Flash are thinking models — they burn tokens on
        # internal reasoning before outputting text.  maxOutputTokens must cover
        # both the thinking budget AND the actual response, so we set it high.
        # We also skip google_search: it triggers multi-turn which makes the
        # response structure unpredictable. The prompt has all the context needed.
        if model.startswith("gemini"):
            import urllib.request, urllib.error as _ue

            gemini_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
            if not gemini_key:
                return jsonify({"ok": False, "error": "GEMINI_API_KEY not set"}), 400

            model_id   = "gemini-2.5-pro" if model == "gemini-pro" else "gemini-2.5-flash"
            model_used = model_id

            # Token budgets — thinking models need much more headroom
            max_tokens = 16000 if model_id == "gemini-2.5-pro" else 4000

            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model_id}:generateContent?key={gemini_key}"
            )
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature":      0.3,
                    "maxOutputTokens":  max_tokens,
                    "responseMimeType": "application/json",
                },
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            for _attempt in range(2):
                try:
                    resp_raw = urllib.request.urlopen(req, timeout=120)
                    break
                except _ue.HTTPError as he:
                    err_body = he.read().decode("utf-8", errors="replace")
                    if he.code == 429 and _attempt == 0:
                        import time as _t; _t.sleep(4)
                    else:
                        return jsonify({"ok": False,
                                        "error": f"Gemini {he.code}: {err_body[:300]}"}), 500

            gdata  = json.loads(resp_raw.read())
            finish = (gdata.get("candidates") or [{}])[0].get("finishReason", "")

            if finish == "MAX_TOKENS":
                thoughts = (gdata.get("usageMetadata") or {}).get("thoughtsTokenCount", 0)
                return jsonify({"ok": False,
                                "error": f"Gemini hit token limit (thinking used {thoughts} tokens). "
                                         f"Try Gemini Flash instead."}), 500

            # Extract text — skip thought parts (role='model' with no text key)
            raw = ""
            for cand in (gdata.get("candidates") or []):
                for part in (cand.get("content", {}).get("parts") or []):
                    t = part.get("text", "")
                    if t and not part.get("thought"):   # skip internal thought blocks
                        raw += t
            raw = raw.strip()

            if not raw:
                log.error(f"  regen-cases Gemini empty: finish={finish} "
                          f"resp={json.dumps(gdata)[:400]}")
                return jsonify({"ok": False,
                                "error": f"Gemini returned no text (finishReason={finish})"}), 500

            usage  = gdata.get("usageMetadata", {})
            inp    = usage.get("promptTokenCount", 0)
            out    = usage.get("candidatesTokenCount", 0)
            rates  = {"gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.0)}
            r_in, r_out = rates.get(model_id, (0.30, 2.50))
            cost   = (inp * r_in + out * r_out) / 1_000_000
            log.info(f"  regen-cases ({model_id}): {ticker} in={inp:,} out={out:,} "
                     f"thinking={usage.get('thoughtsTokenCount',0):,} cost=${cost:.4f}")

        # ── Claude path ────────────────────────────────────────────────────────
        else:
            import anthropic as _anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
            if not api_key:
                return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set"}), 400

            client    = _anthropic.Anthropic(api_key=api_key)
            tools     = [{"type": "web_search_20250305", "name": "web_search"}]
            messages  = [{"role": "user", "content": prompt}]
            total_inp = total_out = 0
            _claude_id_map = {"claude-haiku":"claude-haiku-4-5-20251001","claude-sonnet":"claude-sonnet-4-6"}
            model_used = _claude_id_map.get(model, "claude-haiku-4-5-20251001")

            for turn in range(6):
                resp = client.messages.create(
                    model      = model_used,
                    max_tokens = 1500,
                    tools      = tools,
                    messages   = messages,
                )
                i, o, _ = calc_cost(resp)
                total_inp += i; total_out += o

                for block in resp.content:
                    if hasattr(block, "text") and block.text.strip():
                        raw = block.text.strip()

                if resp.stop_reason != "tool_use":
                    break

                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     block.input.get("query", "") if hasattr(block, "input") else "",
                        })
                if not tool_results:
                    break
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user",      "content": tool_results})

            cost = (total_inp * 0.80 + total_out * 4.00) / 1_000_000
            log.info(f"  regen-cases (claude): {ticker} turns={turn+1} in={total_inp:,} out={total_out:,} cost=${cost:.4f}")
            log_cost("claude-scenarios", ticker, total_inp, total_out, cost, model="claude-haiku-4-5")

        # ── Parse JSON — extract first complete { } object ─────────────────────
        if not raw:
            raise ValueError("No text in response")
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        # Find the first { and its matching }
        j = raw.find("{")
        if j < 0:
            raise ValueError("No JSON object found in response")
        raw = raw[j:]
        # Walk to find the matching closing brace
        depth, in_str, esc = 0, False, False
        end = -1
        BS = "\\"
        for i, ch in enumerate(raw):
            if esc:               esc = False;          continue
            if ch == BS and in_str: esc = True;         continue
            if ch == '"':         in_str = not in_str;  continue
            if in_str:            continue
            if ch == "{":         depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:    end = i + 1;          break
        if end < 0:
            raise ValueError("Incomplete JSON object in response")
        cases = json.loads(raw[:end])

    except Exception as e:
        log.error(f"  regen-cases error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

    # Load existing record and append with date stamp
    db     = load_data()
    stamp  = f"--- {today} · {model_used} ---"
    updated = False

    for rec in db:
        if rec.get("t", "").upper() == ticker:
            for field in ("base", "bear", "bull"):
                new_val  = cases.get(field, "")
                existing = rec.get(field, "")
                if existing and existing != "—":
                    rec[field] = existing.rstrip() + "\n" + stamp + "\n" + new_val
                else:
                    rec[field] = stamp + "\n" + new_val
            updated = True
            log.info(f"  regen-cases: {ticker} updated OK")
            break

    if not updated:
        return jsonify({"ok": False, "error": f"{ticker} not found in DB"}), 404

    try:
        save_tickers(db)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Return the full updated field values for the ticker
    rec_out = next((r for r in db if r.get("t","").upper() == ticker), {})
    return jsonify({
        "ok":     True,
        "ticker": ticker,
        "date":   today,
        "base":   rec_out.get("base", ""),
        "bear":   rec_out.get("bear", ""),
        "bull":   rec_out.get("bull", ""),
    })


@app.route("/api/heatmap-data")
def heatmap_data():
    """
    Return all tickers with sector, price, and 1D % change for the heatmap view.
    Response: { ok, tickers: [{t, n, s, p, ch}] }
    NOTE: Only 1D change (ch) is available directly from the JSON.
          Multi-period changes (1W/1M/3M) can be added later via price_history.db.
    """
    db = load_data()
    out = []
    for rec in db:
        t = rec.get("t", "")
        if not t or t.startswith("__"):
            continue
        out.append({
            "t":  t,
            "n":  rec.get("n", t),
            "s":  rec.get("s") or "Other",
            "p":  rec.get("p") or 0,
            "ch": rec.get("ch") or 0,   # 1D % change
        })
    return jsonify({"ok": True, "tickers": out})


@app.route("/api/sources")
def get_sources():
    """
    Return all sources and their episodes from sources.json registry.
    Falls back to scanning ep keys in the DB if registry is missing.
    Response: { sources: [ { prefix, label, color, count, episodes:[{key,date,title}] } ] }
    """
    registry = load_sources()

    if registry:
        # Use the registry as source of truth
        result = []
        for prefix, info in registry.items():
            episodes = [
                {"key": k, "date": v.get("date",""), "title": v.get("title",""),
                 "summary": v.get("article_summary", "")}
                for k, v in (info.get("episodes") or {}).items()
            ]
            result.append({
                "prefix":   prefix,
                "label":    info.get("label", prefix.upper()),
                "color":    info.get("color", "#64748b"),
                "count":    len(episodes),
                "episodes": episodes,
            })
        return jsonify({"sources": result})

    # Fallback: derive from DB ep keys (no registry file)
    db = load_data()
    prefix_tickers = {}
    for rec in db:
        seen = set()
        for ep in (rec.get("e") or []):
            if ":" not in ep: continue
            pfx = ep.split(":")[0].strip().lower()
            if pfx not in seen:
                prefix_tickers[pfx] = prefix_tickers.get(pfx, 0) + 1
                seen.add(pfx)

    builtin_labels = {
        "ian": "Barron's Ian Salisbury",
        "stw": "Barron's Streetwise",
        "div": "Dividends",
        "bl":  "Barrons Live",
    }
    result = []
    for prefix in sorted(prefix_tickers.keys()):
        result.append({
            "prefix": prefix,
            "label":  builtin_labels.get(prefix, prefix.upper()),
            "color":  "#64748b",
            "count":  prefix_tickers[prefix],
            "episodes": [],
        })
    return jsonify({"sources": result})

@app.route("/api/delete-episode", methods=["POST"])
def delete_episode():
    """
    Remove all traces of one episode from streetwise_data.json and
    streetwise_episodes.json.

    Body: { ep_key }  e.g. { "ep_key": "div:2026/3/15" }

    For each ticker:
      - Remove ep_key from the e[] array
      - Remove the === ep_key | ... === summary section
      - If e[] becomes empty, optionally remove the ticker entirely
        (controlled by body.remove_empty, default false)
    """
    import re as _re

    body      = request.get_json(force=True)
    ep_key    = body.get("ep_key", "").strip()
    rm_empty  = bool(body.get("remove_empty", False))

    if not ep_key:
        return jsonify({"ok": False, "error": "ep_key required"}), 400

    db      = load_data()
    touched = 0
    removed_tickers = []
    kept    = []

    prefix = ep_key.split(":")[0]

    for rec in db:
        e_list = rec.get("e", [])
        if ep_key not in e_list:
            kept.append(rec)
            continue

        # Remove episode tag
        rec["e"] = [t for t in e_list if t != ep_key]

        # Remove the === ep_key | ... === section from summary
        old_sum = rec.get("sum", "")
        if old_sum:
            # Match the heading line and everything after it until the next === or end
            pattern = "===\\s*" + _re.escape(ep_key) + "\\s*\\|[^=]*===\\n?"
            m = _re.search(pattern, old_sum)
            if m:
                before   = old_sum[:m.start()].rstrip()
                after    = old_sum[m.end():]
                # Drop after content until the next heading
                next_hdg = _re.search(r"===", after)
                after    = after[next_hdg.start():] if next_hdg else ""
                new_sum  = (before + "\n\n" + after).strip() if after else before
                rec["sum"] = new_sum

        # Remove per-episode discussion narrative
        if ep_key in rec.get("disc", {}):
            del rec["disc"][ep_key]

        # Remove src label if no remaining episodes from this prefix
        has_prefix = any(t.startswith(prefix + ":") for t in rec.get("e", []))
        if not has_prefix:
            # src stores the lowercased source label — find and remove any entry
            # that belongs to this prefix (matched via the sources registry)
            src_label = (load_sources().get(prefix) or {}).get("label", "")
            if src_label:
                rec["src"] = [s for s in rec.get("src", []) if s != src_label.lower()]

        touched += 1

        if rm_empty and not rec.get("e"):
            removed_tickers.append(rec["t"])
        else:
            kept.append(rec)

    # Save updated data
    try:
        save_tickers(kept)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Remove from episode registry
    ep_file  = "streetwise_episodes.json"
    registry = {}
    if os.path.exists(ep_file):
        try:
            with open(ep_file) as f:
                registry = json.load(f)
        except Exception:
            pass
    if ep_key in registry:
        del registry[ep_key]
        with open(ep_file, "w") as f:
            json.dump(registry, f, indent=2)

    # Remove episode from sources registry so it disappears from the popup dropdown
    sources_reg = load_sources()
    prefix_eps  = (sources_reg.get(prefix) or {}).get("episodes", {})
    if ep_key in prefix_eps:
        del prefix_eps[ep_key]
        save_sources(sources_reg)
        log.info(f"  delete-episode: removed {ep_key} from sources registry")

    log.info(f"  delete-episode: {ep_key}  touched={touched}  removed_tickers={len(removed_tickers)}")
    return jsonify({
        "ok":              True,
        "ep_key":          ep_key,
        "tickers_touched": touched,
        "tickers_removed": removed_tickers,
    })


@app.route("/api/watchlists", methods=["GET"])
def get_watchlists():
    """Return saved watchlists from the __watchlists__ meta-record."""
    db = load_data_raw()
    for rec in db:
        if rec.get("t") == "__watchlists__":
            return jsonify({"ok": True, "watchlists": rec.get("watchlists", {})})
    return jsonify({"ok": True, "watchlists": {}})


@app.route("/api/watchlists", methods=["POST"])
def save_watchlists():
    """Save watchlists into the __watchlists__ meta-record atomically."""
    body = request.get_json(force=True, silent=True) or {}
    watchlists = body.get("watchlists", {})
    db = load_data_raw()
    found = False
    for rec in db:
        if rec.get("t") == "__watchlists__":
            rec["watchlists"] = watchlists
            found = True
            break
    if not found:
        db.insert(0, {"t": "__watchlists__", "__meta__": True, "watchlists": watchlists})
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)
    log.info(f"  watchlists saved: {list(watchlists.keys())}")
    return jsonify({"ok": True})


@app.route("/api/watchlist-build", methods=["POST"])
def watchlist_build():
    """
    Ask Claude to select tickers from the database matching a natural-language query.
    Body: { query, max_results }
    Returns: { ok, tickers: ["AAPL",...], reasoning: "..." }

    Claude receives the full ticker database as context and returns
    a JSON selection — no web search needed since the data is local.
    """
    import anthropic as _anthropic

    body        = request.get_json(force=True)
    query       = body.get("query", "").strip()
    max_results = int(body.get("max_results", 10))

    if not query:
        return jsonify({"ok": False, "error": "query required"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        return jsonify({"ok": False, "error": "ANTHROPIC_API_KEY not set"}), 400

    # Build a compact summary of the database for Claude
    db = load_data()
    ticker_list = []
    for rec in db:
        t       = rec.get("t", "")
        name    = rec.get("n", "")
        sector  = rec.get("sector", "N/A")
        status  = rec.get("s", "flat")
        ytype   = rec.get("y", "Stock")
        rec_a   = rec.get("rec_analyst") or rec.get("rec", "")
        episodes = len(rec.get("e", []))
        # Include a short snippet of the summary for context
        summ    = (rec.get("sum", "") or "")[:300].replace("\n", " ")
        ticker_list.append(
            f"{t} | {name} | {ytype} | {sector} | status={status} | "
            f"rec={rec_a} | episodes={episodes} | summary={summ}"
        )

    db_context = "\n".join(ticker_list)

    prompt = f"""You are a portfolio screening assistant. The user wants to build a watchlist.

USER REQUEST: {query}

DATABASE (pipe-separated: ticker | name | type | sector | status | rec | episodes | summary):
{db_context}

Instructions:
- Select up to {max_results} tickers from the database that best match the user's request
- Base your selection ONLY on the data provided — do not invent tickers not in the list
- Return ONLY valid JSON in this exact format, nothing else:
{{
  "tickers": ["TICK1", "TICK2", ...],
  "reasoning": "One sentence explaining the selection criteria and why these tickers match"
}}
"""

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        resp   = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 1000,
            messages   = [{"role": "user", "content": prompt}],
        )
        inp, out, cost = calc_cost(resp)
        log.info(f"  watchlist-build: in={inp:,} out={out:,} cost=${cost:.4f}")
        log_cost("claude-watchlist-build", "batch", inp, out, cost, model="claude-haiku-4-5-20251001")

        raw = resp.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()

        data = json.loads(raw)
        tickers = [t.strip().upper() for t in data.get("tickers", [])]
        # Validate — only return tickers that actually exist in db
        valid_tickers = {r["t"].upper() for r in db}
        tickers = [t for t in tickers if t in valid_tickers]

        log.info(f"  watchlist-build: selected {len(tickers)} tickers")
        return jsonify({
            "ok":        True,
            "tickers":   tickers,
            "reasoning": data.get("reasoning", ""),
        })

    except json.JSONDecodeError as e:
        log.error(f"  watchlist-build JSON error: {e} — raw: {raw[:200]}")
        return jsonify({"ok": False, "error": "Claude returned invalid JSON"}), 500
    except Exception as e:
        log.error(f"  watchlist-build error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/enrich-yields", methods=["POST"])
def enrich_yields():
    """
    Bulk-fetch dividend yield from Yahoo Finance for all tickers in the DB
    and persist the value into streetwise_data.json.

    Body (optional): { "tickers": ["SCHD","VYM"] }  — omit to run all
    Returns: { ok, updated, skipped, errors, results: [{ticker, yield}] }
    """
    body    = request.get_json(force=True) or {}
    targets = [t.upper() for t in body.get("tickers", [])]

    db      = load_data()
    lookup  = {r["t"].upper(): r for r in db}
    to_run  = [t for t in lookup] if not targets else [t for t in targets if t in lookup]

    updated = 0
    skipped = 0
    errors  = []
    results = []

    for ticker in to_run:
        y_sym = YAHOO_MAP.get(ticker, ticker)
        try:
            info  = yf.Ticker(y_sym).info
            raw   = info.get("dividendYield")   # float like 0.0312
            if raw is not None and raw > 0:
                lookup[ticker]["div_yield"] = round(float(raw), 6)
                updated += 1
                results.append({"ticker": ticker, "yield": round(raw * 100, 2)})
                log.info(f"  enrich-yields: {ticker} = {raw*100:.2f}%")
            else:
                # Explicitly store None so we don't keep re-querying no-dividend stocks
                lookup[ticker]["div_yield"] = None
                skipped += 1
        except Exception as e:
            log.warning(f"  enrich-yields: {ticker} failed — {e}")
            errors.append({"ticker": ticker, "error": str(e)})

    # Save
    merged = list(lookup.values())
    try:
        save_tickers(merged)
        log.info(f"  enrich-yields: {updated} updated, {skipped} no-dividend, {len(errors)} errors")
        return jsonify({
            "ok": True, "updated": updated,
            "skipped": skipped, "errors": errors,
            "results": sorted(results, key=lambda x: -x["yield"])
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/etf-holdings/<ticker>")
def etf_holdings(ticker):
    """
    Return top holdings + sector weightings for an ETF via yfinance funds_data.
    Cached in-memory for 6 hours to avoid repeated slow fetches.
    """
    import time as _time
    ticker = ticker.upper()
    cache_key = f"__etf_holdings_{ticker}__"
    cached = _cache.get(cache_key)
    if cached and (_time.time() - cached.get("_ts", 0)) < 21600:   # 6h TTL
        payload = {k: v for k, v in cached.items() if k != "_ts"}
        return jsonify(payload)

    try:
        y_sym = YAHOO_MAP.get(ticker, ticker)
        fd    = yf.Ticker(y_sym).funds_data

        # Top holdings — symbol is the DataFrame index, not a column
        holdings = []
        if fd.top_holdings is not None and not fd.top_holdings.empty:
            df = fd.top_holdings.reset_index()   # move index → column
            cols = [c.lower() for c in df.columns]
            # Locate columns flexibly (yfinance column names vary by version)
            sym_col  = next((df.columns[i] for i, c in enumerate(cols) if 'symbol' in c), None)
            name_col = next((df.columns[i] for i, c in enumerate(cols) if 'name' in c or 'holding' in c), None)
            pct_col  = next((df.columns[i] for i, c in enumerate(cols) if 'percent' in c or 'asset' in c or 'weight' in c), None)
            log.info(f"  etf-holdings cols: {list(df.columns)} → sym={sym_col} name={name_col} pct={pct_col}")
            for _, row in df.iterrows():
                sym  = str(row[sym_col])  if sym_col  else ""
                name = str(row[name_col]) if name_col else ""
                pct  = float(row[pct_col]) if pct_col and row[pct_col] is not None else 0
                # yfinance returns fraction (0.07) or percent (7.0) — normalise
                if pct_col and pct < 2:
                    pct = pct * 100
                holdings.append({"symbol": sym, "name": name, "pct": round(pct, 2)})

        # Sector weightings — sorted descending
        sectors = []
        sw = fd.sector_weightings or {}
        for k, v in sorted(sw.items(), key=lambda x: -(x[1] or 0)):
            if v:
                sectors.append({"key": k, "pct": round(float(v) * 100, 2)})

        total_pct = round(sum(h["pct"] for h in holdings), 2)
        result = {"ok": True, "ticker": ticker,
                  "holdings": holdings, "sectors": sectors,
                  "total_pct": total_pct}

        _cache[cache_key] = {**result, "_ts": _time.time()}
        return jsonify(result)

    except Exception as e:
        log.warning(f"etf-holdings {ticker}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/add-ticker", methods=["POST"])
def add_ticker():
    """
    Add a new ticker (or update an existing one) with a custom summary.
    Body: {
      ticker, name, type, sector, status, rec_analyst,
      price, base, bear, bull, summary, source, tags
    }
    If ticker already exists, merges the summary as a new section.
    """
    body   = request.get_json(force=True)
    ticker = body.get("ticker", "").strip().upper()
    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400

    today    = datetime.now().strftime("%Y-%m-%d")
    source   = body.get("source", "custom").strip() or "custom"
    prefix   = source[:3].lower()
    ep_key   = f"{prefix}:{datetime.now().strftime('%Y/%-m/%-d')}"
    label    = body.get("tags", "").strip() or today
    heading  = f"=== {ep_key} | {source} · {label} ==="
    raw_sum  = (body.get("summary", "") or "").strip()
    new_sum  = (heading + "\n" + raw_sum) if raw_sum else ""

    ALLOWED_STATUS = {"hot","rot","pull","press","dip","rec","caut","flat"}

    db     = load_data()
    lookup = {r["t"].upper(): r for r in db}

    if ticker in lookup:
        # Update existing — merge summary, update fields if provided
        rec = lookup[ticker]
        if new_sum:
            existing = rec.get("sum", "") or ""
            rec["sum"] = (existing.rstrip() + "\n\n" + new_sum).strip()
        for field, key in [
            ("name","n"), ("type","y"), ("sector","sector"),
            ("status","s"), ("rec_analyst","rec_analyst"),
            ("price","p"), ("base","base"), ("bear","bear"), ("bull","bull")
        ]:
            val = body.get(field, "")
            if val:
                rec[key] = val
        # Add ep_key and source tag
        if ep_key and ep_key not in rec.get("e", []):
            rec.setdefault("e", []).append(ep_key)
        if source not in rec.get("src", []):
            rec.setdefault("src", []).append(source)
        action = "updated"
    else:
        # Brand new ticker
        new_rec = {
            "t":   ticker,
            "n":   body.get("name", ticker),
            "y":   body.get("type", "Stock"),
            "s":   body.get("status", "flat") if body.get("status","") in ALLOWED_STATUS else "flat",
            "p":   body.get("price", ""),
            "e":   [ep_key] if ep_key else [],
            "src": [source],
            "sum": new_sum,
            "base": body.get("base", ""),
            "bear": body.get("bear", ""),
            "bull": body.get("bull", ""),
        }
        if body.get("sector"):   new_rec["sector"]     = body["sector"]
        if body.get("rec_analyst"): new_rec["rec_analyst"] = body["rec_analyst"]
        lookup[ticker] = new_rec
        action = "added"

    merged = list(lookup.values())
    try:
        save_tickers(merged)
        log.info(f"  add-ticker: {action} {ticker}")
        return jsonify({"ok": True, "action": action, "ticker": ticker})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/bulk-add-tickers", methods=["POST"])
def bulk_add_tickers():
    """
    Bulk-import tickers from a CSV upload.
    Body: { tickers: ["AAPL","TSLA",...], source: "Finviz Screen", tag: "Tech Watch" }

    Each ticker is added as a new record (or updated if it already exists).
    Returns: { ok, added, updated, skipped, errors }

    The 'source' is stored in rec["src"] (list) and used for the episode key.
    The 'tag'    is stored as the episode label inside rec["sum"] heading and in rec["tags"].
    """
    body    = request.get_json(force=True, silent=True) or {}
    tickers = body.get("tickers", [])
    source  = (body.get("source", "") or "csv-import").strip()
    tag     = (body.get("tag",    "") or "").strip()

    if not tickers or not isinstance(tickers, list):
        return jsonify({"ok": False, "error": "tickers list required"}), 400

    # Sanitise: uppercase, strip, deduplicate, max 10 chars, valid chars
    import re
    clean_pattern = re.compile(r'^[A-Z0-9.\-\^=]{1,10}$')
    seen = set()
    valid = []
    for t in tickers:
        t = str(t).strip().upper()
        if t and t not in seen and clean_pattern.match(t):
            seen.add(t)
            valid.append(t)

    if not valid:
        return jsonify({"ok": False, "error": "no valid tickers after sanitisation"}), 400

    _now     = datetime.now()
    today    = _now.strftime("%Y-%m-%d")
    prefix   = source[:3].lower()
    ep_key   = f"{prefix}:{_now.year}/{_now.month}/{_now.day}"
    label    = tag or today
    heading  = f"=== {ep_key} | {source} · {label} ==="

    db     = load_data()
    lookup = {r["t"].upper(): r for r in db if r.get("t")}

    added = updated = skipped = 0
    errors = []

    for ticker in valid:
        try:
            if ticker in lookup:
                rec = lookup[ticker]
                # Append source tag if not already present
                if source not in rec.get("src", []):
                    rec.setdefault("src", []).append(source)
                # Append tag to tags list if provided
                if tag and tag not in rec.get("tags", []):
                    rec.setdefault("tags", []).append(tag)
                # Add episode key reference
                if ep_key not in rec.get("e", []):
                    rec.setdefault("e", []).append(ep_key)
                updated += 1
            else:
                new_rec = {
                    "t":    ticker,
                    "n":    ticker,          # name filled later by Yahoo on first quote fetch
                    "y":    "Stock",
                    "s":    "flat",
                    "p":    "",
                    "e":    [ep_key],
                    "src":  [source],
                    "tags": [tag] if tag else [],
                    "sum":  heading,
                    "base": "", "bear": "", "bull": "",
                }
                lookup[ticker] = new_rec
                added += 1
        except Exception as exc:
            errors.append({"ticker": ticker, "error": str(exc)})
            skipped += 1

    try:
        save_tickers(list(lookup.values()))
        log.info(f"  bulk-add-tickers: +{added} new, ~{updated} updated, {skipped} skipped (source={source!r} tag={tag!r})")
        return jsonify({
            "ok":      True,
            "added":   added,
            "updated": updated,
            "skipped": skipped,
            "errors":  errors,
        })
    except Exception as e:
        log.error(f"bulk-add-tickers save failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/edit-ticker", methods=["POST"])
def edit_ticker():
    """
    Rename a ticker symbol in streetwise_data.json.
    Also updates the in-memory quote cache key.
    Body: { old_ticker, new_ticker }
    """
    body       = request.get_json(force=True)
    old_ticker = body.get("old_ticker", "").strip().upper()
    new_ticker = body.get("new_ticker", "").strip().upper()

    if not old_ticker or not new_ticker:
        return jsonify({"ok": False, "error": "old_ticker and new_ticker required"}), 400
    if old_ticker == new_ticker:
        return jsonify({"ok": True, "message": "no change"})

    data  = load_data()
    found = False
    for rec in data:
        if rec.get("t", "").upper() == old_ticker:
            rec["t"] = new_ticker
            found    = True
            log.info(f"  ticker renamed: {old_ticker} → {new_ticker}")
            break

    if not found:
        return jsonify({"ok": False, "error": f"{old_ticker} not found in database"}), 404

    try:
        save_tickers(data)
        # Move cache entry to new key
        if old_ticker in _cache:
            _cache[new_ticker] = _cache.pop(old_ticker)
        return jsonify({"ok": True, "old": old_ticker, "new": new_ticker})
    except Exception as e:
        log.error(f"edit-ticker save failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/edit-fields", methods=["POST"])
def edit_fields():
    """
    Update any static fields on a ticker record in streetwise_data.json.
    Body: { ticker, fields: { sector, n, y, s, rec_analyst, ... } }
    Only the fields present in the body are updated — others are unchanged.
    """
    body    = request.get_json(force=True)
    ticker  = body.get("ticker", "").strip().upper()
    fields  = body.get("fields", {})

    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400
    if not fields:
        return jsonify({"ok": True, "message": "nothing to update"})

    # Only allow safe, known fields — prevent overwriting structural keys
    ALLOWED = {"sector", "n", "y", "s", "rec_analyst", "p", "base", "bear", "bull"}
    updates = {k: v for k, v in fields.items() if k in ALLOWED}
    if not updates:
        return jsonify({"ok": False, "error": "no valid fields to update"}), 400

    data  = load_data()
    found = False
    for rec in data:
        if rec.get("t", "").upper() == ticker:
            rec.update(updates)
            found = True
            log.info(f"  fields updated: {ticker}  {updates}")
            break

    if not found:
        return jsonify({"ok": False, "error": f"{ticker} not found"}), 404

    try:
        save_tickers(data)
        return jsonify({"ok": True, "ticker": ticker, "updated": updates})
    except Exception as e:
        log.error(f"edit-fields save failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/save-research", methods=["POST"])
def save_research():
    """
    Append a research section to a ticker's summary in streetwise_data.json.
    Body: { ticker, section }
    section is a pre-formatted === ... === block ready to insert.
    """
    body    = request.get_json(force=True)
    ticker  = body.get("ticker", "").upper().strip()
    section = body.get("section", "").strip()

    if not ticker or not section:
        return jsonify({"ok": False, "error": "ticker and section required"}), 400

    data = load_data()
    found = False
    for rec in data:
        if rec.get("t", "").upper() == ticker:
            existing = rec.get("sum", "")
            rec["sum"] = (existing.rstrip() + "\n\n" + section) if existing.strip() else section
            found = True
            break

    if not found:
        return jsonify({"ok": False, "error": f"{ticker} not found in database"}), 404

    try:
        save_tickers(data)
        # Also update allData cache-busting by clearing quote cache for this ticker
        cache_expire([ticker])
        log.info(f"Research saved for {ticker}")
        return jsonify({"ok": True})
    except Exception as e:
        log.error(f"Save research failed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Finnhub — earnings calendar + news sentiment ──────────────────────────────
def _fmt_rev(v):
    if not v: return None
    v = float(v)
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    return f"${v:.0f}"

def _eps_surprise(actual, estimate):
    try:
        a, e = float(actual), float(estimate)
        if e == 0: return None
        return round((a - e) / abs(e) * 100, 1)
    except Exception:
        return None

@app.route("/api/finnhub/<ticker>")
def get_finnhub(ticker):
    """Finnhub earnings calendar + news sentiment. Requires FINNHUB_API_KEY."""
    import urllib.request as _ur, urllib.error as _ue
    api_key = os.environ.get("FINNHUB_API_KEY", "").strip()
    ticker  = ticker.upper()
    if not api_key:
        return jsonify({"ok": True, "ticker": ticker, "earnings": {}, "sentiment": {},
                        "error": "FINNHUB_API_KEY not set — add to /etc/streetwise.env"})
    result = {"ok": True, "ticker": ticker, "earnings": {}, "sentiment": {}}
    # Earnings calendar
    try:
        today  = date.today().isoformat()
        ahead  = (date.today() + timedelta(days=90)).isoformat()
        url    = (f"https://finnhub.io/api/v1/calendar/earnings"
                  f"?from={today}&to={ahead}&symbol={ticker}&token={api_key}")
        resp   = _ur.urlopen(_ur.Request(url), timeout=10)
        cal    = json.loads(resp.read()).get("earningsCalendar", [])
        if cal:
            e = cal[0]
            result["earnings"] = {
                "next_date":       e.get("date", ""),
                "eps_estimate":    e.get("epsEstimate"),
                "eps_actual":      e.get("epsActual"),
                "revenue_estimate": _fmt_rev(e.get("revenueEstimate")),
                "surprise":        _eps_surprise(e.get("epsActual"), e.get("epsEstimate")),
            }
    except Exception as ex:
        log.warning(f"Finnhub earnings {ticker}: {ex}")
        result["earnings_error"] = str(ex)
    # News sentiment
    try:
        url  = f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={api_key}"
        resp = _ur.urlopen(_ur.Request(url), timeout=10)
        data = json.loads(resp.read())
        result["sentiment"] = {
            "score":    data.get("sentiment", {}).get("bullishPercent", 0.5),
            "buzz":     data.get("buzz", {}).get("buzz", 0),
            "articles": data.get("buzz", {}).get("articlesInLastWeek", 0),
        }
    except Exception as ex:
        log.warning(f"Finnhub sentiment {ticker}: {ex}")

    # Price target
    try:
        url  = f"https://finnhub.io/api/v1/stock/price-target?symbol={ticker}&token={api_key}"
        data = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        result["price_target"] = {
            "mean":    data.get("targetMean"),
            "high":    data.get("targetHigh"),
            "low":     data.get("targetLow"),
            "median":  data.get("targetMedian"),
            "updated": data.get("lastUpdated", ""),
        }
    except Exception as ex:
        log.warning(f"Finnhub price-target {ticker}: {ex}")

    # Analyst recommendation trends (latest month)
    try:
        url  = f"https://finnhub.io/api/v1/stock/recommendation?symbol={ticker}&token={api_key}"
        data = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        if data:
            r = data[0]
            result["recommendation"] = {
                "period":      r.get("period", ""),
                "strong_buy":  r.get("strongBuy", 0),
                "buy":         r.get("buy", 0),
                "hold":        r.get("hold", 0),
                "sell":        r.get("sell", 0),
                "strong_sell": r.get("strongSell", 0),
            }
    except Exception as ex:
        log.warning(f"Finnhub recommendation {ticker}: {ex}")

    # EPS beat/miss history (last 4 quarters)
    try:
        url  = f"https://finnhub.io/api/v1/stock/earnings?symbol={ticker}&limit=4&token={api_key}"
        data = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        result["earnings_history"] = [
            {"period": e.get("period",""), "actual": e.get("actual"),
             "estimate": e.get("estimate"), "surprise": e.get("surprisePercent")}
            for e in (data or [])[:4]
        ]
    except Exception as ex:
        log.warning(f"Finnhub earnings history {ticker}: {ex}")

    # Insider sentiment (last 6 months)
    try:
        from_d = (date.today() - timedelta(days=180)).isoformat()
        to_d   = date.today().isoformat()
        url    = (f"https://finnhub.io/api/v1/stock/insider-sentiment"
                  f"?symbol={ticker}&from={from_d}&to={to_d}&token={api_key}")
        data   = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        rows   = data.get("data") or []
        if rows:
            latest = rows[-1]
            result["insider"] = {
                "mspr":   latest.get("mspr"),
                "change": latest.get("change"),
                "month":  str(latest.get("month", "")),
            }
    except Exception as ex:
        log.warning(f"Finnhub insider {ticker}: {ex}")

    # Basic financials / key metrics
    try:
        url  = f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}&metric=all&token={api_key}"
        data = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        m    = data.get("metric", {})
        result["metrics"] = {
            "beta":          m.get("beta"),
            "pe_ttm":        m.get("peTTM"),
            "pb":            m.get("pb"),
            "roe":           m.get("roeTTM"),
            "debt_equity":   m.get("totalDebt/totalEquityQuarterly"),
            "current_ratio": m.get("currentRatioQuarterly"),
            "rev_growth":    m.get("revenueGrowthTTMYoy"),
        }
    except Exception as ex:
        log.warning(f"Finnhub metrics {ticker}: {ex}")

    # Company news (last 7 days, up to 8 articles)
    try:
        from_d = (date.today() - timedelta(days=7)).isoformat()
        to_d   = date.today().isoformat()
        url    = (f"https://finnhub.io/api/v1/company-news"
                  f"?symbol={ticker}&from={from_d}&to={to_d}&token={api_key}")
        data   = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        result["news"] = [
            {"headline": n.get("headline",""), "source": n.get("source",""),
             "url": n.get("url",""), "dt": n.get("datetime", 0)}
            for n in (data or [])[:8]
        ]
    except Exception as ex:
        log.warning(f"Finnhub news {ticker}: {ex}")

    # Peers
    try:
        url  = f"https://finnhub.io/api/v1/stock/peers?symbol={ticker}&token={api_key}"
        data = json.loads(_ur.urlopen(_ur.Request(url), timeout=10).read())
        result["peers"] = [p for p in (data or []) if p != ticker][:10]
    except Exception as ex:
        log.warning(f"Finnhub peers {ticker}: {ex}")

    return jsonify(result)


# ── Perplexity /v1/responses — deep research (fast-search preset) ─────────────
@app.route("/api/perplexity", methods=["POST"])
def perplexity_research():
    """
    Perplexity deep research using sonar-pro via /chat/completions.
    Body: {ticker, name, query}. Requires PERPLEXITY_API_KEY.
    """
    import urllib.request as _ur, urllib.error as _ue
    body   = request.get_json(force=True)
    ticker = body.get("ticker", "").upper().strip()
    name   = body.get("name", "")
    query  = body.get("query", "").strip()
    if not ticker:
        return jsonify({"ok": False, "error": "ticker required"}), 400
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return jsonify({"ok": False,
                        "error": "PERPLEXITY_API_KEY not set — add to /etc/streetwise.env"})

    default_query = (
        f"Give me a comprehensive deep-dive investment research report on {ticker} ({name}). "
        f"Cover: latest earnings results with actual vs estimated EPS and revenue, "
        f"revenue growth trajectory (YoY and QoQ with exact figures), "
        f"RPO or backlog trends if applicable, "
        f"forward guidance and management commentary, "
        f"analyst consensus price targets and recent upgrades/downgrades, "
        f"key catalysts and risks in the next 12 months, "
        f"valuation (P/E, P/S, EV/EBITDA vs peers), "
        f"competitive positioning and market share trends, "
        f"and any recent material news or events. "
        f"Be specific — include exact dollar amounts, percentages, dates, and quarter references."
    )
    user_query = query if query else default_query

    system_prompt = (
        f"You are a senior equity research analyst writing an institutional-grade report on {ticker} ({name}). "
        f"Write in depth with specific numbers, percentages, dates, and quarter references throughout. "
        f"Do not hedge excessively — give your best analysis based on the latest available data. "
        f"Structure your response with clear markdown headers (##) for each section. "
        f"Each section should have multiple sentences with supporting data, not just a single line."
    )

    payload = json.dumps({
        "model": "sonar-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_query},
        ],
        "max_tokens": 4096,
        "temperature": 0.2,
    }).encode()

    req = _ur.Request(
        "https://api.perplexity.ai/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        resp = _ur.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            text = "Perplexity returned an empty response — check your API key and billing."
        usage = data.get("usage", {})
        p_inp  = usage.get("prompt_tokens", 0)
        p_out  = usage.get("completion_tokens", 0)
        p_cost = (p_inp * 3.0 + p_out * 15.0) / 1_000_000  # sonar-pro pricing
        log.info(f"  perplexity sonar-pro: {ticker} {len(text)} chars in={p_inp} out={p_out} cost=${p_cost:.4f}")
        log_cost("perplexity-sonar-pro", ticker, p_inp, p_out, p_cost, model="sonar-pro")
        return jsonify({"ok": True, "text": text})
    except _ue.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:400]
        log.error(f"Perplexity HTTP {e.code}: {err}")
        return jsonify({"ok": False, "error": f"Perplexity API error {e.code}: {err}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Perplexity /search — live news per ticker ──────────────────────────────────
@app.route("/api/news/<ticker>")
def get_news(ticker):
    """
    Live news headlines for a ticker via Perplexity /search endpoint.
    Returns top 5 results: title, url, snippet.
    Requires PERPLEXITY_API_KEY.
    """
    import urllib.request as _ur, urllib.error as _ue
    ticker  = ticker.upper()
    api_key = os.environ.get("PERPLEXITY_API_KEY", "").strip()
    if not api_key:
        return jsonify({"ok": False,
                        "error": "PERPLEXITY_API_KEY not set — add to /etc/streetwise.env"})
    payload = json.dumps({
        "query":              f"{ticker} stock news earnings analyst",
        "max_results":        5,
        "max_tokens_per_page": 256,
    }).encode()
    req = _ur.Request(
        "https://api.perplexity.ai/search",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
    )
    try:
        resp    = _ur.urlopen(req, timeout=20)
        data    = json.loads(resp.read())
        results = data.get("results") or data.get("hits") or []
        articles = [
            {"title": r.get("title", ""),
             "url":   r.get("url", ""),
             "snippet": (r.get("text") or r.get("snippet") or "")[:300]}
            for r in results
        ]
        log.info(f"  news /search: {ticker} {len(articles)} articles")
        return jsonify({"ok": True, "ticker": ticker, "articles": articles})
    except _ue.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        log.error(f"Perplexity /search HTTP {e.code}: {err}")
        return jsonify({"ok": False, "error": f"Perplexity search error {e.code}: {err}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── SEC EDGAR helpers — free, no API key ──────────────────────────────────────
_sec_tickers_cache: dict = {}
_sec_tickers_ts: float   = 0.0

def _sec_get_cik(ticker: str) -> str | None:
    """Look up CIK for a ticker using SEC's company_tickers.json (cached 24h)."""
    import urllib.request as _ur
    global _sec_tickers_cache, _sec_tickers_ts
    if not _sec_tickers_cache or (time.time() - _sec_tickers_ts) > 86400:
        try:
            req  = _ur.Request("https://www.sec.gov/files/company_tickers.json",
                               headers={"User-Agent": "Streetwise/1.0 research@streetwise.app"})
            data = json.loads(_ur.urlopen(req, timeout=15).read())
            _sec_tickers_cache = {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                                  for v in data.values()}
            _sec_tickers_ts = time.time()
            log.info(f"  sec: loaded {len(_sec_tickers_cache)} tickers from EDGAR")
        except Exception as ex:
            log.warning(f"  sec: could not load company_tickers.json: {ex}")
    return _sec_tickers_cache.get(ticker.upper())

def _sec_get_rpo_xbrl(cik: str) -> list:
    """Fetch RPO figures from SEC XBRL API. Returns list of {end, val, form} dicts."""
    import urllib.request as _ur
    # Try primary URL first, then alternate path
    for path in [
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/RevenueRemainingPerformanceObligation.json",
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/ContractWithCustomerLiability.json",
    ]:
        try:
            req  = _ur.Request(path, headers={"User-Agent": "Streetwise/1.0 research@streetwise.app"})
            data = json.loads(_ur.urlopen(req, timeout=15).read())
            rows = (data.get("units") or {}).get("USD", [])
            # Keep only annual filings (10-K / 20-F), sorted newest first
            annual = sorted(
                [r for r in rows if r.get("form") in ("10-K", "20-F")],
                key=lambda x: x.get("end", ""), reverse=True
            )
            if annual:
                return annual, data.get("entityName", ""), data.get("label", ""), path
        except Exception:
            continue
    return [], "", "", ""


def _sec_get_rpo_efts_fallback(cik: str, ticker: str) -> tuple:
    """Fallback when XBRL has no RPO data.
    Downloads the latest 10-K primary document via EDGAR submissions API,
    finds the 'remaining performance obligation' section, returns raw text.
    Returns (extracted_text, filing_info_str) or ("", "") if not found.
    """
    import urllib.request as _ur
    import re
    try:
        # Step A — get latest 10-K accession number from submissions API
        subs_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        req = _ur.Request(subs_url,
                          headers={"User-Agent": "Streetwise/1.0 research@streetwise.app"})
        subs = json.loads(_ur.urlopen(req, timeout=15).read())

        filings      = subs.get("filings", {}).get("recent", {})
        forms        = filings.get("form", [])
        adsh_list    = filings.get("accessionNumber", [])
        dates        = filings.get("filingDate", [])
        primary_docs = filings.get("primaryDocument", [])

        adsh = filing_date = primary_doc = matched_form = None
        for i, form in enumerate(forms):
            if form in ("10-K", "20-F", "10-Q"):
                adsh         = adsh_list[i]
                filing_date  = dates[i]
                primary_doc  = primary_docs[i] if i < len(primary_docs) else None
                matched_form = form
                break

        if not adsh or not primary_doc:
            log.info(f"  sec efts: no recent 10-K found for CIK {cik}")
            return "", ""

        # Step B — download the primary filing document (cap at 2 MB)
        adsh_clean = adsh.replace("-", "")
        cik_int    = str(int(cik))
        doc_url    = (f"https://www.sec.gov/Archives/edgar/data/"
                      f"{cik_int}/{adsh_clean}/{primary_doc}")
        log.info(f"  sec efts: downloading {doc_url[:80]}...")
        req2     = _ur.Request(doc_url,
                               headers={"User-Agent": "Streetwise/1.0 research@streetwise.app"})
        response = _ur.urlopen(req2, timeout=30)
        html     = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")

        # Step C — strip HTML, find RPO mentions
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&nbsp;',  ' ', text)
        text = re.sub(r'&amp;',   '&', text)
        text = re.sub(r'\s+',     ' ', text)

        matches = list(re.finditer(r'remaining performance obligation', text, re.IGNORECASE))
        if not matches:
            log.info(f"  sec efts: 'remaining performance obligation' not found in {matched_form}")
            return "", ""

        # Extract text around first mention (200 chars before, 2500 after)
        m       = matches[0]
        start   = max(0, m.start() - 200)
        end     = min(len(text), m.start() + 2500)
        extract = text[start:end].strip()

        filing_info = f"{ticker} {matched_form} filed {filing_date} (EDGAR full-text)"
        log.info(f"  sec efts: found RPO text in {matched_form} {filing_date} — {len(extract)} chars")
        return extract, filing_info

    except Exception as ex:
        log.warning(f"  sec efts fallback {ticker}: {ex}")
        return "", ""


# ── RPO Extractor — SEC EDGAR XBRL → Claude Sonnet 4.6 → Gemini 2.5 Flash ────
@app.route("/api/rpo/<ticker>", methods=["POST"])
def rpo_extract(ticker):
    """3-step RPO pipeline. Step 1 uses free SEC EDGAR XBRL (no Exa key needed).
    Requires ANTHROPIC_API_KEY + GEMINI_API_KEY. EXA_API_KEY now optional."""
    import urllib.request as _ur, urllib.error as _ue, anthropic as _ant
    body   = request.get_json(force=True)
    name   = body.get("name", "")
    ticker = ticker.upper()

    ant_key     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    exa_key     = os.environ.get("EXA_API_KEY",       "").strip()
    gemini_key  = os.environ.get("GEMINI_API_KEY",    "").strip()
    extra_prompt = body.get("extra_prompt", "").strip()
    extra_note   = f"\n\nAdditional instructions from analyst: {extra_prompt}" if extra_prompt else ""

    missing = [k for k, v in [("ANTHROPIC_API_KEY", ant_key),
                               ("GEMINI_API_KEY",    gemini_key)] if not v]
    if missing:
        return jsonify({"ok": False, "error": f"Missing keys: {', '.join(missing)}"})

    result = {"ok": True, "ticker": ticker}

    # Step 1 — SEC EDGAR XBRL (free, no API key) → Claude formats + interprets
    try:
        cik = _sec_get_cik(ticker)
        if not cik:
            result["step1"] = (
                f"• **CIK not found:** {ticker} was not found in SEC EDGAR.\n"
                f"• **Tip:** Check the ticker is a US-listed company.\n"
                f"• **Alternative:** Open the SEC Filings tab → search EDGAR manually."
            )
        else:
            rows, entity_name, label, src_path = _sec_get_rpo_xbrl(cik)
            if not rows:
                # XBRL concept not tagged — try EFTS full-text fallback
                efts_text, efts_info = _sec_get_rpo_efts_fallback(cik, ticker)
                if efts_text:
                    client  = _ant.Anthropic(api_key=ant_key)
                    msg_efts = client.messages.create(
                        model="claude-sonnet-4-5", max_tokens=600,
                        messages=[{"role": "user", "content":
                            f"Extract all Remaining Performance Obligation (RPO) figures from this "
                            f"SEC filing excerpt for {ticker} and format as bullet points ONLY "
                            f"(no tables, no headers, no markdown ##).\n\n"
                            f"Source: {efts_info}\n\n"
                            f"Text:\n{efts_text}\n\n"
                            f"Required output (use only what you can find — do not fabricate):\n"
                            f"• **Total RPO:** $X.XB as of [date]\n"
                            f"• **Current (next 12m):** $X.XB (if disclosed)\n"
                            f"• **Long-term (beyond 12m):** $X.XB (if disclosed)\n"
                            f"• **YoY change:** vs prior period if mentioned\n"
                            f"• **Source:** {efts_info}\n"
                            f"If the filing does not disclose RPO, say so clearly."
                            f"{extra_note}"}])
                    result["step1"] = msg_efts.content[0].text.strip()
                    log.info(f"  rpo step1 (EFTS fallback): {ticker} — parsed from {efts_info}")
                else:
                    result["step1"] = (
                        f"• **No RPO data:** SEC EDGAR has no XBRL filing for "
                        f"RevenueRemainingPerformanceObligation for {ticker}, and no RPO "
                        f"disclosure was found in the latest 10-K text.\n"
                        f"• **Business type note:** RPO is typically disclosed by SaaS / "
                        f"subscription companies (MSFT, CRM, NOW, ADBE, NVDA). "
                        f"Restaurants, retailers, and industrials rarely have RPO.\n"
                        f"• **Manual option:** Open the SEC tab, find the latest 10-K, "
                        f"search Ctrl+F → 'remaining performance obligation'."
                    )
            else:
                # Build YoY summary from XBRL rows
                def _fmt_usd(v):
                    if v >= 1e12: return f"${v/1e12:.2f}T"
                    if v >= 1e9:  return f"${v/1e9:.2f}B"
                    return f"${v/1e6:.0f}M"

                latest = rows[0]
                prior  = rows[1] if len(rows) > 1 else None
                val_fmt = _fmt_usd(latest["val"])
                yoy_str = "—"
                if prior and prior["val"]:
                    chg = (latest["val"] - prior["val"]) / prior["val"] * 100
                    yoy_str = f"{'+' if chg >= 0 else ''}{chg:.1f}% vs {prior['end']}"

                # Pass to Claude for interpretation and current/long-term split note
                xbrl_summary = (
                    f"Ticker: {ticker} ({entity_name})\n"
                    f"XBRL concept: {label}\n"
                    f"Latest filing ({latest['form']} {latest['end']}): {val_fmt}\n"
                    f"Prior year filing: {_fmt_usd(prior['val']) if prior else '—'}\n"
                    f"YoY change: {yoy_str}\n"
                    f"Last 4 annual values: " +
                    ", ".join(f"{r['end']}: {_fmt_usd(r['val'])}" for r in rows[:4])
                )

                client = _ant.Anthropic(api_key=ant_key)
                msg = client.messages.create(
                    model="claude-sonnet-4-5", max_tokens=500,
                    messages=[{"role": "user", "content":
                        f"Format this SEC EDGAR RPO data for {ticker} as bullet points ONLY "
                        f"(no tables, no headers, no markdown ##):\n\n{xbrl_summary}\n\n"
                        f"Required format:\n"
                        f"• **Total RPO:** value from latest 10-K\n"
                        f"• **YoY change:** percentage change with context\n"
                        f"• **Trend:** 1-sentence description of the RPO trend over 4 years\n"
                        f"• **Current (12m) / Long-term split:** note that XBRL total is available "
                        f"but the split requires reading the 10-K footnote\n"
                        f"• **Filing date:** date of latest 10-K\n"
                        f"• **Source:** SEC EDGAR XBRL — free, official data"
                        f"{extra_note}"}])
                result["step1"] = msg.content[0].text.strip()
                log.info(f"  rpo step1: {ticker} CIK={cik} RPO={val_fmt} YoY={yoy_str}")

    except Exception as ex:
        log.error(f"RPO step1 {ticker}: {ex}")
        result["step1"] = f"• **Error (Step 1):** {str(ex)[:200]}"

    # Step 2 — Claude verifies unbilled backlog
    try:
        client = _ant.Anthropic(api_key=ant_key)
        msg2 = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=512,
            messages=[{"role": "user", "content":
                f"Based on this RPO data for {ticker}:\n{result.get('step1','')}\n\n"
                f"Summarize the backlog verification using ONLY bullet points. "
                f"No markdown tables, no headers, just • bullets.\n\n"
                f"• **Billed backlog:** amount already invoiced (estimate or — if unavailable)\n"
                f"• **Unbilled backlog:** contracted but not yet billed\n"
                f"• **12m conversion rate:** estimated % converting to revenue next 12 months\n"
                f"• **Confidence:** High / Medium / Low — one sentence reason\n"
                f"• **Business type note:** is this a SaaS/subscription company where RPO is meaningful?"
                f"{extra_note}"}])
        i2, o2, c2 = calc_cost(msg2)
        result["step2"] = msg2.content[0].text.strip()
        log_cost("rpo-step2", ticker, i2, o2, c2, model="claude-sonnet-4-5")
    except Exception as ex:
        log.error(f"RPO step2 {ticker}: {ex}")
        result["step2"] = f"• **Error (Step 2):** {str(ex)[:200]}"

    # Step 3 — Gemini 2.5 Flash builds revenue bridge
    try:
        gurl = ("https://generativelanguage.googleapis.com/v1beta/"
                f"models/gemini-2.5-flash:generateContent?key={gemini_key}")
        gpl  = json.dumps({"contents": [{"parts": [{"text":
            f"Build a 4-quarter revenue bridge for {ticker} ({name}) based on:\n"
            f"RPO: {result.get('step1','')}\nBacklog: {result.get('step2','')}\n\n"
            f"Use ONLY bullet points — no markdown tables, no headers.\n"
            f"• **Q1 forecast:** $X.XB (reason)\n• **Q2 forecast:** $X.XB\n"
            f"• **Q3 forecast:** $X.XB\n• **Q4 forecast:** $X.XB\n"
            f"• **Full year:** $X.XB (+XX% YoY)\n"
            f"• **Key assumption:** one sentence\n"
            f"If RPO data is unavailable, say so clearly rather than fabricating numbers."
            f"{extra_note}"}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}}).encode()
        greq  = _ur.Request(gurl, data=gpl, headers={"Content-Type": "application/json"})
        gdata = json.loads(_ur.urlopen(greq, timeout=30).read())
        parts = (gdata.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        result["step3"] = "".join(p.get("text", "") for p in parts).strip()
        g_usage = gdata.get("usageMetadata", {})
        g_inp = g_usage.get("promptTokenCount", 0)
        g_out = g_usage.get("candidatesTokenCount", 0)
        g_cost = (g_inp * 0.075 + g_out * 0.30) / 1_000_000
        log_cost("rpo-step3-gemini", ticker, g_inp, g_out, g_cost, model="gemini-2.5-flash")
    except Exception as ex:
        log.error(f"RPO step3 {ticker}: {ex}")
        result["step3"] = f"• **Error (Step 3):** {str(ex)[:200]}"

    return jsonify(result)


# ── /api/dcf/<ticker> ─────────────────────────────────────────────────────────
# Strategy: yfinance → market data only (price, mktcap, div, P/E, beta)
#           Gemini Flash (temperature=0) → DCF model (FCF metric, growth, WACC, IV)
# Rationale: yfinance sector/industry classification and FCF normalization are
#            unreliable across ADRs and non-standard tickers. Gemini already knows
#            which metric suits each company (OCF vs FCF) and has audited financials
#            in its training data. This removes all the fragile sector heuristics.

_dcf_mem: dict = {}   # in-memory layer (process lifetime)
_DCF_TTL = 7 * 86400  # 7 days — Gemini fundamental data doesn't change weekly

def _dcf_db_init():
    """Create dcf_cache table in price_history.db if it doesn't exist."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dcf_cache (
                ticker   TEXT PRIMARY KEY,
                ts       REAL NOT NULL,
                payload  TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

def _dcf_db_load(sym: str):
    """Return (ts, data_dict) from DB, or None if missing/stale."""
    try:
        conn = sqlite3.connect(DB_FILE)
        row  = conn.execute(
            "SELECT ts, payload FROM dcf_cache WHERE ticker=?", (sym,)
        ).fetchone()
        conn.close()
        if row:
            return row[0], json.loads(row[1])
    except Exception:
        pass
    return None

def _dcf_db_save(sym: str, ts: float, data: dict):
    """Upsert DCF result into DB."""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT OR REPLACE INTO dcf_cache (ticker, ts, payload) VALUES (?,?,?)",
            (sym, ts, json.dumps(data))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"  DCF DB save failed for {sym}: {e}")

_dcf_db_init()   # run once at import time

@app.route("/api/dcf/<ticker>")
def get_dcf_analysis(ticker):
    """
    Returns DCF intrinsic value analysis for a ticker.
    Market data (price, mktcap, div, pe, beta) from yfinance (always live).
    DCF model (FCF, growth rates, WACC, IV scenarios) from Gemini Flash at temp=0,
    cached in price_history.db for 7 days so Gemini isn't called on every load.
    """
    sym   = ticker.upper()
    force = bool(request.args.get("force"))
    now   = time.time()

    # ── 1. In-memory cache (fastest) ─────────────────────────────────────────
    mem = _dcf_mem.get(sym)
    if mem and (now - mem["ts"]) < _DCF_TTL and not force:
        return jsonify(mem["data"])

    # ── 2. SQLite cache (survives restarts) ───────────────────────────────────
    if not force:
        row = _dcf_db_load(sym)
        if row:
            db_ts, db_data = row
            if (now - db_ts) < _DCF_TTL:
                # refresh price + MoS from yfinance (cheap) then return
                try:
                    info  = yf.Ticker(sym).info
                    price = float(info.get("currentPrice") or info.get("previousClose") or db_data.get("price", 0))
                    iv    = db_data.get("iv_base") or 0
                    db_data["price"] = round(price, 2)
                    db_data["mos"]   = round(((iv - price) / iv * 100)) if iv else None
                    db_data["rating"] = ("buy" if (db_data["mos"] or 0) > 25 else
                                         "hold" if (db_data["mos"] or 0) > 0 else "watch")
                    db_data["cached_ts"] = db_ts
                except Exception:
                    pass
                _dcf_mem[sym] = {"ts": db_ts, "data": db_data}
                return jsonify(db_data)

    try:
        import urllib.request, urllib.error as _ue
        import re as _re

        # ── Step 1: yfinance — market data + raw financials ──────────────────
        tk        = yf.Ticker(sym)
        info      = tk.info
        price     = float(info.get("currentPrice") or info.get("previousClose") or 0)
        mktcap_r  = info.get("marketCap", 0) or 0
        mktcap    = f"${mktcap_r/1e9:.1f}B" if mktcap_r >= 1e9 else f"${mktcap_r/1e6:.0f}M"
        pe_v      = info.get("forwardPE") or info.get("trailingPE")
        pe_str    = f"Fwd P/E: {pe_v:.1f}×" if pe_v else "P/E: N/A"
        dy        = info.get("dividendYield", 0) or 0
        raw_beta  = float(info.get("beta", 1.0) or 1.0)
        beta      = round(max(0.5, min(2.5, raw_beta)), 2)
        sector    = info.get("sector", "")
        gm        = info.get("grossMargins", 0) or 0
        moat      = "Wide" if (mktcap_r > 50e9 and gm > 0.40) else "Narrow"
        ev_r      = info.get("enterpriseValue", 0) or 0
        name      = info.get("longName", sym)

        # Raw financials for Gemini (yfinance reports in local currency)
        _fin_ccy   = info.get("financialCurrency", "USD") or "USD"
        _trd_ccy   = info.get("currency", "USD") or "USD"
        _is_adr    = _fin_ccy != _trd_ccy

        # Get live FX rate for ADR conversion (local → USD)
        _fx = 1.0
        if _is_adr:
            _FX_FALLBACK = {"TWD": 0.031, "HKD": 0.128, "BRL": 0.200,
                            "CNH": 0.138, "KRW": 0.00072, "INR": 0.012, "JPY": 0.0067}
            _fx = _FX_FALLBACK.get(_fin_ccy, 1.0)
            try:
                _fx_info = yf.Ticker(f"{_fin_ccy}{_trd_ccy}=X").info
                _fx = float(_fx_info.get("regularMarketPrice") or _fx_info.get("previousClose") or _fx)
            except Exception:
                pass
            if dy > 0 and _fx < 1.0:
                dy = dy * _fx

        div_str = f"{dy*100:.1f}%" if dy else "0%"

        # Pull financials and convert to USD billions
        def _to_usd_b(raw_val):
            v = float(raw_val) if raw_val and raw_val != "N/A" else None
            if v is None: return None
            v_usd = v * _fx          # no-op if _fx == 1.0
            return round(v_usd / 1e9, 2)

        yf_fcf    = _to_usd_b(info.get("freeCashflow"))
        yf_ocf    = _to_usd_b(info.get("operatingCashflow"))
        yf_ebitda = _to_usd_b(info.get("ebitda"))
        yf_debt   = _to_usd_b(info.get("totalDebt", 0) or 0)
        yf_cash   = _to_usd_b(info.get("totalCash", 0) or 0)
        yf_shares_raw = info.get("sharesOutstanding")
        yf_shares = None
        if yf_shares_raw:
            s = float(yf_shares_raw)
            yf_shares = round(s / 1e9, 3)   # always in billions

        def _fmt(v, suffix="B"):
            return f"${v}{suffix}" if v is not None else "N/A"

        # ── Sector classification → drives model choice in prompt ─────────────
        _sector_up = (sector or "").lower()
        _industry  = (info.get("industry", "") or "").lower()
        _is_bank   = any(x in _sector_up or x in _industry for x in
                         ("bank", "insurance", "diversified financial", "capital markets",
                          "financial services", "thrift", "mortgage", "credit"))
        _is_reit   = any(x in _sector_up or x in _industry for x in
                         ("reit", "real estate investment trust"))
        _is_energy = any(x in _sector_up or x in _industry for x in
                         ("oil", "gas", "energy", "mining", "utilities", "pipeline"))

        # Extra data for banks (residual income needs book value + ROE)
        yf_bvps = _to_usd_b(info.get("bookValue"))       # book value per share (not in B)
        yf_roe  = info.get("returnOnEquity")              # already a ratio
        yf_eps  = info.get("trailingEps")
        # Rough book value per share for residual income (not converted to billions)
        _bvps_raw = float(info.get("bookValue") or 0) * _fx
        _roe_pct  = round(float(yf_roe or 0) * 100, 1) if yf_roe else None

        # Extra data for REITs (FFO ≈ Net Income + D&A − gains on sales)
        # yfinance doesn't expose FFO directly; pass net income + D&A so Gemini can calculate
        yf_net_income = _to_usd_b(info.get("netIncomeToCommon"))
        yf_da         = None  # yfinance doesn't reliably expose D&A in info

        # Build sector-specific task block for prompt
        if _is_bank:
            model_block = f"""SECTOR: Financial / Bank / Insurance — use RESIDUAL INCOME model (NOT DCF).
- Book Value Per Share: ${_bvps_raw:.2f}
- Return on Equity (ROE): {f"{_roe_pct}%" if _roe_pct else "N/A"}
- Trailing EPS: {f"${yf_eps:.2f}" if yf_eps else "N/A"}
- Net Income: {_fmt(yf_net_income)}
Steps:
1. Use Residual Income = EPS − (Cost of Equity × BVPS). Project 5 years.
2. Terminal value = BVPS grows at TGR perpetually
3. Intrinsic value = BVPS + PV of residual incomes + PV of terminal value
4. fcf_source should be "Residual Income model"
5. Set fcf = net income, ebitda = net income (best proxy available)"""
        elif _is_reit:
            model_block = f"""SECTOR: Real Estate / REIT — use FFO (Funds From Operations) model.
- Net Income: {_fmt(yf_net_income)}
- OCF (proxy for FFO): {_fmt(yf_ocf)}
- Total Debt: {_fmt(yf_debt)}
- Total Cash: {_fmt(yf_cash)}
Steps:
1. Estimate trailing FFO = Net Income + Depreciation & Amortization − gains on property sales
   (yfinance D&A not available — use OCF as best proxy for FFO if D&A unknown)
2. Apply a dividend discount / FFO multiple approach for terminal value
3. Use cap rate or P/FFO multiple for terminal value (not perpetuity growth)
4. fcf_source should be "FFO (REIT model)"
5. Set fcf = estimated FFO"""
        else:
            _sw = "platform/software" if any(x in _industry for x in ("software","internet","platform","streaming")) else ""
            # Detect depressed FCF: capex-heavy company in investment cycle
            _fcf_ratio = (yf_fcf / yf_ocf) if (yf_fcf and yf_ocf and yf_ocf > 0) else None
            _fcf_depressed = _fcf_ratio is not None and _fcf_ratio < 0.40
            # Normalised FCF proxy = EBITDA × (1 - tax) when FCF is cycle-depressed
            _norm_fcf = round(yf_ebitda * 0.79, 2) if (_fcf_depressed and yf_ebitda) else None
            _capex = round(yf_ocf - yf_fcf, 2) if (yf_ocf and yf_fcf) else None

            _capex_note = ""
            if _fcf_depressed and _norm_fcf:
                _capex_note = (
                    f"\nNOTE — FCF IS CYCLE-DEPRESSED: FCF/OCF = {_fcf_ratio:.0%} "
                    f"(CapEx=${_capex}B = {_capex/_to_usd_b(info.get('totalRevenue',1) or 1)*100:.0f}% of revenue). "
                    f"This company is in a peak capex investment cycle. "
                    f"Use NORMALISED FCF = EBITDA × (1−21%%) = ${_norm_fcf}B as your DCF base "
                    f"(reflects mid-cycle earnings power, not trough FCF). "
                    f"Set fcf_source to 'Normalised FCF (EBITDA×79%% — peak capex cycle)'."
                )

            model_block = f"""SECTOR: {sector or "General"} — use standard FCF / OCF DCF model.
Steps:
1. Choose base cash flow:
   - {"Use OCF — this is a " + _sw + " company, CapEx is growth investment" if _sw else "Use FCF (OCF − CapEx) for capex-heavy companies"}
   - {"" if _sw else "Use OCF if FCF is negative or < 50% of OCF (likely one-off CapEx spike)"}
   - If both are unreliable, use EBITDA × (1 − 21% tax rate) as proxy{_capex_note}
2. Estimate analyst-consensus 5-year growth rate (g1) and slower phase-2 rate (g2)
3. Terminal value:
   - Stable compounders / software: perpetuity growth (TGR 2–3%)
   - Cyclicals / semiconductors / {"energy" if _is_energy else "capex-heavy"}: EV/EBITDA exit multiple"""

        # ADR note for prompt
        adr_note = (
            f"IMPORTANT — {sym} is an ADR: reports in {_fin_ccy}, trades in USD. "
            f"The figures above are already converted to USD using fx_rate={_fx:.4f}. "
            f"All outputs (iv_base/bull/bear) must be in USD per ADR share."
        ) if _is_adr else ""

        log.info(f"  DCF {sym} yfinance inputs: price={price} fcf={yf_fcf} ocf={yf_ocf} "
                 f"ebitda={yf_ebitda} debt={yf_debt} cash={yf_cash} shares={yf_shares} "
                 f"beta={beta} fx={_fx:.4f} is_adr={_is_adr} sector='{sector}'")

        # ── Step 2: Gemini Flash — valuation math on real yfinance numbers ────
        gemini_key = os.environ.get("GEMINI_API_KEY", "").strip().strip('"').strip("'")
        if not gemini_key:
            return jsonify({"ok": False, "error": "GEMINI_API_KEY not set"}), 400

        today_str = datetime.now().strftime("%B %Y")
        prompt = f"""You are a CFA-level financial analyst. Perform an intrinsic value analysis for {sym} ({name}).
Reference date: {today_str}. Current market price: ${price:.2f}. Sector: {sector or "N/A"}.

== LIVE DATA FROM YAHOO FINANCE (already in USD) ==
- Trailing FCF (OCF − CapEx): {_fmt(yf_fcf)}
- Trailing OCF (Operating Cash Flow): {_fmt(yf_ocf)}
- Trailing EBITDA: {_fmt(yf_ebitda)}
- Total Debt: {_fmt(yf_debt)}
- Total Cash: {_fmt(yf_cash)}
- Shares Outstanding: {f"{yf_shares}B" if yf_shares else "N/A"}
- Beta: {beta}
- Gross Margin: {gm*100:.1f}%
{adr_note}

== MACRO ASSUMPTIONS (March 2026) ==
- Risk-Free Rate: 4.44%
- Equity Risk Premium: 5.0%
- Corporate Tax Rate: 21%

== YOUR TASKS ==
{model_block}
- Calculate WACC = Risk-Free Rate + Beta × Equity Risk Premium (adjust for leverage if applicable)
- Produce bull, base, bear intrinsic values per share

CRITICAL UNITS — output will be wrong if violated:
- fcf, ebitda, net_debt: BILLIONS of USD (e.g. 45.0 not 45000000000)
- shares: BILLIONS (e.g. 10.3 not 10300000000)
- g1_base/bull/bear, g2_base/bull/bear, wacc, tgr: PERCENTAGE POINTS (e.g. 12.0 not 0.12)
- iv_base/bull/bear: USD per share integer (e.g. 268)

Return ONLY valid JSON, no markdown fences, no text outside the JSON:
{{
  "fcf": <$B>,
  "fcf_source": "<e.g. 'Trailing OCF' or 'Trailing FCF' or 'EBITDA proxy'>",
  "ebitda": <$B>,
  "net_debt": <$B, negative = net cash>,
  "shares": <billions>,
  "g1_base": <pct-points>, "g1_bull": <pct-points>, "g1_bear": <pct-points>,
  "g2_base": <pct-points>, "g2_bull": <pct-points>, "g2_bear": <pct-points>,
  "wacc": <pct-points>,
  "tgr": <pct-points or null if exit_multiple>,
  "tv_method": "<perpetuity or exit_multiple>",
  "tv_horizon": <5 or 10>,
  "tv_label": "<e.g. 'Perpetuity @ 2.5%% TGR · WACC 9.5%%'>",
  "exit_mult": <e.g. 15.0 or null>,
  "iv_base": <USD/share>, "iv_bull": <USD/share>, "iv_bear": <USD/share>,
  "rationale": "<1-2 sentences on metric choice and key assumptions>",
  "risks": [
    {{"level": "high", "text": "<risk>"}},
    {{"level": "mid",  "text": "<risk>"}},
    {{"level": "low",  "text": "<risk>"}}
  ],
  "cats": [
    {{"tag": "Bull", "text": "<bull thesis>"}},
    {{"tag": "Base", "text": "<base thesis>"}},
    {{"tag": "Bear", "text": "<bear thesis>"}}
  ]
}}"""

        url = (
            "https://generativelanguage.googleapis.com/v1beta/"
            f"models/gemini-2.5-flash:generateContent?key={gemini_key}"
        )
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature":     0,
                "maxOutputTokens": 8192,
            },
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )

        try:
            resp_raw = urllib.request.urlopen(req, timeout=60)
        except _ue.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            log.error(f"  DCF Gemini HTTP {he.code} for {sym} — {err_body[:300]}")
            return jsonify({"ok": False, "error": f"Gemini API error {he.code}: {err_body[:200]}"}), 502

        g_data  = json.loads(resp_raw.read())
        parts   = (g_data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        raw_txt = "".join(p.get("text", "") for p in parts).strip()

        # ── Clean up Gemini's response before JSON parsing ────────────────────
        # 1. Strip markdown fences (with or without closing fence)
        fence = _re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_txt)
        if fence:
            json_txt = fence.group(1).strip()
        else:
            # Remove opening fence only (response may be truncated)
            json_txt = _re.sub(r'^```(?:json)?\s*', '', raw_txt.strip())

        # 2. Strip // single-line comments
        json_txt = _re.sub(r'//[^\n"]*', '', json_txt)
        # 3. Strip trailing commas before } or ]
        json_txt = _re.sub(r',\s*([}\]])', r'\1', json_txt)
        # 4. Python literals → JSON literals
        json_txt = json_txt.replace(': True', ': true').replace(': False', ': false').replace(': None', ': null')
        json_txt = json_txt.replace(':True', ':true').replace(':False', ':false').replace(':None', ':null')
        # 5. If JSON is truncated mid-object, try to close it
        try:
            g = json.loads(json_txt)
        except Exception:
            open_b = json_txt.count('{') - json_txt.count('}')
            open_a = json_txt.count('[') - json_txt.count(']')
            # Drop trailing incomplete token and close
            json_txt = _re.sub(r',?\s*"[^"]*$', '', json_txt)  # drop dangling key
            json_txt = _re.sub(r',?\s*[\w"]+\s*:\s*[^,}\]]*$', '', json_txt)
            json_txt += ']' * max(0, open_a) + '}' * max(0, open_b)
            try:
                g = json.loads(json_txt)
            except Exception as je2:
                # Dump full cleaned text to file for debugging
                _dbg_path = f"/tmp/dcf_debug_{sym}.txt"
                try:
                    with open(_dbg_path, "w") as _dbg:
                        _dbg.write(f"=== RAW ===\n{raw_txt}\n\n=== CLEANED ===\n{json_txt}")
                except Exception:
                    pass
                log.error(f"  DCF Gemini JSON parse error for {sym}: {je2} | debug dump: {_dbg_path}")
                return jsonify({"ok": False, "error": f"Gemini returned invalid JSON: {je2}"}), 502

        log.info(f"  DCF {sym} Gemini raw: fcf={g.get('fcf')} ocf_src='{g.get('fcf_source')}' "
                 f"shares={g.get('shares')} wacc={g.get('wacc')} "
                 f"iv_base={g.get('iv_base')} iv_bull={g.get('iv_bull')} iv_bear={g.get('iv_bear')}")

        # ── Step 3: Extract + sanitise Gemini fields ──────────────────────────
        def _f(key, default=None):
            v = g.get(key, default)
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        def _i(key, default=None):
            v = g.get(key, default)
            try:
                return int(round(float(v))) if v is not None else default
            except (TypeError, ValueError):
                return default

        # Auto-normalise units — Gemini sometimes returns raw dollars or decimal ratios
        def _billions(key, default=0.0):
            v = _f(key, default)
            if v is None: return default
            return round(v / 1e9 if abs(v) > 1e6 else v, 2)  # raw dollars → $B

        def _pct(key, default=0.0):
            v = _f(key, default)
            if v is None: return default
            return round(v * 100 if abs(v) < 2.0 else v, 1)  # decimal → pct-points

        fcf        = _billions("fcf", 0)
        ebitda     = _billions("ebitda", 0)
        net_debt   = _billions("net_debt", 0)
        shares_raw = _f("shares", 1)
        shares     = round(shares_raw / 1e9 if shares_raw > 1e6 else shares_raw, 3)
        g1_base    = _pct("g1_base", 10.0)
        g1_bull    = _pct("g1_bull", g1_base + 5.0)
        g1_bear    = _pct("g1_bear", g1_base - 5.0)
        g2_base    = _pct("g2_base", g1_base * 0.5)
        g2_bull    = _pct("g2_bull", g2_base + 3.0)
        g2_bear    = _pct("g2_bear", g2_base - 3.0)
        wacc       = _pct("wacc", 9.5)
        tgr_raw    = _f("tgr")
        tgr        = round(tgr_raw * 100 if tgr_raw is not None and abs(tgr_raw) < 2.0 else tgr_raw, 1) if tgr_raw is not None else None
        iv_base    = _i("iv_base")
        iv_bull    = _i("iv_bull")
        iv_bear    = _i("iv_bear")
        tv_method  = g.get("tv_method", "perpetuity")
        tv_horizon = _i("tv_horizon", 10)
        tv_label   = g.get("tv_label", f"WACC {wacc}%  ·  Gemini estimate")
        exit_mult  = _f("exit_mult")
        fcf_source = g.get("fcf_source", "Gemini estimate")
        rationale  = g.get("rationale", "")
        risks      = g.get("risks") or [{"level": "low", "text": "No risk flags returned — verify with latest 10-K"}]
        cats       = g.get("cats")  or []

        # Fallback thesis from streetwise_data.json if Gemini gave nothing
        if not cats:
            db     = load_data_raw()
            rec_db = next((r for r in db if r.get("t", "").upper() == sym), {})
            for tag, key in [("Bull", "bull"), ("Base", "base"), ("Bear", "bear")]:
                if rec_db.get(key):
                    cats.append({"tag": tag, "text": rec_db[key]})
        if not cats:
            cats = [{"tag": "N/A", "text": "No thesis data found."}]

        # ── Step 4: MoS + rating using live price ─────────────────────────────
        mos    = round(((iv_base - price) / iv_base * 100)) if iv_base else None
        rating = ("buy" if (mos or 0) > 25 else
                  "hold" if (mos or 0) > 0 else "watch")

        # EV/EBITDA from live market data (yfinance enterprise value ÷ Gemini EBITDA)
        ebitda_r    = ebitda * 1e9
        ev_ebitda_v = round(ev_r / ebitda_r, 1) if ebitda_r > 0 else None
        ev_ebitda_s = f"{ev_ebitda_v}×" if ev_ebitda_v else "N/A"

        # ── Step 5: Log Gemini cost ───────────────────────────────────────────
        usage  = g_data.get("usageMetadata", {})
        g_inp  = usage.get("promptTokenCount", 0)
        g_out  = usage.get("candidatesTokenCount", 0)
        r_in, r_out = 0.30, 2.50   # gemini-2.5-flash per M tokens (Mar 2026)
        g_cost = (g_inp * r_in + g_out * r_out) / 1e6
        log_cost("dcf-gemini", sym, g_inp, g_out, g_cost, model="gemini-2.5-flash")

        result = {
            "ok":               True,
            "ticker":           sym,
            "name":             name,
            "price":            round(price, 2),
            "mktcap":           mktcap,
            "fcf":              fcf,
            "fcf_source":       fcf_source,
            "ebitda":           ebitda,
            "g1_base":          g1_base,   "g1_bull": g1_bull,   "g1_bear": g1_bear,
            "g2_base":          g2_base,   "g2_bull": g2_bull,   "g2_bear": g2_bear,
            "tgr":              tgr,
            "wacc_default":     wacc,
            "wacc_rf":          4.44,      "wacc_erp": 5.0,
            "net_debt":         net_debt,
            "shares":           shares,
            "iv_base":          iv_base,   "iv_bull": iv_bull,   "iv_bear": iv_bear,
            "tv_method":        tv_method,
            "tv_horizon":       tv_horizon,
            "tv_label":         tv_label,
            "exit_mult":        exit_mult,
            "div":              div_str,
            "pe":               pe_str,
            "evfcf":            "N/A",
            "ev_ebitda":        ev_ebitda_s,
            "sector":           sector,
            "moat":             moat,
            "rating":           rating,
            "mos":              mos,
            "beta":             beta,
            "beta_raw":         round(raw_beta, 2),
            "beta_u":           None,
            "de_ratio":         0,
            "gross_margins":    round(gm * 100, 1),
            "financial_currency": "USD",
            "currency_converted": False,
            "dcf_source":       "gemini-2.5-flash",
            "rationale":        rationale,
            "risks":            risks,
            "cats":             cats,
        }
        ts = time.time()
        result["cached_ts"] = ts
        _dcf_mem[sym] = {"ts": ts, "data": result}
        _dcf_db_save(sym, ts, result)
        log.info(f"DCF {sym} (Gemini): price=${price} iv_base=${iv_base} mos={mos}% wacc={wacc}% [saved to DB]")
        return jsonify(result)

    except Exception as ex:
        log.exception(f"DCF error for {sym}")
        return jsonify({"ok": False, "error": str(ex)}), 500


# ── /api/crisis-monitor ───────────────────────────────────────────────────────
_crisis_cache: dict = {"ts": 0, "data": None}
_CRISIS_TTL = 900  # 15 minutes

@app.route("/api/crisis-monitor")
def crisis_monitor():
    """
    Hormuz Crisis Monitor — 5 targeted indicators for detecting crisis resolution.
      1. CBOE VIX            — broad fear gauge            (crisis over: VIX < 20)
      2. CNN Fear & Greed     — sentiment score 0-100       (crisis over: score > 50)
      3. WTI M1-M3 Spread    — crude backwardation proxy   (crisis over: spread < $2/bbl)
      4. Dutch TTF Gas        — European LNG pressure       (crisis over: TTF < €30/MWh)
      5. Frontline FRO stock  — VLCC freight rate proxy     (crisis over: FRO down >20% from 52w high)
    Cached 15 min.
    """
    import urllib.request as _ur

    now   = time.time()
    force = request.args.get("force") == "1"
    if not force and _crisis_cache["data"] and now - _crisis_cache["ts"] < _CRISIS_TTL:
        return jsonify(_crisis_cache["data"])

    result: dict = {"ok": True}

    # ── Helper: fetch yfinance history ────────────────────────────────────────
    def _yf_closes(sym, period="90d"):
        h = yf.Ticker(sym).history(period=period, interval="1d")
        if h.empty:
            raise ValueError("no data")
        return h["Close"].dropna()

    def _yf_block(sym, label, unit, closes, signal_thresh, signal_dir, signal_note, extra=None):
        cur   = float(closes.iloc[-1])
        prev  = float(closes.iloc[-2]) if len(closes) > 1 else cur
        w_ago = float(closes.iloc[-6]) if len(closes) >= 6 else prev
        m_ago = float(closes.iloc[0])
        spark = [round(float(v), 2) for v in closes.tail(30).tolist()]
        d = {
            "label": label, "symbol": sym, "unit": unit,
            "current":    round(cur, 2),
            "pct_day":    round((cur - prev) / prev * 100, 2) if prev else 0,
            "pct_week":   round((cur - w_ago) / w_ago * 100, 2) if w_ago else 0,
            "pct_month":  round((cur - m_ago) / m_ago * 100, 2) if m_ago else 0,
            "high_52w":   round(float(closes.max()), 2),
            "low_52w":    round(float(closes.min()), 2),
            "spark":      spark,
            "signal_thresh": signal_thresh,
            "signal_dir":    signal_dir,
            "signal_note":   signal_note,
        }
        if extra:
            d.update(extra)
        return d

    # ── 1. CBOE VIX ───────────────────────────────────────────────────────────
    try:
        c = _yf_closes("^VIX")
        result["vix"] = _yf_block("^VIX", "CBOE VIX", "", c, 20, "below",
                                   "Crisis over when VIX < 20")
    except Exception as ex:
        result["vix"] = {"label": "CBOE VIX", "symbol": "^VIX", "error": str(ex)}

    # ── 2. CNN Fear & Greed + Put/Call Ratio ─────────────────────────────────
    # Two calls to CNN:
    #   BASE URL  → current score + greed_factors (PCR lives here only)
    #   DATED URL → 90-day historical spark (greed_factors stripped for payload size)
    import gzip as _gz
    _CNN_HEADERS = {
        "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer":         "https://edition.cnn.com/markets/fear-and-greed",
        "Origin":          "https://edition.cnn.com",
        "Connection":      "keep-alive",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-site",
    }

    def _cnn_fetch(url):
        rb = _ur.urlopen(_ur.Request(url, headers=_CNN_HEADERS), timeout=12).read()
        try:
            rb = _gz.decompress(rb)
        except Exception:
            pass
        return json.loads(rb)

    # ── Fear & Greed composite (dated URL for 90-day history) ────────────────
    try:
        start_90d = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
        fg_hist   = _cnn_fetch(
            f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start_90d}")
        fg        = fg_hist.get("fear_and_greed", {})
        score     = float(fg.get("score", 0))
        rating    = str(fg.get("rating", "unknown")).replace("_", " ").title()
        hist_pts  = fg_hist.get("fear_and_greed_historical", {}).get("data", [])
        spark     = [round(float(d["y"]), 1) for d in hist_pts[-30:]] if hist_pts else [score]
        prev_s    = float(spark[-2]) if len(spark) >= 2 else score
        result["fg"] = {
            "label": "Fear & Greed", "symbol": "CNN", "unit": "/100",
            "current":       round(score, 1),
            "rating":        rating,
            "pct_day":       round(score - prev_s, 1),
            "pct_week":      None,
            "spark":         spark,
            "high_52w":      round(max(spark), 1),
            "low_52w":       round(min(spark), 1),
            "signal_thresh": 50,
            "signal_dir":    "above",
            "signal_note":   "Crisis over when F&G > 50 (neutral/greed)",
        }
    except Exception as ex:
        score    = None
        hist_pts = []
        result["fg"] = {"label": "Fear & Greed", "symbol": "CNN", "error": str(ex)}

    # ── CBOE SKEW Index via yfinance ──────────────────────────────────────────
    # Measures tail-risk / extreme downside fear from S&P 500 options pricing.
    # Normal: 100–130 | Elevated: 130–150 | Crisis: >150
    # Same meaning as put/call: high SKEW = institutions buying deep OTM puts.
    # Crisis-end signal: SKEW drops back below 130 (options market normalising).
    try:
        c = _yf_closes("^SKEW")
        result["pcr"] = _yf_block("^SKEW", "CBOE SKEW Index", "", c, 130, "below",
                                   "Crisis over when SKEW < 130")
    except Exception as ex:
        result["pcr"] = {"label": "CBOE SKEW Index", "symbol": "^SKEW", "error": str(ex)}

    # ── 3. WTI M1–M3 Crude Spread (EIA API) ───────────────────────────────────
    eia_key = os.environ.get("EIA_API_KEY", "").strip()
    try:
        if not eia_key:
            raise ValueError("EIA_API_KEY not set")
        url = (
            "https://api.eia.gov/v2/petroleum/pri/fut/data/"
            f"?api_key={eia_key}&frequency=daily"
            "&data[0]=value"
            "&facets[series][]=RCLC1"
            "&facets[series][]=RCLC3"
            "&sort[0][column]=period&sort[0][direction]=desc"
            "&length=90"
        )
        raw  = json.loads(_ur.urlopen(_ur.Request(url), timeout=15).read())
        rows = raw.get("response", {}).get("data", [])
        m1 = {r["period"]: float(r["value"])
              for r in rows if r.get("series") == "RCLC1"
              and r.get("value") not in (None, ".")}
        m3 = {r["period"]: float(r["value"])
              for r in rows if r.get("series") == "RCLC3"
              and r.get("value") not in (None, ".")}
        common = sorted(set(m1) & set(m3))
        if not common:
            raise ValueError("no overlapping dates for M1/M3")
        spreads = {d: round(m1[d] - m3[d], 3) for d in common}
        sorted_d = sorted(spreads)
        spark    = [spreads[d] for d in sorted_d[-30:]]
        cur      = spreads[sorted_d[-1]]
        prev     = spreads[sorted_d[-2]] if len(sorted_d) >= 2 else cur
        w_ago    = spreads[sorted_d[-6]] if len(sorted_d) >= 6 else prev
        result["spread"] = {
            "label": "WTI Backwardation", "symbol": "M1−M3", "unit": "$/bbl",
            "current":       round(cur, 2),
            "pct_day":       round(cur - prev, 2),    # absolute $/bbl change
            "pct_week":      round(cur - w_ago, 2),
            "pct_month":     None,
            "high_52w":      round(max(spreads.values()), 2),
            "low_52w":       round(min(spreads.values()), 2),
            "spark":         spark,
            "date":          sorted_d[-1],
            "signal_thresh": 2,
            "signal_dir":    "below",
            "signal_note":   "Crisis over when spread < $2/bbl",
        }
    except Exception as ex:
        result["spread"] = {"label": "WTI Backwardation", "symbol": "M1−M3", "error": str(ex)}

    # ── 4. JKM vs TTF Spread ─────────────────────────────────────────────────
    # Signal: JKM (Asia LNG) drops below TTF (Europe gas) → Qatari bidding war over.
    # Uses yfinance: NG=F (Henry Hub $/MMBtu) vs TTF=F (€/MWh → $/MMBtu via EURUSD=X).
    # HH correlates with global LNG arbitrage pricing during supply crises.
    # IMF PCPS API attempted first but often unreachable from cloud servers.
    import pandas as _pd

    _jkm_map: dict = {}
    _ttf_map: dict = {}
    _spread_source = "unknown"

    def _strip_tz(series):
        """Strip timezone from a yfinance DatetimeIndex so DataFrames align."""
        idx = series.index
        if hasattr(idx, 'tz') and idx.tz is not None:
            idx = idx.tz_convert('UTC').tz_localize(None)
        series.index = idx.normalize()   # date-only, no time component
        return series

    # Source 1 — IMF PCPS (try briefly; Oracle egress often blocked/slow)
    try:
        imf_url = (
            "https://dataservices.imf.org/REST/SDMX_JSON.svc/"
            "CompactData/PCPS/M.W0.PNGASJP+PNGASEU.USD"
            "?startPeriod=2023-01"
        )
        imf_raw = json.loads(_ur.urlopen(_ur.Request(imf_url), timeout=8).read())
        series  = (imf_raw.get("CompactData", {})
                          .get("DataSet",    {})
                          .get("Series",     []))
        if isinstance(series, dict):
            series = [series]
        for s in series:
            commodity = s.get("@COMMODITY", "")
            obs = s.get("Obs", [])
            if isinstance(obs, dict):
                obs = [obs]
            vals = {o["@TIME_PERIOD"]: float(o["@OBS_VALUE"])
                    for o in obs
                    if o.get("@OBS_VALUE") not in (None, "", "NA")}
            if commodity == "PNGASJP":
                _jkm_map = vals
            elif commodity == "PNGASEU":
                _ttf_map = vals
        if _jkm_map and _ttf_map:
            _spread_source = "IMF PCPS"
        else:
            raise ValueError("IMF series incomplete")
    except Exception as imf_ex:
        log.warning(f"  JKM/TTF IMF failed ({imf_ex}), trying yfinance")

    # Source 2 — yfinance: NG=F (Henry Hub) vs TTF=F converted to $/MMBtu
    if not (_jkm_map and _ttf_map):
        try:
            ng_s  = _strip_tz(_yf_closes("NG=F",    period="365d"))  # $/MMBtu
            ttf_s = _strip_tz(_yf_closes("TTF=F",   period="365d"))  # €/MWh
            eur_s = _strip_tz(_yf_closes("EURUSD=X", period="365d")) # EUR/USD

            # Outer join on date, forward-fill gaps (different trading calendars)
            all_idx = ng_s.index.union(ttf_s.index).union(eur_s.index)
            ng_s  = ng_s.reindex(all_idx).ffill().bfill()
            ttf_s = ttf_s.reindex(all_idx).ffill().bfill()
            eur_s = eur_s.reindex(all_idx).ffill().bfill()

            df = _pd.DataFrame({"ng": ng_s, "ttf": ttf_s, "eur": eur_s}).dropna()
            if df.empty:
                raise ValueError("DataFrame empty after ffill/bfill")

            # Convert TTF €/MWh → $/MMBtu  (1 MWh = 3.41214 MMBtu)
            df["ttf_usd"] = df["ttf"] * df["eur"] / 3.41214

            # Resample to month-end, keep last value
            # _jkm_map = TTF (European price) — the "expensive" benchmark
            # _ttf_map = HH  (US Henry Hub)   — the "cheap" baseline
            # Spread = TTF - HH = European gas premium over US (positive = crisis pressure)
            df_m = df.resample("ME").last().dropna()
            for dt, row in df_m.iterrows():
                k = dt.strftime("%Y-%m")
                _jkm_map[k] = round(float(row["ttf_usd"]), 3)   # TTF in $/MMBtu
                _ttf_map[k] = round(float(row["ng"]),      3)   # HH  in $/MMBtu
            _spread_source = "TTF−HH proxy"
        except Exception as yf_ex:
            result["ttf"] = {
                "label": "EU Gas Premium (TTF−HH)", "symbol": "TTF−HH",
                "error": f"yfinance: {yf_ex}",
            }

    if _jkm_map and _ttf_map and "ttf" not in result:
        # spread = "JKM" (or TTF) minus "TTF_ref" (or HH) — positive = Europe/Asia at premium
        common   = sorted(set(_jkm_map) & set(_ttf_map))
        spreads  = {d: round(_jkm_map[d] - _ttf_map[d], 3) for d in common}
        dates    = sorted(spreads)
        spark    = [spreads[d] for d in dates[-12:]]
        cur      = spreads[dates[-1]]
        prev     = spreads[dates[-2]] if len(dates) >= 2 else cur
        # Label adapts based on which source fired
        is_proxy = "HH" in _spread_source or "proxy" in _spread_source
        lbl      = "EU Gas Premium (TTF−HH)" if is_proxy else "JKM vs TTF Spread"
        sym      = "TTF−HH"                  if is_proxy else "JKM−TTF"
        thresh   = 8   if is_proxy else 0    # HH proxy: <$8 = TTF normalising; IMF: <$0
        note     = ("Crisis over when EU premium < $8/MMBtu"
                    if is_proxy else
                    "Crisis over when JKM drops below TTF price")
        result["ttf"] = {
            "label":         lbl,
            "symbol":        sym,
            "unit":          "$/MMBtu",
            "current":       round(cur, 2),
            "jkm":           round(_jkm_map[dates[-1]], 2),
            "ttf_val":       round(_ttf_map[dates[-1]], 2),
            "pct_day":       round(cur - prev, 2),
            "pct_week":      None,
            "spark":         spark,
            "high_52w":      round(max(spreads[d] for d in dates[-12:]), 2),
            "low_52w":       round(min(spreads[d] for d in dates[-12:]), 2),
            "last_date":     dates[-1],
            "data_source":   _spread_source,
            "signal_thresh": thresh,
            "signal_dir":    "below",
            "signal_note":   note,
        }

    # ── 5. MOVE Index — bond market implied volatility ────────────────────────
    # ICE BofA MOVE = bond market's VIX. Normal 70-80, crisis >100.
    # At 111.95 (Mar 2026) = stagflation fear; Fed rate-cut expectations collapsing.
    # Crisis-end signal: MOVE drops back below 80 (bond market calming).
    # Source 1: ^MOVE via yfinance (works on some builds of yfinance)
    # Source 2: 21-day rolling vol of ^TNX (10y Treasury yield) as proxy
    try:
        c = _yf_closes("^MOVE")
        result["move"] = _yf_block("^MOVE", "MOVE Index", "", c, 80, "below",
                                   "Crisis over when MOVE < 80")
        result["move"]["sublabel"] = "Bond mkt implied vol"
    except Exception:
        # Fallback: annualised 21-day rolling σ of daily TNX changes (bps)
        try:
            tnx = _yf_closes("^TNX", period="90d")   # 10y yield in %
            bps = tnx.diff().dropna() * 100           # daily change in basis points
            vol = bps.rolling(21).std().dropna() * (252 ** 0.5)  # annualise
            if vol.empty:
                raise ValueError("TNX rolling vol empty")
            cur   = float(vol.iloc[-1])
            prev  = float(vol.iloc[-2]) if len(vol) > 1 else cur
            w_ago = float(vol.iloc[-6]) if len(vol) >= 6 else prev
            spark = [round(float(v), 1) for v in vol.tail(30).tolist()]
            result["move"] = {
                "label":      "MOVE (TNX proxy)",
                "symbol":     "^TNX vol",
                "unit":       " bp/yr",
                "current":    round(cur, 1),
                "pct_day":    round((cur - prev) / prev * 100, 2) if prev else 0,
                "pct_week":   round((cur - w_ago) / w_ago * 100, 2) if w_ago else 0,
                "high_52w":   round(float(vol.max()), 1),
                "low_52w":    round(float(vol.min()), 1),
                "spark":      spark,
                "sublabel":   "21d realised TNX vol",
                "signal_thresh": 40,    # proxy equivalent of MOVE ~80
                "signal_dir":    "below",
                "signal_note":   "Crisis over when bond vol < 40 bp/yr",
            }
        except Exception as ex2:
            result["move"] = {"label": "MOVE Index", "symbol": "^MOVE",
                              "error": str(ex2)}

    # ── 6. Frontline FRO — VLCC freight rate proxy ────────────────────────────
    try:
        c      = _yf_closes("FRO", period="365d")
        cur    = float(c.iloc[-1])
        prev   = float(c.iloc[-2]) if len(c) > 1 else cur
        w_ago  = float(c.iloc[-6]) if len(c) >= 6 else prev
        high52 = float(c.max())
        low52  = float(c.min())
        pct_peak = round((cur - high52) / high52 * 100, 1) if high52 else 0
        spark  = [round(float(v), 2) for v in c.tail(30).tolist()]
        result["tanker"] = {
            "label": "VLCC Rates (FRO)", "symbol": "FRO", "unit": "$/sh",
            "current":       round(cur, 2),
            "pct_day":       round((cur - prev) / prev * 100, 2) if prev else 0,
            "pct_week":      round((cur - w_ago) / w_ago * 100, 2) if w_ago else 0,
            "pct_from_peak": pct_peak,
            "high_52w":      round(high52, 2),
            "low_52w":       round(low52, 2),
            "spark":         spark,
            "signal_thresh": -20,
            "signal_dir":    "below_peak",   # pct_from_peak < -20 = crisis over
            "signal_note":   "Crisis over when FRO down >20% from 52w high",
        }
    except Exception as ex:
        result["tanker"] = {"label": "VLCC Rates (FRO)", "symbol": "FRO", "error": str(ex)}

    result["cached_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _crisis_cache["ts"]   = now
    _crisis_cache["data"] = result
    log.info(
        f"  crisis-monitor: VIX={result.get('vix',{}).get('current','?')} "
        f"F&G={result.get('fg',{}).get('current','?')} "
        f"MOVE={result.get('move',{}).get('current','?')} "
        f"spread={result.get('spread',{}).get('current','?')} "
        f"TTF={result.get('ttf',{}).get('current','?')} "
        f"FRO={result.get('tanker',{}).get('current','?')}"
    )
    return jsonify(result)


# ── /api/costs endpoint ───────────────────────────────────────────────────────
@app.route("/api/costs")
def get_costs():
    """Return cost ledger summary — today, this month, all-time, and last 50 events."""
    if not os.path.exists(COSTS_FILE):
        return jsonify({"ok": True, "events": [], "today": 0, "month": 0, "alltime": 0})
    try:
        with open(COSTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    today = datetime.utcnow().strftime("%Y-%m-%d")
    month = datetime.utcnow().strftime("%Y-%m")

    today_cost  = sum(e["cost"] for e in events if e.get("date") == today)
    month_cost  = sum(e["cost"] for e in events if e.get("date", "").startswith(month))
    alltime_cost = sum(e["cost"] for e in events)

    # Per-service breakdown (all-time)
    by_service: dict = {}
    for e in events:
        svc = e.get("service", "unknown")
        by_service[svc] = by_service.get(svc, 0) + e["cost"]
    by_service = {k: round(v, 4) for k, v in sorted(by_service.items(), key=lambda x: -x[1])}

    return jsonify({
        "ok":       True,
        "today":    round(today_cost,   4),
        "month":    round(month_cost,   4),
        "alltime":  round(alltime_cost, 4),
        "by_service": by_service,
        "events":   events[-50:][::-1],   # last 50, newest first
        "total_events": len(events),
    })


# ── /health endpoint ──────────────────────────────────────────────────────────
@app.route("/health")
def health_check():
    """
    Health check — returns API key status, data file info, DB stats, disk free, last backup.
    Exempt from auth (safe for monitoring / uptime checkers).
    """
    import shutil

    def key_ok(name): return bool(os.environ.get(name, "").strip())

    data_ok   = os.path.exists(DATA_FILE)
    data_size = round(os.path.getsize(DATA_FILE) / 1024, 1) if data_ok else 0
    try:
        ticker_count = len([r for r in load_data_raw() if not r.get("__meta__")]) if data_ok else 0
    except Exception:
        ticker_count = -1

    db_ok = os.path.exists(DB_FILE)
    db_rows = db_tickers = 0
    if db_ok:
        try:
            conn = sqlite3.connect(DB_FILE)
            db_rows    = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            db_tickers = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
            conn.close()
        except Exception:
            pass

    disk = shutil.disk_usage(".")
    disk_free_gb = round(disk.free / 1024**3, 1)

    backups = sorted([f for f in os.listdir(".") if f.startswith("streetwise_data.bak.")], reverse=True)

    return jsonify({
        "ok": True,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "api_keys": {
            "anthropic":  key_ok("ANTHROPIC_API_KEY"),
            "gemini":     key_ok("GEMINI_API_KEY"),
            "finnhub":    key_ok("FINNHUB_API_KEY"),
            "perplexity": key_ok("PERPLEXITY_API_KEY"),
            "exa":        key_ok("EXA_API_KEY"),
        },
        "data_file": {
            "exists":       data_ok,
            "size_kb":      data_size,
            "ticker_count": ticker_count,
        },
        "price_db": {
            "exists":  db_ok,
            "rows":    db_rows,
            "tickers": db_tickers,
        },
        "disk_free_gb": disk_free_gb,
        "last_backup":  backups[0] if backups else None,
        "backup_count": len(backups),
    })


@app.route("/api/table-data")
def table_data():
    """
    Return all tickers with period % returns and sparkline data from price_history.db.
    Columns: sector, ticker, name, price, change, pct, 1W, 3W, 1M, 3M, 6M, YTD, 1Y,
             52W high, 52W low, spark_1y (52 weekly closes), spark_1m (22 daily closes)
    """
    import sqlite3 as _sq
    from datetime import date as _date, timedelta as _td

    db = load_data()
    if not os.path.exists("price_history.db"):
        return jsonify({"ok": False, "error": "price_history.db not found"}), 404

    today    = _date.today()
    iso      = lambda d: d.isoformat()
    d_1w     = iso(today - _td(days=7))
    d_3w     = iso(today - _td(days=21))
    d_1m     = iso(today - _td(days=30))
    d_3m     = iso(today - _td(days=91))
    d_6m     = iso(today - _td(days=182))
    d_1y     = iso(today - _td(days=365))
    d_ytd    = f"{today.year}-01-01"

    conn = _sq.connect("price_history.db")
    cur  = conn.cursor()

    def last_close(ticker, before):
        r = cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
            (ticker, before)
        ).fetchone()
        return r[0] if r else None

    def pct(new, old):
        if not old or old == 0 or not new: return None
        return round((new - old) / old * 100, 2)

    rows = []
    for d in db:
        t = d.get("t", "")
        if not t or t.startswith("__"): continue

        # Last 2 closes for current price + daily change
        last2 = cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 2",
            (t, today.isoformat())
        ).fetchall()
        db_cur  = last2[0][0] if last2 else None
        db_prev = last2[1][0] if len(last2) > 1 else None
        cur_price = d.get("price_raw") or db_cur

        # Period anchors
        p_1w  = last_close(t, d_1w)
        p_3w  = last_close(t, d_3w)
        p_1m  = last_close(t, d_1m)
        p_3m  = last_close(t, d_3m)
        p_6m  = last_close(t, d_6m)
        p_1y  = last_close(t, d_1y)
        p_ytd = last_close(t, d_ytd)

        # 52W high / low
        hl = cur.execute(
            "SELECT MAX(close), MIN(close) FROM prices WHERE ticker=? AND date>=?",
            (t, d_1y)
        ).fetchone()
        h52 = round(hl[0], 2) if hl and hl[0] else None
        l52 = round(hl[1], 2) if hl and hl[1] else None

        # 1Y sparkline — sample every 5 trading days (~weekly)
        closes_1y = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_1y)
        ).fetchall()]
        spark_1y = closes_1y[::5] if len(closes_1y) > 10 else closes_1y

        # YTD sparkline — daily
        spark_ytd = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_ytd)
        ).fetchall()]

        # 1M sparkline — daily
        spark_1m = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_1m)
        ).fetchall()]

        rows.append({
            "t":   t,
            "n":   d.get("n", t),
            "sec": d.get("sector") or "N/A",
            "ind": d.get("industry", ""),
            "type": d.get("y", "Stock"),
            "status": d.get("status", ""),
            "rec":  d.get("rec", ""),
            "wl":   d.get("watchlists") or [],
            "e":    d.get("e") or [],
            "currency": d.get("currency", "USD"),
            "px":  round(float(cur_price), 2) if cur_price else None,
            "chg": round(float(d.get("diff_raw")), 2) if d.get("diff_raw") is not None
                   else (round(float(cur_price) - float(db_prev), 2) if cur_price and db_prev else None),
            "pct": round(float(d.get("pct_raw") or 0), 2) if d.get("pct_raw") is not None else None,
            "w1":  pct(cur_price, p_1w),
            "w3":  pct(cur_price, p_3w),
            "m1":  pct(cur_price, p_1m),
            "m3":  pct(cur_price, p_3m),
            "m6":  pct(cur_price, p_6m),
            "ytd": pct(cur_price, p_ytd),
            "y1":  pct(cur_price, p_1y),
            "h52": h52,
            "l52": l52,
            "s1y": spark_1y,
            "sytd": spark_ytd,
            "s1m": spark_1m,
        })

    conn.close()
    rows.sort(key=lambda r: r["t"])
    return jsonify({"ok": True, "rows": rows, "as_of": today.isoformat()})


@app.route("/api/refresh-table", methods=["POST"])
def refresh_table():
    """
    Full refresh for Table View:
      1. Fetch live quotes for ALL tickers (price, change, pct, sector, rec)
         — updates in-memory cache + persists sector/rec back to JSON
      2. Fetch 1Y daily history from Yahoo for ALL tickers
         — saves/upserts rows into price_history.db
      3. Returns the full table-data payload (same as /api/table-data)
         so the frontend can re-render immediately.
    Runs synchronously; expect ~30-60 seconds for 200+ tickers.
    Accepts optional JSON body: {"tickers": ["AAPL","MSFT",...]} to restrict scope.
    """
    import sqlite3 as _sq
    from datetime import date as _date, timedelta as _td

    body       = request.get_json(silent=True) or {}
    db         = load_data()
    all_tickers = [d["t"] for d in db if d.get("t") and not d["t"].startswith("__")]

    wanted_set = None
    if body.get("tickers"):
        wanted_set = {t.strip().upper() for t in body["tickers"]}
        all_tickers = [t for t in all_tickers if t in wanted_set]

    log.info(f"refresh-table: {len(all_tickers)} tickers — quotes + 1Y history")

    # ── Step 1: live quotes ───────────────────────────────────────────────────
    changed = False
    for item in db:
        t = item.get("t","")
        if not t or t.startswith("__"): continue
        if wanted_set and t not in wanted_set: continue
        cache_expire([t])
        q = fetch_quote(t)
        item.update(q)
        if q.get("sector") and q["sector"] != "N/A":
            item["sector"] = q["sector"]; changed = True
        if q.get("rec") and q["rec"] != "N/A":
            item["rec"] = q["rec"]; changed = True
        if q.get("div_yield") is not None:
            item["div_yield"] = q["div_yield"]; changed = True

    if changed:
        try: save_tickers(db)
        except Exception as e: log.warning(f"refresh-table: could not persist: {e}")

    # ── Step 2: 1Y daily history → price_history.db ──────────────────────────
    # Fetch 1Y daily for all tickers; db_upsert saves rows automatically
    for t in all_tickers:
        try:
            fetch_yf_history(t, "1Y")   # internally calls db_upsert
        except Exception as e:
            log.warning(f"refresh-table: history failed for {t}: {e}")

    log.info(f"refresh-table: done — rebuilding table rows")

    # ── Step 3: rebuild table rows (same logic as /api/table-data) ───────────
    today    = _date.today()
    iso      = lambda d: d.isoformat()
    d_1w     = iso(today - _td(days=7))
    d_3w     = iso(today - _td(days=21))
    d_1m     = iso(today - _td(days=30))
    d_3m     = iso(today - _td(days=91))
    d_6m     = iso(today - _td(days=182))
    d_1y     = iso(today - _td(days=365))
    d_ytd    = f"{today.year}-01-01"

    if not os.path.exists("price_history.db"):
        return jsonify({"ok": False, "error": "price_history.db not found"}), 404

    conn = _sq.connect("price_history.db")
    cur  = conn.cursor()

    def last_close(ticker, before):
        r = cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
            (ticker, before)
        ).fetchone()
        return r[0] if r else None

    def pct(new, old):
        if not old or old == 0 or not new: return None
        return round((new - old) / old * 100, 2)

    rows = []
    for d in db:
        t = d.get("t", "")
        if not t or t.startswith("__"): continue
        if wanted_set and t not in wanted_set: continue

        # Last 2 closes for current price + daily change
        last2 = cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 2",
            (t, today.isoformat())
        ).fetchall()
        db_cur  = last2[0][0] if last2 else None
        db_prev = last2[1][0] if len(last2) > 1 else None
        cur_price = d.get("price_raw") or db_cur

        p_1w  = last_close(t, d_1w)
        p_3w  = last_close(t, d_3w)
        p_1m  = last_close(t, d_1m)
        p_3m  = last_close(t, d_3m)
        p_6m  = last_close(t, d_6m)
        p_1y  = last_close(t, d_1y)
        p_ytd = last_close(t, d_ytd)

        hl = cur.execute(
            "SELECT MAX(close), MIN(close) FROM prices WHERE ticker=? AND date>=?",
            (t, d_1y)
        ).fetchone()
        h52 = round(hl[0], 2) if hl and hl[0] else None
        l52 = round(hl[1], 2) if hl and hl[1] else None

        closes_1y = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_1y)
        ).fetchall()]
        spark_1y = closes_1y[::5] if len(closes_1y) > 10 else closes_1y

        spark_ytd = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_ytd)
        ).fetchall()]

        spark_1m = [r[0] for r in cur.execute(
            "SELECT close FROM prices WHERE ticker=? AND date>=? ORDER BY date", (t, d_1m)
        ).fetchall()]

        rows.append({
            "t":      t,
            "n":      d.get("n", t),
            "sec":    d.get("sector") or "N/A",
            "ind":    d.get("industry", ""),
            "type":   d.get("type", "Stock"),
            "status": d.get("status", ""),
            "rec":    d.get("rec", ""),
            "wl":     d.get("watchlists") or [],
            "e":      d.get("e") or [],
            "currency": d.get("currency", "USD"),
            "px":  round(float(cur_price), 2) if cur_price else None,
            "chg": round(float(d.get("diff_raw")), 2) if d.get("diff_raw") is not None
                   else (round(float(cur_price) - float(db_prev), 2) if cur_price and db_prev else None),
            "pct": round(float(d.get("pct_raw") or 0), 2) if d.get("pct_raw") is not None else None,
            "w1":  pct(cur_price, p_1w),
            "w3":  pct(cur_price, p_3w),
            "m1":  pct(cur_price, p_1m),
            "m3":  pct(cur_price, p_3m),
            "m6":  pct(cur_price, p_6m),
            "ytd": pct(cur_price, p_ytd),
            "y1":  pct(cur_price, p_1y),
            "h52": h52,
            "l52": l52,
            "s1y": spark_1y,
            "sytd": spark_ytd,
            "s1m": spark_1m,
        })

    conn.close()
    rows.sort(key=lambda r: r["t"])
    return jsonify({"ok": True, "rows": rows, "as_of": today.isoformat(),
                    "refreshed": len(all_tickers)})


@app.route("/api/delete-tickers", methods=["POST"])
def delete_tickers():
    """
    Delete one or more tickers from streetwise_data.json AND price_history.db.
    Body: {"tickers": ["AAPL", "MSFT"]}
    """
    body    = request.get_json(silent=True) or {}
    targets = {str(t).strip().upper() for t in (body.get("tickers") or []) if t and str(t).strip()}
    if not targets:
        return jsonify({"ok": False, "error": "No tickers provided"}), 400

    db = load_data()
    before = len(db)
    db_new = [d for d in db if d.get("t", "").upper() not in targets]
    removed_json = before - len(db_new)

    try:
        save_tickers(db_new)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to save JSON: {e}"}), 500

    # Also remove from price_history.db
    removed_db = 0
    if os.path.exists(DB_FILE):
        try:
            conn = sqlite3.connect(DB_FILE)
            cur  = conn.cursor()
            for t in targets:
                cur.execute("DELETE FROM prices WHERE ticker=?", (t,))
                removed_db += cur.rowcount
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"delete-tickers: DB error: {e}")

    # Also expire from memory cache
    cache_expire(list(targets))

    log.info(f"delete-tickers: removed {removed_json} JSON entries, {removed_db} price rows for {sorted(targets)}")
    return jsonify({
        "ok": True,
        "removed_tickers": removed_json,
        "removed_price_rows": removed_db,
        "tickers": sorted(targets),
    })


if __name__ == "__main__":
    # Force UTF-8 on Windows so the startup banner (─ ✓ ⚠ …) doesn't crash
    # WHY: Windows defaults to cp1252; Unicode chars in banner cause UnicodeEncodeError on startup
    # DEBUG: If you see "UnicodeEncodeError: 'charmap' codec can't encode character" on launch,
    #   this reconfigure call is missing or failed. Check Python version >= 3.7 (reconfigure added 3.7)
    # NOTE: The banner ticker count includes the __sources__ meta-record, so it may read "N tickers"
    #   where N = actual tickers + 1. This is cosmetic only; load_data() correctly excludes meta records.
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sep = "─" * 54
    print(f"\n\033[1m\033[36m{sep}\033[0m")
    print(f"\033[1m  BARRONS WATCHLISTS  —  server.py\033[0m")
    print(f"\033[36m{sep}\033[0m")

    # Data files check
    db_ok   = os.path.exists(DB_FILE)
    data_ok = os.path.exists(DATA_FILE)
    data    = []
    if data_ok:
        try:
            with open(DATA_FILE, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except Exception:
            pass

    print(f"  \033[2m{'streetwise_data.json':<26}\033[0m  ", end="")
    if data_ok:
        print(f"\033[32m✓  {len(data)} tickers\033[0m")
    else:
        print(f"\033[33m⚠  not found\033[0m")

    print(f"  \033[2m{'price_history.db':<26}\033[0m  ", end="")
    if db_ok:
        conn = sqlite3.connect(DB_FILE)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            tkrs = conn.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
            print(f"\033[32m✓  {rows:,} rows · {tkrs} tickers\033[0m")
        except Exception:
            print(f"\033[33m⚠  db error\033[0m")
        finally:
            conn.close()
    else:
        print(f"\033[33m⚠  not found — run: python history_manager.py --init\033[0m")

    # API key status
    def _key_status(label, val, hint):
        if val:
            masked = val[:10] + "…" + val[-4:] if len(val) > 16 else val
            print(f"  \033[2m{label:<26}\033[0m  \033[32m✓  {masked}\033[0m")
        else:
            print(f"  \033[2m{label:<26}\033[0m  \033[33m⚠  not set  ({hint})\033[0m")

    _key_status("Anthropic API key",   os.environ.get("ANTHROPIC_API_KEY",""),  "Claude + Live Research + RPO")
    _key_status("Gemini API key",      os.environ.get("GEMINI_API_KEY",""),     "Gemini search + RPO step 3")
    _key_status("Finnhub API key",     os.environ.get("FINNHUB_API_KEY",""),    "Earnings calendar + sentiment")
    _key_status("Perplexity API key",  os.environ.get("PERPLEXITY_API_KEY",""), "Research tab + live news (/v1/responses + /search)")
    _key_status("Exa API key",         os.environ.get("EXA_API_KEY",""),        "RPO 10-K document retrieval")

    # Cache TTL
    print(f"  \033[2m{'Quote cache TTL':<26}\033[0m  \033[36m{CACHE_TTL}s\033[0m")

    # Supported ranges
    ranges_str = "  ".join(RANGES.keys())
    print(f"  \033[2m{'History ranges':<26}\033[0m  \033[36m{ranges_str}\033[0m")

    print(f"\033[36m{sep}\033[0m")
    print(f"  \033[1m\033[32mListening on  http://localhost:5000\033[0m")
    print(f"\033[36m{sep}\033[0m\n")

    import socket as _socket
    try:
        lan_ip = _socket.gethostbyname(_socket.gethostname())
    except Exception:
        lan_ip = "unknown"
    print(f"  LAN/Tailscale:  http://{lan_ip}:5000")
    print(sep)



    app.run(debug=False, host="0.0.0.0", port=5000, use_reloader=False)
