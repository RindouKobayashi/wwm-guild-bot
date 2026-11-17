"""Validated game rank labels shared by Discord and Activity; no live requests."""


def integer(value):
    if type(value) is int:return value
    if isinstance(value,str) and value.isascii() and value.isdigit():return int(value)
    return None


def validate(data):
    if data.get('format')!=1 or set(data.get('modes',{}))!={'1v1','3v3'}:
        raise ValueError('Missing PvP rank modes')
    for mode in data['modes'].values():
        rows=mode.get('ranks');grades=mode.get('grades')
        if not rows or not isinstance(grades,dict):raise ValueError('Empty PvP rank labels')
        seen=set()
        for key,row in rows.items():
            grade,division=row.get('grade'),row.get('division')
            if (type(grade) is not int or type(division) is not int or grade<1 or not 1<=division<=9
                    or key!=str(grade*10+division) or (grade,division) in seen):
                raise ValueError('Invalid PvP combined-grade identity')
            seen.add((grade,division))
            if not isinstance(row.get('name'),str) or not row['name'].strip():raise ValueError('Unresolved PvP rank name')
            if str(grade) in grades and (not isinstance(grades[str(grade)],str) or not grades[str(grade)].strip()):raise ValueError('Unresolved PvP grade label')
        if not set(grades)<={str(g) for g,_ in seen}:raise ValueError('PvP grade coverage mismatch')


def format_rank(grade,division,mode='1v1',data=None,legacy_names=None,legacy_suffixes=None):
    if grade is None:return None
    g=integer(grade);d=integer(division)
    if g is None or g<0:return 'Rank unavailable (invalid grade)'
    if data is None:
        from utility.game_assets import current
        data=current().section('pvp_ranks',{})
    if data and data.get('display_policy')=='legacy_until_client_verified':
        if mode not in data['modes']:return 'Rank unavailable (unknown mode)'
        # Correct table identity alone does not prove the running client's final
        # localized labels. Preserve established display until that is verified.
        data={}
    if data:
        ranks=data['modes'].get(mode)
        if ranks is None:return 'Rank unavailable (unknown mode)'
        # Zero is the caller's historical "no division" value, not division I.
        if division is None or d==0:
            if str(g) in ranks['grades']:return ranks['grades'][str(g)]
            known=any(row['grade']==g for row in ranks['ranks'].values())
            return f'Rank reported without division (grade {g})' if known else f'Unknown rank (grade {g})'
        if d is None or not 1<=d<=9:return 'Rank unavailable (invalid division)'
        row=ranks['ranks'].get(str(g*10+d))
        return row['name'] if row else f'Unknown rank (grade {g}, division {d})'
    # Explicit old-checkout compatibility. Never overrides a loaded game table.
    names=legacy_names or {};suffixes=legacy_suffixes or {}
    name=names.get(g,names.get(str(g),f'Unknown rank (grade {g})'))
    suffix=suffixes.get(d,suffixes.get(str(d),str(d) if d is not None else ''))
    return (name+' '+suffix).strip()
