"""Fixed stage labels only; never include exception text or credentials."""
from contextlib import contextmanager

STAGES = frozenset({
    'game_tcp_connect', 'game_seed', 'game_session_key', 'game_connect_reply',
    'account_entity', 'sdk_login_reply', 'avatar_reconnect', 'avatar_token',
    'protected_api', 'steam_ticket', 'sdk_exchange',
})


@contextmanager
def stage(label):
    if label not in STAGES:
        raise ValueError('Unsupported authentication diagnostic stage')
    try:
        yield
    except Exception as error:
        # Keep the original exception type: only SavedLoginExpired permits renewal.
        if getattr(error, 'auth_stage', None) not in STAGES:
            error.auth_stage = label
        raise


def label(error):
    value = getattr(error, 'auth_stage', None)
    return value if value in STAGES else None
