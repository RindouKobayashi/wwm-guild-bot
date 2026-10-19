"""One saved TEST game-login reuse attempt; never calls Steam or SDK login.

Uses only the hash-verified native test capture. Default is offline preflight.
Live mode uses the existing bounded native connection and own-player validator.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.append(str(HERE.parents[1] / '.venv/Lib/site-packages'))

import bootstrap_test_api_token as bootstrap
from test_native_login_context import load_native_login_report

RESULT_NAME = 'saved-game-login-reuse.redacted.json'


def saved_authentication(template, expected_uid, report):
    _, auth, _, _ = load_native_login_report(expected_uid, template[4])
    if not hmac.compare_digest(hashlib.sha256(str(auth.get('sdkuid', '')).encode()).hexdigest(), expected_uid):
        raise ValueError('Saved test authentication identity mismatch')
    if not isinstance(auth.get('sessionid'), str) or not auth['sessionid']:
        raise ValueError('Saved test game credential missing')
    report.update(authentication_source='verified_saved_test_game_login',
                  steam_api_called=False, fresh_steam_ticket_requested=False,
                  sdk_requests_attempted=0, saved_game_credential_reused=True)
    return copy.deepcopy(auth)


class Run:
    def running(self):
        from mitmproxy import ctx
        lock = bootstrap.ROOT / 'native-provider.lock'
        locked = False
        try:
            template, expected_uid, _, check = bootstrap.load_context()
            saved_authentication(template, expected_uid, {})
            if check is None:
                raise ValueError('Own-test-character validator missing')
            if os.environ.get('WWM_SAVED_TEST_LOGIN_EXECUTE') != '1':
                print(json.dumps({'status': 'saved_test_login_preflight_ok',
                                  'network_requests': 0, 'steam_api_called': False}))
                return
            diagnostic = os.environ.get('WWM_SAVED_TEST_LOGIN_DIAGNOSTIC') == '1'
            nested_snapshot = os.environ.get('WWM_SAVED_TEST_LOGIN_DIAGNOSTIC') == 'nested-snapshot'
            provider = os.environ.get('WWM_SAVED_TEST_LOGIN_DIAGNOSTIC') == 'provider'
            invocation = os.environ.get('WWM_TEST_PROVIDER_INVOCATION', '') if provider else uuid.uuid4().hex
            if provider:
                previous = json.loads((bootstrap.ROOT / 'saved-game-login-nested-snapshot.redacted.json').read_text())
                if (previous.get('status') != 'test_api_token_obtained_and_validated'
                        or previous.get('steam_api_called') is not False
                        or previous.get('sdk_requests_attempted') != 0
                        or previous.get('requested_test_player_returned') is not True
                        or not re.fullmatch(r'[0-9a-f]{32}', invocation)):
                    raise ValueError('Successful Steam-free baseline and launcher invocation required')
            if nested_snapshot:
                previous = json.loads((bootstrap.ROOT / 'saved-game-login-diagnostic.redacted.json').read_text())
                if (previous.get('owned_avatar_snapshot_received') is not True
                        or previous.get('steam_api_called') is not False
                        or previous.get('token_obtained') is not False):
                    raise ValueError('Prior missing snapshot-token diagnostic required')
            if diagnostic:
                previous = json.loads((bootstrap.ROOT / RESULT_NAME).read_text())
                if (previous.get('existing_test_avatar_reconnected') is not True
                        or previous.get('steam_api_called') is not False
                        or previous.get('sdk_requests_attempted') != 0
                        or previous.get('token_obtained') is not False):
                    raise ValueError('Prior Steam-free test reconnect required')
            # One attempt for this fixed capture, independent of current Steam login.
            if provider:
                if lock.read_text() != invocation:
                    raise ValueError('Provider launcher lock mismatch')
            else:
                with lock.open('x') as handle:
                    handle.write('saved-test-game-login')
                locked = True
            label = ('saved-game-login-provider-' + invocation if provider else
                     'saved-game-login-nested-snapshot' if nested_snapshot else
                     'saved-game-login-diagnostic' if diagnostic else 'saved-game-login-reuse')
            with (bootstrap.ROOT / (label + '.attempted.private.json')).open('x') as handle:
                json.dump({'attempt_consumed': True}, handle)
            os.environ.update(WWM_TEST_API_TOKEN_EXECUTE='1',
                              WWM_TEST_API_TOKEN_RETRY='native-provider',
                              WWM_TEST_PROVIDER_INVOCATION=invocation)
            bootstrap.run(authentication_provider=saved_authentication,
                          result_name='saved-game-login-provider-result.redacted.json' if provider else label + '.redacted.json')
        finally:
            if locked:
                lock.unlink(missing_ok=True)
            ctx.master.shutdown()


addons = [Run()]
