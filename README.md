# Streetwise — Stock Analysis Dashboard

A self-hosted stock analysis dashboard that ingests Barron's articles via a Chrome extension, extracts tickers using AI (Claude / Gemini), stores investment theses, and serves live price data through a Flask API.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│  Chrome Extension (barrons-ext)                      │
│  Reads Barron's article → POST /api/ingest-page      │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  Flask Server  (server.py)                           │
│  ├── AI extraction  (Claude / Gemini)                │
│  ├── Live quotes    (yfinance, 5-min cache)          │
│  ├── Price history  (SQLite + yfinance fallback)     │
│  └── Static data    (streetwise_data.json)           │
└────────────────────┬─────────────────────────────────┘
                     │
┌────────────────────▼─────────────────────────────────┐
│  Dashboard  (widget.html)                            │
│  Browser-based UI — no build step required           │
└──────────────────────────────────────────────────────┘
```

---

## Features

| Feature | Details |
|---|---|
| **Article ingestion** | Chrome extension sends Barron's articles to server; AI extracts every mentioned ticker |
| **AI models** | Claude Haiku / Sonnet (Anthropic) and Gemini Flash / Pro (Google) |
| **Live quotes** | Yahoo Finance via `yfinance`; in-memory TTL cache (5 min) |
| **Price history** | SQLite DB for 1W–3Y ranges; 1D/5D always fetched live (intraday) |
| **Investment thesis** | Structured per-ticker summaries: bullet points, base/bear/bull cases |
| **Research endpoint** | Streaming SSE endpoint; Claude with web search tool |
| **Dividend enrichment** | Bulk-fetch `dividendYield` from Yahoo Finance for all tickers |
| **Cloudflare tunnel** | `cloudflared` for remote access from mobile/tablet |
| **Multi-source** | Barron's Streetwise, Ian Salisbury, Barrons Live, Dividends, custom sources |

---

## File Structure

```
streetwise/
├── server.py                  # Flask API server — main backend (~2 000 lines)
├── widget.html                # Main dashboard UI (standalone HTML/JS/CSS)
├── popup.html                 # Article sender popup (served at /sender)
├── updater.py                 # CLI script to ingest .txt/.docx research files
├── fetch_yields.py            # Standalone yield fetcher
├── reformat_summaries.py      # One-off utility: reformat legacy AI summaries
├── manifest.json              # Chrome extension manifest (root, for popup.html)
├── start_streetwise.bat       # Windows launcher (server + cloudflare tunnel)
├── streetwise_db_schema.svg   # Database schema diagram
├── thesis_write_paths.svg     # Thesis write-path diagram
├── streetwise_widget_data.js  # Bundled static data for offline widget
│
├── barrons-ext/               # Chrome extension source
│   └── barrons-ext/
│       ├── manifest.json
│       ├── popup.html / popup.js
│       └── progress.html / progress.js
│
└── price_history.db           # SQLite price cache (gitignored — regenerates)
    streetwise_data.json       # Ticker + thesis database (gitignored — personal data)
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install flask flask-cors yfinance anthropic
```

For the CLI updater (`updater.py`):
```bash
pip install python-docx
```

### 2. Set API keys

**Windows (cmd):**
```cmd
set ANTHROPIC_API_KEY=sk-ant-...
set GEMINI_API_KEY=AIza...
```

**Windows (PowerShell):**
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:GEMINI_API_KEY    = "AIza..."
```

**Linux / macOS:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=AIza...
```

### 3. Start the server

```bash
python server.py
```

Server starts on `http://localhost:5000`.

Open the dashboard: `http://localhost:5000`
Open the article sender: `http://localhost:5000/sender`

### 4. (Optional) Cloudflare tunnel for remote access

```bash
cloudflared tunnel --url http://localhost:5000
```

Or use the Windows batch launcher:
```cmd
start_streetwise.bat
```

---

## Chrome Extension Setup

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked** → select `barrons-ext/barrons-ext/`
4. Navigate to any Barron's article
5. Click the extension icon → choose source/date/model → **Send to Streetwise**

### Extension fields

| Field | Description |
|---|---|
| **Source** | Barron's Ian Salisbury / Barron's Streetwise / Barrons Live / Dividends |
| **Date (M/D)** | Article date, e.g. `3/18` |
| **Year** | Article year, e.g. `2026` |
| **Model** | Claude Haiku (fast/cheap) / Claude Sonnet / Gemini Flash / Gemini Pro |

---

## API Reference

