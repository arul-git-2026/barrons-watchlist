"""
fix_quote_types.py  —  backfill quoteType for all tickers in the DB

Reads every ticker from streetwise_data.json, fetches quoteType from
Yahoo Finance, then updates:
  "y"       → asset type  (Stock / ETF / Mutual Fund / Index / Crypto …)
  "sector"  → "ETF" / "Mutual Fund" for non-equity types (was "N/A")
  "industry"→ Yahoo industry (if empty and equity)

Run on the server:
    cd /opt/streetwise
    python3 fix_quote_types.py
    sudo systemctl restart streetwise
"""

import json
import os
import time

import yfinance as yf

# ── Config ────────────────────────────────────────────────────────────────────

DATA_FILE   = "streetwise_data.json"
DELAY_SEC   = 0.3      # polite pause between Yahoo calls
YAHOO_MAP   = {        # tickers whose Yahoo symbol differs from stored key
    "WALMEX": "WALMEX.MX",
    "000660": "000660.KS",
    "SSNLF":  "005930.KS",
}

# ── Mappings (must match server.py) ──────────────────────────────────────────

QT_TO_TYPE = {
    "EQUITY":         "Stock",
    "ETF":            "ETF",
    "MUTUALFUND":     "Mutual Fund",
    "INDEX":          "Index",
    "CURRENCY":       "Currency",
    "CRYPTOCURRENCY": "Crypto",
    "FUTURE":         "Future",
    "OPTION":         "Option",
}

QT_TO_SECTOR = {
    "ETF":            "ETF",
    "MUTUALFUND":     "Mutual Fund",
    "INDEX":          "Index",
    "CURRENCY":       "Currency",
    "CRYPTOCURRENCY": "Crypto",
}

_SECTOR_NOISE = {
    "ECNQQUOTE", "EQUITY", "ETF", "MUTUALFUND", "INDEX",
    "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION",
}


def clean_sector(info):
    qt = (info.get("quoteType") or "").upper()
    if qt in QT_TO_SECTOR:
        return QT_TO_SECTOR[qt]
    sector = info.get("sector", "") or ""
    if sector and sector.upper() not in _SECTOR_NOISE:
        return sector
    return "N/A"


def load_db():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_db(records):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


def main():
    all_records = load_db()
    tickers = [r for r in all_records if r.get("t") and not r.get("__meta__")]

    print(f"\nProcessing {len(tickers)} tickers …\n")

    changed  = []
    failed   = []
    unchanged = []

    # Track counts by type for summary
    type_counts = {}

    for rec in tickers:
        sym     = rec["t"]
        y_sym   = YAHOO_MAP.get(sym, sym)

        print(f"  {sym:<16}", end=" ", flush=True)
        try:
            info = yf.Ticker(y_sym).info
            qt   = (info.get("quoteType") or "").upper()

            new_type   = QT_TO_TYPE.get(qt, "Stock")
            new_sector = clean_sector(info)
            new_ind    = info.get("industry", "") or ""

            old_type   = rec.get("y", "Stock")
            old_sector = rec.get("sector", "")
            old_ind    = rec.get("industry", "")

            updates = {}
            if new_type != old_type:
                updates["y"] = (old_type, new_type)
                rec["y"] = new_type
            if new_sector and new_sector != old_sector:
                updates["sector"] = (old_sector, new_sector)
                rec["sector"] = new_sector
            if new_ind and not old_ind:   # only fill if currently empty
                updates["industry"] = ("", new_ind)
                rec["industry"] = new_ind

            type_counts[new_type] = type_counts.get(new_type, 0) + 1

            if updates:
                parts = []
                for field, (old, new) in updates.items():
                    parts.append(f"{field}: '{old}' → '{new}'")
                print(f"UPDATED  {qt:<14}  {' | '.join(parts)}")
                changed.append(sym)
            else:
                print(f"ok       {qt:<14}  {new_type} / {new_sector}")
                unchanged.append(sym)

        except Exception as e:
            print(f"FAILED   {e}")
            failed.append((sym, str(e)))

        time.sleep(DELAY_SEC)

    # Save
    if changed:
        save_db(all_records)
        print(f"\n✓ Saved {len(changed)} changes to {DATA_FILE}")
    else:
        print(f"\nNo changes needed.")

    # Summary
    print(f"\n{'─'*55}")
    print(f"  Updated  : {len(changed)}")
    print(f"  Unchanged: {len(unchanged)}")
    print(f"  Failed   : {len(failed)}")
    if failed:
        for sym, err in failed:
            print(f"    {sym}: {err}")
    print(f"\n  Asset type breakdown:")
    for t, n in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t:<20} {n}")
    print(f"{'─'*55}\n")

    if changed:
        print("Restart the service:\n  sudo systemctl restart streetwise\n")


if __name__ == "__main__":
    main()
