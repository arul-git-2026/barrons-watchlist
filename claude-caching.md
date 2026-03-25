# Claude Prompt Caching — Streetwise Integration Guide

Claude's **prompt caching** feature lets you cache large, static portions of a prompt (system prompts, long article text, reference data) on Anthropic's servers and reuse them across multiple API calls. This eliminates redundant token processing and can cut costs and latency dramatically for repeated calls.

---

## How Prompt Caching Works

When you add `"cache_control": {"type": "ephemeral"}` to a content block, Anthropic caches that block for **5 minutes** after the first call. Subsequent calls that include the same cached block skip tokenisation for that portion entirely.

```
First call:   full prompt processed → cache written  (normal price)
Later calls:  cached block skipped  → cache read     (10% of normal input price)
```

Cache lifetime is **5 minutes** from the last use. Each use resets the timer.

---

## Pricing Impact (March 2026)

| Model | Normal input | Cache write | Cache read |
|---|---|---|---|
| Claude Haiku 4.5 | $0.80 /M | $1.00 /M (+25%) | $0.08 /M (−90%) |
| Claude Sonnet 4.6 | $3.00 /M | $3.75 /M (+25%) | $0.30 /M (−90%) |

Cache writes cost 25% more than a normal input token; cache reads cost 90% less. Savings materialise on the second and subsequent calls within 5 minutes.

---

## Where Caching Applies in Streetwise

### 1. `/api/ingest-page` — Article Extraction

The extraction prompt has two parts:
- **Static system instructions** (~600 tokens) — field definitions, status codes, formatting rules. These never change.
- **Dynamic article text** — changes every call.

Cache the static instructions. The article body stays uncached.

**Current code** (`server.py:1130`):
```python
msg = client.messages.create(
    model      = claude_model,
    max_tokens = 8000,
    messages   = [{"role": "user", "content": extract_prompt}],
)
```

**With caching** — split the prompt into a cached system prefix + dynamic user content:
```python
import anthropic

client = anthropic.Anthropic(api_key=api_key)

STATIC_EXTRACTION_SYSTEM = """You are a financial research assistant extracting tickers from articles.

For EACH ticker return a JSON object with exactly these fields:
{
  "ticker":   "BKU",
  "name":     "BankUnited",
  "type":     "Stock",
  "sector":   "Financial Services",
  "rec":      "Buy",
  "price":    "~$46",
  "status":   "rot",
  "summary":  "6-8 bullet points, each on its own line...",
  "base":     "one sentence base-case outcome",
  "bear":     "one sentence bear-case risk",
  "bull":     "one sentence bull-case upside"
}

status = hot | rot | pull | press | dip | rec | caut | flat
sector = GICS sector e.g. "Financial Services", "Technology"
rec    = analyst consensus: "Strong Buy", "Buy", "Hold", "Sell", "N/A"

Return ONLY a valid JSON array. No markdown, no prose, no backticks."""

msg = client.messages.create(
    model      = claude_model,
    max_tokens = 8000,
    system     = [
        {
            "type": "text",
            "text": STATIC_EXTRACTION_SYSTEM,
            "cache_control": {"type": "ephemeral"},   # <── cache this block
        }
    ],
    messages   = [
        {
            "role":    "user",
            "content": f"Source: {source_label}\nDate: {label}\n\nARTICLE:\n{text[:15000]}",
        }
    ],
)
```

**Expected savings:** If you process 3+ articles in quick succession (e.g. batch ingestion), the ~600-token system block is read from cache after the first call at $0.08/M instead of $0.80/M.

---

### 2. `/api/research` — Streaming Research

The research system prompt is ~200 tokens and is the same for every ticker query within the same session. Cache it.

**Current code** (`server.py:768`):
```python
resp = client.messages.create(
    model      = claude_model_id,
    max_tokens = 2000,
    system     = system_prompt,
    tools      = [{"type": "web_search_20250305", "name": "web_search"}],
    messages   = [{"role": "user", "content": query}],
)
```

**With caching:**
```python
resp = client.messages.create(
    model      = claude_model_id,
    max_tokens = 2000,
    system     = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},   # <── cache this block
        }
    ],
    tools    = [{"type": "web_search_20250305", "name": "web_search"}],
    messages = [{"role": "user", "content": query}],
)
```

---

### 3. `/api/regen-cases` — Regenerate Base/Bear/Bull Cases

When regenerating cases for multiple tickers in a batch, the instruction block is identical for every call. Cache it.

---

