"""Explicit one-attempt Windows TEST token launcher; never starts WWM or Discord."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import argparse

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'output/test-steam-20261018'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--without-steam', action='store_true', help='Reuse the verified saved test-game login; no Steam/SDK calls')
    args = parser.parse_args()
    executable = shutil.which('mitmdump')
    if not executable:
        print('mitmdump is missing. Use the existing mitmproxy installation on this PC.')
        return 1
    invocation = uuid.uuid4().hex
    runs = ROOT / 'provider-runs'
    runs.mkdir(parents=True, exist_ok=True)
    lock = ROOT / 'native-provider.lock'
    try:
        handle = lock.open('x')
    except FileExistsError:
        print('Another provider run is active, or its lock needs checking: ' + str(lock))
        return 1
    with handle:
        handle.write(invocation)
    log = runs / (invocation + '.private.log')
    if args.without_steam:
        print('One TEST-account attempt using the verified saved login. No Steam or SDK login calls.')
        print('May reconnect a lingering TEST session. Saved login lifetime is not established. No game RAM access.')
    else:
        print('One TEST-account attempt. Steam must be signed into the saved test account; WWM can stay closed.')
        print('Account mismatch stops before requesting a ticket or SDK login. No game RAM access.')
        print('After validation, renews private .wwm-auth/context.json and backs up the old login. Never commit these files.')
    env = os.environ.copy()
    env.update(WWM_TEST_API_TOKEN_EXECUTE='1', WWM_TEST_API_TOKEN_RETRY='native-provider',
               WWM_TEST_PROVIDER_INVOCATION=invocation, SSLKEYLOGFILE='')
    if args.without_steam:
        env.update(WWM_SAVED_TEST_LOGIN_EXECUTE='1', WWM_SAVED_TEST_LOGIN_DIAGNOSTIC='provider')
    else:
        env['WWM_EXPORT_FRESH_TEST_CONTEXT'] = '1'
    addon = 'probe_saved_test_game_login.py' if args.without_steam else 'bootstrap_test_api_token.py'
    try:
        with log.open('w', encoding='utf-8') as output:
            result = subprocess.run([executable, '-n', '-q', '-s', str(HERE / addon)],
                cwd=HERE.parents[1], env=env, stdout=output, stderr=subprocess.STDOUT, timeout=75)
        path = ROOT / ('saved-game-login-provider-result.redacted.json' if args.without_steam else 'api-token-bootstrap-result.redacted.json')
        report = json.loads(path.read_text()) if path.exists() else {}
        if report.get('invocation_id') != invocation:
            print('No fresh result was produced. Private diagnostic log: ' + str(log))
            return 1
        (runs / (invocation + '.redacted.json')).write_text(json.dumps(report, indent=2))
        if result.returncode or report.get('status') != 'test_api_token_obtained_and_validated':
            print('Stopped: ' + str(report.get('steam_status') or report.get('error_reason') or report.get('status')))
            if 'game_login_error_code' in report:
                print('Game login code: ' + str(report['game_login_error_code']))
            print('Redacted result: ' + str(runs / (invocation + '.redacted.json')))
            return 1
        print('Success: API token obtained; protected test-character lookup passed.')
        print('Encrypted token: ' + str(ROOT / 'api-token.user-dpapi.json'))
        print('Current Windows user only. Token is not printed or installed into the bot.')
        if report.get('portable_context_exported'):
            print('Fresh portable test login installed: .wwm-auth/context.json (private; old context backed up).')
            print('Verify without Steam: .venv\\Scripts\\python.exe -m wwm_auth --fetch')
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print('Provider stopped: ' + type(exc).__name__ + '. Private log: ' + str(log))
        return 1
    finally:
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
