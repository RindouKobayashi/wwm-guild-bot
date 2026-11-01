"""Single-writer bounded SQLite history. Called only by the panel worker thread."""
import json
import sqlite3
import time
import uuid
from .analytics import aggregate, conditions, failure_reason

class Store:
    def __init__(self, path, budget=250*1024*1024):
        self.path, self.budget = path, budget
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version > 2:
            self.db.close()
            raise RuntimeError('History schema is newer than this panel; use the matching application version.')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, time REAL, kind TEXT, source TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS event_lookup ON events(kind,time);
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, body TEXT);
            CREATE INDEX IF NOT EXISTS event_source_lookup ON events(kind,source,time);
            CREATE INDEX IF NOT EXISTS event_id_lookup ON events(kind,json_extract(body,'$.data.id'));
            CREATE INDEX IF NOT EXISTS event_parent_lookup ON events(kind,json_extract(body,'$.data.parent_id'));
            CREATE INDEX IF NOT EXISTS event_log_parent ON events(kind,json_extract(body,'$.data.request_id'));
            CREATE TABLE IF NOT EXISTS jobs (instance TEXT, name TEXT, time REAL, body TEXT, PRIMARY KEY(instance,name));
            CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, rule TEXT, target TEXT, state TEXT, first_seen REAL, last_seen REAL, recovered REAL, count INTEGER, acknowledged REAL, note TEXT, context TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS active_incident ON incidents(rule,target) WHERE state='active';
        ''')
        self.db.execute('PRAGMA user_version=2')
        for key, body in self.db.execute('SELECT id,body FROM operations').fetchall():
            item = json.loads(body)
            if item['state'] in ('queued', 'running'):
                item.update(state='interrupted', finished=time.time(), message='Panel stopped before completion could be verified.')
                self.db.execute('UPDATE operations SET body=? WHERE id=?', (json.dumps(item), key))
        self.db.commit()

    def append(self, events):
        if self.path.stat().st_size > self.budget * .75:
            self.prune()
        self.db.executemany('INSERT INTO events(time,kind,source,body) VALUES(?,?,?,?)',
            [(e['time'], e['kind'], e['source'], json.dumps(e, allow_nan=False)) for e in events])
        for event in events:
            if event['kind'] == 'job':
                self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?)',
                    (event.get('instance','legacy'), event['data']['name'], event['time'], json.dumps(event)))
        self.db.commit()

    def query(self, kind, before=None, limit=100, search='', source='', level='', since=0, **filters):
        limit = min(max(int(limit), 1), 200)
        where, args = conditions(kind, since=since, search=search, source=source, level=level, **filters)
        sql = 'SELECT seq,body FROM events WHERE '+where
        if before:
            sql += ' AND seq<?'; args.append(int(before))
        sql += ' ORDER BY seq DESC LIMIT ?'; args.append(limit)
        rows = self.db.execute(sql, args).fetchall()
        return {'items': [dict(json.loads(body), cursor=seq) for seq, body in rows],
                'next_cursor': rows[-1][0] if len(rows) == limit else None}

    def request(self, key):
        row = self.db.execute("SELECT body FROM events WHERE kind='request' AND json_extract(body,'$.data.id')=? ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
        if not row:
            return None
        item = json.loads(row[0]); item['data']['explanation'] = failure_reason(item['data'])
        return item

    def analytics(self, **filters):
        return aggregate(self.db, **filters)

    def command_summary(self, since=0):
        where,args=conditions('command',since=since)
        rows=self.db.execute("SELECT json_extract(body,'$.data.name'),json_extract(body,'$.data.outcome'),"
                             "json_extract(body,'$.data.duration_ms') FROM events WHERE "+where,args)
        groups={};total=0;errors=0;durations=[]
        for name,outcome,duration in rows:
            total+=1;errors+=int(outcome=='error');duration=float(duration or 0);durations.append(duration)
            item=groups.setdefault(name or 'unknown',{'name':name or 'unknown','count':0,'errors':0,'max_ms':0,'sum_ms':0})
            item['count']+=1;item['errors']+=int(outcome=='error');item['max_ms']=max(item['max_ms'],duration);item['sum_ms']+=duration
        for item in groups.values():
            item['mean_ms']=item.pop('sum_ms')/item['count']
        durations.sort()
        import math
        return {'total':total,'errors':errors,'success':total-errors,
                'p95_ms':durations[max(0,math.ceil(len(durations)*.95)-1)] if durations else None,
                'slowest':sorted(groups.values(),key=lambda item:-item['max_ms'])[:10]}

    def investigation(self, key):
        request = self.request(key)
        if not request:
            return None
        root = request['data'].get('parent_id') or key
        rows = self.db.execute("SELECT body FROM events WHERE kind IN ('request','job','command') AND "
            "(json_extract(body,'$.data.id')=? OR json_extract(body,'$.data.parent_id')=?) ORDER BY time LIMIT 201", (root,root)).fetchall()
        items = [json.loads(row[0]) for row in rows[:200]]
        ids = {root,key} | {item['data'].get('id') for item in items}
        ids.discard(None)
        placeholders = ','.join('?' for _ in ids)
        logs = self.db.execute("SELECT body FROM events WHERE kind='log' AND json_extract(body,'$.data.request_id') IN ("+placeholders+") ORDER BY time LIMIT 101", list(ids)).fetchall()
        return {'root_id':root,'events':items,'logs':[json.loads(row[0]) for row in logs[:100]],
                'truncated':len(rows)>200 or len(logs)>100}

    def jobs(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM jobs ORDER BY time DESC LIMIT 200')]

    def incident(self, rule, target, active, context=None, occurrence='', commit=True):
        now = time.time()
        row = self.db.execute("SELECT id,context FROM incidents WHERE rule=? AND target=? AND state='active'",(rule,target)).fetchone()
        context = dict(context or {}, occurrence=str(occurrence)[:100])
        if active and not row:
            self.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,rule,target,'active',now,now,None,1,None,'',json.dumps(context)))
        elif active:
            changed = json.loads(row[1]).get('occurrence') != context['occurrence']
            self.db.execute('UPDATE incidents SET last_seen=?, count=count+?, context=? WHERE id=?',
                            (now,int(changed),json.dumps(context),row[0]))
        elif row:
            self.db.execute("UPDATE incidents SET state='recovered',recovered=? WHERE id=?",(now,row[0]))
        if commit:
            self.db.commit()

    def apply_incidents(self, rules):
        try:
            for rule in rules:
                self.incident(*rule,commit=False)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def incidents(self):
        names = ['id','rule','target','state','first_seen','last_seen','recovered','count','acknowledged','note','context']
        rows = self.db.execute("SELECT * FROM incidents ORDER BY (state='active') DESC,last_seen DESC LIMIT 200").fetchall()
        result = [dict(zip(names,row)) for row in rows]
        for item in result:
            item['context'] = json.loads(item['context'])
        return result

    def acknowledge(self, key, note):
        cursor = self.db.execute('UPDATE incidents SET acknowledged=?,note=? WHERE id=?',(time.time(),note[:280],key))
        self.db.commit()
        return cursor.rowcount == 1

    def recent_auth(self):
        rows = self.db.execute("SELECT json_extract(body,'$.data.status'),json_extract(body,'$.data.id') FROM events "
            "WHERE kind='request' AND source!='activity' AND time>? ORDER BY seq DESC LIMIT 3",(time.time()-3600,)).fetchall()
        return {'rejected':len(rows)==3 and all(row[0] in (401,403) for row in rows),'latest':rows[0][1] if rows else None,
                'recovered':bool(rows and rows[0][0] == 200)}

    def operation(self, item=None, key=None):
        if item:
            self.db.execute('INSERT OR REPLACE INTO operations VALUES(?,?)', (item['id'], json.dumps(item)))
            self.db.commit()
            return item
        row = self.db.execute('SELECT body FROM operations WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def operations(self):
        return sorted([json.loads(r[0]) for r in self.db.execute('SELECT body FROM operations')],
                      key=lambda v: v['started'], reverse=True)[:30]

    def prune(self):
        now = time.time()
        self.db.execute("DELETE FROM events WHERE (kind='metric' AND time<?) OR (kind!='metric' AND time<?)",
                        (now-14*86400, now-72*3600))
        self.db.execute("DELETE FROM operations WHERE json_extract(body,'$.started')<?", (now-30*86400,))
        self.db.execute('DELETE FROM jobs WHERE time<?', (now-72*3600,))
        self.db.execute("DELETE FROM incidents WHERE state='recovered' AND recovered<?", (now-30*86400,))
        self.db.commit()
        # Keep headroom for journal writes. Reclaim physical pages, not just rows.
        while self.path.stat().st_size > self.budget * .8:
            deleted = self.db.execute('DELETE FROM events WHERE seq IN (SELECT seq FROM events ORDER BY time LIMIT 1000)').rowcount
            self.db.commit()
            self.db.execute('VACUUM')
            if not deleted:
                raise OSError('History budget exhausted by retained operation audit')
        self.db.execute('PRAGMA incremental_vacuum(1000)')

    def close(self):
        self.db.close()
