"""Durable scheduler state. Reservations are serialized by SQLite, not labels."""
import json
import sqlite3
import time
from pathlib import Path


MIGRATIONS = [
    # 1: daily architecture review lane
    """
    CREATE TABLE IF NOT EXISTS reviews(
      run_id TEXT PRIMARY KEY, slot INTEGER NOT NULL, attempted_sha TEXT,
      completed_sha TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
      started REAL NOT NULL, finished REAL, error TEXT, report TEXT);
    CREATE TABLE IF NOT EXISTS candidates(
      key TEXT PRIMARY KEY, run_id TEXT NOT NULL, title TEXT NOT NULL,
      outcome TEXT NOT NULL, disposition TEXT NOT NULL DEFAULT 'pending',
      issue INTEGER, reason TEXT, revisit TEXT, assessed INTEGER NOT NULL DEFAULT 0,
      assessment TEXT, created REAL NOT NULL, updated REAL NOT NULL,
      payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS improvement_issues(
      improvement TEXT NOT NULL, issue INTEGER NOT NULL,
      PRIMARY KEY(improvement, issue));
    """,
]


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
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.db.executescript(script + f'PRAGMA user_version={index};')

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
                for key in ('recovery:' + str(issue), 'retry:' + str(issue)):
                    self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, 'null'))

    def retry(self, issue):
        """Explicit operator retry. Keeps prior branch/clone recorded until replacement."""
        try:
            with self.db:
                cursor = self.db.execute("UPDATE jobs SET status='working',attempt=attempt+1,repairs=0,pid=NULL,error=NULL,updated=? WHERE issue=? AND status='blocked'", (time.time(), issue))
                return bool(cursor.rowcount)
        except sqlite3.IntegrityError:
            return False

    def review(self, run_id=None):
        if run_id:
            row = self.db.execute('SELECT * FROM reviews WHERE run_id=?', (run_id,)).fetchone()
        else:
            row = self.db.execute('SELECT * FROM reviews ORDER BY started DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def running_review(self):
        row = self.db.execute("SELECT * FROM reviews WHERE status='running' ORDER BY started DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def begin_review(self, run_id, slot, sha, attempt):
        with self.db:
            self.db.execute("INSERT INTO reviews(run_id,slot,attempted_sha,status,attempt,started) VALUES(?,?,?,'running',?,?)",
                            (run_id, slot, sha, attempt, time.time()))

    def finish_review(self, run_id, status, error=None, completed_sha=None, report=None):
        with self.db:
            self.db.execute("UPDATE reviews SET status=?,finished=?,error=?,"
                            "completed_sha=COALESCE(?,completed_sha),report=COALESCE(?,report) WHERE run_id=?",
                            (status, time.time(), error, completed_sha, report, run_id))
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                            ('review:last_finished_at', json.dumps(time.time())))

    def last_completed_sha(self):
        row = self.db.execute("SELECT completed_sha FROM reviews WHERE completed_sha IS NOT NULL "
                              "ORDER BY finished DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def record_candidates(self, run_id, candidates):
        """Persist new pending candidates; returns {key: prior disposition} skipped.

        Re-proposals of a rejected or deferred key reopen it only when the
        payload changed materially — the recorded revisit condition or new
        evidence is the reviewer's reason for resubmitting it. Identical
        re-proposals stay suppressed.
        """
        skipped = {}
        now = time.time()
        with self.db:
            for item in candidates:
                row = self.db.execute('SELECT disposition,payload FROM candidates WHERE key=?', (item['key'],)).fetchone()
                if row and row['disposition'] in ('deferred', 'rejected') and json.loads(row['payload']) != item:
                    self.db.execute("UPDATE candidates SET disposition='pending',run_id=?,title=?,outcome=?,"
                                    "reason=NULL,revisit=NULL,assessed=0,assessment=NULL,payload=?,updated=? WHERE key=?",
                                    (run_id, item['title'], item['outcome'], json.dumps(item), now, item['key']))
                    continue
                if row:
                    skipped[item['key']] = row['disposition']
                    continue
                self.db.execute("INSERT INTO candidates(key,run_id,title,outcome,disposition,payload,created,updated) "
                                "VALUES(?,?,?,?,'pending',?,?,?)",
                                (item['key'], run_id, item['title'], item['outcome'],
                                 json.dumps(item), now, now))
        return skipped

    def candidate(self, key):
        row = self.db.execute('SELECT * FROM candidates WHERE key=?', (key,)).fetchone()
        return dict(row) if row else None

    def candidates(self, disposition=None):
        query, args = 'SELECT * FROM candidates', []
        if disposition:
            query += ' WHERE disposition=?'
            args.append(disposition)
        return [dict(r) for r in self.db.execute(query + ' ORDER BY created', args)]

    def disposition_candidate(self, key, decision, reason='', revisit=None, issue=None,
                              sources=('pending',)):
        if decision not in ('accepted', 'deferred', 'rejected'):
            raise ValueError('Invalid disposition')
        marks = ','.join('?' for _ in sources)
        with self.db:
            return bool(self.db.execute(
                "UPDATE candidates SET disposition=?,reason=?,revisit=?,issue=COALESCE(?,issue),updated=? "
                "WHERE key=? AND disposition IN (" + marks + ")",
                (decision, reason[:4000], revisit, issue, time.time(), key, *sources)).rowcount)

    def map_improvement(self, improvement, issue):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO improvement_issues VALUES(?,?)', (improvement, issue))

    def improvement_issues(self, improvement):
        return [r[0] for r in self.db.execute(
            'SELECT issue FROM improvement_issues WHERE improvement=? ORDER BY issue', (improvement,))]

    def unresolved_accepted(self):
        """Accepted, unassessed improvements with unfinished or unmapped work.

        Complement of pending_assessments: these still owe the planner an
        outcome — in-flight issues, chains stalled behind a blocked
        prerequisite, or accepted work that never received a mapped issue.
        """
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM candidates WHERE disposition='accepted' AND assessed=0 "
            "AND (NOT EXISTS (SELECT 1 FROM improvement_issues WHERE improvement=candidates.key) "
            "     OR EXISTS (SELECT 1 FROM improvement_issues ii LEFT JOIN jobs j ON j.issue=ii.issue "
            "                WHERE ii.improvement=candidates.key AND (j.status IS NULL OR j.status!='done')))")]

    def pending_assessments(self):
        """Accepted improvements whose mapped issues all finished and are unassessed."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM candidates WHERE disposition='accepted' AND assessed=0 "
            "AND EXISTS (SELECT 1 FROM improvement_issues WHERE improvement=candidates.key) "
            "AND NOT EXISTS (SELECT 1 FROM improvement_issues ii LEFT JOIN jobs j ON j.issue=ii.issue "
            "              WHERE ii.improvement=candidates.key AND (j.status IS NULL OR j.status!='done'))")]

    def record_assessment(self, key, assessment):
        with self.db:
            self.db.execute('UPDATE candidates SET assessed=1,assessment=?,updated=? WHERE key=?',
                            (json.dumps(assessment), time.time(), key))

    def review_summary(self):
        return {'latest': self.review(),
                'running': self.get('reviewer'),
                'stage': self.get('review:stage'),
                'last_slot': self.get('review:last_slot'),
                'active_improvement': self.get('review:active_improvement'),
                'pending_candidates': len(self.candidates('pending')),
                'pending_assessments': len(self.pending_assessments()),
                'rollout_failure': self.get('review:rollout_failure')}
