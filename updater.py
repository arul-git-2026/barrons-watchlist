"""
updater.py
──────────────────────────────────────────────────────────────────────────────
Single script for ingesting any research source into the Streetwise dashboard.

Usage
-----
  # Barron's Streetwise podcast episode
  python updater.py --source streetwise --date "3/13" --year 2026 \
    --file transcript_mar13.txt --title "Anything-But-AI Rally"

  # Ian Salisbury article
  python updater.py --source ian --date "3/6" --year 2026 \
    --file article_mar06.txt --title "Small-Cap Revival"

  # Any other source (analyst note, blog post, etc.)
  python updater.py --source "Goldman Sachs" --date "3/10" --year 2026 \
    --file goldman_note.txt --title "Tariff Impact on Industrials"

  # One-time cleanup: normalize legacy M/D keys to YYYY/M/D
  python updater.py --cleanup --year 2026

Arguments
---------
  --source   Who wrote / produced this content.
             Built-in values: streetwise, ian
             Any other string is accepted as a custom source label.
             (default: streetwise)
  --date     M/D date, e.g. "3/13"          (required unless --cleanup)
  --year     4-digit year, e.g. 2026         (default: current year)
  --file     Path to .txt or .docx file      (required unless --cleanup)
  --title    Short title shown in the UI     (optional)
  --cleanup  Normalize all legacy episode keys in streetwise_data.json
             and deduplicate. No API call needed.

Install
-------
  pip install anthropic python-docx
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit("pip install anthropic")

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE     = "streetwise_data.json"
EPISODES_FILE = "streetwise_episodes.json"

# ── Token cost tracker ────────────────────────────────────────────────────────
# Haiku 4.5 pricing (per million tokens, as of March 2026)
_PRICES = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-haiku-4-5":          {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-5":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-5":           {"input": 15.00, "output": 75.00},
}

class TokenTracker:
    """Accumulates token usage across all API calls in a run."""
    def __init__(self):
        self.calls:        list[dict] = []
        self.total_input:  int = 0
        self.total_output: int = 0
        self.total_cost:   float = 0.0

    def record(self, response, label: str = ""):
        """Call after every client.messages.create() with the response object."""
        usage  = response.usage
        inp    = getattr(usage, "input_tokens",  0)
        out    = getattr(usage, "output_tokens", 0)
        model  = getattr(response, "model", "claude-haiku-4-5-20251001")
        prices = _PRICES.get(model, _PRICES["claude-haiku-4-5-20251001"])
        cost   = (inp * prices["input"] + out * prices["output"]) / 1_000_000

        self.calls.append({"label": label, "input": inp, "output": out, "cost": cost, "model": model})
        self.total_input  += inp
        self.total_output += out
        self.total_cost   += cost
        print(f"    tokens  in={inp:,}  out={out:,}  cost=${cost:.4f}  [{model}]")

    def summary(self):
        """Print a full cost summary."""
        sep = "─" * 60
        print(f"\n{sep}")
        print(f"  API Cost Summary")
        print(f"  {'Call':<35} {'In':>7} {'Out':>7} {'Cost':>9}")
        print(f"  {'─'*35} {'─'*7} {'─'*7} {'─'*9}")
        for c in self.calls:
            lbl = c['label'][:35]
            print(f"  {lbl:<35} {c['input']:>7,} {c['output']:>7,} ${c['cost']:>8.4f}")
        print(f"  {'─'*35} {'─'*7} {'─'*7} {'─'*9}")
        print(f"  {'TOTAL':<35} {self.total_input:>7,} {self.total_output:>7,} ${self.total_cost:>8.4f}")
        print(f"{sep}\n")

_tracker = TokenTracker()

# Sources that use "ian:" prefix in episode keys
IAN_SOURCES   = {"ian", "ian salisbury"}

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ── File reader ───────────────────────────────────────────────────────────────

def read_file(path: str) -> str:
    p = Path(path)
    if not p.exists():
        sys.exit(f"File not found: {path}")

    if p.suffix.lower() == ".docx":
        try:
            from docx import Document
        except ImportError:
            sys.exit("pip install python-docx")
        noise = {
            "Save", "Reprints", "Gift Article", "Follow", "Preview",
            "Subscribed", "Advertisement - Scroll to Continue",
            "Newsletter Sign-up", "Market Lab",
            "Exclusive data, tables and charts from Barron's Market Lab.",
            "About This Summary", "Key Points", "In this article",
        }
        doc   = Document(path)
        lines = [
            p.text.strip() for p in doc.paragraphs
            if p.text.strip()
            and p.text.strip() not in noise
            and not p.text.strip().isdigit()
        ]
        return "\n".join(lines)

    return p.read_text(encoding="utf-8")


# ── Claude extraction ─────────────────────────────────────────────────────────

EXTRACT_PROMPT = """\
You are a financial research assistant reading content from {source_label}.

