#!/usr/bin/env bash
# Loads the wiegand-agent firmware onto the Uno from the Pi (no PC needed).
# Plug the Uno into the Pi by USB, then run:  ./flash-uno.sh
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"

if ! command -v arduino-cli >/dev/null; then
  echo "==> Installing arduino-cli (one time)"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR="$HOME/.local/bin" sh
fi

echo "==> Making sure the Uno board package is installed"
arduino-cli core update-index >/dev/null
arduino-cli core install arduino:avr

PORT="$(ls /dev/serial/by-id/* 2>/dev/null | head -1 || true)"
[ -z "$PORT" ] && PORT="$(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null | head -1 || true)"
if [ -z "$PORT" ]; then
  echo "No Uno found on USB. Check the cable and try again."; exit 1
fi
echo "==> Uno found on $PORT"

echo "==> Pausing the simulator service while uploading"
sudo systemctl stop wiegand-sim 2>/dev/null || true

arduino-cli compile --fqbn arduino:avr:uno wiegand-agent
arduino-cli upload -p "$PORT" --fqbn arduino:avr:uno wiegand-agent

echo "==> Starting the simulator service again"
sudo systemctl start wiegand-sim 2>/dev/null || true
echo "Done. The dashboard should show 'Uno connected (fw 1.0)' within a few seconds."
