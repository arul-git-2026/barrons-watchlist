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
"""

from __future__ import annotations

from flask import Flask, jsonify, send_file, request, session, make_response
import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import time
import logging
from datetime import datetime, date, timedelta
from flask_cors import CORS

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

_AUTH_EXEMPT = {"/favicon.ico", "/health", "/api/costs", "/api/crisis-monitor"}

# Cloudflare Access JWT header — presence means CF Access already authenticated the user
_CF_JWT_HEADER = "Cf-Access-Jwt-Assertion"

@app.before_request
def _check_token():
    """Reject requests that don't carry the correct token (when auth is enabled).

    Auth passes if ANY of these are true:
      1. STREETWISE_TOKEN env var is not set       → local dev, no auth
      2. Path is in _AUTH_EXEMPT                   → public endpoints
      3. Cf-Access-Jwt-Assertion header present    → Cloudflare Access already authed
      4. ?token=<value> matches                    → direct URL access
      5. X-Streetwise-Token header matches         → programmatic access
      6. session['auth'] == True                   → browser session already authenticated
    """
    if not _AUTH_TOKEN:
        return  # auth disabled — local dev mode
    if request.path in _AUTH_EXEMPT:
        return
    # Cloudflare Access JWT — if present, CF already verified the user's identity
    if request.headers.get(_CF_JWT_HEADER):
        session["auth"] = True
        return
    # Already authenticated this browser session via cookie
    if session.get("auth"):
        return
    # Check token in URL or header (direct access fallback)
    provided = (
        request.args.get("token", "")
        or request.headers.get("X-Streetwise-Token", "")
    )
    if provided == _AUTH_TOKEN:
        session["auth"] = True   # set session cookie for all subsequent requests
        session.permanent = False
        return
    log.warning(f"  auth: rejected {request.remote_addr} → {request.path}")
    return jsonify({"ok": False, "error": "Unauthorized — missing or invalid token"}), 403


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

def _clean_sector(info: dict) -> str:
    """
    Return a clean sector string from a Yahoo Finance info dict.
    Yahoo returns sector=None for many international stocks and falls back
    to quoteType which contains internal codes like ECNQQUOTE, EQUITY etc.
    We only use quoteType if it looks like a real sector name.
    """
    sector = info.get("sector", "") or ""
    if sector and sector.upper() not in _QUOTE_TYPE_NOISE:
        return sector

    # No real sector — try quoteType but only keep human-readable values
    qt = (info.get("quoteType") or "").upper()
    if qt in _QUOTE_TYPE_NOISE:
        return "N/A"

    # quoteType has a real label (rare but possible)
    return qt.title() if qt else "N/A"


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
            "diff_fmt":   f"{'+' if diff >= 0 else ''}{diff:,.2f}",
            "pct_fmt":    f"{'+' if pct >= 0 else ''}{pct:.2f}%",
            "pct_raw":    round(pct, 3),
            "is_up":      diff >= 0,
            "sector":     _clean_sector(info),
            "rec":        rec,
            "mktcap":     info.get("marketCap"),
            "pe":         info.get("trailingPE") or info.get("forwardPE"),
            "div_yield":  info.get("dividendYield"),   # Yahoo returns e.g. 0.92 meaning 0.92%
            "52w_high":   info.get("fiftyTwoWeekHigh"),
            "52w_low":    info.get("fiftyTwoWeekLow"),
            "fetched_at": datetime.utcnow().isoformat() + "Z",
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
    """widget_v2.html — development/testing version. Production stays at /"""
    resp = send_file("widget_v2.html")
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
{text[:15000]}
"""

    ilog(f"model={gemini_model if use_gemini else claude_model}")
    cost       = 0.0
    model_used = gemini_model if use_gemini else claude_model

    try:
        if use_gemini:
            import urllib.request, urllib.error as _ue
            max_tokens = 16000 if gemini_model == "gemini-2.5-pro" else 8192
            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{gemini_model}:generateContent?key={gemini_key}"
            )
            payload = json.dumps({
                "contents": [{"parts": [{"text": extract_prompt}]}],
                "generationConfig": {
                    "temperature":     0.2,
                    "maxOutputTokens": max_tokens,
                    "responseMimeType": "application/json",
                },
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
            # Summary
            if is_ian_style:
                heading  = f"=== {ep_key} | {source_label} · {label} ==="
                existing = rec.get("sum", "")
                if heading in existing:
                    before = existing.split(heading)[0].rstrip()
                    rec["sum"] = f"{before}\n\n{heading}\n{new_sum}" if before else f"{heading}\n{new_sum}"
                else:
                    rec["sum"] = f"{existing.rstrip()}\n\n{heading}\n{new_sum}" if existing.strip() else f"{heading}\n{new_sum}"
            else:
                rec["sum"] = (rec.get("sum", "") + "\n\n" + new_sum).strip()
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
            summary = f"=== {ep_key} | {source_label} · {label} ===\n{new_sum}" if is_ian_style else new_sum
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
        if not ep_entry.get("title") and title:
            # Parse title from label: "M/D/YYYY — Title" → "Title"
            ep_title = title if title else label.split(" — ", 1)[-1] if " — " in label else label
            _dp = date.split("/") if "/" in date else [date]
            if len(_dp) == 2:
                ep_entry["date"] = f"{year}-{int(_dp[0]):02d}-{int(_dp[1]):02d}"
            else:
                ep_entry["date"] = f"{year}-{date}"
            ep_entry["title"] = ep_title
            save_sources(sources_reg)
            ilog(f"registered episode {ep_key} in sources.json")

        ilog(f"saved {added} new · {updated} updated")
        return jsonify({"ok": True, "added": added, "updated": updated,
                        "tickers": tickers_touched, "cost": f"{cost:.4f}",
                        "model": model_used, "log": srv_log})
    except Exception as e:
        log.error(f"  ingest-page save error: {e}"); srv_log.append(f"save error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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
                {"key": k, "date": v.get("date",""), "title": v.get("title","")}
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

        # Remove src tag if no more episodes from this source
        prefix = ep_key.split(":")[0]
        has_prefix = any(t.startswith(prefix + ":") for t in rec.get("e", []))
        if not has_prefix and prefix in rec.get("src", []):
            rec["src"] = [s for s in rec["src"] if s != prefix]

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

_dcf_cache: dict = {}
_DCF_TTL = 3600  # 1 hour — yfinance fundamentals don't change intraday

@app.route("/api/dcf/<ticker>")
def get_dcf_analysis(ticker):
    """
    Returns DCF inputs + computed intrinsic values (base/bull/bear) for a ticker.
    Pulls real fundamentals from yfinance; narratives from streetwise_data.json.
    """
    sym = ticker.upper()

    # ── Cache check ───────────────────────────────────────────────────────────
    cached = _dcf_cache.get(sym)
    if cached and (time.time() - cached["ts"]) < _DCF_TTL and not request.args.get("force"):
        return jsonify(cached["data"])

    try:
        tk   = yf.Ticker(sym)
        info = tk.info

        # ── Prices & market data ──────────────────────────────────────────────
        price     = float(info.get("currentPrice") or info.get("previousClose") or 0)
        mktcap_r  = info.get("marketCap", 0) or 0
        mktcap    = f"${mktcap_r/1e9:.1f}B" if mktcap_r >= 1e9 else f"${mktcap_r/1e6:.0f}M"

        # ── Free cash flow, EBITDA & balance sheet ────────────────────────────
        # Normalization strategy (sector-aware):
        #   Tech sector  → use Operating Cash Flow (3-yr median).
        #     Rationale: Big Tech CapEx is growth investment (AWS infra, data centres).
        #     yfinance FCF = OCF − total CapEx incl. finance leases → severely understates
        #     earning power (AMZN FY2022 FCF was −$19B; OCF was +$46B).
        #   All others   → use Free Cash Flow (3-yr median) to cap one-time TTM spikes
        #     (e.g. OMC FCF > EBITDA anomaly).
        #   Fallback: TTM freeCashflow from info if cashflow statement unavailable.
        _sector_early = info.get("sector", "")
        # Big Tech tickers that are NOT classified as "Technology" in yfinance
        # (e.g. AMZN = Consumer Cyclical, GOOGL = Communication Services)
        # but whose CapEx is primarily growth investment → use OCF like Tech
        _OCF_TICKERS = {"AMZN","GOOGL","GOOG","META","MSFT","NVDA","AAPL","NFLX"}
        _use_ocf = (_sector_early == "Technology") or (sym in _OCF_TICKERS)

        fcf_r      = info.get("freeCashflow", 0) or 0
        fcf_source = "TTM"
        try:
            cf_df = tk.cashflow
            if cf_df is not None and not cf_df.empty:
                if _use_ocf and "Operating Cash Flow" in cf_df.index:
                    # Tech: OCF is the analyst-grade "normalized" FCF
                    ocf_hist = cf_df.loc["Operating Cash Flow"].dropna()
                    if len(ocf_hist) >= 2:
                        n = min(len(ocf_hist), 3)
                        fcf_r = float(ocf_hist.iloc[:n].median())
                        fcf_source = f"{n}-yr median OCF"
                else:
                    # Non-Tech: derive FCF, median to cap one-time spikes
                    if "Free Cash Flow" in cf_df.index:
                        fcf_hist = cf_df.loc["Free Cash Flow"].dropna()
                    elif "Operating Cash Flow" in cf_df.index and "Capital Expenditure" in cf_df.index:
                        fcf_hist = (cf_df.loc["Operating Cash Flow"] + cf_df.loc["Capital Expenditure"]).dropna()
                    else:
                        fcf_hist = pd.Series(dtype=float)
                    if len(fcf_hist) >= 2:
                        n = min(len(fcf_hist), 3)
                        fcf_r = float(fcf_hist.iloc[:n].median())
                        fcf_source = f"{n}-yr median FCF"
        except Exception:
            pass  # fall back to TTM

        ebitda_r  = info.get("ebitda", 0) or 0
        debt      = info.get("totalDebt",  0) or 0
        cash      = info.get("totalCash",  0) or 0
        shares_r  = info.get("sharesOutstanding", 0) or 0

        # Fix 2: ADR currency conversion — financials are in local currency, price in USD
        # e.g. TSM: financialCurrency=TWD, currency=USD → FCF/EBITDA/debt in TWD, price in USD
        financial_currency = info.get("financialCurrency", "USD") or "USD"
        trading_currency   = info.get("currency", "USD") or "USD"
        fx_rate = 1.0
        currency_converted = False
        # Fallback rates for common ADR pairs if yfinance FX fetch fails
        _FX_FALLBACKS = {
            "TWDUSD=X": 0.031,   # Taiwan Dollar
            "HKDUSD=X": 0.128,   # Hong Kong Dollar
            "BRLUSD=X": 0.200,   # Brazilian Real
            "CNHUSD=X": 0.138,   # Chinese Yuan (offshore)
            "KRWUSD=X": 0.00072, # Korean Won
            "INRUSD=X": 0.012,   # Indian Rupee
            "JPYUSD=X": 0.0067,  # Japanese Yen
        }
        if financial_currency != trading_currency:
            pair = f"{financial_currency}{trading_currency}=X"
            try:
                fx_ticker = yf.Ticker(pair)
                fx_info   = fx_ticker.info
                fx_rate   = float(
                    fx_info.get("regularMarketPrice")
                    or fx_info.get("previousClose")
                    or _FX_FALLBACKS.get(pair, 1.0)
                )
                if fx_rate > 0 and fx_rate != 1.0:
                    currency_converted = True
            except Exception:
                fx_rate = _FX_FALLBACKS.get(pair, 1.0)
                if fx_rate != 1.0:
                    currency_converted = True
        # Apply FX conversion to all financial statement figures (shares stay as-is)
        fcf_r    *= fx_rate
        ebitda_r *= fx_rate
        debt     *= fx_rate
        cash     *= fx_rate

        fcf       = round(fcf_r / 1e9, 2)               # $B (USD)
        ebitda    = round(ebitda_r / 1e9, 2)             # $B (USD)
        net_debt  = round((debt - cash) / 1e9, 1)        # $B (USD)
        shares    = round(shares_r / 1e9, 3)             # billions

        # ── WACC: Hamada re-levering + CAPM (late March 2026) ────────────────────
        # Method: Pure Play approach (Damodaran Jan 2026 unlevered betas)
        #   Step 1 — take sector unlevered beta (business risk only, no leverage noise)
        #   Step 2 — re-lever with this stock's actual D/E → stock-specific levered beta
        #   Step 3 — CAPM: Cost of Equity = RF + β_L × ERP
        # RF = 4.44% (10Y UST March 2026) · ERP = 5.0% · Tax = 21%
        _SECTOR_U_BETA = {
            "Technology":             1.15,
            "Healthcare":             0.82,
            "Financial Services":     0.45,
            "Energy":                 0.58,
            "Consumer Cyclical":      0.88,
            "Consumer Defensive":     0.65,
            "Basic Materials":        0.96,
            "Industrials":            0.89,
            "Real Estate":            0.40,
            "Communication Services": 0.85,
            "Utilities":              0.35,
        }
        TAX            = 0.21
        rf             = 4.44
        erp            = 5.0
        _sec_tmp       = info.get("sector", "")
        raw_beta       = float(info.get("beta", 1.0) or 1.0)
        equity_mv      = price * shares_r
        de_ratio       = (debt / equity_mv) if equity_mv > 0 else 0.25

        if _sec_tmp in _SECTOR_U_BETA:
            # Hamada: β_L = β_U × (1 + (1-T) × D/E)
            u_beta = _SECTOR_U_BETA[_sec_tmp]
            beta   = round(u_beta * (1 + (1 - TAX) * de_ratio), 2)
            beta   = max(0.20, min(3.0, beta))          # hard floor/cap for extreme leverage
        else:
            # Fallback: use raw yfinance beta, bounded to reasonable range
            u_beta = None
            beta   = round(max(0.5, min(2.5, raw_beta)), 2)

        cost_equity    = rf + beta * erp
        total_capital  = equity_mv + debt
        dw             = (debt / total_capital) if total_capital > 0 else 0.25
        ew             = 1.0 - dw
        cost_debt_at   = 5.0 * (1 - TAX)               # after-tax cost of debt
        wacc_raw       = ew * cost_equity + dw * cost_debt_at

        # ── Growth rate estimation from analyst estimates ─────────────────────
        eg    = float(info.get("earningsGrowth",  0) or 0) * 100
        rg    = float(info.get("revenueGrowth",   0) or 0) * 100
        # For Big Tech, earnings growth is volatile (quarterly EPS noise).
        # Use max(earningsGrowth, revenueGrowth) — revenue is more stable — with an 8% floor.
        if sym in _OCF_TICKERS:
            g_raw = max(eg, rg)
            g_floor = 8.0
        else:
            g_raw = eg if abs(eg) > 0.5 else rg
            g_floor = -15.0
        g1_base = round(max(g_floor, min(25.0, g_raw if g_raw != 0 else 3.0)), 1)
        g1_bull = round(g1_base + 5.0, 1)
        g1_bear = round(g1_base - 5.0, 1)
        g2_base = round(max(0.0, g1_base * 0.55), 1)   # phase 2 mean-reverts
        g2_bull = round(max(0.0, g1_bull * 0.55), 1)
        g2_bear = round(g1_bear * 0.55, 1)

        # ── Valuation multiples (live market data) ───────────────────────────────
        pe_v         = info.get("forwardPE") or info.get("trailingPE")
        pe_str       = f"Fwd P/E: {pe_v:.1f}×" if pe_v else "P/E: N/A"
        dy           = info.get("dividendYield", 0) or 0
        div_str      = f"{dy*100:.1f}%" if dy else "0%"
        ev_r         = info.get("enterpriseValue", 0) or 0
        evfcf_v      = round(ev_r / fcf_r, 1)    if fcf_r    > 0 else None
        ev_ebitda_v  = round(ev_r / ebitda_r, 1) if ebitda_r > 0 else None
        evfcf_s      = f"{evfcf_v}×"    if evfcf_v    else "N/A"
        ev_ebitda_s  = f"{ev_ebitda_v}×" if ev_ebitda_v else "N/A"
        gm           = info.get("grossMargins", 0) or 0
        moat         = "Wide" if (mktcap_r > 50e9 and gm > 0.40) else "Narrow"

        # ── Terminal value method — full GICS sector table (March 2026) ──────────
        # Source: user-provided sector calibration + Damodaran 2026 data
        # RF = 3.96% (10Y UST), ERP = 4.4%
        # Each sector: (wacc_lo, wacc_hi, tv_method, tv_horizon, tgr, mult_base, mult_bull, mult_bear)
        sector   = info.get("sector", "")
        industry = info.get("industry", "")

        # Ticker-level overrides for mega-cap Big Tech
        _BIGTECH_TICKERS = {"AMZN","GOOGL","GOOG","META","MSFT","NVDA"}
        _FINTECH_TICKERS = {"PYPL","SQ","GPN","FIS"}
        _FINTECH_INDS    = {"Payment","Credit Services","Capital Markets","Insurance"}

        ev_ebitda_live = ev_ebitda_v or 9.0

        if sym in _BIGTECH_TICKERS or (
            sector == "Technology" and mktcap_r > 500e9
        ):
            # Big Tech — perpetuity, WACC 9.0–10.5%, long reinvestment runway
            wacc           = round(max(9.0, min(10.5, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 3.0 if sym == "AMZN" else 2.5
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%"

        elif sector == "Technology":
            # Mid-cap Tech — perpetuity, slightly higher WACC
            wacc           = round(max(9.0, min(10.5, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 2.5
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%"

        elif sector == "Healthcare":
            # Pharma/Healthcare — 5yr EBITDA exit; patent cliffs limit visibility
            wacc           = round(max(7.5, min(8.5, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 5
            tgr            = None
            exit_mult_base = 13.0 if ev_ebitda_live >= 12 else 9.5
            exit_mult_bull = exit_mult_base + 2.0
            exit_mult_bear = max(exit_mult_base - 2.0, 5.0)
            tv_label       = f"{exit_mult_base}× EV/EBITDA exit yr 5  ·  WACC {wacc}%"

        elif sector == "Financial Services" or sym in _FINTECH_TICKERS or any(k in industry for k in _FINTECH_INDS):
            # Fintech/Payments — FCF multiple, high regulatory WACC
            wacc           = round(max(9.5, min(11.0, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 12.0
            exit_mult_bull = 17.0
            exit_mult_bear =  9.0
            tv_label       = f"12× FCF exit yr 10  ·  WACC {wacc}%  (fintech)"

        elif sector == "Energy":
            # Energy — commodity-linked, high WACC, EBITDA exit
            wacc           = round(max(10.0, min(12.0, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 6.0
            exit_mult_bull = 7.0
            exit_mult_bear = 5.0
            tv_label       = f"6× EV/EBITDA exit yr 10  ·  WACC {wacc}%  (energy)"

        elif sector == "Real Estate":
            # REITs — use NOI cap-rate; approximated as exit multiple on EBITDA
            wacc           = round(max(7.0, min(8.0, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 15.0  # ~6.5% cap rate ≈ 15× NOI
            exit_mult_bull = 18.0
            exit_mult_bear = 13.0
            tv_label       = f"~6.5% cap rate (15× NOI)  ·  WACC {wacc}%  (REIT)"

        elif sector == "Industrials":
            # Cyclical industrials — GDP-linked, EBITDA exit
            wacc           = round(max(8.5, min(9.5, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 13.0
            exit_mult_bull = 15.0
            exit_mult_bear = 10.0
            tv_label       = f"13× EV/EBITDA exit yr 10  ·  WACC {wacc}%  (industrials)"

        elif sector == "Consumer Cyclical":
            # Consumer discretionary — spend-sensitive
            wacc           = round(max(9.0, min(11.0, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 12.0
            exit_mult_bull = 14.0
            exit_mult_bear =  9.0
            tv_label       = f"12× EV/EBITDA exit yr 10  ·  WACC {wacc}%  (cons. cyclical)"

        elif sector == "Consumer Defensive":
            # Staples — low vol, perpetuity appropriate
            wacc           = round(max(7.0, min(8.0, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 2.0
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%  (cons. defensive)"

        elif sector == "Basic Materials":
            # Asset-heavy, commodity-linked, highest WACC
            wacc           = round(max(10.5, min(12.5, wacc_raw)), 1)
            tv_method      = "exit_multiple"
            tv_horizon     = 10
            tgr            = None
            exit_mult_base = 7.0
            exit_mult_bull = 9.0
            exit_mult_bear = 5.0
            tv_label       = f"7× EV/EBITDA exit yr 10  ·  WACC {wacc}%  (materials)"

        elif sector == "Communication Services":
            # High capex, utility-like — perpetuity
            wacc           = round(max(8.5, min(10.0, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 2.0
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%  (comm. services)"

        elif sector == "Utilities":
            # Rate-sensitive, dividend-driven — low WACC, perpetuity
            wacc           = round(max(6.5, min(8.0, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 1.5
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%  (utilities)"

        else:
            # Fallback
            wacc           = round(max(7.0, min(12.0, wacc_raw)), 1)
            tv_method      = "perpetuity"
            tv_horizon     = 10
            tgr            = 2.5
            exit_mult_base = exit_mult_bull = exit_mult_bear = None
            tv_label       = f"Perpetuity @ {tgr}% TGR  ·  WACC {wacc}%"

        # ── DCF helpers ───────────────────────────────────────────────────────
        def _project(fcf_b, ebitda_b, g1, g2, horizon):
            """Project FCFs and EBITDA over `horizon` years (2-phase growth)."""
            h1   = min(5, horizon)
            h2   = max(0, horizon - h1)
            fcfs = []
            f, e = fcf_b, ebitda_b
            for _ in range(h1):
                f *= (1 + g1 / 100.0); e *= (1 + g1 / 100.0); fcfs.append(f)
            for _ in range(h2):
                f *= (1 + g2 / 100.0); e *= (1 + g2 / 100.0); fcfs.append(f)
            return fcfs, e

        def _pv(fcfs, r):
            return sum(f / (1 + r) ** (y + 1) for y, f in enumerate(fcfs))

        def _iv_perpetuity(fcf_b, ebitda_b, g1, g2, tg, w, nd, sh, horizon):
            if sh <= 0 or fcf_b <= 0 or w / 100.0 <= tg / 100.0:
                return None
            r            = w / 100.0
            fcfs, _      = _project(fcf_b, ebitda_b, g1, g2, horizon)
            pv_fcfs      = _pv(fcfs, r)
            terminal_fcf = fcfs[-1]
            tv           = terminal_fcf * (1 + tg / 100.0) / (r - tg / 100.0)
            ev           = pv_fcfs + tv / (1 + r) ** horizon - nd
            return max(round(ev / sh, 0), 0)

        def _iv_exit_multiple(fcf_b, ebitda_b, g1, g2, mult, w, nd, sh, horizon):
            if sh <= 0 or fcf_b <= 0 or ebitda_b <= 0:
                return None
            r              = w / 100.0
            fcfs, ebitda_n = _project(fcf_b, ebitda_b, g1, g2, horizon)
            pv_fcfs        = _pv(fcfs, r)
            tv             = mult * ebitda_n           # exit mult × EBITDA at horizon
            ev             = pv_fcfs + tv / (1 + r) ** horizon - nd
            return max(round(ev / sh, 0), 0)

        # ── Compute base / bull / bear intrinsic values ───────────────────────
        _args = (fcf, ebitda, net_debt, shares, tv_horizon)
        if tv_method == "perpetuity":
            iv_base = _iv_perpetuity(*_args[:2], g1_base, g2_base, tgr, wacc,        *_args[2:])
            iv_bull = _iv_perpetuity(*_args[:2], g1_bull, g2_bull, tgr, wacc * 0.90, *_args[2:])
            iv_bear = _iv_perpetuity(*_args[:2], g1_bear, g2_bear, tgr, wacc * 1.10, *_args[2:])
        else:
            iv_base = _iv_exit_multiple(*_args[:2], g1_base, g2_base, exit_mult_base, wacc,        *_args[2:])
            iv_bull = _iv_exit_multiple(*_args[:2], g1_bull, g2_bull, exit_mult_bull, wacc * 0.90, *_args[2:])
            iv_bear = _iv_exit_multiple(*_args[:2], g1_bear, g2_bear, exit_mult_bear, wacc * 1.10, *_args[2:])

        mos = round(((iv_base - price) / iv_base * 100)) if iv_base else None
        rating = ("buy" if (mos or 0) > 25 else
                  "hold" if (mos or 0) > 0 else "watch")

        # ── Risks: data-driven from yfinance signals ───────────────────────────
        risks = []
        if fcf < 0:
            risks.append({"level": "high",
                          "text": f"Negative FCF (${fcf:.2f}B {fcf_source}) — cash burn risk"})
        if dw > 0.5:
            risks.append({"level": "high",
                          "text": f"High leverage: debt {dw*100:.0f}% of capital; net debt ${net_debt:.1f}B"})
        if beta > 1.3:
            risks.append({"level": "mid",
                          "text": f"Elevated beta ({beta:.2f}) — amplifies market drawdowns"})
        if dy > 0.06:
            risks.append({"level": "mid",
                          "text": f"High dividend yield ({div_str}) — sustainability risk if FCF declines"})
        if pe_v and pe_v > 25:
            risks.append({"level": "mid",
                          "text": f"P/E {pe_v:.1f}× — premium valuation leaves little margin for error"})
        sector_s = info.get("sector", "")
        if sector_s:
            risks.append({"level": "low",
                          "text": f"Sector exposure: {sector_s} — macro/regulatory cycle risk"})
        if not risks:
            risks.append({"level": "low",
                          "text": "No significant risk flags detected — verify with latest 10-K"})

        # ── Catalysts: pull narrative from streetwise_data.json ───────────────
        db       = load_data_raw()
        rec_db   = next((r for r in db if r.get("t","").upper() == sym), {})
        cats     = []
        if rec_db.get("bull"):
            cats.append({"tag": "Bull", "text": rec_db["bull"]})
        if rec_db.get("base"):
            cats.append({"tag": "Base", "text": rec_db["base"]})
        if rec_db.get("bear"):
            cats.append({"tag": "Bear", "text": rec_db["bear"]})
        if not cats:
            cats.append({"tag": "N/A",
                         "text": "No thesis data found. Add research notes via the sidebar."})

        result = {
            "ok":            True,
            "ticker":        sym,
            "name":          info.get("longName", sym),
            "price":         round(price, 2),
            "mktcap":        mktcap,
            "fcf":           fcf,
            "ebitda":        ebitda,
            "g1_base":       g1_base,  "g1_bull": g1_bull,  "g1_bear": g1_bear,
            "g2_base":       g2_base,  "g2_bull": g2_bull,  "g2_bear": g2_bear,
            "tgr":           tgr,
            "wacc_default":  wacc,
            "wacc_rf":       rf,       "wacc_erp": erp,
            "net_debt":      net_debt,
            "shares":        shares,
            "iv_base":       iv_base,  "iv_bull": iv_bull,  "iv_bear": iv_bear,
            "tv_method":     tv_method,
            "tv_horizon":    tv_horizon,
            "tv_label":      tv_label,
            "exit_mult":     exit_mult_base,
            "div":           div_str,
            "pe":            pe_str,
            "evfcf":         evfcf_s,
            "ev_ebitda":     ev_ebitda_s,
            "sector":        sector,
            "moat":          moat,
            "rating":        rating,
            "mos":           mos,
            "beta":          beta,
            "beta_raw":      round(raw_beta, 2),
            "beta_u":        round(u_beta, 2) if u_beta else None,
            "de_ratio":      round(de_ratio, 2),
            "gross_margins":      round(gm * 100, 1),
            "fcf_source":         fcf_source,
            "financial_currency": financial_currency,
            "currency_converted": currency_converted,
            "risks":              risks,
            "cats":               cats,
        }
        _dcf_cache[sym] = {"ts": time.time(), "data": result}
        log.info(f"DCF {sym}: price={price} fcf={fcf}B iv_base={iv_base} wacc={wacc}%")
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


if __name__ == "__main__":
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
    print(f"[36m{sep}[0m\n")

    app.run(debug=False, host="0.0.0.0", port=5000, use_reloader=False)
