#!/usr/bin/env bash
# Stop only this checkout's recorded production launcher.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec .venv/bin/python -B - <<'PY'
import os
import signal
import time
from pathlib import Path

root = Path.cwd().resolve()
pid_file = root / 'activity/logs/production/launcher.pid'
if not pid_file.exists():
    print('No recorded background launcher. Stop a foreground launcher with Ctrl+C.')
    raise SystemExit(0)
try:
    pid = int(pid_file.read_text().strip())
    if pid <= 1:
        raise ValueError('Invalid launcher PID')
except ValueError:
    raise SystemExit('Invalid launcher.pid; no process was signalled.')
proc = Path('/proc') / str(pid)

def matches():
    try:
        args = (proc / 'cmdline').read_bytes().split(b'\0')
        return (proc.stat().st_uid == os.getuid()
                and (proc / 'cwd').resolve() == root
                and b'activity/run_stack.py' in args
                and b'--environment' in args and b'production' in args)
    except FileNotFoundError:
        return False

if not matches():
    raise SystemExit('Recorded PID is no longer this production launcher; no process was signalled.')
os.kill(pid, signal.SIGTERM)
print(f'Stopping production launcher {pid} and its owned components...')
for _ in range(60):
    if not matches():
        pid_file.unlink(missing_ok=True)
        print('Launcher stopped. Previously reused services remain running.')
        break
    time.sleep(0.5)
else:
    raise SystemExit('Shutdown is taking longer than expected. Check activity/logs/production/launcher.log; no force kill was sent.')
PY
