#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$EUID" == 0 ]]; then echo 'Run as your normal bot user, without sudo.' >&2; exit 1; fi
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
if [[ "${1:-}" != '--check' ]]; then
    .venv/bin/python -m pip install -r requirements-auth.txt
    if [[ -f "${WWM_AUTH_CONTEXT:-.wwm-auth/context.json}" ]]; then
        auth_folder="$(dirname -- "${WWM_AUTH_CONTEXT:-.wwm-auth/context.json}")"
        if [[ -f "$auth_folder/steam-headless.json" && -f "$auth_folder/sdk-renewal.json" ]]; then
            command -v dotnet >/dev/null || { echo 'Headless renewal requires the .NET 8 SDK.' >&2; exit 1; }
            dotnet restore wwm_auth/headless_steam/HeadlessSteam.csproj --locked-mode --nologo
            dotnet build wwm_auth/headless_steam/HeadlessSteam.csproj -c Release --no-restore --nologo
        fi
    fi
fi
exec .venv/bin/python -m wwm_auth
