import argparse
import json
import os
from urllib.parse import urlsplit
from aiohttp import web
from .setup import CONFIG, configure, inspect
from .server import Panel, create_app
from .supervisor import BOT, FileLock

def main():
    parser = argparse.ArgumentParser(description='Private WWM control panel (manual startup)')
    parser.add_argument('--environment', choices=['test','production'], required=True)
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--check', action='store_true', help='Validate panel and stack configuration without starting either')
    parser.add_argument('--start-stack', action='store_true', help='Start the bot stack under panel ownership on launch')
    parser.add_argument('--owner', help='Tailscale user login; inferred from device owner on initial setup')
    args = parser.parse_args()
    os.chdir(BOT)
    if args.setup:
        print('Private panel configured: ' + configure(args.owner))
        print('Setup completed. Run the panel launcher to start it; the bot is not started.')
        return
    if not CONFIG.exists():
        raise RuntimeError('First run: add --setup to configure your owner identity and private route.')
    config = json.loads(CONFIG.read_text())
    other = config.get('other_url', '')
    if other:
        parsed = urlsplit(other)
        tailnet = config['hostname'].split('.', 1)[1]
        if (parsed.scheme != 'https' or parsed.port != 8443 or not (parsed.hostname or '').endswith('.'+tailnet)
                or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise RuntimeError('other_url must be the other machine’s HTTPS URL on this tailnet, port 8443.')
    _, _, hostname, configured = inspect()
    if not configured or hostname != config['hostname']:
        raise RuntimeError('Private route or hostname changed. Rerun --setup; panel not started.')
    from activity.run_stack import configuration
    configuration(args.environment)
    if args.check:
        print('Panel and stack configuration OK: https://' + hostname + ':8443', flush=True)
        return
    with_lock = FileLock(BOT/'control_panel'/'data'/'panel.lock')
    try:
        panel = Panel(args.environment, config['owner'], hostname, config.get('other_url', ''))
        from .incidents import Monitor
        panel.monitor = Monitor(config.get('incident_thresholds'))
        panel.start_stack = args.start_stack
        print('Open https://' + hostname + ':8443 through Tailscale.', flush=True)
        print('Starting bot stack under panel ownership.' if args.start_stack else 'Panel only; start the bot from the dashboard when ready.', flush=True)
        web.run_app(create_app(panel), host='127.0.0.1', port=8770, access_log=None)
    finally:
        with_lock.close()

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
