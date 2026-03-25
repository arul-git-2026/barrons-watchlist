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
| **Availability domain** | any (leave default) |

**Image and shape** — this is the most important part:

1. Under **Image and shape**, click **Edit**
2. Click **Change image** → select **Ubuntu** → **22.04** → confirm
3. Click **Change shape**
   - Select **Ampere** (ARM processor)
   - Select **VM.Standard.A1.Flex**
   - Set **OCPUs: 2** and **Memory: 12 GB** (well within the Always Free 4 OCPU / 24 GB allowance)
   - Click **Select shape**

**Networking** — leave defaults (a VCN and subnet will be created automatically)

**SSH keys** — you need these to connect:

1. Select **Generate a key pair for me**
2. Click **Save private key** — this downloads `ssh-key-XXXX.key`
3. Store this file safely — you cannot get it again

### 2c. Launch

Click **Create**. The instance will show **Provisioning** for ~2 minutes, then **Running**.

---

## Part 3 — Note Your Public IP Address

1. Click on your instance name (`streetwise`)
2. Under **Instance information** → **Primary VNIC** → **Public IP address**
3. Copy this IP — you will use it everywhere below
   > Example: `140.238.211.99`

---

## Part 4 — Open Port 5000 in Oracle's Firewall

Oracle blocks all ports except 22 (SSH) by default. You need to open port 5000.

1. On your instance page, scroll to **Primary VNIC** → click the **subnet** link
2. Click **Default Security List for ...**
3. Click **Add Ingress Rules**
4. Fill in:

| Field | Value |
|---|---|
| Source CIDR | `0.0.0.0/0` |
| IP Protocol | TCP |
| Destination Port Range | `5000` |
| Description | `Streetwise dashboard` |

5. Click **Add Ingress Rules**

---

## Part 5 — Connect to Your Server

### On Windows — use PuTTY or Windows Terminal

**Option A: Windows Terminal / PowerShell (recommended)**

```powershell
# First, fix the key file permissions (Windows SSH requires this)
icacls "C:\path\to\ssh-key-XXXX.key" /inheritance:r /grant:r "%username%:(R)"

# Connect
ssh -i "C:\path\to\ssh-key-XXXX.key" ubuntu@YOUR_SERVER_IP
```

**Option B: PuTTY**

1. Download PuTTY from https://putty.org
2. Open PuTTYgen → Load your `.key` file → Save private key as `.ppk`
3. Open PuTTY → Host: `YOUR_SERVER_IP` → Port: `22`
4. Go to Connection → SSH → Auth → browse to your `.ppk` file
5. Click Open

You should see: `ubuntu@streetwise:~$`

---

## Part 6 — Run the Setup Script

Once connected via SSH, run these two commands:

```bash
# Download the setup script from your GitHub repo
curl -fsSL https://raw.githubusercontent.com/arul-git-2026/barrons-watchlist/dev_02/deploy/setup_oracle.sh -o setup_oracle.sh

# Run it
bash setup_oracle.sh
```

The script will:
- Install Python 3, pip, git
- Clone your repo into `/opt/streetwise`
- Install all Python dependencies
- Open port 5000 in Ubuntu's firewall (`ufw`)
- Create a systemd service so the server starts automatically on reboot
- Prompt you to enter your API keys

**It takes about 3–5 minutes.**

---

## Part 7 — Enter Your API Keys on the Server

The setup script creates `/etc/streetwise.env`. Edit it:

```bash
sudo nano /etc/streetwise.env
```

Fill in:
```
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
STREETWISE_TOKEN=choose-any-secret-word-here
```

For `STREETWISE_TOKEN` — pick anything memorable but not obvious, e.g. `barrons2026` or `my-dashboard-99`.

Save: `Ctrl+O` → Enter → `Ctrl+X`

Then restart the server:
```bash
sudo systemctl restart streetwise
```

---

## Part 8 — Upload Your Data to the Server

Back on your **Windows PC**, open a terminal and run:

```cmd
deploy\upload_data.bat
```

This copies `streetwise_data.json` (your tickers) to the server via SCP.

You will be prompted for the path to your SSH key and server IP the first time. After that, edit `deploy\upload_data.bat` directly to hardcode them.

---

## Part 9 — Access Your Dashboard

Open in any browser, from anywhere:

```
http://YOUR_SERVER_IP:5000/?token=YOUR_SECRET_TOKEN
```

Bookmark this URL.

**Chrome extension** — update the server URL in the extension popup to point to your Oracle Cloud IP instead of localhost.

---

## Part 10 — Check the Server is Running

From your SSH session:

```bash
# Check service status
sudo systemctl status streetwise

# Watch live logs
sudo journalctl -u streetwise -f

# Restart if needed
sudo systemctl restart streetwise
```

---

## Keeping Your Data in Sync

Whenever you add new tickers on your local machine and want to push them to the server:

```cmd
deploy\upload_data.bat
```

To pull data FROM the server to your local machine (e.g. if you added articles while away):

```cmd
deploy\download_data.bat
```

---

## Updating the Server Code

When you push changes to GitHub and want the server to pick them up:

```bash
# On the server (via SSH):
bash /opt/streetwise/deploy/update.sh
```

Or from your Windows PC:

```cmd
deploy\remote_update.bat
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Can't connect via SSH | Check your key file path; check the VM is Running in OCI console |
| Port 5000 not reachable | Re-check Part 4 (OCI Security List) AND `sudo ufw status` on server |
| Server not starting | `sudo journalctl -u streetwise -n 50` to see the last 50 log lines |
| API key errors | `sudo nano /etc/streetwise.env` → check for spaces or quotes around keys |
| Data not showing | Run `deploy\upload_data.bat` to push your local data |
| Token rejected (403) | Check `STREETWISE_TOKEN` matches in `/etc/streetwise.env` |

---

## Security Notes

- Your dashboard URL includes a secret token — anyone who knows the full URL can access it
- Do not share the URL publicly
- Port 22 (SSH) is open — your SSH key is your only protection; keep the `.key` file safe
- Oracle Cloud Free Tier VMs are real internet-connected machines — keep the OS updated:
  ```bash
  sudo apt update && sudo apt upgrade -y
  ```

---

## Next Steps (Optional, Later)

- **Custom domain** — point a domain name at your IP, then enable HTTPS with Let's Encrypt (certbot)
- **nginx reverse proxy** — run on port 80/443 instead of 5000
- **Automatic data sync** — set up a cron job to pull data on a schedule
