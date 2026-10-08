#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$EUID" == 0 ]]; then echo 'Run as your usual bot user, without sudo.' >&2; exit 1; fi
if [[ "${1:-}" == '--check' && ! -x .venv/bin/python ]]; then echo 'Linux .venv is missing; run setup without --check first.' >&2; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then
    echo 'Creating a Linux Python environment (your Windows .venv cannot be reused).'
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
if ! .venv/bin/python -c 'import dotenv, aiohttp, discord, msgpack' >/dev/null 2>&1; then
    echo 'Installing project dependencies into .venv.'
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -B activity/deploy/setup_pi.py "$@"
