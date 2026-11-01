"""Compact analytics records. Payload bodies stay in the events table."""
import math
from .analytics import window

SCHEMA = '''
CREATE TABLE IF NOT EXISTS event_meta (
 seq INTEGER PRIMARY KEY REFERENCES events(seq) ON DELETE CASCADE,
 kind TEXT NOT NULL, time REAL NOT NULL, source TEXT NOT NULL,
 endpoint TEXT, label TEXT, outcome TEXT, duration_ms REAL, status INTEGER,
 retries INTEGER, timeouts INTEGER, attempt_count INTEGER,
 request_id TEXT, parent_id TEXT, name TEXT, command_type TEXT, search_text TEXT, method TEXT
);
CREATE INDEX IF NOT EXISTS meta_window ON event_meta(kind,time,seq);
CREATE INDEX IF NOT EXISTS meta_endpoint_window ON event_meta(kind,endpoint,time,seq);
CREATE INDEX IF NOT EXISTS meta_outcome_window ON event_meta(kind,outcome,time,seq);
CREATE TABLE IF NOT EXISTS metadata_progress(id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER, target INTEGER);
'''

def insert(db, seq, event):
    kind = event['kind']
    if kind not in ('request','command'):
        return
    data = event['data']
    attempts = data.get('attempts') or []
    duration = data.get('duration_ms') or 0
    if not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration < 0:
        duration = 0
    # Do not duplicate IDs/endpoints/labels into a second search string.
    # Concatenate those scalar columns only when a text search is requested.
    texts = [data.get('error')]
    texts += [attempt.get('error') for attempt in attempts if isinstance(attempt,dict)]
    search = ' '.join(str(value)[:300] for value in texts if value is not None)
    db.execute('INSERT OR IGNORE INTO event_meta VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (seq,kind,event['time'],event['source'],data.get('endpoint') or '/',data.get('label'),
         data.get('outcome') if data.get('outcome') in ('success','error') else 'unknown',duration,
         data.get('status'),max(0,len(attempts)-1),int(any(attempt.get('error') in ('TimeoutError','ServerTimeoutError')
             for attempt in attempts if isinstance(attempt,dict))),len(attempts),data.get('id'),data.get('parent_id'),
         data.get('name'),data.get('type'),search,data.get('method')))

def conditions(kind, since=0, until=None, search='', source='', outcome='', http_status='',
               min_duration=0, retried='', endpoint_name='', level=''):
    start,end = window(since,until)
    clauses = ['m.kind=?','m.time>=?','m.time<=?'];args=[kind,start,end]
    for column,value in [('source',source),('outcome',outcome),('endpoint',endpoint_name)]:
        if value:
            clauses.append('m.'+column+'=?');args.append(value)
    if search:
        columns=('request_id','parent_id','endpoint','label','name','outcome','search_text')
        clauses.append('(' + "||' '||".join("coalesce(m."+column+",'')" for column in columns) + ') LIKE ?')
        args.append('%'+search[:100]+'%')
    if level:
        clauses.append('0')  # Request/command events have no log severity.
    if http_status:
        status=int(http_status)
        if not 100<=status<=599:raise ValueError('Invalid HTTP status')
        clauses.append('m.status=?');args.append(status)
    duration=float(min_duration)
    if not math.isfinite(duration) or duration<0:raise ValueError('Invalid duration')
    if duration:
        clauses.append('m.duration_ms>=?');args.append(duration)
    if retried=='true':clauses.append('m.retries>0')
    elif retried not in ('','false'):raise ValueError('Invalid retry filter')
    return ' AND '.join(clauses),args

def progress(db):
    cursor,target=db.execute('SELECT cursor,target FROM metadata_progress WHERE id=1').fetchone()
    return {'complete':cursor>=target,'cursor':cursor,'target':target}

def percentile(db,where,args,count,fraction):
    if not count:return None
    if fraction==.5:
        offset=(count-1)//2;limit=2 if count%2==0 else 1
    else:
        offset=max(0,math.ceil(count*fraction)-1);limit=1
    values=[row[0] for row in db.execute('SELECT m.duration_ms FROM event_meta m WHERE '+where+
        ' ORDER BY m.duration_ms LIMIT ? OFFSET ?',[*args,limit,offset])]
    return sum(values)/len(values) if values else None

def aggregate(db,**filters):
    where,args=conditions('request',**filters)
    upstream=where+" AND m.source!='activity'"
    row=db.execute("SELECT count(*),coalesce(sum(m.outcome='success'),0),coalesce(sum(m.outcome='error'),0),"
        "coalesce(sum(m.outcome='unknown'),0),coalesce(sum(m.retries),0),coalesce(sum(m.timeouts),0) "
        'FROM event_meta m WHERE '+upstream,args).fetchone()
    counts=dict(zip(('total','success','error','unknown','retries','timeouts'),row))
    counts['activity_requests']=db.execute("SELECT count(*) FROM event_meta m WHERE "+where+" AND m.source='activity'",args).fetchone()[0]
    counts['median_ms']=percentile(db,upstream,args,counts['total'],.5)
    counts['p95_ms']=percentile(db,upstream,args,counts['total'],.95)
    for outcome in ('success','error','unknown'):
        counts[outcome+'_rate']=counts[outcome]/counts['total'] if counts['total'] else None
    start,end=window(filters.get('since',0),filters.get('until'))
    width=max(60,math.ceil((end-start)/120));size=max(1,math.ceil((end-start)/width))
    series=[{'time':start+i*width,'count':0,'errors':0,'mean_ms':None} for i in range(size)]
    for index,count,errors,mean in db.execute("SELECT CAST((m.time-?)/? AS INTEGER),count(*),sum(m.outcome='error'),avg(m.duration_ms) "
            'FROM event_meta m WHERE '+upstream+' GROUP BY 1',[start,width,*args]):
        series[min(size-1,max(0,index))].update(count=count,errors=errors,mean_ms=mean)
    endpoints=[dict(zip(('endpoint','label','count','errors'),row)) for row in db.execute(
        "SELECT m.endpoint,coalesce(max(m.label),m.endpoint),count(*),sum(m.outcome='error') FROM event_meta m WHERE "+
        upstream+' GROUP BY m.endpoint ORDER BY count(*) DESC,m.endpoint',args)]
    return {'since':start,'until':end,'as_of':end,'summary':counts,'series':series,'endpoints':endpoints,'indexing':progress(db)}
