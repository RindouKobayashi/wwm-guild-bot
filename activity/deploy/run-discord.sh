#!/usr/bin/env bash
set -euo pipefail
bot_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$bot_root"
exec "$bot_root/.venv/bin/python" -B activity/server.py --discord --profile production --port 8768
