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
        print('Private test-account context and dependencies OK. No Steam or MITM needed.')
        if args.fetch:
            from .service import get_token
            get_token(force=True)
            print('Token retrieved and protected own-character lookup passed. Token text not printed.')
        else:
            print('Offline check only; no login or API requests sent.')
        return 0
    except Exception as exc:
        print('Not ready: ' + type(exc).__name__ + '. Check private context, permissions and dependencies; see wwm_auth/README.md.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
