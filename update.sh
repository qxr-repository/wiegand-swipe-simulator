#!/usr/bin/env bash
# Pulls the latest version from GitHub and restarts the service.
# Your settings, tests, schedule and logs (in ./data) are kept.
set -euo pipefail
cd "$(dirname "$0")"
git pull
sudo systemctl restart wiegand-sim
echo "Updated and restarted. If wiegand-agent/ changed, also run ./flash-uno.sh"
