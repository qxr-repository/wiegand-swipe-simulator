#!/usr/bin/env bash
# Installs the Wiegand Simulator controller as a service that starts at boot.
# Run from this folder:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$USER}"

echo "==> Installing Python packages"
sudo apt-get update -q
sudo apt-get install -y -q python3-flask python3-serial

echo "==> Letting $RUN_USER use the USB serial port"
sudo usermod -aG dialout "$RUN_USER"

echo "==> Creating the wiegand-sim service"
sudo tee /etc/systemd/system/wiegand-sim.service >/dev/null <<EOF
[Unit]
Description=QXR Wiegand Swipe Simulator controller
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now wiegand-sim
sleep 3
sudo systemctl --no-pager --lines=5 status wiegand-sim || true

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Open the dashboard at:"
echo "   http://$(hostname).local:8080   or   http://$IP:8080"
echo "If the Uno doesn't have the wiegand-agent firmware yet, run ./flash-uno.sh"
