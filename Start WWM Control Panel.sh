#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec .venv/bin/python -B -m control_panel --environment production "$@"
