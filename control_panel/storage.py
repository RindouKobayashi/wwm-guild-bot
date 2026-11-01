"""Single-writer bounded SQLite history. Called only by the panel worker thread."""
import json
import sqlite3
import time

class Store:
    def __init__(self, path, budget=250*1024*1024):
        self.path, self.budget = path, budget
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA auto_vacuum=INCREMENTAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, time REAL, kind TEXT, source TEXT, body TEXT);
            CREATE INDEX IF NOT EXISTS event_lookup ON events(kind,time);
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, body TEXT);
        ''')
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
        self.db.commit()

    def query(self, kind, before=None, limit=100, search='', source='', level='', since=0):
        limit = min(max(int(limit), 1), 200)
        sql = 'SELECT seq,body FROM events WHERE kind=? AND time>=?'
        args = [kind, since]
        if before:
            sql += ' AND seq<?'; args.append(int(before))
        if source:
            sql += ' AND source=?'; args.append(source)
        if search:
            sql += ' AND body LIKE ?'; args.append('%' + search[:100] + '%')
        if level:
            sql += " AND json_extract(body,'$.data.level')=?"; args.append(level)
        sql += ' ORDER BY seq DESC LIMIT ?'; args.append(limit)
        rows = self.db.execute(sql, args).fetchall()
        return {'items': [dict(json.loads(body), cursor=seq) for seq, body in rows],
                'next_cursor': rows[-1][0] if len(rows) == limit else None}

    def request(self, key):
        row = self.db.execute("SELECT body FROM events WHERE kind='request' AND json_extract(body,'$.data.id')=? ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
        return json.loads(row[0]) if row else None

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
