"""Offline check by default; --fetch makes one bounded test-account login attempt."""
import argparse
import importlib.metadata
import json
import os
from .context import context_path, load, ROOT


def main():
    # Match the bot's normal .env loading without importing settings/Discord.
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--renew-login', action='store_true', help='Renew the configured TEST SDK login through headless Steam and validate it')
    args = parser.parse_args()
    try:
        mode = os.environ.get('WWM_AUTH_MODE', 'legacy').strip().lower()
        if mode not in ('legacy', 'saved_login'):
            raise ValueError('Unsupported authentication mode')
        print('Bot authentication mode: ' + mode)
        if mode != 'saved_login':
            print('The bot provider is NOT enabled. Set WWM_AUTH_MODE=saved_login in the root .env; --fetch alone does not enable it.')
        context = load()
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
        key = load_pem_public_key(context['public_key_pem'].encode('ascii'))
        if not isinstance(key, RSAPublicKey) or key.key_size != 2048:
            raise ValueError('Unsupported public key')
        print('Private test-account context and dependencies OK. No desktop Steam or MITM needed.')
        directory = context_path().parent
        if (directory / 'steam-headless.json').is_file() and (directory / 'sdk-renewal.json').is_file():
            from .renewal import configuration, HELPER
            configuration(context)
            print('Automatic headless renewal configured.' if HELPER.is_file() else
                  'Headless renewal configured, but helper needs building on this device.')
        if args.fetch or args.renew_login:
            from .service import get_token
            get_token(force=True, renew_login=args.renew_login)
            print('Token retrieved and protected own-character lookup passed. Token text not printed.')
        else:
            print('Offline check only; no login or API requests sent.')
        return 0
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            print('Not ready: ' + str(exc))
            return 1
        print('Not ready: ' + type(exc).__name__ + '. Check private context, permissions and dependencies; see wwm_auth/README.md.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
