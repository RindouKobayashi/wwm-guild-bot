#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
if [[ "${EUID}" == 0 ]]; then echo 'Run as your normal bot user, not root.' >&2; exit 1; fi
case "${1:-}" in setup|probe) ;; *) echo 'Usage: bash wwm_auth/headless_steam/run.sh setup|probe' >&2; exit 2;; esac
command -v dotnet >/dev/null || { echo 'Install the .NET 8 SDK for your Pi architecture first.' >&2; exit 1; }
dotnet restore wwm_auth/headless_steam/HeadlessSteam.csproj --locked-mode --nologo
dotnet build wwm_auth/headless_steam/HeadlessSteam.csproj -c Release --no-restore --nologo
exec dotnet wwm_auth/headless_steam/bin/Release/net8.0/HeadlessSteam.dll "$1" "${WWM_AUTH_CONTEXT:-$PWD/.wwm-auth/context.json}"
