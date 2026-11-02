"""Local-only presets, bounded tool results and ephemeral posting previews."""
import json
import time
import uuid

SCHEMA='''
CREATE TABLE IF NOT EXISTS presets(id TEXT PRIMARY KEY, name TEXT NOT NULL, tool TEXT NOT NULL, inputs TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tool_results(id TEXT PRIMARY KEY, tool TEXT NOT NULL, time REAL NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS tool_result_time ON tool_results(time);
CREATE TABLE IF NOT EXISTS action_previews(id TEXT PRIMARY KEY, time REAL NOT NULL, body TEXT NOT NULL);
'''

class WorkspaceStore:
    def presets(self):
        return [dict(id=r[0],name=r[1],tool=r[2],inputs=json.loads(r[3])) for r in self.db.execute('SELECT * FROM presets ORDER BY name')]

    def save_preset(self,name,tool,inputs):
        if self.db.execute('SELECT count(*) FROM presets').fetchone()[0]>=50:raise ValueError('You can save at most 50 presets. Delete one before adding another.')
        key=uuid.uuid4().hex
        with self.db:self.db.execute('INSERT INTO presets VALUES(?,?,?,?)',(key,name,tool,json.dumps(inputs)))
        return dict(id=key,name=name,tool=tool,inputs=inputs)

    def delete_preset(self,key):
        with self.db:return self.db.execute('DELETE FROM presets WHERE id=?',(key,)).rowcount>0

    def tool_result(self,key,item=None):
        if item is not None:
            with self.db:self.db.execute('INSERT OR REPLACE INTO tool_results VALUES(?,?,?,?)',(key,item['tool'],item['time'],json.dumps(item,allow_nan=False)))
            return item
        row=self.db.execute('SELECT body FROM tool_results WHERE id=? AND time>?',(key,time.time()-72*3600)).fetchone()
        return json.loads(row[0]) if row else None

    def tool_results(self,tool):
        return [dict(id=r[0],tool=r[1],time=r[2],environment=json.loads(r[3])['environment']) for r in self.db.execute('SELECT id,tool,time,body FROM tool_results WHERE tool=? AND time>? ORDER BY time DESC LIMIT 50',(tool,time.time()-72*3600))]

    def action_preview(self,key,item=None):
        if item is not None:
            with self.db:self.db.execute('INSERT OR REPLACE INTO action_previews VALUES(?,?,?)',(key,time.time(),json.dumps(item)))
            return item
        row=self.db.execute('SELECT body FROM action_previews WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def prune_workspace(self):
        now=time.time()
        self.db.execute('DELETE FROM tool_results WHERE id IN (SELECT id FROM tool_results WHERE time<? ORDER BY time LIMIT 1000)',(now-72*3600,))
        self.db.execute('DELETE FROM action_previews WHERE time<?',(now-120,))
        free=self.db.execute('PRAGMA freelist_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]
        if self.storage_bytes()-free>self.budget*.8:
            self.db.execute('DELETE FROM tool_results WHERE id IN (SELECT id FROM tool_results ORDER BY time LIMIT 100)')
        self.db.commit()
