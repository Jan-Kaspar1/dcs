"""Durable scheduler state. Reservations are serialized by SQLite, not labels."""
import json
import sqlite3
import time
from pathlib import Path


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
              issue INTEGER PRIMARY KEY, worker TEXT NOT NULL, concurrency_group TEXT NOT NULL,
              status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
              repairs INTEGER NOT NULL DEFAULT 0, branch TEXT, pr INTEGER, pid INTEGER,
              process_start TEXT, session TEXT, started REAL, updated REAL NOT NULL,
              error TEXT, clone TEXT, prompt TEXT, log TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS live_worker ON jobs(worker)
              WHERE status IN ('working','pr-open');
            CREATE UNIQUE INDEX IF NOT EXISTS live_group ON jobs(concurrency_group)
              WHERE status IN ('working','pr-open');
        """)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def paused(self):
        return bool(self.get('paused', False) or self.get('integrity_error'))

    def pause(self, reason='Operator paused'):
        self.set('pause_reason', reason)
        self.set('paused', True)

    def resume(self):
        if self.get('integrity_error'):
            raise RuntimeError('Resolve and clear integrity_error before resuming')
        self.set('paused', False)
        self.set('pause_reason', None)

    def integrity(self, reason):
        self.set('integrity_error', reason)
        self.pause(reason)

    def capacity(self):
        merges = self.get('merges', 0)
        return 20 if merges >= 15 else 10 if merges >= 5 else 5

    def jobs(self, statuses=None):
        query, args = 'SELECT * FROM jobs', []
        if statuses:
            args = list(statuses)
            query += ' WHERE status IN (' + ','.join('?' for _ in args) + ')'
        return [dict(r) for r in self.db.execute(query + ' ORDER BY issue', args)]

    def job(self, issue):
        row = self.db.execute('SELECT * FROM jobs WHERE issue=?', (issue,)).fetchone()
        return dict(row) if row else None

    def reserve(self, issue, worker, group, dependencies=()):
        """Return reserved job, or None if paused, occupied, or dependencies incomplete."""
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if self.paused():
                self.db.rollback()
                return None
            if any(not self.job(d) or self.job(d)['status'] != 'done' for d in dependencies):
                self.db.rollback()
                return None
            if len(self.jobs(('working', 'pr-open'))) >= self.capacity():
                self.db.rollback()
                return None
            now = time.time()
            self.db.execute('INSERT INTO jobs(issue,worker,concurrency_group,status,started,updated) VALUES(?,?,?,?,?,?)',
                            (issue, worker, group or 'issue-' + str(issue), 'working', now, now))
            self.db.commit()
            return self.job(issue)
        except sqlite3.IntegrityError:
            self.db.rollback()
            return None
        except Exception:
            self.db.rollback()
            raise

    def update_job(self, issue, **fields):
        allowed = {'worker','concurrency_group','status','attempt','repairs','branch','pr','pid',
                   'process_start','session','started','error','clone','prompt','log'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job fields')
        if 'status' in fields and fields['status'] not in ('working','pr-open','blocked','done'):
            raise ValueError('Invalid status')
        fields['updated'] = time.time()
        with self.db:
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE issue=?',
                            (*fields.values(), issue))

    def repair(self, issue):
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET repairs=repairs+1,status='working',updated=? WHERE issue=? AND repairs<3 AND status IN ('working','pr-open')", (time.time(), issue))
            if cursor.rowcount:
                return True
            self.db.execute("UPDATE jobs SET status='blocked',error='Repair limit exhausted',updated=? WHERE issue=? AND status!='done'", (time.time(), issue))
            return False

    def complete(self, issue):
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status='done',pid=NULL,updated=? WHERE issue=? AND status!='done'", (time.time(), issue))
            if cursor.rowcount:
                merges = self.get('merges', 0) + 1
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('merges', json.dumps(merges)))

    def retry(self, issue):
        """Explicit operator retry. Keeps prior branch/clone recorded until replacement."""
        try:
            with self.db:
                cursor = self.db.execute("UPDATE jobs SET status='working',attempt=attempt+1,repairs=0,pid=NULL,error=NULL,updated=? WHERE issue=? AND status='blocked'", (time.time(), issue))
                return bool(cursor.rowcount)
        except sqlite3.IntegrityError:
            return False
