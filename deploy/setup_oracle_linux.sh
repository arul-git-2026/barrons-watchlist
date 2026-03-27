#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Streetwise — Oracle Cloud Oracle Linux Bootstrap
# Works with Oracle Linux 8 and 9 (A1.Flex ARM64 and E2.1.Micro x86)
#
# Default SSH user on Oracle Linux is 'opc' (NOT 'ubuntu')
# SSH in with:  ssh -i your-key.key opc@YOUR_SERVER_IP
#
# Run once on a fresh VM:
#   bash setup_oracle_linux.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="https://github.com/arul-git-2026/barrons-watchlist.git"
BRANCH="dev_02"
INSTALL_DIR="/opt/streetwise"
SERVICE_USER="opc"
ENV_FILE="/etc/streetwise.env"
SERVICE_FILE="/etc/systemd/system/streetwise.service"

GREEN="\033[32m"; YELLOW="\033[33m"; CYAN="\033[36m"; RESET="\033[0m"
info()  { echo -e "${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $*${RESET}"; }

# ── 0. Swap (only for small instances — E2.1.Micro has 1 GB, A1.Flex has 6+ GB)
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
if [ "$TOTAL_RAM_MB" -lt 1800 ] && [ ! -f /swapfile ]; then
    info "Creating 2 GB swap file (1 GB RAM instance detected)..."
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile > /dev/null
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
    echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf > /dev/null
    sudo sysctl -p > /dev/null
    ok "Swap: 2 GB created and enabled"
elif [ "$TOTAL_RAM_MB" -ge 1800 ]; then
    ok "Sufficient RAM (${TOTAL_RAM_MB} MB) — swap not needed"
else
    ok "Swap already exists — skipping"
fi

# ── 1. System update + packages ───────────────────────────────────────────────
info "Updating system packages (this may take a few minutes)..."
sudo dnf update -y -q
sudo dnf install -y python3 python3-pip git curl
ok "System packages installed"

# ── 2. Clone / update repo ────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already exists — pulling latest $BRANCH..."
    sudo git -C "$INSTALL_DIR" fetch origin
    sudo git -C "$INSTALL_DIR" checkout "$BRANCH"
    sudo git -C "$INSTALL_DIR" pull origin "$BRANCH"
else
    info "Cloning repo into $INSTALL_DIR..."
    sudo git clone --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
ok "Repo ready at $INSTALL_DIR"

# ── 3. Python virtual environment + dependencies ──────────────────────────────
info "Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet flask flask-cors yfinance anthropic python-docx
ok "Python dependencies installed"

# ── 4. Create env file if it doesn't exist ────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    info "Creating $ENV_FILE — fill in your keys next..."
    sudo tee "$ENV_FILE" > /dev/null <<'ENVEOF'
# Streetwise environment variables
# Fill in your real values, then: sudo systemctl restart streetwise

ANTHROPIC_API_KEY=sk-ant-REPLACE_ME
GEMINI_API_KEY=AIza-REPLACE_ME

# Secret token — anyone with this can access your dashboard
# Use only letters, numbers, hyphens — no # + & % ? characters
STREETWISE_TOKEN=REPLACE_ME
ENVEOF
    sudo chmod 600 "$ENV_FILE"
    ok "$ENV_FILE created"
else
    warn "$ENV_FILE already exists — skipping (edit manually if needed)"
fi

# ── 5. Install systemd service (writes User=opc, not ubuntu) ──────────────────
info "Installing systemd service..."
sudo tee "$SERVICE_FILE" > /dev/null <<SVCEOF
[Unit]
Description=Streetwise Stock Dashboard
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/.venv/bin/python server.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=streetwise
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SVCEOF
sudo systemctl daemon-reload
sudo systemctl enable streetwise
ok "systemd service installed and enabled"

# ── 6. Firewall — firewalld (Oracle Linux uses this, not ufw) ─────────────────
info "Configuring firewalld..."
sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-service=ssh   2>/dev/null || true
sudo firewall-cmd --permanent --add-port=5000/tcp
sudo firewall-cmd --reload
ok "firewalld: SSH (22) and port 5000 open"

# Oracle Cloud injects an iptables REJECT rule that can block traffic even when
# firewalld is configured correctly. Insert an ACCEPT rule to be safe.
info "Patching Oracle Cloud iptables REJECT rule for port 5000..."
if ! sudo iptables -C INPUT -p tcp --dport 5000 -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 1 -p tcp --dport 5000 -j ACCEPT
    ok "iptables ACCEPT inserted at position 1"
else
    ok "iptables ACCEPT rule already present"
fi
# Persist iptables rules across reboots
if ! sudo dnf list installed iptables-services &>/dev/null 2>&1; then
    sudo dnf install -y -q iptables-services
fi
sudo service iptables save
sudo systemctl enable iptables
ok "iptables rules saved (will survive reboot)"

# ── 7. Create data directory and placeholder files ────────────────────────────
info "Setting up data directory..."
touch "$INSTALL_DIR/streetwise_data.json" 2>/dev/null || true
if [ ! -s "$INSTALL_DIR/streetwise_data.json" ]; then
    echo "[]" > "$INSTALL_DIR/streetwise_data.json"
fi
chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/streetwise_data.json" 2>/dev/null || true
ok "Data directory ready"

# ── 8. Start the service ──────────────────────────────────────────────────────
info "Starting Streetwise service..."
sudo systemctl start streetwise || true
sleep 2

if sudo systemctl is-active --quiet streetwise; then
    ok "Streetwise is RUNNING"
else
    warn "Service did not start — check: sudo journalctl -u streetwise -n 30"
    warn "Most likely cause: API keys not yet set in $ENV_FILE"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_SERVER_IP")

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo -e "${GREEN}  Setup complete!${RESET}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit your API keys and token:"
echo "       sudo nano $ENV_FILE"
echo ""
echo "  2. Restart the server after editing:"
echo "       sudo systemctl restart streetwise"
echo ""
echo "  3. Update your Windows batch files (SERVER_USER and SERVER_IP):"
echo "       In deploy\upload_data.bat, download_data.bat, remote_update.bat"
echo "       Change:  SET SERVER_USER=ubuntu  →  SET SERVER_USER=opc"
echo "       Change:  SET SERVER_IP=...       →  SET SERVER_IP=$PUBLIC_IP"
echo ""
echo "  4. Upload your data from Windows:"
echo "       deploy\upload_data.bat"
echo ""
echo "  5. Open your dashboard:"
echo "       http://$PUBLIC_IP:5000/?token=YOUR_TOKEN"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status streetwise        # check status"
echo "    sudo journalctl -u streetwise -f        # live logs"
echo "    sudo systemctl restart streetwise       # restart"
echo "    sudo firewall-cmd --list-ports          # verify firewall"
echo ""
