#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Keep the old Activity-only escape hatch; the normal launcher includes the panel.
for option in "$@"; do
    if [[ "$option" == '--activity-only' ]]; then
        exec .venv/bin/python -B activity/run_stack.py --environment production "$@"
    fi
done
exec .venv/bin/python -B -m control_panel --environment production --start-stack "$@"
