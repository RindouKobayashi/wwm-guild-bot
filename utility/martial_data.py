"""Dependency-free validation for extracted martial joins."""
def validate(data):
    if data.get('format')!=1 or not data.get('kungfu') or not data.get('paths'):raise ValueError('Empty martial catalogue')
    for key,row in data['kungfu'].items():
        if not key.isdigit() or not row.get('name') or not isinstance(row['name'],str):raise ValueError('Unresolved martial name')
        path=data['paths'].get(str(row['path_id']))
        if not path or row['icon_key']!=path['icon_key'] or row['icon_sha256']!=path['icon_sha256']:raise ValueError('Unresolved martial path/icon join')


def validate_approval(directory):
    import json
    from utility.game_assets import digest
    value=json.loads((directory/'startup-approval.json').read_text(encoding='utf-8'))
    if value.get('format')!=1 or value.get('enabled') is not True:raise ValueError('Startup icon uploads are not approved')
    apps=value.get('applications',{})
    if set(apps)!= {'test','production'} or any(not isinstance(v,str) or not v.isdigit() for v in apps.values()) or apps['test']==apps['production']:
        raise ValueError('Explicit distinct test/production application identities are required')
    if digest(directory/'catalogue.json')!=value['catalogue_sha256'] or digest(directory/'upload-pack/upload-manifest.json')!=value['upload_manifest_sha256']:
        raise ValueError('Icon approval does not match this release')
    return value
