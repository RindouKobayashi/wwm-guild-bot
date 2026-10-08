#!/usr/bin/env bash
# Run as the bot's Linux user, not root. Does not start or restart the bot.
set -euo pipefail
bot_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "$EUID" == 0 ]]; then echo 'Run as the bot user, without sudo.' >&2; exit 1; fi
if [[ "$bot_root" == *'"'* || "$bot_root" == *'%'* || "$bot_root" == *$'\n'* ]]; then echo 'Unsupported characters in deployment path.' >&2; exit 1; fi
test -x "$bot_root/.venv/bin/python" || { echo 'Create/install the bot Linux .venv first.' >&2; exit 1; }
test -f "$bot_root/activity/.env.production" || { echo 'Copy activity/.env.production.example to activity/.env.production and fill Goose Overlord OAuth values locally.' >&2; exit 1; }
chmod 600 "$bot_root/activity/.env.production"
"$bot_root/.venv/bin/python" -B -c 'import aiohttp, dotenv'
test -f "$bot_root/activity/public/discord-sdk.js"
test -f "$bot_root/activity/public/catalogue.json"
service_file="$(mktemp)"
trap 'rm -f -- "$service_file"' EXIT
cat > "$service_file" <<EOF
[Unit]
Description=WWM Discord Activity
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory="$bot_root"
ExecStart=/bin/bash "$bot_root/activity/deploy/run-discord.sh"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$service_file" /etc/systemd/system/wwm-activity.service
sudo systemctl daemon-reload
sudo systemctl enable --now wwm-activity.service
sudo systemctl status --no-pager wwm-activity.service
