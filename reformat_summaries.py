"""
reformat_summaries.py
─────────────────────────────────────────────────────────────────────
Rewrites all plain-paragraph summaries in streetwise_data.json into
structured bullet points using Claude Haiku.

Format target (inside each === section):
  • **Why mentioned:** ...
  • **Analyst thesis:** ...
  • **Valuation:** ...
  • **Key metrics:** ...
  • **Catalyst:** ...
  • **Risk:** ...

Skips summaries that already use bullet format (start with •).
Saves after every ticker so if it crashes / runs out of credits
you can re-run and it picks up where it left off.

Usage
-----
  python reformat_summaries.py              # dry run
  python reformat_summaries.py --apply      # rewrite all, save to DB
  python reformat_summaries.py --apply --backup   # save .bak first
  python reformat_summaries.py --ticker SCHD --apply  # single ticker
  python reformat_summaries.py --apply --limit 20    # first 20 tickers
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import anthropic

DATA_FILE = "streetwise_data.json"
MODEL     = "claude-haiku-4-5-20251001"

REFORMAT_PROMPT = """Rewrite the following investment research summary as 6-8 structured bullet points.

Rules:
- Each bullet on its own line, starting with bullet character: •
- Bold label before colon using double asterisks: • **Label:** explanation
- One concise sentence per bullet
- Labels to use (pick most relevant): Why mentioned, Analyst thesis, \
Valuation, Key metrics, Price target, EPS forecast, Catalyst, Risk, \
Macro tailwind, Sector context, Status
- Preserve ALL factual content — numbers, percentages, names, price targets
- Do NOT add information not in the original
- Output ONLY the bullet points, nothing else — no intro, no header, no trailing text

