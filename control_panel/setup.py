"""Inspect and configure only the private panel's dedicated Serve port."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from .supervisor import run_json, BOT

CONFIG = BOT/'control_panel'/'local.json'

def tailscale():
    path = Path('C:/Program Files/Tailscale/tailscale.exe') if os.name == 'nt' else Path('/usr/bin/tailscale')
    found = str(path) if path.exists() else shutil.which('tailscale')
    if not found:
        raise RuntimeError('Install and sign in to Tailscale first.')
    return found

def route_state(routes, hostname):
    found = []
    for candidate in [routes, *routes.get('Foreground', {}).values()]:
        key = hostname + ':8443'
        web = candidate.get('Web', {}).get(key)
        public = candidate.get('AllowFunnel', {}).get(key) is True
        tcp = candidate.get('TCP', {}).get('8443')
        if public:
            raise RuntimeError('Port 8443 has public Funnel enabled. Disable that specific route before panel setup.')
        if web is not None or tcp:
            if not web or web.get('Handlers') != {'/': {'Proxy': 'http://127.0.0.1:8770'}}:
                raise RuntimeError('Port 8443 belongs to another route. No Tailscale settings were changed.')
            found.append(True)
    return bool(found)

def inspect():
    executable = tailscale()
    status = run_json([executable, 'status', '--json'])
    if status.get('BackendState') != 'Running':
        raise RuntimeError('Sign in to Tailscale before starting the panel.')
    hostname = status.get('Self', {}).get('DNSName', '').rstrip('.')
    if not hostname.endswith('.ts.net'):
        raise RuntimeError('Enable Tailscale HTTPS and MagicDNS for this device.')
    routes = run_json([executable, 'serve', 'status', '--json'])
    configured = route_state(routes, hostname)
    return executable, status, hostname, configured

def configure(owner=None):
    executable, status, hostname, configured = inspect()
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', 8770))
        except OSError:
            raise RuntimeError('Local port 8770 is busy. Stop its listener before setup.') from None
    previous = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    user_id = str(status.get('Self', {}).get('UserID', ''))
    identity = status.get('User', {}).get(user_id, {}).get('LoginName')
    owner = owner or previous.get('owner') or identity
    if not owner or '@' not in owner:
        raise RuntimeError('Cannot infer your owner login. Run setup with --owner your-tailscale-login.')
    if not configured:
        result = subprocess.run([executable, 'serve', '--bg', '--https=8443', 'http://127.0.0.1:8770'],
                                capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('Could not configure private Serve. Check Tailscale HTTPS consent and serving permissions; rerun setup after resolving them.')
        if not inspect()[3]:
            raise RuntimeError('Private route verification failed; panel was not started.')
    CONFIG.write_text(json.dumps(dict(previous, owner=owner, hostname=hostname), indent=2)+'\n')
    if os.name != 'nt':
        CONFIG.chmod(0o600)
    return 'https://' + hostname + ':8443'
