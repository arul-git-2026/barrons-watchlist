"""
add_tickers.py  —  bulk-add tickers to the Streetwise DB

Usage:
    1. Edit the TICKERS list below (just the symbols).
    2. Copy this file to the server and run:
           cd /opt/streetwise
           python3 add_tickers.py
    3. The script will:
         - fetch full info from Yahoo Finance (name, sector, type, currency, price…)
         - save to streetwise_data.json  (skips existing tickers)
         - seed 1-year of daily closes into price_history.db
    4. Restart the service:
           sudo systemctl restart streetwise
"""

import json
import os
import sqlite3
from datetime import datetime, date, timedelta

import yfinance as yf

# ── Config ────────────────────────────────────────────────────────────────────

DATA_FILE = "streetwise_data.json"
DB_FILE   = "price_history.db"
SOURCE    = "personal"

# ── YOUR TICKERS HERE ─────────────────────────────────────────────────────────

TICKERS = [
    "AAPL",
    "MSFT",
    "005930.KS",   # Samsung — use the Yahoo symbol directly
    # add as many as you like...
]

# ─────────────────────────────────────────────────────────────────────────────


def load_db():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_db(records):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def clean_sector(info):
    NOISE = {"ECNQQUOTE", "EQUITY", "ETF", "MUTUALFUND", "INDEX",
             "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION"}
    sector = info.get("sector") or ""
    if sector and sector.upper() not in NOISE:
        return sector
    qt = (info.get("quoteType") or "").upper()
    return "N/A" if qt in NOISE else qt.title() if qt else "N/A"


def fetch_yahoo(symbol):
    """Returns (info_dict, history_df) from Yahoo Finance."""
    tk   = yf.Ticker(symbol)
    info = tk.info
    hist = tk.history(period="1y")
    return info, hist


def seed_price_history(db_path, symbol, hist_df):
    """Upsert 1-year of daily closes into price_history.db."""
    if hist_df.empty:
        return 0
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            ticker TEXT NOT NULL,
            date   TEXT NOT NULL,
            close  REAL NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    rows = [
        (symbol, str(idx.date()), float(row["Close"]))
        for idx, row in hist_df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.close()
    return len(rows)


def build_record(symbol, info, episode_key):
    cur   = info.get("currency", "USD")
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    prev  = info.get("regularMarketPreviousClose") or price
    diff  = price - prev
    pct   = (diff / prev * 100) if prev else 0

    # Detect type
    qt     = (info.get("quoteType") or "").upper()
    ty     = "ETF" if qt == "ETF" else "Stock"

    # Analyst rec
    rec_r = (info.get("recommendationKey") or "n/a").lower()
    rec   = rec_r.replace("_", " ").title() if rec_r != "n/a" else "N/A"

    name  = info.get("longName") or info.get("shortName") or symbol

    return {
        "t":          symbol,
        "n":          name,
        "y":          ty,
        "s":          "flat",
        "sector":     clean_sector(info),
        "currency":   cur,
        "price_fmt":  f"{price:,.2f}",
        "price_raw":  price,
        "diff_raw":   round(diff, 3),
        "diff_fmt":   f"{'+' if diff >= 0 else ''}{diff:,.2f}",
        "pct_fmt":    f"{'+' if pct >= 0 else ''}{pct:.2f}%",
        "pct_raw":    round(pct, 3),
        "is_up":      diff >= 0,
        "rec":        rec,
        "mktcap":     info.get("marketCap"),
        "pe":         info.get("trailingPE") or info.get("forwardPE"),
        "div_yield":  info.get("dividendYield"),
        "52w_high":   info.get("fiftyTwoWeekHigh"),
        "52w_low":    info.get("fiftyTwoWeekLow"),
        "e":          [episode_key],
        "src":        [SOURCE],
        "sum":        "",
        "base":       "",
        "bear":       "",
        "bull":       "",
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }


def main():
    today       = date.today()
    episode_key = f"per:{today.year}/{today.month}/{today.day}"

    print(f"\nSource   : {SOURCE}")
    print(f"Episode  : {episode_key}")
    print(f"Tickers  : {TICKERS}\n")

    # Load existing DB
    all_records = load_db()
    existing    = {r["t"].upper() for r in all_records if r.get("t") and not r.get("__meta__")}

    added   = []
    skipped = []
    failed  = []

    for sym in TICKERS:
        sym = sym.strip().upper()
        if not sym:
            continue

        if sym in existing:
            print(f"  SKIP  {sym}  (already in DB)")
            skipped.append(sym)
            continue

        print(f"  FETCH {sym} ...", end=" ", flush=True)
        try:
            info, hist = fetch_yahoo(sym)

            # Require at least a name to consider it valid
            name = info.get("longName") or info.get("shortName") or ""
            if not name and hist.empty:
                raise ValueError("no data returned from Yahoo — check the symbol")

            record = build_record(sym, info, episode_key)
            all_records.append(record)
            existing.add(sym)

            rows = seed_price_history(DB_FILE, sym, hist)

            print(f"OK  — {record['n']}  |  {record['sector']}  |  "
                  f"{record['currency']} {record['price_raw']:,.2f}  "
                  f"({rows} price rows)")
            added.append(sym)

        except Exception as e:
            print(f"FAILED  — {e}")
            failed.append((sym, str(e)))

    # Save
    if added:
        save_db(all_records)

    # Summary
    print(f"\n{'─'*55}")
    print(f"  Added   : {len(added)}  {added}")
    print(f"  Skipped : {len(skipped)}  {skipped}")
    print(f"  Failed  : {len(failed)}  {[s for s,_ in failed]}")
    if failed:
        for sym, err in failed:
            print(f"    {sym}: {err}")
    print(f"{'─'*55}\n")

    if added:
        print("✓ Done. Restart the service:\n  sudo systemctl restart streetwise\n")
    else:
        print("Nothing new was added.\n")


if __name__ == "__main__":
    main()
