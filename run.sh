#!/usr/bin/env bash
# Start the private panel and its production bot stack in the background.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
    echo 'Missing .venv. Run: bash "Setup Raspberry Pi.sh"' >&2
    exit 1
fi

# Fail visibly on configuration errors before detaching.
bash "Start WWM Bot and Activity.sh" --check
log_dir="activity/logs/production"
mkdir -p -- "$log_dir"
nohup bash "Start WWM Bot and Activity.sh" >> "$log_dir/launcher.log" 2>&1 < /dev/null &
stack_pid=$!
sleep 2
if ! kill -0 "$stack_pid" 2>/dev/null; then
    echo 'Startup stopped. Recent launcher output:' >&2
    tail -n 25 "$log_dir/launcher.log" >&2
    exit 1
fi
printf '%s\n' "$stack_pid" > "$log_dir/launcher.pid"
printf 'Production launcher started in background (PID %s).\n' "$stack_pid"
echo 'Startup progress: tail -f activity/logs/production/launcher.log'
echo 'Bot, Activity and tunnel logs: activity/logs/production/'
echo 'The private control panel URL is printed in launcher.log (HTTPS port 8443).'
echo 'Open it from your Tailscale-connected computer or phone; no Pi desktop browser is required.'
echo 'The panel stays available if a bot component fails; inspect its service states and operation history.'
echo 'Stop this launcher and its owned processes: kill -TERM '"$stack_pid"
