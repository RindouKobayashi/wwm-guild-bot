"""Allowlisted public profile data for the authenticated Activity viewer."""
import math
import time

FIELDS = ['base', 'name_card', 'achievement', 'club', 'attr', 'kongfu', 'lunjian',
          'lunjian3v3_prop', 'fight_shoulder', 'coop_score', 'gameplay_resources',
          'school', 'fashion', 'birthday', 'jieyi']


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def profile(data, school_names, guild_name=None):
    base = data.get('base') or {}
    card = data.get('name_card') or {}
    quantity = (data.get('achievement') or {}).get('quantity') or {}
    club = data.get('club')
    school = base.get('school')
    try:
        school = int(school) if school is not None else None
    except (ValueError, TypeError):
        school = None
    online = base.get('is_online')
    state = ('Invisible' if base.get('invisible') else
             'Online' if online == 1 else 'Offline' if online == 0 else 'Not reported')
    duration = number(base.get('online_time'))
    return {
        'name': base.get('nickname') or data.get('name') or 'Name unavailable',
        'number': str(base['number_id']) if base.get('number_id') is not None else None,
        'level': number(base.get('level')),
        'stage_name': base.get('ly_stage_name') or None,
        'signature': card.get('sign') or None,
        'status': state,
        'region': base.get('oversea_tag') or None,
        'created': number(base.get('create_time')),
        'online_hours': round(duration / 3600, 1) if duration is not None and duration >= 0 else None,
        'sect': school_names.get(school, f'Unmapped sect (ID {school})' if school is not None else None),
        'guild': {'status': 'not_reported' if club is None else 'joined' if club.get('club_id') else 'none',
                  'name': guild_name},
        'achievements': {label: number(quantity.get(key, quantity.get(str(key))))
                         for key, label in ((1, 'Normal'), (2, 'Hard'), (3, 'Expert'))},
    }


def detail_groups(data, names, mappings, school_ranks=None, now=None):
    """Use the same fields, rank mappings and energy rules as the bot profile."""
    now = int(time.time()) if now is None else now
    constants = mappings['constants']
    base = data.get('base') or {}; attr = data.get('attr') or {}
    resource = (data.get('gameplay_resources') or {}).get('50', (data.get('gameplay_resources') or {}).get(50)) or {}
    value = number(resource.get('value')); logout = number(base.get('logout_time'))
    online = base.get('is_online')
    regen = max(0, now-int(logout))//constants['ENERGY_REGEN_SECONDS'] if online == 0 and logout else 0
    cap = constants['MAX_ENERGY']
    energy = min(cap, max(0, int(value)+regen)) if value is not None else min(cap, regen) if logout and online == 0 else None
    energy_note = ('Game reading' if online == 1 else 'Offline estimate: game reading plus regeneration' if online == 0 else 'Last game reading; online state unavailable') if value is not None else 'Regeneration floor only; base energy unknown' if energy is not None else 'Not reported'
    rank_names = constants['GRADE_NAMES']; suffixes = constants['SMALL_GRADE_SUFFIXES']
    def rank(source):
        grade = source.get('grade'); small = source.get('small_grade')
        if grade is None:return None
        label = rank_names.get(str(grade), f'Unmapped rank (ID {grade})')
        suffix = suffixes.get(str(small), f'Division {small}' if small is not None else '')
        return (label+' '+suffix).strip()
    def rows(source, fields):
        return [{'label':label,'value':number(source.get(key))} for key,label in fields]
    one = data.get('lunjian') or {}; three = data.get('lunjian3v3_prop') or {}; group = data.get('fight_shoulder') or {}
    kongfu = data.get('kongfu') or {}
    birthday = data.get('birthday') or {}
    # Match the bot's visibility rule; missing visibility is not permission to show it.
    visible_birthday = (f"{birthday['month']:02d}-{birthday['day']:02d}" if birthday.get('visible') == 0
                        and type(birthday.get('month')) is int and type(birthday.get('day')) is int
                        and 1<=birthday['month']<=12 and 1<=birthday['day']<=31 else None)
    cohort = data.get('jieyi') or {}
    sect = data.get('school') or {}; status = sect.get('status')
    mapped_status = (school_ranks or {}).get(status)
    return {
        'Energy & activity': [{'label':'Energy','value':energy},{'label':'Energy limit','value':cap},
             {'label':'Reading type','value':energy_note},{'label':'Regeneration added','value':regen if online == 0 and logout else None},
             {'label':'Assist points','value':number((data.get('coop_score') or {}).get('score'))}],
        'Masteries': rows(attr,[('XIUWEI_KUNGFU','Martial'),('XIUWEI_TRADE3','Scholar'),('XIUWEI_TRADE4','Healer'),('XIUWEI_EXPLORE','Exploration')]),
        'Base attributes': rows(attr,[('STR','Power (STR)'),('CON','Body (CON)'),('BAS','Momentum (BAS)'),('CRI','Agility (CRI)'),('AGI','Defense (AGI)')]),
        'Martial arts & sect': [{'label':label,'value':names.get('martial',{}).get(str(kongfu.get(key)), f'Unmapped martial art (ID {kongfu[key]})' if kongfu.get(key) else None)} for key,label in [('kongfu_main','Main martial art'),('kongfu_sub','Secondary martial art')]] + [{'label':'Sect standing','value':mapped_status if mapped_status is not None else f'Unmapped standing (ID {status})' if status is not None else None}],
        'Arena summaries': [{'label':'1v1 rank','value':rank(one)},{'label':'1v1 battles','value':number(one.get('total_num'))},
            {'label':'1v1 best win streak','value':number(one.get('max_winning_streak'))},{'label':'3v3 rank','value':rank(three)},
            {'label':'3v3 battles','value':number(three.get('total_num'))},{'label':'Group strategy score','value':number(group.get('score'))},
            {'label':'Group strategy battles','value':number(group.get('total_num'))}],
        'Fashion & social': [{'label':'Elegance','value':number((data.get('fashion') or {}).get('score'))},
            {'label':'Visible birthday (MM-DD)','value':visible_birthday},{'label':'Sworn cohort','value':cohort.get('jieyi_name')},
            {'label':'Cohort motto','value':cohort.get('jieyi_text')}],
    }