Content label: {label}

Extract EVERY stock, ETF, or mutual fund mentioned — including brief references,
comparisons, and cautionary examples.

For EACH one return a JSON object with exactly these fields:

{{
  "ticker":   "BKU",
  "name":     "BankUnited",
  "type":     "Stock",
  "sector":   "Financial Services",
  "rec":      "Buy",
  "price":    "~$46",
  "status":   "rot",
  "summary":  "6-8 bullet points using this exact format — each bullet on its own line:\n• **Key term or metric:** one sentence explanation\nExample:\n• **Why mentioned:** Barron\'s highlights it as a dividend compounder with 12% EPS growth.\n• **Valuation:** Trades at 17x forward P/E vs 5-yr avg of 19x — modest discount.\n• **Catalyst:** March reconstitution adds Financials exposure, reduces Energy.\n• **Risk:** Rising rates pressure dividend yield spread.\nUse bold (**) for the label before each colon. Cover: why mentioned, analyst thesis, key metrics, valuation, price targets/EPS if available, risks, and any sector/macro tailwind.",
  "base":     "one sentence base-case outcome",
  "bear":     "one sentence bear-case risk",
  "bull":     "one sentence bull-case upside"
}}

sector = the GICS sector e.g. "Financial Services", "Technology", "Healthcare",
         "Consumer Cyclical", "Industrials", "Energy", "Real Estate",
         "Communication Services", "Utilities", "Basic Materials",
         "Consumer Defensive". Use "N/A" if unknown.
rec    = analyst consensus if mentioned: "Strong Buy", "Buy", "Hold", "Sell".
         Use "N/A" if not mentioned.

type   = Stock | ETF | Fund
status = hot | rot | pull | press | dip | rec | caut | flat
  hot   strong momentum / analyst conviction buy
  rot   sector rotation / thematic tailwind
  pull  pulling back from highs — possible entry
  press under real fundamental pressure
  dip   fear-based selloff = buy opportunity
  rec   turnaround / recovery
  caut  proceed with caution
  flat  neutral / no strong view

Return ONLY a valid JSON array. No markdown, no prose, no backticks.

CONTENT:
{text}
"""

def extract_via_claude(source_label: str, label: str, text: str) -> list[dict]:
    print("  Calling Claude API…", flush=True)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": EXTRACT_PROMPT.format(
                source_label=source_label,
                label=label,
                text=text,
            )
        }]
    )
    _tracker.record(msg, "extract tickers")
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw   = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    try:
        data = json.loads(raw)
        print(f"  Extracted {len(data)} entries", flush=True)
        return data
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}\n  First 600 chars:\n{raw[:600]}")
        sys.exit(1)


# ── Summary merge (only for Streetwise — Ian keeps sections separate) ─────────

MERGE_PROMPT = """\
A ticker appears in BOTH an existing database entry AND a new episode.
Produce a single merged summary that:
- Naturally integrates both entries
- Notes which episodes the ticker appeared in
- Highlights any evolution in the thesis (confirmed? changed? contradicted?)
- Reads as a continuous narrative, not two summaries stapled together
- Is 200-350 words total

EXISTING SUMMARY:
{existing}

NEW EPISODE ({label}) SUMMARY:
{new_sum}