## Multi-Turn Conversation Caching (Future Feature)

If you add a per-ticker chat/Q&A feature where the user asks follow-up questions, cache the entire conversation history up to the last user turn:

```python
messages = [
    # All prior turns — cache the whole history
    {"role": "user",      "content": [{"type": "text", "text": "What is AAPL's thesis?",
                                        "cache_control": {"type": "ephemeral"}}]},
    {"role": "assistant", "content": "• **Earnings:** ..."},
    # Current (new) question — NOT cached
    {"role": "user",      "content": "What are the key risks?"},
]
```

The cache breakpoint should be placed at the last content block that doesn't change between turns.

---

## Checking Cache Hit/Miss in Responses

The API returns cache usage in `response.usage`:

```python
usage = response.usage
print(f"Input tokens:        {usage.input_tokens}")
print(f"Cache write tokens:  {usage.cache_creation_input_tokens}")
print(f"Cache read tokens:   {usage.cache_read_input_tokens}")
```

Update `calc_cost()` in `server.py` to account for cache pricing:

```python
def calc_cost(response) -> tuple[int, int, float]:
    """Return (input_tokens, output_tokens, cost_usd) from an API response."""
    usage     = getattr(response, "usage", None)
    inp       = getattr(usage, "input_tokens",                  0) if usage else 0
    out       = getattr(usage, "output_tokens",                 0) if usage else 0
    cache_w   = getattr(usage, "cache_creation_input_tokens",   0) if usage else 0
    cache_r   = getattr(usage, "cache_read_input_tokens",       0) if usage else 0
    model     = getattr(response, "model", "claude-haiku-4-5-20251001")
    prices    = _TOKEN_PRICES.get(model, _TOKEN_PRICES["claude-haiku-4-5-20251001"])

    normal_in = inp - cache_w - cache_r   # tokens priced at normal input rate
    cost = (
        normal_in * prices["input"]
        + cache_w * prices["input"] * 1.25   # cache write: +25%
        + cache_r * prices["input"] * 0.10   # cache read:  −90%
        + out     * prices["output"]
    ) / 1_000_000

    return inp, out, cost
```

And update `_TOKEN_PRICES` to add the cache rates explicitly:

```python
_TOKEN_PRICES = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00,  "cache_write": 1.00,  "cache_read": 0.08},
    "claude-haiku-4-5":          {"input": 0.80,  "output": 4.00,  "cache_write": 1.00,  "cache_read": 0.08},
    "claude-sonnet-4-5":         {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-opus-4-5":           {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
}
```

---

## Minimum Cacheable Size

A content block must be **at least 1 024 tokens** to be eligible for caching. Blocks shorter than this are processed normally even if marked with `cache_control`.

| Block | Approx tokens | Cacheable? |
|---|---|---|
| Static extraction system (full) | ~600 | No — below threshold |
| Static extraction system + field definitions | ~1 100 | Yes |
| Full article text (15 000 chars) | ~3 750 | Yes |
| Research system prompt | ~200 | No |

**Practical implication for Streetwise:** Article text is the best caching target — it is long and could be reprocessed (e.g. re-extract with a different model or re-generate cases). Cache the article block, not the short system instructions.

---

## Caching the Article Text for Re-extraction

If you want to let users re-run extraction on the same article with a different model without re-sending the full text, cache the article content block:

```python
msg = client.messages.create(
    model      = claude_model,
    max_tokens = 8000,
    messages   = [
        {
            "role": "user",
            "content": [
                # System instructions (short — not cached)
                {"type": "text", "text": STATIC_EXTRACTION_SYSTEM},
                # Article text (long — cache this)
                {
                    "type": "text",
                    "text": f"Source: {source_label}\nDate: {label}\n\nARTICLE:\n{text[:15000]}",
                    "cache_control": {"type": "ephemeral"},
                },
                # Dynamic per-call context (not cached)
                {"type": "text", "text": hints_block or "No additional hints."},
            ],
        }
    ],
)
```

---

## Summary: Quick Wins

| Where | What to cache | Estimated saving |
|---|---|---|
| `ingest-page` | Article text (>1 024 tokens) | 90% off input for re-extractions |
| `ingest-page` batch | Static instructions if >1 024 tokens | 90% off from 2nd article onward |
| `regen-cases` batch | Instruction block | 90% off from 2nd ticker onward |
| `research` session | System prompt (too short — skip) | n/a |

---

## References

- [Anthropic Prompt Caching Guide](https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching)
- [Python SDK — anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python)
