"""OFFLINE export of the verified TEST SDK template; no Steam/network requests."""
import base64
import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def export():
    from bootstrap_test_api_token import load_context
    from wwm_auth.context import load, context_path, write_private
    from wwm_auth.renewal import configuration, SDK_URL
    template, uid, _, _ = load_context()
    url, original, metadata, key, steam_id = template
    import hashlib
    context = load()
    if (url != SDK_URL or uid != context['expected_sdkuid_sha256']
            or hashlib.sha256(steam_id.encode()).hexdigest() != context['expected_steamid_sha256']):
        raise ValueError('SDK export test identity mismatch')
    request = copy.deepcopy(original)
    request.pop('sessionid', None)
    request.pop('gas_token', None)
    write_private(context_path().parent / 'sdk-renewal.json', dict(format=1, url=url,
        request=request, metadata=metadata, signing_key_base64=base64.b64encode(key).decode(),
        expected_sdkuid_sha256=uid, expected_steamid_sha256=context['expected_steamid_sha256']))
    configuration(context)


class HeadlessSdkConfigExport:
    def running(self):
        from mitmproxy import ctx
        try:
            export()
            print('Private TEST SDK renewal configuration exported; no network requests.')
        except Exception:
            print('SDK configuration export failed; no credentials printed.')
        finally:
            ctx.master.shutdown()


addons = [HeadlessSdkConfigExport()]
