#!/usr/bin/env bash
# install.sh — register the dispatcher app as a systemd service
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVC_NAME="dispatcher"
SVC_FILE="/etc/systemd/system/${SVC_NAME}.service"
SVC_USER="$(whoami)"

# Prefer a local venv if present, otherwise fall back to system python3
if [[ -x "${APP_DIR}/venv/bin/python3" ]]; then
    PYTHON="${APP_DIR}/venv/bin/python3"
else
    PYTHON="$(command -v python3)"
fi

echo "=== Dispatcher service installer ==="
printf "  Directory : %s\n" "$APP_DIR"
printf "  Python    : %s\n" "$PYTHON"
printf "  User      : %s\n" "$SVC_USER"
echo ""

# Verify Flask is available
if ! "$PYTHON" -c "import flask" 2>/dev/null; then
    echo "ERROR: Flask not found. Install it first:"
    echo "  pip3 install flask"
    exit 1
fi

# Write the systemd unit file
sudo tee "$SVC_FILE" > /dev/null <<SERVICE
[Unit]
Description=Radio Dispatcher Console
Documentation=https://github.com/mostlychris/dispatcher
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON} ${APP_DIR}/app.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=dispatcher
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

echo "Service unit written to ${SVC_FILE}"

# Sudoers — the app calls 'sudo systemctl restart ...' for radio services.
# Grant the service user passwordless access for exactly those commands.
SUDOERS_FILE="/etc/sudoers.d/dispatcher"
SUDOERS_TMP="$(mktemp)"

# Cover both /usr/bin and /bin (some distros symlink one to the other)
cat > "$SUDOERS_TMP" <<SUDOERS
# Dispatcher — passwordless systemctl for radio service management
${SVC_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart stfu.service
${SVC_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart mmdvm_bridge.service
${SVC_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart analog_bridge.service
${SVC_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart stfu.service
${SVC_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart mmdvm_bridge.service
${SVC_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart analog_bridge.service
SUDOERS

if sudo visudo -c -f "$SUDOERS_TMP" 2>/dev/null; then
    sudo cp "$SUDOERS_TMP" "$SUDOERS_FILE"
    sudo chmod 440 "$SUDOERS_FILE"
    echo "Sudoers rules written to ${SUDOERS_FILE}"
else
    echo "WARNING: sudoers validation failed — skipping. The app's restart buttons"
    echo "         may not work until you add the rules manually."
fi
rm -f "$SUDOERS_TMP"

# Reload systemd, enable at boot, and start now
sudo systemctl daemon-reload
sudo systemctl enable "${SVC_NAME}.service"
sudo systemctl restart "${SVC_NAME}.service"

echo ""
echo "=== Done ==="
echo ""
sudo systemctl status "${SVC_NAME}.service" --no-pager -l
echo ""
echo "Useful commands:"
echo "  Follow logs  : journalctl -u ${SVC_NAME} -f"
echo "  Stop service : sudo systemctl stop ${SVC_NAME}"
echo "  Start service: sudo systemctl start ${SVC_NAME}"
echo "  Remove       : bash ${APP_DIR}/uninstall.sh"
