"""Durable QA lane state. Mirrors agent_pool.state conventions:
stdlib SQLite, WAL, JSON values in a settings table, and a runs table
whose records survive process death so reconciliation can complete
interrupted runs on the next start.
"""
import json
import sqlite3
import time
from pathlib import Path

# Run record lifecycle:
#   queued    - submitted by WSL dispatch, waiting for a cycle
#   running   - a live runner process owns it (pid recorded)
#   finished  - a report was produced (outcome set)
#   interrupted - runner died mid-run; reconciled, outcome 'interrupted'
#   superseded - a queued run replaced by a newer queued revision
RUN_STATUSES = ('queued', 'running', 'finished', 'interrupted', 'superseded')
ACTIVE_STATUSES = ('running',)


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(
              key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
              run_id TEXT PRIMARY KEY,
              attempted_sha TEXT NOT NULL,
              completed_sha TEXT,
              status TEXT NOT NULL,
              outcome TEXT,
              attempt INTEGER NOT NULL DEFAULT 1,
              day TEXT NOT NULL,
              pid INTEGER,
              created REAL NOT NULL,
              started REAL,
              finished REAL,
              report TEXT,
              error TEXT,
              range_first TEXT);
        """)

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute(
            'SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO settings VALUES(?,?)',
                (key, json.dumps(value)))

    def run(self, run_id):
        row = self.db.execute(
            'SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def runs(self, statuses=None):
        query, args = 'SELECT * FROM runs', []
        if statuses:
            args = list(statuses)
            query += ' WHERE status IN (' + ','.join('?' * len(args)) + ')'
        return [dict(r) for r in
                self.db.execute(query + ' ORDER BY created', args)]

    def enqueue(self, run_id, sha, now, day, attempt=1):
        """Queue a revision for testing. Returns the queued record.

        Only the newest queued revision runs: every still-queued record is
        superseded, and the new record's range_first carries the oldest
        untested revision so the report preserves the intervening range.
        Re-enqueueing the same SHA is a no-op.
        """
        for row in self.runs(('queued', 'running')):
            if row['attempted_sha'] == sha:
                return row
        queued = self.runs(('queued',))
        range_first = None
        for row in queued:
            if range_first is None:
                range_first = row['range_first'] or row['attempted_sha']
        if range_first is None:
            last = self.runs(('finished', 'interrupted'))
            if last:
                prev = last[-1]['completed_sha'] or last[-1]['attempted_sha']
                if prev != sha:
                    range_first = prev
        with self.db:
            for row in queued:
                self.db.execute(
                    "UPDATE runs SET status='superseded', finished=? "
                    'WHERE run_id=?', (now, row['run_id']))
            self.db.execute(
                'INSERT INTO runs(run_id,attempted_sha,status,attempt,day,'
                'created,range_first) VALUES(?,?,?,?,?,?,?)',
                (run_id, sha, 'queued', attempt, day, now, range_first))
        return self.run(run_id)

    def next_queued(self):
        queued = self.runs(('queued',))
        return queued[-1] if queued else None

    def attempts_for(self, sha):
        row = self.db.execute(
            "SELECT COALESCE(MAX(attempt),0) FROM runs WHERE attempted_sha=? "
            "AND status IN ('running','finished','interrupted')",
            (sha,)).fetchone()
        return row[0]

    def started_today(self, day):
        row = self.db.execute(
            "SELECT COUNT(*) FROM runs WHERE day=? AND status IN "
            "('running','finished','interrupted')", (day,)).fetchone()
        return row[0]

    def begin(self, run_id, pid, now):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='running', pid=?, started=? "
                "WHERE run_id=? AND status='queued'", (pid, now, run_id))
        return self.run(run_id)

    def finish(self, run_id, outcome, completed_sha, report, now):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='finished', outcome=?, "
                'completed_sha=?, report=?, finished=? WHERE run_id=?',
                (outcome, completed_sha, report, now, run_id))

    def interrupt(self, run_id, error, now):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='interrupted', "
                "outcome='interrupted', error=?, finished=? "
                'WHERE run_id=?', (error, now, run_id))

    def fail_queued(self, run_id, error, now):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='interrupted', "
                "outcome='interrupted', error=?, finished=? "
                'WHERE run_id=?', (error, now, run_id))

    def last_attempted_sha(self):
        rows = self.runs(('queued', 'running', 'finished', 'interrupted'))
        return rows[-1]['attempted_sha'] if rows else None
