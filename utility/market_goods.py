"""Portable market names/art; host-approved names remain a read-only fallback."""


def key(value):
    if isinstance(value,bool):return None
    if type(value) is int:return str(value) if value>0 else None
    if isinstance(value,str) and value.isascii() and value.isdigit():return str(int(value)) if int(value)>0 else None
    return None


def validate(data):
    if data.get('format')!=1 or not isinstance(data.get('goods'),dict) or not data['goods']:raise ValueError('Empty market goods catalogue')
    for identifier,row in data['goods'].items():
        if key(identifier)!=identifier or row.get('item_id')!=int(identifier):raise ValueError('Market item identity mismatch')
        if not isinstance(row.get('name'),str) or not row['name'].strip():raise ValueError('Missing market name')
        if row.get('image')!='art/'+identifier+'.png':raise ValueError('Invalid market artwork path')
        if not isinstance(row.get('icon_resource'),str) or not row['icon_resource']:raise ValueError('Missing source icon reference')
        checksum=row.get('sha256','')
        if len(checksum)!=64 or any(c not in '0123456789abcdef' for c in checksum):raise ValueError('Invalid market artwork checksum')


def goods():
    from utility.game_assets import current
    return current().section('market_goods',{}).get('goods',{})


def name(value):
    row=goods().get(key(value),{})
    return row.get('name')


def image(value):
    from utility.game_assets import current,inside
    assets=current();row=assets.section('market_goods',{}).get('goods',{}).get(key(value))
    if not assets.directory or not row:return None
    try:
        path=inside(assets.directory/'market',row['image'])
        return path if path.is_file() else None
    except (OSError,ValueError):return None
