"""
fetch_yields.py  —  one-time bulk yield fetch
─────────────────────────────────────────────
Fetches dividendYield from Yahoo Finance for every ticker in
streetwise_data.json and saves the result back to the DB.

Run once:
    python fetch_yields.py

After that, every manual Refresh in the dashboard keeps yields
up to date automatically.
"""

import json, os, shutil, time
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("yfinance not installed — run: pip install yfinance")

DATA_FILE = "streetwise_data.json"

# Same symbol overrides used by server.py
YAHOO_MAP = {
    "BRK.B": "BRK-B", "BRK/B": "BRK-B",
}

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="", help="Single ticker to update (omit for all)")
    args = parser.parse_args()
    target = args.ticker.upper().strip()

    sep = "─" * 56
    print(f"\n{sep}")
    if target:
        print(f"  fetch_yields.py  —  {target}")
    else:
        print("  fetch_yields.py  —  all tickers")
    print(f"{sep}\n")

    if not Path(DATA_FILE).exists():
        raise SystemExit(f"  {DATA_FILE} not found")

    with open(DATA_FILE, encoding="utf-8") as f:
        db = json.load(f)
    print(f"  Loaded {len(db)} records\n")

    updated = skipped = errors = 0

    for i, rec in enumerate(db, 1):
        ticker = rec.get("t", "").upper()
        if not ticker:
            continue
        if target and ticker != target:
            continue

        y_sym = YAHOO_MAP.get(ticker, ticker)
        try:
            info  = yf.Ticker(y_sym).info
            raw   = info.get("dividendYield")

            if raw and raw > 0:
                rec["div_yield"] = round(float(raw), 6)
                print(f"  {i:>3}. {ticker:<8}  {raw:.2f}%")
                updated += 1
            else:
                rec["div_yield"] = None   # no dividend — store None explicitly
                skipped += 1

        except Exception as e:
            print(f"  {i:>3}. {ticker:<8}  ERROR: {e}")
            errors += 1

        # Gentle rate limiting — Yahoo blocks fast loops
        time.sleep(0.4)

    # Atomic write — preserve __sources__ meta-record
    import json as _json2
    existing = _json2.load(open(DATA_FILE, encoding="utf-8")) if os.path.exists(DATA_FILE) else []
    meta = [r for r in existing if r.get("__meta__")]
    tickers_only = [r for r in db if not r.get("__meta__")]
    final = meta + tickers_only
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

    print(f"\n{sep}")
    print(f"  Done.")
    print(f"  Yields found    : {updated}")
    print(f"  No dividend     : {skipped}")
    print(f"  Errors          : {errors}")
    print(f"  Saved → {DATA_FILE}")
    print(f"{sep}\n")

    # Print top yields
    paying = [(r["t"], r["div_yield"]) for r in db if r.get("div_yield")]
    paying.sort(key=lambda x: -x[1])
    if paying:
        print("  Top yields:")
        for t, y in paying[:10]:
            print(f"    {t:<10} {y:.2f}%")
        print()

if __name__ == "__main__":
    main()
