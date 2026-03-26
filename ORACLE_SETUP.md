# Oracle Cloud Setup Guide — Streetwise Dashboard

Complete beginner guide to deploying Streetwise on Oracle Cloud Free Tier so you can
access your dashboard from work, home, or holiday.

---

## What You Will End Up With

```
Your browser (anywhere)
        │
        │  http://YOUR_SERVER_IP:5000/?token=YOUR_SECRET
        ▼
Oracle Cloud VM  (Ubuntu 22.04, Always Free)
        │
        └── streetwise server.py  (runs 24/7, auto-restarts on reboot)
                │
                ├── Reads:  streetwise_data.json  (your tickers)
                ├── Reads:  price_history.db       (price cache)
                └── Calls:  Anthropic / Yahoo Finance APIs
```

---

## Part 1 — Create Your Oracle Cloud Account

1. Go to **https://cloud.oracle.com** → click **Start for free**
2. Fill in your details — use a real credit card (required for verification, you will NOT be charged for Always Free resources)
3. Choose your **Home Region** — pick the one geographically closest to you (e.g. `UK South (London)`, `Germany Central`, `US East`)
   > This cannot be changed later, so pick carefully.
4. Complete email verification and sign in

---

## Part 2 — Create Your Virtual Machine

### 2a. Open the Compute panel

1. In the Oracle Cloud Console, click the **hamburger menu** (top-left ☰)
2. Go to **Compute → Instances**
3. Click **Create instance**

### 2b. Configure the instance

| Setting | Value |
|---|---|
| **Name** | `streetwise` |
| **Compartment** | `(root)` (default) |
| **Availability domain** | Try all three (AD-1, AD-2, AD-3) if Ampere is unavailable |

**Image and shape — preferred (Ampere ARM):**

1. Click **Change image** → select **Ubuntu** → **22.04** → confirm
2. Click **Change shape** → select **Ampere** → `VM.Standard.A1.Flex`
3. Set OCPUs: 2, Memory: 12 GB → click **Select shape**

**If Ampere is not available (all 3 ADs):**

- Click **Change shape** → **Specialty and previous generation** → `VM.Standard.E2.1.Micro`
- This is 1 OCPU / 1 GB RAM — sufficient for Streetwise (Flask is lightweight)
- The setup script adds a 2 GB swap file automatically to prevent memory issues

> ⚠️ Do NOT use `VM.Standard.E3.Flex` — it is a **paid** shape

**SSH keys:**

1. Select **Generate a key pair for me**
2. Click **Save private key** → file downloads as `ssh-key-XXXX.key`
3. Store safely — you cannot retrieve it again

### 2c. Launch

Click **Create**. Wait ~2 minutes for status to show **Running**.

---

## Part 3 — Note Your Public IP Address

1. Click your instance name (`streetwise`)
2. Under **Primary VNIC** → **Public IP address** → copy it
   > Your IP: `79.76.99.90`

---

## Part 4 — Open Port 5000 in Oracle's Firewall

> ⚠️ Oracle has TWO separate firewalls: the OCI Security List AND Ubuntu's iptables.
> Both must allow port 5000. The setup script handles iptables. You handle the Security List here.

**Finding the correct Security List — navigate from the instance (not from the VCN list):**

1. Go to **Compute → Instances → streetwise**
2. Scroll to **Primary VNIC** → click the **Subnet** link (blue text)
3. Left sidebar → **Security Lists** → **Default Security List for...**
4. Click **Add Ingress Rules**

| Field | Value |
|---|---|
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `5000` |
| Description | `Streetwise dashboard` |

5. Click **Add Ingress Rules**

---

## Part 5 — Connect to Your Server

### On Windows — PowerShell (recommended)

**Step 1 — Fix key file permissions:**
```powershell
icacls "C:\Users\vasanthaganesh.arulm\Downloads\streetwise\ssh-key-2026-03-25.key" /inheritance:r
icacls "C:\Users\vasanthaganesh.arulm\Downloads\streetwise\ssh-key-2026-03-25.key" /grant:r "vasanthaganesh.arulm:(R)"
```
> If `%username%` fails (German Windows), use your literal username as above.

**Step 2 — Connect:**
```powershell
ssh -i "C:\Users\vasanthaganesh.arulm\Downloads\streetwise\ssh-key-2026-03-25.key" ubuntu@79.76.99.90
```

You should see: `ubuntu@streetwise:~$`

### On Windows — PuTTY (alternative)

1. Download PuTTY from https://putty.org
2. Open PuTTYgen → Load your `.key` file → Save private key as `.ppk`
3. Open PuTTY → Host: `79.76.99.90` → Port: `22`
4. Go to Connection → SSH → Auth → browse to your `.ppk` file
5. Click Open

