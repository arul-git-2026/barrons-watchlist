# Streetwise Server — How To

## Access the dashboard
```
https://app.barrons-watchlist-research.com/v2
```
Log in with the passphrase (same value as the token in your `.env` — `AUTH_TOKEN`).

---

## SSH into the server
```bash
ssh -i C:\Users\vasanthaganesh.arulm\Downloads\claude\streetwise\ssh-key-2026-03-27.key opc@132.145.243.138
```

---

## Deploy code changes (from your PC)
```bash
# 1. Commit and push locally (done via Claude)
git push

# 2. SSH in, then:
cd /opt/streetwise
git pull
sudo systemctl restart streetwise
```

---

## Service management (on server)
```bash
sudo systemctl status streetwise      # is it running?
sudo systemctl restart streetwise     # restart after code change
sudo systemctl stop streetwise        # stop
sudo systemctl start streetwise       # start
sudo journalctl -u streetwise -f      # live logs
sudo journalctl -u streetwise -n 100  # last 100 log lines
```

---

## Clear a stuck cache (on server)
```bash
cd /opt/streetwise

# Single ticker
python3 -c "import sqlite3; c=sqlite3.connect('price_history.db'); c.execute(\"DELETE FROM dcf_cache WHERE ticker='TSM'\"); c.commit()"

# All DCF cache
python3 -c "import sqlite3; c=sqlite3.connect('price_history.db'); c.execute('DELETE FROM dcf_cache'); c.commit()"
```

---

## If port 5000 is stuck after a crash
```bash
sudo fuser -k 5000/tcp
sudo systemctl start streetwise
```

---

## Cloudflare Tunnel (should auto-run, but if down)
```bash
sudo systemctl status cloudflared
sudo systemctl restart cloudflared
```

---

## Key file locations (on server)
```
/opt/streetwise/server.py          # Flask app
/opt/streetwise/widget_v2.html     # Dashboard UI
/opt/streetwise/price_history.db   # SQLite — price history + DCF cache
/opt/streetwise/.env               # API keys (GEMINI_API_KEY, AUTH_TOKEN, etc.)
```

---

## Chrome extension auth
The extension sends `X-Streetwise-Token: <token>` as a request header.
Set the token in the extension popup — same value as `AUTH_TOKEN` in `.env`.

---

## Branch
Active development branch: `feature/research`
Main branch: `master`
