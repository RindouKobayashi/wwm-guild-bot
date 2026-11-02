"""Explicit read-only tool registry and host-approved API profiles."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .privacy import SECRET_KEY

PROFILE_PATH = Path(__file__).parent/'profiles.local.json'
RANKS = {'rank_team10_dungeon_22':'10-player dungeon', 'rank_team_dungeon_22_st':'Dungeon speed runs',
         'rank_petbattle_3v3':'Pet battles · 3v3'}

def servers():
    # Use the same bundled labels as Activity; never require game files.
    path=Path(__file__).parents[1]/'activity/public/catalogue.json'
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        mapping=data.get('servers',data.get('hostnums',{}))
        if isinstance(mapping,dict):
            result=[{'value':str(k),'label':str(v)} for k,v in mapping.items() if str(k).isdigit() and isinstance(v,str)]
            if result:return result
    except (OSError,ValueError):pass
    return [{'value':str(n),'label':label} for n,label in ((10595,'Default server · 10595'),(10403,'Server 10403'),(10103,'Server 10103'))]

def field(key,label,kind='text',**options):return dict(key=key,label=label,type=kind,**options)

def builtin():
    return [
      dict(id='player',name='Find player',description='Look up a player by their in-game number ID. Names and personal text are omitted.',fields=[field('number_id','Player number ID',placeholder='Example: 123456789'),field('fields','Additional data','select',advanced=True,default='basic',options=[{'value':'basic','label':'Basic statistics'},{'value':'gameplay','label':'Basic + gameplay statistics'}])]),
      dict(id='guild',name='Find guild',description='Search by guild number ID or exact name. Results contain approved statistics only.',fields=[field('by','Search using','select',default='number',options=[{'value':'number','label':'Guild number ID'},{'value':'name','label':'Guild name'}]),field('value','Guild number ID or name',placeholder='Enter an ID or guild name')]),
      dict(id='combat',name='Combat plan',description='Inspect available combat statistics for one player.',fields=[field('number_id','Player number ID'),field('hostnum','Server','select',default='auto',options=[{'value':'auto','label':'Detect from player lookup'}]+servers())]),
      dict(id='rankings',name='Rankings',description='Fetch one page of an approved ranking. Player names are omitted.',fields=[field('rank','Ranking','select',default=next(iter(RANKS)),options=[{'value':k,'label':v} for k,v in RANKS.items()]),field('page','Page','number',default='1',min=1,max=100)])]

def load_profiles(path=None):
    path=path or PROFILE_PATH
    if not path.exists():return {}
    if path.stat().st_size>65536:raise ValueError('API profile file exceeds 64 KiB.')
    entries=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(entries,list) or len(entries)>20:raise ValueError('Profiles must be a list of at most 20 entries.')
    result={}
    for profile in entries:
        if not isinstance(profile,dict):raise ValueError('Each API profile must be an object.')
        key=profile.get('id','');url=urlsplit(profile.get('origin',''))
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',key) or key in result:raise ValueError('Invalid or duplicate API profile ID.')
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.path not in ('','/') or url.query or url.fragment or url.port not in (None,443):raise ValueError('Profile origin must be an exact HTTPS origin on port 443.')
        if not profile.get('response_fields') or not isinstance(profile['response_fields'],list):raise ValueError('Each profile needs response privacy fields.')
        if profile.get('response_format','json') not in ('json','text','msgpack'):raise ValueError('Unsupported profile response format.')
        if not isinstance(profile.get('paths'),list) or not profile['paths'] or any(not isinstance(p,str) or not re.fullmatch(r'/[A-Za-z0-9/_-]*',p) or '..' in p for p in profile['paths']):raise ValueError('Profiles require fixed approved paths.')
        methods=profile.get('methods',['GET'])
        if not methods or any(m not in ('GET','POST') for m in methods) or ('POST' in methods and profile.get('read_only') is not True):raise ValueError('POST profiles must explicitly declare read_only=true.')
        for group in ('query_fields','body_fields','header_fields','response_fields'):
            names=profile.get(group,[])
            if not isinstance(names,list) or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',n) or SECRET_KEY.search(n) for n in names):raise ValueError('Unsafe profile field name.')
        if any(n.lower() in ('host','connection','transfer-encoding','content-length','proxy-authorization') for n in profile.get('header_fields',[])):raise ValueError('Unsafe profile header.')
        credentials=profile.get('credential_headers',{})
        if not isinstance(credentials,dict) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}',k) or k.lower() in ('host','connection','content-length','transfer-encoding') or not isinstance(v,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*',v) for k,v in credentials.items()):raise ValueError('Credentials must reference host environment variable names.')
        result[key]=profile
    return result

def catalogue():
    error=None
    try:profiles=load_profiles()
    except (ValueError,OSError,TypeError):profiles={};error='Custom API profiles are invalid. Check profiles.local.json on this host.'
    custom=[]
    for key,p in profiles.items():
        custom.append(dict(id='custom-'+key,name=p.get('name',key),description=p.get('description','Approved read-only API'),origin=p['origin'],advanced=True,
            fields=[field('path','Path','select',default=p['paths'][0],options=[{'value':x,'label':x} for x in p['paths']]),field('method','Method','select',default=p.get('methods',['GET'])[0],options=[{'value':x,'label':x} for x in p.get('methods',['GET'])]),*[field(group,group.title()+' · JSON object','json',default='{}',allowed=p.get(group+'_fields',[])) for group in ('query','body','header')]]))
    return dict(items=builtin()+custom,profile_error=error,limits=dict(seconds=30,active=1,preview_bytes=16384))

def validate(tool,inputs,profiles=None):
    if not isinstance(inputs,dict) or len(json.dumps(inputs,allow_nan=False).encode())>65536:raise ValueError('Request inputs must fit within 64 KiB.')
    definitions={t['id']:t for t in builtin()}
    if tool.startswith('custom-'):
        p=(load_profiles() if profiles is None else profiles).get(tool[7:])
        if not p:raise ValueError('This API profile is not available on this machine.')
        if set(inputs)-{'path','method','query','body','header'}:raise ValueError('Unknown profile inputs.')
        value=dict(path=inputs.get('path',p['paths'][0]),method=inputs.get('method',p.get('methods',['GET'])[0]))
        if value['path'] not in p['paths'] or value['method'] not in p.get('methods',['GET']):raise ValueError('Choose an approved path and method.')
        for group in ('query','body','header'):
            data=inputs.get(group,{})
            if isinstance(data,str):data=json.loads(data)
            if not isinstance(data,dict) or set(data)-set(p.get(group+'_fields',[])):raise ValueError('Unapproved '+group+' fields.')
            if any(not isinstance(v,(str,int,float,bool,type(None))) for v in data.values()):raise ValueError('Profile inputs must be simple scalar values.')
            value[group]=data
        if value['method']=='GET' and value['body']:raise ValueError('GET lookups cannot contain a request body.')
        return value
    if tool not in definitions:raise ValueError('Unknown lookup tool.')
    definition=definitions[tool];allowed={f['key'] for f in definition['fields']}
    if set(inputs)-allowed:raise ValueError('Unrecognized lookup input.')
    value={f['key']:str(inputs.get(f['key'],f.get('default',''))).strip() for f in definition['fields']}
    for f in definition['fields']:
        v=value[f['key']]
        if not v or len(v)>100:raise ValueError(f['label']+' is required (maximum 100 characters).')
        if f['type']=='select' and v not in {o['value'] for o in f['options']}:raise ValueError('Choose a listed '+f['label'].lower()+'.')
        if f['type']=='number' and (not v.isdigit() or not f.get('min',1)<=int(v)<=f.get('max',100)):raise ValueError('Page must be between 1 and 100.')
    if 'number_id' in value and not re.fullmatch(r'[0-9]{1,20}',value['number_id']):raise ValueError('Player number ID must contain 1–20 digits.')
    if tool=='guild' and value['by']=='number' and not re.fullmatch(r'[0-9]{1,20}',value['value']):raise ValueError('Guild number ID must contain 1–20 digits.')
    return value

def compare(left,right,path='$'):
    """Compare only already-sanitized previews, preserving omission uncertainty."""
    rows=[]
    if any(isinstance(x,dict) and any(k in x for k in ('omitted','truncated','omitted_items')) for x in (left,right)):
        return [dict(path=path,change='unavailable',before=left,after=right)]
    if isinstance(left,dict) and isinstance(right,dict):
        for key in sorted(left.keys()|right.keys()):
            if key not in left:rows.append(dict(path=path+'.'+key,change='added',after=right[key]))
            elif key not in right:rows.append(dict(path=path+'.'+key,change='removed',before=left[key]))
            else:rows.extend(compare(left[key],right[key],path+'.'+key))
    elif isinstance(left,list) and isinstance(right,list):
        for index in range(min(40,max(len(left),len(right)))):
            if index>=len(left):rows.append(dict(path=path+'['+str(index)+']',change='added',after=right[index]))
            elif index>=len(right):rows.append(dict(path=path+'['+str(index)+']',change='removed',before=left[index]))
            else:rows.extend(compare(left[index],right[index],path+'['+str(index)+']'))
    elif left!=right:rows.append(dict(path=path,change='changed',before=left,after=right))
    return rows[:500]
