"""Test-Steam-account-only SDK probe; offline preflight unless explicitly enabled.

No fallback to the old main-account template. No game connection or RAM access.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
from probe_fresh_sdk_login import prepare_template, exchange, response_summary
from probe_steam_ticket import request_ticket, DEFAULT_DLL
from check_sdk_client_signature import EXPECTED_CAPTURE

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'output/test-steam-20261018'


def validate_profile(profile, capture_bytes):
    digest = hashlib.sha256(capture_bytes).hexdigest()
    if (len(capture_bytes) > 64 * 1024 * 1024 or digest == EXPECTED_CAPTURE
            or not hmac.compare_digest(digest, profile['capture_sha256'])):
        raise ValueError('Test capture identity mismatch or forbidden main capture')
    if profile.get('main_account_capture_forbidden') is not True:
        raise ValueError('Main account exclusion required')


def validate_account(profile, account):
    digest = hashlib.sha256(account.encode()).hexdigest()
    if not hmac.compare_digest(digest, profile['test_sdkuid_sha256']):
        raise ValueError('Test account identity mismatch')


class Probe:
    def running(self):
        from mitmproxy import ctx, io
        try:
            profile = json.loads((ROOT / 'test-account-profile.private.json').read_text())
            source = Path(profile['capture_path']).resolve()
            if source.parent != (HERE / 'output/token-capture').resolve():
                raise ValueError('Capture outside private recording folder')
            validate_profile(profile, source.read_bytes())
            prepared = []
            with source.open('rb') as handle:
                for flow in io.FlowReader(handle).stream():
                    req = getattr(flow, 'request', None)
                    if (req is not None and req.pretty_host == 'mgbjp-h72na.unisdk.easebar.com'
                            and req.path.endswith('/sdk/uni_sauth')):
                        prepared.append(prepare_template(req))
                        res = getattr(flow, 'response', None)
                        if res is None or not response_summary(json.loads(res.content))['login_credential_present']:
                            raise ValueError('Saved successful response not recognized')
            if len(prepared) != 1:
                raise ValueError('Exactly one SDK template required')
            url, original, metadata, key, account = prepared[0]
            validate_account(profile, account)
            report = {'status': 'test_steam_sdk_preflight_ok', 'account_guard_required': True,
                      'main_account_template_used': False, 'sdk_requests_attempted': 0,
                      'steam_api_called': False, 'game_connection_started': False,
                      'game_memory_access': False, 'credential_values_saved': False}
            execute = os.environ.get('WWM_TEST_STEAM_SDK_EXECUTE') == '1'
            if execute:
                # Persistent one-shot latch before any ticket/network operation.
                # A consumed probe requires review rather than an automatic retry.
                with (ROOT / 'standalone-sdk-attempt.private.json').open('x') as marker:
                    json.dump({'status': 'attempt_consumed', 'capture_sha256': profile['capture_sha256']}, marker)
                report['steam_api_called'] = True
                result = request_ticket(DEFAULT_DLL, expected_steam_id=account,
                    consume_ticket=lambda ticket: exchange(ticket, url, original, metadata, key))
                report['steam_probe'] = result
                report.update(result.get('consumer_result') or {'status': result['status']})
            output = ROOT / ('standalone-sdk-result.redacted.json' if execute else 'standalone-sdk-preflight.redacted.json')
            output.write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            ctx.master.shutdown()


addons = [Probe()]
