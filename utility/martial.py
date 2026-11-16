"""Portable martial identities; no settings, network or extraction imports."""
import json
import os
from pathlib import Path
from .game_assets import current

REGISTRY = Path(__file__).resolve().parents[1] / 'assets/discord/martial-emojis.json'

LOCAL_REGISTRY = REGISTRY.parents[2] / 'data/martial-emojis.json'


def read_registry(public=REGISTRY, local=LOCAL_REGISTRY):
    result={'format':1,'applications':{},'environments':{}}
    for path in (public,local):
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            for app,rows in value.get('applications',{}).items():
                result['applications'].setdefault(app,{}).update(rows)
            result['environments'].update(value.get('environments',{}))
        except (OSError,ValueError,TypeError,AttributeError):continue
    return result


def reload_registry():
    global _registry
    _registry=read_registry()


reload_registry()


def identifier(value):
    if isinstance(value, bool):return None
    if isinstance(value, int):return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():return int(value) or None
    return None


def catalogue(legacy):
    rows = {int(k): dict(v, source='legacy_compatibility') for k,v in legacy.items()}
    for key, row in current().section('martial', {}).get('kungfu', {}).items():
        rows[int(key)] = {**rows.get(int(key), {}), **row, 'emoji':'', 'source':'game_assets'}
    return rows


def lookup(value, rows, application_id=None):
    key = identifier(value)
    if key is None:return None
    row = dict(rows.get(key) or {'name':f'Unknown kungfu ({key})','source':'unknown'})
    row.setdefault('emoji','')
    environment='production' if os.getenv('GITHUB_BRANCH','main')=='main' else 'test'
    application_id=application_id or _registry.get('environments',{}).get(environment)
    if application_id and row.get('icon_key'):
        try:
            registry=_registry
            emoji=registry.get('applications',{}).get(str(application_id),{}).get(row['icon_key'],{})
            if emoji.get('sha256')==row.get('icon_sha256') and str(emoji.get('id','')).isdigit():
                row['emoji']=f"<:{emoji['name']}:{emoji['id']}>"
        except (OSError, ValueError, TypeError):pass
    return row


def equipped(player):
    data=player.get('kongfu') if isinstance(player,dict) else None
    if not isinstance(data,dict):return []
    return [key for field in ('kongfu_main','kongfu_sub') if (key:=identifier(data.get(field))) is not None]
