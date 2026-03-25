#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Streetwise — Pull latest code and restart the server
# Run on the Oracle Cloud VM:
#   bash /opt/streetwise/deploy/update.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

INSTALL_DIR="/opt/streetwise"
BRANCH="dev_02"

GREEN="\033[32m"; CYAN="\033[36m"; RESET="\033[0m"

echo -e "${CYAN}▶ Pulling latest code from $BRANCH...${RESET}"
git -C "$INSTALL_DIR" fetch origin
git -C "$INSTALL_DIR" checkout "$BRANCH"
git -C "$INSTALL_DIR" pull origin "$BRANCH"

echo -e "${CYAN}▶ Updating Python dependencies...${RESET}"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade flask flask-cors yfinance anthropic python-docx

echo -e "${CYAN}▶ Restarting service...${RESET}"
sudo systemctl restart streetwise
sleep 2

if sudo systemctl is-active --quiet streetwise; then
    echo -e "${GREEN}✓ Streetwise updated and running${RESET}"
    sudo systemctl status streetwise --no-pager -l | tail -5
else
    echo "Service failed to start — check logs:"
    sudo journalctl -u streetwise -n 30 --no-pager
    exit 1
fi