def equipment(result, mappings):
    slots = mappings['constants']['SLOT_NAMES']; entries=[]
    for slot,item in (result.get('wear_equips') or {}).items():
        if not isinstance(item,dict):continue
        ex = item.get('ex') or {}; item_id = str(item.get('No',''))
        affixes=[]
        for pair in ex.get('base_affixes') or []:
            if not isinstance(pair,list) or len(pair)<2:continue
            affix=mappings['affixes'].get(str(pair[0]),{})
            display=None; fmt=affix.get('format') or ''
            if fmt and number(pair[1]) is not None:
                try:display=fmt.replace('米','m').replace('秒','s').replace('点','pt').format(pair[1])
                except (ValueError,TypeError,IndexError,KeyError):pass
            affixes.append({'name':affix.get('name') or f'Unmapped affix (ID {pair[0]})','display':display,
                            'value':number(pair[1]),'format':affix.get('format') or None,
                            'minimum':affix.get('minimum') or None,'maximum':affix.get('maximum') or None})
        for affix in affixes:
            def limit(raw):
                try:return number(float(raw)) if raw is not None else None
                except (ValueError,TypeError):return None
            lo,hi,value=limit(affix['minimum']),limit(affix['maximum']),affix['value']
            affix['roll_percent']=round((value-lo)/(hi-lo)*100,1) if value is not None and lo is not None and hi is not None and hi>lo and lo<=value<=hi else None
            affix['range_status']='Comparable' if affix['roll_percent'] is not None else 'Fixed reference' if lo is not None and lo==hi else 'Outside reference range' if value is not None and lo is not None and hi is not None and hi>lo else 'Range unavailable'
        entries.append({'slot':slots.get(str(slot),f'Slot {slot}'),
                        'name':mappings['equipment'].get(item_id) or f'Unmapped equipment (ID {item_id})',
                        'durability':number(ex.get('durability')),
                        'retuned_count':number(ex.get('retoned')),
                        'next_retune':number(ex.get('next_retone_ts')),
                        'acquired':number(ex.get('gain_ts')),
                        'attributes':[{'label':str(k),'value':number(v)} for k,v in (ex.get('base_attrs') or {}).items()],
                        'affixes':affixes})
    return entries


def collections(data, mappings):
    buckets={}
    def add(bucket,item_id,value):
        candidates=mappings['items'].get(str(item_id),{});name=None
        for category in mappings['constants']['COLLECTION_CATEGORY_SCOPES'][bucket]:
            options=candidates.get(category,[])
            if options:
                name=options[0] if len(options)==1 else None
                break
        buckets.setdefault(bucket,[]).append({'name':name or f'Unmapped item (ID {item_id})','value':number(value)})
    for field,key in [('fashion','assets'),('guise','owned_views')]:
        for item_id,value in ((data.get(field) or {}).get(key) or {}).items():add(field,item_id,value)
    for item_id,info in ((data.get('title_prop') or {}).get('titles') or {}).items():
        add('titles',item_id,info.get('level') if isinstance(info,dict) else None)
    bag=(data.get('ride') or {}).get('ride_bag') or {};seen=set()
    for source in ['no2num','no2max_level']:
        for item_id,value in (bag.get(source) or {}).items():
            if item_id not in seen:add('ride',item_id,value);seen.add(item_id)
    return buckets