ORIGINAL SUMMARY:
{summary}
"""


def needs_reformat(text: str) -> bool:
    if not text or not text.strip():
        return False
    first_line = text.strip().split("\n")[0].strip()
    return not (first_line.startswith("•") or first_line.startswith("**"))


def extract_sections(summary: str):
    HEADING = re.compile(r"(===\s*[^=]+\s*===)", re.MULTILINE)
    parts    = HEADING.split(summary)
    sections = []
    i = 0
    while i < len(parts):
        if HEADING.match(parts[i]):
            heading = parts[i]
            body    = parts[i+1].strip() if i+1 < len(parts) else ""
            sections.append((heading, body))
            i += 2
        else:
            if parts[i].strip():
                sections.append((None, parts[i].strip()))
            i += 1
    return sections


class OutOfCredits(Exception):
    pass


def reformat_body(client, body: str) -> str:
    try:
        msg = client.messages.create(
            model      = MODEL,
            max_tokens = 600,
            messages   = [{"role": "user",
                           "content": REFORMAT_PROMPT.format(summary=body)}],
        )
        return msg.content[0].text.strip()
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise OutOfCredits() from e
        raise


def reformat_summary(client, summary: str, dry_run: bool = False,
                     ticker: str = "") -> tuple:
    sections    = extract_sections(summary)
    reformatted = 0
    new_parts   = []

    for heading, body in sections:
        if not needs_reformat(body):
            new_parts.append((heading, body))
            continue
        reformatted += 1
        if dry_run:
            preview = body[:100].replace("\n", " ")
            print(f"    [{ticker}] Would reformat: {preview}…")
            new_parts.append((heading, body))
        else:
            new_body = reformat_body(client, body)
            new_parts.append((heading, new_body))
            time.sleep(0.3)

    result = ""
    for heading, body in new_parts:
        if heading:
            result += heading + "\n" + body
        else:
            result += body
        result += "\n\n"
    return result.strip(), reformatted


def main():
    parser = argparse.ArgumentParser(description="Reformat DB summaries to bullet points")
    parser.add_argument("--apply",  action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--ticker", default="")
    parser.add_argument("--limit",  type=int, default=0)
    args    = parser.parse_args()
    dry_run = not args.apply

    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  Reformat summaries  ({'DRY RUN' if dry_run else 'LIVE'})")
    print(f"{sep}\n")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip().strip('"').strip("'")
    if not api_key:
        print("  ERROR: ANTHROPIC_API_KEY not set"); sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    if not Path(DATA_FILE).exists():
        print(f"  ERROR: {DATA_FILE} not found"); sys.exit(1)

    with open(DATA_FILE, encoding="utf-8", errors="replace") as f:
        db = json.load(f)
    print(f"  Loaded {len(db)} records")

    if args.backup and not dry_run:
        shutil.copy(DATA_FILE, DATA_FILE + ".bak")
        print(f"  Backup saved")

    # Checkpoint — resume from where we left off if interrupted
    CHECKPOINT = DATA_FILE + ".checkpoint"
    done: set  = set()
    if not dry_run and Path(CHECKPOINT).exists():
        try:
            with open(CHECKPOINT, encoding="utf-8") as f:
                done = set(json.load(f))
            print(f"  Resuming — {len(done)} tickers already done, skipping them")
            print(f"  (delete {CHECKPOINT} to start from scratch)")
        except Exception:
            done = set()

    def atomic_write(path, data):
        """Write ticker JSON atomically, preserving __sources__ meta-record."""
        existing = []
        if os.path.exists(path):
            try:
                existing = json.load(open(path, encoding="utf-8", errors="replace"))
            except Exception:
                pass
        meta = [r for r in existing if r.get("__meta__")]
        tickers_only = [r for r in data if not r.get("__meta__")]
        final = meta + tickers_only
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(final, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def save_progress():
        tmp = CHECKPOINT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(done), f)
        os.replace(tmp, CHECKPOINT)

    total_tickers = total_sections = 0
    HAIKU_RATE    = (0.80 + 4.00) / 2 / 1_000_000
    token_cost    = 0.0
    target        = args.ticker.upper()
    limit         = args.limit

    try:
        for rec in db:
            t = rec.get("t", "").upper()
            if target and t != target:
                continue
            if t in done:
                continue
            if not rec.get("sum"):
                if not dry_run:
                    done.add(t); save_progress()
                continue

            new_sum, n = reformat_summary(client, rec["sum"],
                                          dry_run=dry_run, ticker=t)

            if not dry_run:
                done.add(t)
                if n > 0:
                    rec["sum"] = new_sum
                    total_tickers  += 1
                    total_sections += n
                    token_cost     += n * 500 * HAIKU_RATE
                    # Save DB atomically — write to .tmp then rename
                    # so a crash mid-write never corrupts the live file
                    atomic_write(DATA_FILE, db)
                    save_progress()
                    print(f"  OK {t:<8}  {n} section(s)  ~${token_cost:.4f} so far")
                else:
                    save_progress()

            if limit and total_tickers >= limit:
                print(f"  Reached --limit {limit}"); break

    except OutOfCredits:
        print("\n  ✗ Out of Anthropic credits.")
        print("  Top up at: console.anthropic.com → Plans & Billing")
        print("  Re-run this script after topping up — it will resume automatically.")
    except OutOfCredits:
        print("\n  ✗ Out of Anthropic credits.")
        print("  Top up at: console.anthropic.com → Plans & Billing")
        print("  Re-run after topping up — it resumes automatically.")
    except KeyboardInterrupt:
        print("\n  Interrupted — progress saved, re-run to continue")
    except Exception as e:
        print(f"\n  Error: {e}")
        print("  Progress saved — re-run to continue")

    # Clear checkpoint when fully done
    if not dry_run and not limit and Path(CHECKPOINT).exists():
        if not target:   # only clear if we processed everything
            os.remove(CHECKPOINT)
            print("  Checkpoint cleared")

    print(f"\n  Tickers processed  : {total_tickers}")
    print(f"  Sections rewritten : {total_sections}")
    print(f"  Estimated cost     : ${token_cost:.4f}")
    print(f"\n{sep}")
    print("  Dry run complete" if dry_run else f"  Done")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()