---

## Part 6 — Run the Setup Script

Once connected via SSH:

```bash
curl -fsSL https://raw.githubusercontent.com/arul-git-2026/barrons-watchlist/dev_02/deploy/setup_oracle.sh -o setup.sh && bash setup.sh
```

> ⚠️ If the repo is private, this returns 404. Fix: GitHub → Settings → Danger Zone → Make public.

The script will:
- Create a 2 GB swap file (critical for 1 GB RAM machines)
- Install Python 3, pip, git, ufw
- Clone the repo into `/opt/streetwise`
- Install Python dependencies in a virtual environment
- Open port 5000 in Ubuntu's firewall (ufw)
- Open port 5000 in iptables (Oracle's OS-level firewall)
- Install and enable the systemd service

**Takes about 3–5 minutes.**

---

## Part 7 — Enter Your API Keys on the Server

```bash
sudo nano /etc/streetwise.env
```

Fill in:
```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
STREETWISE_TOKEN=your-secret-token-here
```

> ⚠️ Token rules: letters, numbers, hyphens ONLY. No `#`, `+`, `&`, `%`, `?` — these break URLs.
> Good: `sw-ABC123-xyz`   Bad: `token#1`, `pass+word`

Save: `Ctrl+O` → Enter → `Ctrl+X`

```bash
sudo systemctl restart streetwise
```

---

## Part 8 — Upload Your Data to the Server

On your **Windows PC**, double-click:

```
deploy\upload_data.bat
```

This copies `streetwise_data.json` (your tickers) and `price_history.db` to the server via SCP.

---

## Part 9 — Access Your Dashboard

Open in any browser, from anywhere:

```
http://79.76.99.90:5000/?token=YOUR_TOKEN
```

Bookmark this URL.

**Chrome extension** — open the extension popup:
- Server URL field: `http://79.76.99.90:5000`
- Token field: your token
- Both are saved automatically to Chrome storage

---

## Part 10 — Check the Server is Running

```bash
sudo systemctl status streetwise
sudo journalctl -u streetwise -f
sudo systemctl restart streetwise
```

---

## Keeping Your Data in Sync

```cmd
deploy\upload_data.bat     ← push local tickers to server
deploy\download_data.bat   ← pull server data back locally
```

---

## Updating the Server Code

```bash
# On the server:
bash /opt/streetwise/deploy/update.sh
```

```cmd
REM From Windows:
deploy\remote_update.bat
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Can't connect via SSH | Check key file path; VM must show Running in OCI console |
| Port 5000 timeout | Check OCI Security List (navigate from instance, not VCN list) |
| Port 5000 blocked after Security List is correct | Run: `sudo iptables -I INPUT 5 -p tcp --dport 5000 -j ACCEPT && sudo netfilter-persistent save` |
| Server not starting | `sudo journalctl -u streetwise -n 50` |
| API key errors | `sudo nano /etc/streetwise.env` — no spaces or quotes around values |
| Data not showing | Run `deploy\upload_data.bat` |
| Token rejected (403) | Check `STREETWISE_TOKEN` in `/etc/streetwise.env`; restart after any change |
| Dashboard loads but shows Error | Token has special chars (`#` `+`); use only letters-numbers-hyphens |
| Old token still works | Browser has session cookie — test in incognito to verify new token |
| Both tokens work | Session cookie from old login persists; open incognito to test cleanly |

---

## Security Notes

- Your dashboard URL includes a secret token — anyone with the full URL can access it
- Do not share the URL publicly
- Port 22 (SSH) is open — your SSH key is the only protection; keep the `.key` file safe
- Keep the OS updated periodically:
  ```bash
  sudo apt update && sudo apt upgrade -y
  ```

---

## Next Steps (Optional, Later)

- **Custom domain** — point a domain name at your IP, then enable HTTPS with Let's Encrypt (certbot)
- **nginx reverse proxy** — run on port 80/443 instead of 5000
- **Automatic data sync** — set up a cron job to pull data on a schedule
- **Migrate to Ampere A1** — when free slots appear in Frankfurt, run `setup_oracle.sh` on the new VM and delete the E2.1.Micro (10-minute migration)

---

## Session Summary — dev_02 Setup (2026-03-25)

### What Was Built

| Item | Detail |
|---|---|
| Oracle Cloud VM | VM.Standard.E2.1.Micro, Ubuntu 22.04, Frankfurt (`eu-frankfurt-1`) |
| Public IP | `79.76.99.90` |
| Dashboard URL | `http://79.76.99.90:5000/?token=<your-token>` |
| Auto-start | systemd service — survives reboots |
| Token auth | `STREETWISE_TOKEN` in `/etc/streetwise.env`; session cookie set on first login |
| Chrome extension | Server URL + token fields added to popup; all API calls include token |
| Data sync | `deploy\upload_data.bat` — one double-click push from Windows |

### Discussion — Issues Encountered and How They Were Resolved

**1. Ampere A1 not available in Frankfurt**
All three Availability Domains (AD-1, AD-2, AD-3) showed no free Ampere slots.
Used `VM.Standard.E2.1.Micro` (Always Free x86, 1 OCPU, 1 GB RAM) as the fallback.
The setup script was updated to create a 2 GB swap file before pip install runs — without swap,
pip can OOM-kill on a 1 GB machine.

**2. Setup script returned 404**
The GitHub repo was private. `raw.githubusercontent.com` returns HTTP 404 for private repos —
not a permissions error, just a 404 with no explanation.
Fixed by making the repo public: GitHub → Settings → Danger Zone → Change visibility → Make public.

**3. `icacls` failed with "account name not found" on German Windows**
The `%username%` environment variable did not expand correctly when passed to `icacls` in the
German locale. Fixed by using the literal Windows username `vasanthaganesh.arulm` directly in the command.

**4. Port 5000 not reachable — three layers of firewall**
Layer 1: Oracle Cloud Security List — added the rule to the **wrong VCN** (there were two VCNs
in the account). Fixed by navigating from Compute → Instance → Primary VNIC → Subnet, which
always leads to the correct security list.
Layer 2: Ubuntu UFW — was correctly configured by the setup script (port 5000 open).
Layer 3: Oracle's default `iptables` rules — the INPUT chain had a `REJECT all` rule at line 5,
which blocked all traffic before UFW rules (lines 6–11) were ever evaluated. UFW adds its rules
after the REJECT, so UFW alone is not enough on Oracle Cloud images. Fixed by inserting an ACCEPT
rule at position 5: `sudo iptables -I INPUT 5 -p tcp --dport 5000 -j ACCEPT`, then saving with
`iptables-persistent` to survive reboots.

**5. Token with special characters (`#`, `+`) broke the URL**
The token `do-dan-WET234#0hgt_dfgrj` caused two problems: the browser treats `#` as a URL fragment
delimiter and strips everything after it (so the server only received `do-dan-WET234`), and `+`
is decoded as a space in query strings. Rule: tokens must use only letters, numbers, and hyphens.

**6. Dashboard loaded but showed "Error — check console"**
The token auth middleware blocked the dashboard's own internal API calls. The page itself loaded
correctly (token in the URL), but `widget.html` makes subsequent `fetch()` calls to `/api/data`,
`/api/status` etc. without any token — these all got 403. Fixed with session cookies: on the first
request with a valid token, the server sets `session['auth'] = True`. All subsequent API calls
from that browser tab carry the session cookie and are automatically authenticated.

**7. Both old and new token appeared to work simultaneously**
After changing the token on the server, `barrons2026` still appeared to work. The reason: the
browser retained the session cookie from the initial login. Even though the server's token had
changed, the session cookie was still valid (it contains `auth=True`, signed with the Flask
secret key). The old cookie only becomes invalid when the secret key changes (which happens when
`STREETWISE_TOKEN` is updated and the service is restarted). To test token enforcement cleanly,
always use an incognito window — it starts with no cookies.

**8. Chrome extension not configured for Oracle Cloud**
The extension's `popup.html` had `http://localhost:5000` hardcoded as the server URL, and there
was no token field at all. All API calls in `popup.js` (`/api/status`, `/api/sources`,
`/api/delete-episode`) and `progress.js` (`/api/ingest-page`) made requests without any token.
Fixed by: adding a token input field to the popup; adding `getToken()` and `authUrl()` helper
functions; updating all fetch calls to use `authUrl()`; passing the token through the job object
to the progress window; saving/restoring token from `chrome.storage.local`.

### Key Commands Reference

```bash
# Check server status
sudo systemctl status streetwise

# View live logs
sudo journalctl -u streetwise -f

# Edit API keys / token
sudo nano /etc/streetwise.env

# Restart after config change
sudo systemctl restart streetwise

# Pull latest code from GitHub
bash /opt/streetwise/deploy/update.sh

# Fix iptables port 5000 (if blocked after reboot)
sudo iptables -I INPUT 5 -p tcp --dport 5000 -j ACCEPT
sudo netfilter-persistent save
```

```cmd
REM Windows — push local data to server
deploy\upload_data.bat

REM Windows — pull server data to local
deploy\download_data.bat

REM Windows — trigger server code update
deploy\remote_update.bat
```
