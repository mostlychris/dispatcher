#!/usr/bin/env bash
# uninstall.sh — remove the dispatcher systemd service
set -euo pipefail

SVC_NAME="dispatcher"

echo "Stopping and disabling ${SVC_NAME} service..."
sudo systemctl stop    "${SVC_NAME}.service" 2>/dev/null || true
sudo systemctl disable "${SVC_NAME}.service" 2>/dev/null || true

sudo rm -f "/etc/systemd/system/${SVC_NAME}.service"
sudo rm -f "/etc/sudoers.d/dispatcher"
sudo systemctl daemon-reload

echo "Done. ${SVC_NAME} service removed."
