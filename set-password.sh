#!/usr/bin/env bash
# Sets (or clears) the dashboard password. User name is: admin
set -euo pipefail
cd "$(dirname "$0")"
read -r -s -p "New dashboard password (leave blank to turn the password off): " PW; echo
mkdir -p data
PW="$PW" python3 - <<'EOF'
import json, os
p = "data/config.json"
c = json.load(open(p)) if os.path.exists(p) else {}
c["password"] = os.environ["PW"]
json.dump(c, open(p, "w"), indent=2)
EOF
sudo systemctl restart wiegand-sim 2>/dev/null || true
[ -n "$PW" ] && echo "Password set. Log in as user 'admin'." || echo "Password turned off."
