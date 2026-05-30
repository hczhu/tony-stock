#!/bin/bash
set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$SCRIPT_DIR/build-tony-stock.sh"

sudo cp "$SCRIPT_DIR/tony-stock.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tony-stock
sudo systemctl restart tony-stock
