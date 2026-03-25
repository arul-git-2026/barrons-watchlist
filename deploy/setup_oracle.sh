#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Streetwise — Oracle Cloud Ubuntu 22.04 Bootstrap
# Run once on a fresh VM:
#   bash setup_oracle.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="https://github.com/arul-git-2026/barrons-watchlist.git"
BRANCH="dev_02"
INSTALL_DIR="/opt/streetwise"
SERVICE_USER="ubuntu"
ENV_FILE="/etc/streetwise.env"
SERVICE_FILE="/etc/systemd/system/streetwise.service"

GREEN="\033[32m"; YELLOW="\033[33m"; CYAN="\033[36m"; RESET="\033[0m"
info()  { echo -e "${CYAN}▶ $*${RESET}"; }
ok()    { echo -e "${GREEN}✓ $*${RESET}"; }
warn()  { echo -e "${YELLOW}⚠ $*${RESET}"; }

# ── 1. System update + packages ───────────────────────────────────────────────
info "Updating system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git ufw curl

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
    info "Creating $ENV_FILE — you will fill in your keys next..."
    sudo tee "$ENV_FILE" > /dev/null <<'ENVEOF'
# Streetwise environment variables
# Fill in your real values, then: sudo systemctl restart streetwise

ANTHROPIC_API_KEY=sk-ant-REPLACE_ME
GEMINI_API_KEY=AIza-REPLACE_ME

# Secret token — anyone with this can access your dashboard
# Choose something memorable but not guessable, e.g. barrons2026
STREETWISE_TOKEN=REPLACE_ME
ENVEOF
    sudo chmod 600 "$ENV_FILE"
    ok "$ENV_FILE created"
else
    warn "$ENV_FILE already exists — skipping (edit it manually if needed)"
fi

# ── 5. Install systemd service ────────────────────────────────────────────────
info "Installing systemd service..."
sudo cp "$INSTALL_DIR/deploy/streetwise.service" "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable streetwise
ok "systemd service installed and enabled"

# ── 6. Firewall ───────────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
sudo ufw allow OpenSSH   > /dev/null
sudo ufw allow 5000/tcp  > /dev/null
sudo ufw --force enable  > /dev/null
ok "UFW: SSH (22) and port 5000 open"

# ── 7. Create data directory and placeholder files ────────────────────────────
info "Setting up data directory..."
touch "$INSTALL_DIR/streetwise_data.json" 2>/dev/null || true
# Initialise empty JSON array if file is empty
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
    warn "Service did not start — check logs: sudo journalctl -u streetwise -n 30"
    warn "Most likely cause: API keys not set in $ENV_FILE"
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
echo "  3. Upload your data from Windows:"
echo "       deploy\\upload_data.bat"
echo ""
echo "  4. Open your dashboard:"
echo "       http://$PUBLIC_IP:5000/?token=YOUR_TOKEN"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status streetwise        # check status"
echo "    sudo journalctl -u streetwise -f        # live logs"
echo "    sudo systemctl restart streetwise       # restart"
echo ""
