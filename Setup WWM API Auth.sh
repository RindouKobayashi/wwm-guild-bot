#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$EUID" == 0 ]]; then echo 'Run as your normal bot user, without sudo.' >&2; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if [[ "${1:-}" != '--check' ]]; then
    .venv/bin/python -m pip install -r requirements-auth.txt
fi
exec .venv/bin/python -m wwm_auth