### `GET /api/data`
Fetch all ticker records. Optional query params:
- `?tickers=AAPL,MSFT` — fetch live quotes for specific tickers
- `?refresh=1` — expire cache and force fresh Yahoo Finance fetch

**Response:** `{ ok, data: [...ticker records], ts }`

---

### `GET /api/history?ticker=AAPL&range=1Y`
Price history for a ticker.

Supported ranges: `1D 5D 1W 1M 3M 6M YTD 1Y 2Y 3Y`

**Response:** `{ ok, ticker, range, rows: [{date, open, high, low, close, volume}] }`

---

### `GET /api/episodes`
All ingested article episodes.

**Response:** `{ ok, episodes: [{key, label, source, date, tickers}] }`

---

### `POST /api/ingest-page`
Ingest article text and extract tickers via AI.

```json
{
  "text":    "Full article text...",
  "date":    "3/18",
  "year":    "2026",
  "source":  "Barron's Ian Salisbury",
  "prefix":  "ian",
  "title":   "Article headline",
  "model":   "claude-haiku",
  "anchors": [{"text": "Apple", "href": "/stocks/AAPL"}]
}
```

**Response:** `{ ok, added, updated, tickers, cost, model, log }`

---

### `POST /api/research`
Stream live research for a ticker via Claude (web search) or Gemini.

```json
{
  "ticker": "AAPL",
  "name":   "Apple Inc.",
  "query":  "Latest earnings and analyst views",
  "model":  "claude-sonnet"
}
```

**Response:** Server-Sent Events stream — `data: {"text":"..."}` … `data: [DONE]`

Models: `claude` / `claude-haiku` / `claude-sonnet` / `gemini`

---

### `POST /api/regen-cases`
Regenerate base/bear/bull investment cases for a ticker via Claude.

```json
{ "ticker": "AAPL" }
```

---

### `POST /api/add-ticker`
Add or update a ticker with a custom summary.

```json
{
  "ticker":       "AAPL",
  "name":         "Apple Inc.",
  "type":         "Stock",
  "sector":       "Technology",
  "status":       "hot",
  "rec_analyst":  "Buy",
  "price":        "~$220",
  "base":         "Steady iPhone cycle plus services growth.",
  "bear":         "Tariff impact on China supply chain.",
  "bull":         "AI integration drives supercycle.",
  "summary":      "Bullet-point thesis...",
  "source":       "custom",
  "tags":         "Q2-2026"
}
```

---

### `POST /api/enrich-yields`
Bulk-fetch dividend yields from Yahoo Finance.

```json
{ "tickers": ["SCHD","VYM","O"] }
```

Omit `tickers` to run for all tickers in the database.

---

### `POST /api/cache/clear`
Expire the in-memory live quote cache.

```json
{ "tickers": ["AAPL","MSFT"] }
```

Omit `tickers` to clear the entire cache.

---

### `GET /api/db/status`
SQLite price history database statistics.

---

### `GET /api/status`
Server health check + quote cache summary.

---

## CLI Ingestion (`updater.py`)

Ingest research files directly from the command line (no browser needed):

```bash
# Barron's Streetwise podcast transcript
python updater.py --source streetwise --date "3/13" --year 2026 \
  --file transcript_mar13.txt --title "Anything-But-AI Rally"

# Ian Salisbury article
python updater.py --source ian --date "3/6" --year 2026 \
  --file article_mar06.txt --title "Small-Cap Revival"

# Custom source (analyst note, blog post, etc.)
python updater.py --source "Goldman Sachs" --date "3/10" --year 2026 \
  --file goldman_note.txt --title "Tariff Impact on Industrials"

# Normalize legacy episode keys
python updater.py --cleanup --year 2026
```

Accepts `.txt` and `.docx` files.

---

## Ticker Record Schema

Each ticker in `streetwise_data.json` follows this structure:

| Field | Key | Description |
|---|---|---|
| Ticker | `t` | Exchange ticker symbol, e.g. `AAPL` |
| Name | `n` | Company name |
| Type | `y` | `Stock`, `ETF`, `Bond`, etc. |
| Sector | `sector` | GICS sector |
| Status | `s` | `hot` / `rot` / `pull` / `press` / `dip` / `rec` / `caut` / `flat` |
| Price | `p` | Last known price (string, e.g. `~$220`) |
| Summary | `sum` | Full AI-generated thesis (multi-section text) |
| Base case | `base` | One-sentence base-case outcome |
| Bear case | `bear` | One-sentence bear-case risk |
| Bull case | `bull` | One-sentence bull-case upside |
| Episodes | `e` | List of episode keys, e.g. `["ian:2026/3/18"]` |
| Sources | `src` | List of source labels |
| Analyst rec | `rec_analyst` | Consensus: `Buy` / `Hold` / `Sell` / `N/A` |
| Div yield | `div_yield` | Float, e.g. `0.031` (populated by enrich-yields) |