Return ONLY the merged paragraph text. No JSON, no labels, no extra text.
"""

def merge_summaries(existing: str, new_sum: str, label: str) -> str:
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": MERGE_PROMPT.format(
                existing=existing, new_sum=new_sum, label=label
            )
        }]
    )
    _tracker.record(msg, f"merge summary")
    return msg.content[0].text.strip()


def strip_old_sections(text: str, ep_key: str, label: str, source_label: str) -> str:
    """
    Remove any previously written sections for this episode, regardless of
    which heading format was used (old --- format or new === format).

    Old format (inline):  --- Source (date) --- text continues on same line
    New format (block):   === ep_key | Source · label ===\ntext on next line
    """
    import re

    # Extract date variants from ep_key e.g. "ian:2026/3/11"
    parts = ep_key.split(":")[-1].split("/")   # ["2026","3","11"]
    if len(parts) == 3:
        year, month, day = parts
        bare_date = f"{month}/{day}"            # "3/11"
        full_date = f"{month}/{day}/{year}"     # "3/11/2026"
    else:
        bare_date = "/".join(parts)
        full_date = bare_date

    result = text

    # ── Strip old --- format ---------------------------------------------------
    # Old format: --- Source (Barron's, date — title) ---
    # source_label may contain parens itself e.g. "Ian Salisbury (Barron's)"
    # so we match on the first word of the source + the date anywhere before ---
    first_word = re.escape(source_label.split()[0])   # e.g. "Ian"
    old_pat = (
        r"---\s*" + first_word +
        r"[^-]*(?:" + re.escape(bare_date) + r"|" + re.escape(full_date) + r")[^-]*---"
    )
    m = re.search(old_pat, result)
    if m:
        before = result[:m.start()].rstrip()
        after  = result[m.end():]
        # Drop text until the next section marker (=== or ---) or end
        next_sec = re.search(r'(===|\n---)', after)
        after    = after[next_sec.start():].lstrip() if next_sec else ""
        result   = (before + "\n\n" + after).strip() if after else before

    # ── Strip new === format ---------------------------------------------------
    new_pat = r"===\s*" + re.escape(ep_key) + r"\s*\|[^=]*==="
    m = re.search(new_pat, result)
    if m:
        before = result[:m.start()].rstrip()
        after  = result[m.end():]
        next_sec = re.search(r'(===|\n---)', after)
        after    = after[next_sec.start():].lstrip() if next_sec else ""
        result   = (before + "\n\n" + after).strip() if after else before

    return result


def append_section(existing: str, new_sum: str,
                   ep_key: str, label: str, source_label: str) -> str:
    """
    Append a new section under a parseable heading.
    Format: === ep_key | Source · label ===

    Before appending, strips any previously written section for this
    episode in any format (old --- dashes or new === equals).
    """
    heading = f"=== {ep_key} | {source_label} · {label} ==="
    cleaned = strip_old_sections(existing, ep_key, label, source_label)
    base    = cleaned.rstrip() if cleaned.strip() else ""
    if base:
        return f"{base}\n\n{heading}\n{new_sum}"
    return f"{heading}\n{new_sum}"


# ── Episode key helpers ───────────────────────────────────────────────────────

def make_ep_key(source_slug: str, year: int, date: str) -> str:
    """
    Build the canonical episode key stored in streetwise_data.json and
    streetwise_episodes.json.

    Streetwise : "stw:2026/3/13"
    Ian        : "ian:2026/3/6"
    Custom     : "goldman-sachs:2026/3/10"
    """
    return f"{source_slug}:{year}/{date}"


def normalize_ep_key(key: str, default_year: int) -> str:
    """
    Convert any legacy format key to canonical form.

    Legacy formats produced by old scripts:
      "3/13"         → "stw:2026/3/13"   (bare M/D = old streetwise, no prefix)
      "2026/3/13"    → "stw:2026/3/13"   (old YYYY/M/D streetwise, no prefix)
      "ian:3/6"      → "ian:2026/3/6"    (ian, no year)
      "stw:2026/3/13"→ unchanged          (already canonical)
    """
    if ":" in key:
        prefix, bare = key.split(":", 1)
        parts = bare.split("/")
        if len(parts) == 2:           # prefix:M/D  → prefix:YYYY/M/D
            return f"{prefix}:{default_year}/{parts[0]}/{parts[1]}"
        return key                    # already prefix:YYYY/M/D
    else:
        parts = key.split("/")
        if len(parts) == 2:           # M/D  → stw:YYYY/M/D
            return f"stw:{default_year}/{parts[0]}/{parts[1]}"
        if len(parts) == 3:           # YYYY/M/D  → stw:YYYY/M/D
            return f"stw:{key}"
        return key


def normalize_db_keys(db: list[dict], default_year: int) -> list[dict]:
    """Rewrite every legacy episode tag in every record to canonical form."""
    for rec in db:
        if "e" in rec:
            seen: list[str] = []
            for tag in rec["e"]:
                norm = normalize_ep_key(tag, default_year)
                if norm not in seen:
                    seen.append(norm)
            rec["e"] = seen
    return db


def ep_already_present(existing_tags: list[str],
                        ep_key: str, year: int) -> bool:
    """True if ep_key (canonical) is already in existing_tags."""
    canonical = normalize_ep_key(ep_key, year)
    return any(normalize_ep_key(t, year) == canonical for t in existing_tags)


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_db() -> list[dict]:
    if not Path(DATA_FILE).exists():
        print(f"  {DATA_FILE} not found — starting fresh")
        return []
    with open(DATA_FILE) as f:
        data = json.load(f)
    print(f"  Loaded {len(data)} existing records")
    return data


def save_db(data: list[dict]):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved {len(data)} records to {DATA_FILE}")


def update_episode_registry(ep_key: str, label: str):
    registry: dict = {}
    if Path(EPISODES_FILE).exists():
        with open(EPISODES_FILE) as f:
            registry = json.load(f)
    if ep_key not in registry:
        registry[ep_key] = label
        with open(EPISODES_FILE, "w") as f:
            json.dump(registry, f, indent=2)
        print(f"  Registered: {ep_key} → {label}")
    else:
        print(f"  Episode already registered: {ep_key}")


# ── Merge ─────────────────────────────────────────────────────────────────────

def merge(
    db:           list[dict],
    entries:      list[dict],
    ep_key:       str,
    label:        str,
    source_slug:  str,
    source_label: str,
    year:         int,
    is_ian_style: bool,   # True = append sections; False = AI merge
    prefix:       str = "stw",
) -> tuple[list[dict], int, int]:

    db     = normalize_db_keys(db, year)
    lookup = {d["t"].upper(): d for d in db}
    added = updated = 0

    # The src tag stored in each record's "src" list
    src_tag = source_slug   # e.g. "streetwise", "ian", "goldman-sachs"

    for e in entries:
        ticker = e.get("ticker", "").strip().upper()
        if not ticker or ticker == "N/A":
            print(f"    Skip (no ticker): {e.get('name', '?')}")
            continue

        new_sum = e.get("summary", "")

        if ticker in lookup:
            rec = lookup[ticker]

            # Episode tag
            if not ep_already_present(rec.get("e", []), ep_key, year):
                rec["e"] = rec.get("e", []) + [ep_key]

            # Source list
            rec.setdefault("src", ["streetwise"])
            if src_tag not in rec["src"]:
                rec["src"].append(src_tag)

            # Summary strategy
            if is_ian_style:
                # Keep sections visually separate under dated headings
                rec["sum"] = append_section(
                    rec.get("sum", ""), new_sum, ep_key, label, source_label
                )
            else:
                # AI merge into a single flowing narrative
                print(f"    Merging  {ticker}…")
                rec["sum"] = merge_summaries(rec.get("sum", ""), new_sum, label)

            # Update scenarios if new ones are more detailed
            for field in ("base", "bear", "bull"):
                val = e.get(field, "")
                if val and len(val) > 10:
                    rec[field] = val

            # Latest episode always wins on status and price
            rec["s"] = e.get("status", rec.get("s", "flat"))
            if e.get("price", "N/A") != "N/A":
                rec["p"] = e["price"]

            # Store sector and rec if provided and not already set
            if e.get("sector") and e["sector"] != "N/A":
                rec["sector"] = e["sector"]
            if e.get("rec") and e["rec"] != "N/A":
                rec["rec_analyst"] = e["rec"]   # stored as rec_analyst to avoid collision with live rec

            print(f"    Updated  {ticker}")
            updated += 1

        else:
            # Brand-new ticker
            if is_ian_style:
                summary = f"=== {ep_key} | {source_label} · {label} ===\n{new_sum}"
            else:
                summary = new_sum

            new_rec = {
                "t":    ticker,
                "n":    e.get("name", ticker),
                "y":    e.get("type", "Stock"),
                "e":    [ep_key],
                "p":    e.get("price", "N/A"),
                "s":    e.get("status", "flat"),
                "src":  [src_tag],
                "sum":  summary,
                "base": e.get("base", ""),
                "bear": e.get("bear", ""),
                "bull": e.get("bull", ""),
            }
            if e.get("sector") and e["sector"] != "N/A":
                new_rec["sector"] = e["sector"]
            if e.get("rec") and e["rec"] != "N/A":
                new_rec["rec_analyst"] = e["rec"]
            lookup[ticker] = new_rec
            print(f"    Added    {ticker} — {e.get('name', '')}")
            added += 1

    # Stamp any src-less entries so the source filter always works
    for rec in lookup.values():
        if "src" not in rec:
            rec["src"] = ["streetwise"]

    return list(lookup.values()), added, updated


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Add any research source to the Streetwise dashboard"
    )
    parser.add_argument(
        "--source", default="streetwise",
        help=(
            'Who produced this content. '
            'Built-ins: "streetwise", "ian". '
            'Any other string creates a new custom source. '
            '(default: streetwise)'
        )
    )
    parser.add_argument(
        "--date", default="",
        help='M/D date, e.g. "3/13"  (required unless --cleanup)'
    )
    parser.add_argument(
        "--year", type=int, default=datetime.now().year,
        help=f'4-digit year (default: {datetime.now().year})'
    )
    parser.add_argument(
        "--file", default="",
        help="Path to .txt or .docx file  (required unless --cleanup)"
    )
    parser.add_argument(
        "--title", default="",
        help='Short title shown in the UI, e.g. "Anything-But-AI Rally"'
    )
    parser.add_argument(
        "--prefix", default="",
        help=(
            "3-4 char prefix for episode keys, e.g. \"stw\", \"ian\", \"gs\". "
            "If omitted, auto-derived from first 3 letters of --source. "
            "Built-in shortcuts: streetwise=stw, ian=ian."
        )
    )
    parser.add_argument(
        "--cleanup", action="store_true",
        help=(
            "One-time fix: normalize all legacy M/D episode keys to YYYY/M/D "
            "and remove duplicates. No API call."
        )
    )
    parser.add_argument(
        "--enrich", action="store_true",
        help=(
            "Fetch sector and analyst rec from Yahoo Finance for all tickers "
            "missing these fields. Run once after initial setup."
        )
    )
    args = parser.parse_args()

    # ── Enrich mode — fetch sector/rec from Yahoo for missing tickers ────────
    if args.enrich:
        try:
            import yfinance as yf
        except ImportError:
            sys.exit("pip install yfinance")
        import time as _time

        sep = "─" * 60
        print(f"\n{sep}")
        print("  Enrich — fetching sector & analyst rec from Yahoo Finance")
        print(f"{sep}\n")

        db      = load_db()
        changed = 0
        total   = len(db)

        for i, rec in enumerate(db, 1):
            ticker = rec.get("t", "")
            if not ticker:
                continue
            # Skip if both already present
            has_sector = rec.get("sector") and rec["sector"] != "N/A"
            has_rec    = rec.get("rec_analyst") and rec["rec_analyst"] != "N/A"
            if has_sector and has_rec:
                continue
            try:
                info   = yf.Ticker(ticker).info
                # Use sector if available; skip Yahoo internal quoteType codes
                _NOISE = {"ECNQQUOTE","EQUITY","ETF","MUTUALFUND","INDEX",
                          "CURRENCY","CRYPTOCURRENCY","FUTURE","OPTION"}
                sector = info.get("sector") or ""
                if not sector or sector.upper() in _NOISE:
                    qt = (info.get("quoteType") or "").upper()
                    sector = "" if qt in _NOISE else qt.title()
                rec_r  = (info.get("recommendationKey") or "").lower()
                rec_v  = rec_r.replace("_", " ").title() if rec_r else ""

                updated_fields = []
                if sector and not has_sector:
                    rec["sector"] = sector
                    updated_fields.append(f"sector={sector}")
                if rec_v and not has_rec:
                    rec["rec_analyst"] = rec_v
                    updated_fields.append(f"rec={rec_v}")

                if updated_fields:
                    print(f"  [{i}/{total}] {ticker:<8}  {', '.join(updated_fields)}")
                    changed += 1
                else:
                    print(f"  [{i}/{total}] {ticker:<8}  no data from Yahoo")

            except Exception as e:
                print(f"  [{i}/{total}] {ticker:<8}  error: {e}")

            _time.sleep(0.3)   # be polite to Yahoo Finance

        if changed:
            save_db(db)
            print(f"\n  Updated {changed} tickers → {DATA_FILE}")
        else:
            print("\n  Nothing to update — all tickers already have sector/rec")
        print(f"{sep}\n")
        return

    # ── Cleanup mode ──────────────────────────────────────────────────────────
    if args.cleanup:
        sep = "─" * 60
        print(f"\n{sep}")
        print("  Cleanup — normalizing episode keys to YYYY/M/D")
        print(f"  Year: {args.year}  (change with --year)")
        print(f"{sep}\n")
        db    = load_db()
        fixed = normalize_db_keys(db, args.year)
        save_db(fixed)
        print(f"\n  Done. Re-run without --cleanup to add new content.\n")
        return

    # ── Validate required args ────────────────────────────────────────────────
    if not args.date:
        sys.exit("--date is required  e.g. --date \"3/13\"")
    if not args.file:
        sys.exit("--file is required  e.g. --file transcript.txt")

    # ── Derive source metadata ────────────────────────────────────────────────
    source_raw  = args.source.strip()
    source_slug = source_raw.lower().replace(" ", "-")   # src tag in records

    # Resolve prefix: explicit --prefix > built-in shortcut > first 3 letters
    prefix_map = {
        "streetwise": "stw",
        "ian":        "ian",
        "ian-salisbury": "ian",
        "stw":        "stw",
    }
    if args.prefix:
        prefix = args.prefix.strip().lower()
    else:
        prefix = prefix_map.get(source_slug, source_slug[:3])

    # Streetwise (stw) = AI-merge summaries into one flowing narrative
    # Everything else  = append section under a dated heading
    is_ian_style = prefix != "stw"

    # Human-readable source label used in summary section headings
    source_label_map = {
        "stw": "Barron's Streetwise",
        "ian": "Ian Salisbury (Barron's)",
    }
    source_label = source_label_map.get(prefix, source_raw)

    # Episode key: prefix:YYYY/M/D  e.g. "stw:2026/3/13", "ian:2026/3/6", "gs:2026/3/10"
    ep_key = f"{prefix}:{args.year}/{args.date}"
    label  = f"{args.date}/{args.year}"
    if args.title:
        label = f"{label} — {args.title}"

    sep = "─" * 60
    print(f"\n{sep}")
    print(f"  Streetwise Updater")
    print(f"  Source  : {source_label}  (prefix: {prefix})")
    print(f"  Episode : {label}")
    print(f"  Key     : {ep_key}")
    print(f"  File    : {args.file}")
    print(f"{sep}\n")

    print("[1/4] Reading file…")
    text = read_file(args.file)
    print(f"  {len(text):,} characters")

    print("\n[2/4] Extracting tickers via Claude…")
    entries = extract_via_claude(source_label, label, text)

    print("\n[3/4] Merging into database…")
    db = load_db()
    merged, added, updated = merge(
        db, entries, ep_key, label,
        source_slug, source_label, args.year, is_ian_style, prefix
    )
    print(f"\n  {added} new  ·  {updated} updated  ·  {len(merged)} total")

    print("\n[4/4] Saving…")
    save_db(merged)
    update_episode_registry(ep_key, label)

    _tracker.summary()
    print(f"{sep}")
    print("  Done — restart server.py to see changes")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()
