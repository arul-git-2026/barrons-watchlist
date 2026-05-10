# Streetwise Server — How To

## Access the dashboard
```
https://app.barrons-watchlist-research.com/v2
```
Log in with the passphrase — same value as `STREETWISE_TOKEN` in `/etc/streetwise.env`.

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

## Rotate the auth token
The token secures both the dashboard login and the Chrome extension API calls.
When rotating, you must update it in **three places**:

```bash
# 1. Generate a new token
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. Update server (SSH in first)
sudo nano /etc/streetwise.env          # set STREETWISE_TOKEN=<new>
sudo nano /opt/streetwise/.env         # set STREETWISE_TOKEN=<new>
sudo systemctl restart streetwise

# 3. Update the Chrome extension (on your PC)
#    Edit barrons-ext/barrons-ext/popup.js → var EXT_TOKEN = '<new>'
#    Then: git add + git commit + git push
#    Then: chrome://extensions → Reload the extension
```

---

## Chrome extension — how it connects
- **Widget** (`app.*`) is protected by Cloudflare Access (browser login wall)
- **Extension** calls `api.barrons-watchlist-research.com` — same Flask app but no CF Access wall
- Every extension request sends `X-Streetwise-Token` header; Flask validates it against `STREETWISE_TOKEN`
- `EXT_TOKEN` is hardcoded in `barrons-ext/barrons-ext/popup.js` — must match server token

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
/opt/streetwise/server.py              # Flask app
/opt/streetwise/templates/widget_v2.html  # Dashboard UI
/opt/streetwise/streetwise_data.json   # Ticker + episode database (gitignored)
/opt/streetwise/price_history.db       # SQLite — price history + DCF cache
/opt/streetwise/.env                   # API keys (local fallback)
/etc/streetwise.env                    # API keys + STREETWISE_TOKEN (production, takes priority)
```

---

## Branch
Active development branch: `feature/research`
Main branch: `master`