### Status values

| Status | Meaning |
|---|---|
| `hot` | Strong buy / high conviction |
| `rot` | Rotation candidate |
| `pull` | Near-term pullback expected |
| `press` | Under pressure |
| `dip` | Buy-the-dip opportunity |
| `rec` | Recovery story |
| `caut` | Cautious / wait-and-see |
| `flat` | Neutral / no strong view |

---

## Price History Database

`price_history.db` is a SQLite database with a single `prices` table:

| Column | Type | Description |
|---|---|---|
| `ticker` | TEXT | Ticker symbol |
| `date` | TEXT | ISO date `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `open` | REAL | |
| `high` | REAL | |
| `low` | REAL | |
| `close` | REAL | |
| `volume` | INTEGER | |

The server queries the DB first for historical ranges; if fewer rows than expected are found, it falls back to Yahoo Finance and saves the result for next time.

**1D and 5D** ranges are always fetched live (intraday intervals are not stored).

---

## AI Models & Costs

| Model | Use case | Input $/M | Output $/M |
|---|---|---|---|
| Claude Haiku 4.5 | Default ingestion (fast, cheap) | $0.80 | $4.00 |
| Claude Sonnet 4.6 | High-quality ingestion / research | $3.00 | $15.00 |
| Gemini 2.5 Flash | Alternative fast extraction | $0.30 | $2.50 |
| Gemini 2.5 Pro | High-quality alternative | $1.25 | $10.00 |

Cost is calculated and logged after every API call.

---

## Remote Access — Oracle Cloud (dev_02)

The dashboard can be self-hosted on Oracle Cloud Free Tier so it's reachable from work, home, or holiday with no VPN and no Cloudflare dependency.

### Architecture

```
Browser (anywhere)
      │
      │  http://SERVER_IP:5000/?token=YOUR_SECRET
      ▼
Oracle Cloud VM  (Ubuntu 22.04, Always Free)
      └── systemd → server.py (auto-starts on reboot)
```

### Quick Deploy

```bash
# On the Oracle Cloud VM (Ubuntu 22.04) — run once:
curl -fsSL https://raw.githubusercontent.com/arul-git-2026/barrons-watchlist/dev_02/deploy/setup_oracle.sh -o setup.sh
bash setup.sh
```

The script installs Python, clones the repo, creates a venv, opens port 5000, and registers a systemd service.

### Configuration

```bash
sudo nano /etc/streetwise.env
```

```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
STREETWISE_TOKEN=your-secret-token
```

### Data Sync (Windows → Server)

```cmd
deploy\upload_data.bat    # push local tickers to server
deploy\download_data.bat  # pull server data back to Windows
deploy\remote_update.bat  # git pull + restart on server
```

### Security

- All requests require `?token=YOUR_SECRET` (or header `X-Streetwise-Token`)
- Token auth is skipped automatically when `STREETWISE_TOKEN` is not set (local dev)
- Returns `403` for missing/wrong tokens

**Full beginner setup guide:** [ORACLE_SETUP.md](ORACLE_SETUP.md)

---

## Remote Access (Cloudflare Tunnel)

```bash
cloudflared tunnel --url http://localhost:5000
```

Cloudflare assigns a random `*.trycloudflare.com` URL. The Chrome extension `manifest.json` already includes `https://*.trycloudflare.com/*` in `host_permissions`, so the extension can reach the server remotely.

---

## Supported Yahoo Finance Ticker Overrides

Some tickers are stored under a local key but query Yahoo Finance with a different symbol:

| Local key | Yahoo symbol |
|---|---|
| `WALMEX` | `WALMEX.MX` |
| `000660` | `000660.KS` |
| `SSNLF` | `005930.KS` |

---

## Development Notes

- **No build step** — `widget.html` is a self-contained single-file app.
- **Hot reload** — Flask runs in debug mode by default; edit and refresh.
- **CORS** — `flask-cors` is enabled for all origins (localhost dev + extension).
- **Atomic writes** — all JSON saves go through a `.tmp` → `os.replace()` pattern to prevent corruption.
- **Logging** — coloured terminal output with timestamps; werkzeug noise suppressed.

---

## License

Private / personal use.
