#!/usr/bin/env bash
# Installs the Wiegand Simulator controller as a service that starts at boot.
# Run from this folder:  ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$USER}"

# If the repo was cloned as root (e.g. after "sudo su"), hand it to the service user,
# otherwise the service can't create its data folder and exits on start.
echo "==> Making sure $RUN_USER owns $(dirname "$DIR")"
sudo chown -R "$RUN_USER": "$(dirname "$DIR")"

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

# Dashboard port (8080 unless changed in data/config.json)
PORT="$(python3 -c "import json;print(json.load(open('data/config.json')).get('http_port',8080))" 2>/dev/null || echo 8080)"

# If the ufw firewall is on (it is on Qumulex servers), open the dashboard port to the local network only.
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  IFACE="$(ip -o -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}')"
  CIDR="$(ip -o -4 addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)"
  NET="$(python3 -c "import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)" "$CIDR" 2>/dev/null || true)"
  if [ -n "$NET" ]; then
    echo "==> Firewall is on: allowing port $PORT from $NET"
    sudo ufw allow from "$NET" to any port "$PORT" proto tcp comment 'Wiegand simulator'
  else
    echo "==> Firewall is on, but the local network couldn't be detected. Open the port with:"
    echo "    sudo ufw allow from <your-subnet> to any port $PORT proto tcp"
  fi
fi
sleep 3
sudo systemctl --no-pager --lines=5 status wiegand-sim || true

IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done. Open the dashboard at:"
echo "   http://$(hostname).local:$PORT   or   http://$IP:$PORT"
echo "If the Uno doesn't have the wiegand-agent firmware yet, run ./flash-uno.sh"
