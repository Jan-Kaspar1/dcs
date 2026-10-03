"""Durable scheduler state. Reservations are serialized by SQLite, not labels."""
import json
import sqlite3
import time
from pathlib import Path

from .merge_flow import flow_report, repair_attribution, window_bounds


def _probe_ordering(db):
    """Migration 6: record when a group was last congested and when a lease took its slot.

    admission_groups.congested_at is when the group's latest congestion or
    block event was recorded; admission_leases.granted is when the lease took
    its slot, so a recovery probe can only close the episode it was granted
    for. Re-appliable by construction — an upgrade interrupted between the two
    ALTERs, or a rewound database, finds the column already present.
    """
    for table, column, decl in (('admission_groups', 'congested_at', 'REAL'),
                                ('admission_leases', 'granted', 'REAL NOT NULL DEFAULT 0')):
        present = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
        if column not in present:
            db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + column + ' ' + decl)


# Ordered schema steps; each entry is SQL to run or a callable taking the
# connection, and each must be re-appliable — a start rewound to an earlier
# version runs the remainder again.
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
    # 2: Lenovo QA findings lane
    """
    CREATE TABLE IF NOT EXISTS qa_reports(
      run_id TEXT PRIMARY KEY, sha TEXT, status TEXT NOT NULL,
      received REAL NOT NULL, count INTEGER NOT NULL DEFAULT 0, summary TEXT);
    CREATE TABLE IF NOT EXISTS qa_findings(
      key TEXT PRIMARY KEY, kind TEXT NOT NULL, module TEXT NOT NULL,
      severity TEXT NOT NULL, confidence TEXT NOT NULL, title TEXT NOT NULL,
      status TEXT NOT NULL, issue INTEGER, fix_sha TEXT,
      cycles INTEGER NOT NULL DEFAULT 0, occurrences INTEGER NOT NULL DEFAULT 1,
      first_run TEXT NOT NULL, last_run TEXT NOT NULL, sha TEXT,
      payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
    """,
    # 3: concurrency groups are descriptive only; dispatch no longer serializes on them
    """
    DROP INDEX IF EXISTS live_group;
    """,
    # 4: unified admission control — inference leases and quota-group state
    """
    CREATE TABLE IF NOT EXISTS admission_leases(
      owner TEXT PRIMARY KEY, model TEXT NOT NULL, grps TEXT NOT NULL,
      clone TEXT, invocation TEXT, probe INTEGER NOT NULL DEFAULT 0,
      started REAL, updated REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS admission_groups(
      grp TEXT PRIMARY KEY, target INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'normal',
      cooldown_until REAL, cooldown_len REAL NOT NULL DEFAULT 0,
      window_start REAL NOT NULL DEFAULT 0, loaded REAL, useful INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS admission_outcomes(
      invocation TEXT PRIMARY KEY, owner TEXT NOT NULL, grps TEXT NOT NULL,
      category TEXT NOT NULL, useful INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL);
    """,
    # 5: append-only work and invocation transitions for explanation/metrics
    """
    CREATE TABLE IF NOT EXISTS work_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL, issue INTEGER, attempt INTEGER,
      at REAL NOT NULL, source_key TEXT UNIQUE,
      payload TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS work_events_issue ON work_events(issue,id);
    """,
    # 6: order a recovery probe's unblock against later provider feedback.
    # Carried as a step rather than a script because SQLite has no
    # `ADD COLUMN IF NOT EXISTS`, and a start that dies between the two
    # ALTERs must not wedge on the next one.
    _probe_ordering,
]

# Bounded cause classes persisted on 'repair'/'redispatch' work_events rows so
# the rolling merge comparison can attribute repair traffic without reading
# invocation logs. jobs.error keeps the free-text detail; the event keeps class.
REPAIR_CAUSES = frozenset(('merge-conflict', 'ci-failure', 'publish-error'))
REDISPATCH_CAUSES = frozenset(('worker-failure', 'quota-requeue', 'timeout-requeue'))


class State:
    def __init__(self, path, capacity=None):
        self.workspace_capacity = capacity
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
        """)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            if callable(script):
                script(self.db)
                self.db.executescript(f'PRAGMA user_version={index};')
                continue
            self.db.executescript(script + f'PRAGMA user_version={index};')

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def record_event(self, kind, issue=None, attempt=None, payload=None, source_key=None):
        """Append one redacted, idempotent transition to the durable work ledger."""
        if not isinstance(kind, str) or not kind:
            raise ValueError('Event kind required')
        value = json.dumps(payload or {}, sort_keys=True)
        with self.db:
            self.db.execute(
                'INSERT OR IGNORE INTO work_events(kind,issue,attempt,at,source_key,payload) '
                'VALUES(?,?,?,?,?,?)',
                (kind, issue, attempt, time.time(), source_key, value))

    def events(self, issue=None, limit=100):
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError('Event limit must be between 1 and 1000')
        sql = 'SELECT id,kind,issue,attempt,at,payload FROM work_events'
        args = []
        if issue is not None:
            sql += ' WHERE issue=?'
            args.append(issue)
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(limit)
        return [dict(row) for row in self.db.execute(sql, args)]

    def merge_flow(self, now=None, window_seconds=7 * 86400):
        """Compare completed merges in adjacent rolling windows.

        Beyond the merge counts, each window carries the work ledger's
        measured detail through agent_pool.merge_flow's shared classifiers:
        dispatch reservations split into first and retry dispatches,
        merged-and-closed completions, the still-blocked backlog replayed
        to each window's end, ranked conflict paths with the
        concentrated/spread verdict, ranked failing-check names for
        ci-failure repairs, the bounded park-cause counts behind
        status:blocked transitions, and the count of publish merges
        resolved mechanically in-process (a separate ledger kind, so they
        never inflate the agent-repair attribution) read against the
        repairs confined to registered paths. The conflicted paths also
        come back attributed by resolution class (resolved /
        registered_unresolved / unregistered), so the planner reads which
        paths the resolver table covers, which it refused, and which it
        does not cover.
        """
        now = time.time() if now is None else now
        bounds = window_bounds(now, window_seconds)
        rows = self.db.execute(
            "SELECT updated FROM jobs WHERE status='done' AND updated>=?",
            (bounds['previous'][0],)).fetchall()
        current_count = sum(row['updated'] >= bounds['current'][0] for row in rows)
        prior_count = len(rows) - current_count
        decline_percent = round(100 * (prior_count - current_count) / prior_count, 1) if prior_count else None
        events = [dict(row) for row in self.db.execute(
            "SELECT kind,issue,attempt,at,payload FROM work_events")]
        jobs_by_issue = {row['issue']: dict(row) for row in self.db.execute(
            "SELECT issue,status,error,updated FROM jobs")}
        attribution = {}
        flow = {}
        for name, (lo, hi) in bounds.items():
            attribution[name] = repair_attribution(events, lo, hi, {}, jobs_by_issue)
            flow[name] = flow_report(events, lo, hi, jobs_by_issue)
        return {'window_days': round(window_seconds / 86400, 2),
                'current_merges': current_count, 'previous_merges': prior_count,
                'decline_percent': decline_percent,
                'repairs_by_cause': {w: attribution[w]['repairs_by_cause'] for w in bounds},
                'redispatches_by_cause': {w: attribution[w]['redispatches_by_cause'] for w in bounds},
                'conflict_repairs': {w: attribution[w]['conflict_repairs'] for w in bounds},
                'mechanical_resolutions': {w: attribution[w]['mechanical_resolutions'] for w in bounds},
                'conflict_paths': {w: attribution[w]['conflict_paths'] for w in bounds},
                'conflict_paths_by_resolution': {
                    w: attribution[w]['conflict_paths_by_resolution'] for w in bounds},
                'repairs_on_registered_paths': {
                    w: attribution[w]['repairs_on_registered_paths'] for w in bounds},
                'conflict_load': {w: attribution[w]['conflict_load'] for w in bounds},
                'failing_checks': {w: attribution[w]['failing_checks'] for w in bounds},
                'dispatches': {w: flow[w]['dispatches'] for w in bounds},
                'first_dispatches': {w: flow[w]['first_dispatches'] for w in bounds},
                'retry_dispatches': {w: flow[w]['retry_dispatches'] for w in bounds},
                'merged_and_closed': {w: flow[w]['merged_and_closed'] for w in bounds},
                'still_blocked': {w: flow[w]['still_blocked'] for w in bounds},
                'parked': {w: flow[w]['parked'] for w in bounds},
                'park_causes': {w: flow[w]['park_causes'] for w in bounds}}

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
        if self.workspace_capacity is not None:
            return self.workspace_capacity
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
            self.db.execute('INSERT INTO work_events(kind,issue,attempt,at,payload) VALUES(?,?,?,?,?)',
                            ('reserved', issue, 1, now, json.dumps({'worker': worker})))
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
            previous = self.db.execute('SELECT status,attempt FROM jobs WHERE issue=?', (issue,)).fetchone()
            self.db.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE issue=?',
                            (*fields.values(), issue))
            if previous and 'status' in fields and fields['status'] != previous['status']:
                self.db.execute('INSERT INTO work_events(kind,issue,attempt,at,payload) VALUES(?,?,?,?,?)',
                                ('status:' + fields['status'], issue, previous['attempt'],
                                 fields['updated'], json.dumps({'from': previous['status']})))

    def repair(self, issue, cause, detail=None):
        if cause not in REPAIR_CAUSES:
            raise ValueError('Invalid repair cause')
        with self.db:
            now = time.time()
            cursor = self.db.execute("UPDATE jobs SET repairs=repairs+1,status='working',updated=? WHERE issue=? AND repairs<3 AND status IN ('working','pr-open')", (now, issue))
            if cursor.rowcount:
                row = self.db.execute('SELECT attempt,repairs FROM jobs WHERE issue=?', (issue,)).fetchone()
                payload = {'cause': cause}
                if detail:
                    payload.update(detail)
                self.db.execute('INSERT OR IGNORE INTO work_events(kind,issue,attempt,at,source_key,payload) VALUES(?,?,?,?,?,?)',
                                ('repair', issue, row['attempt'], now,
                                 'repair:%s:%s:%s' % (issue, row['attempt'], row['repairs']),
                                 json.dumps(payload, sort_keys=True)))
                return True
            self.db.execute("UPDATE jobs SET status='blocked',error='Repair limit exhausted',updated=? WHERE issue=? AND status!='done'", (now, issue))
            return False

    def complete(self, issue):
        with self.db:
            cursor = self.db.execute("UPDATE jobs SET status='done',pid=NULL,updated=? WHERE issue=? AND status!='done'", (time.time(), issue))
            if cursor.rowcount:
                attempt = self.db.execute('SELECT attempt FROM jobs WHERE issue=?', (issue,)).fetchone()[0]
                self.db.execute('INSERT INTO work_events(kind,issue,attempt,at,payload) VALUES(?,?,?,?,?)',
                                ('merged-and-closed', issue, attempt, time.time(), '{}'))
                merges = self.get('merges', 0) + 1
                self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('merges', json.dumps(merges)))
                for key in ('recovery:' + str(issue), 'retry:' + str(issue)):
                    self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, 'null'))

    def retry(self, issue, cause, detail=None):
        """Explicit operator retry. Keeps prior branch/clone recorded until replacement."""
        if cause not in REDISPATCH_CAUSES:
            raise ValueError('Invalid redispatch cause')
        try:
            with self.db:
                now = time.time()
                cursor = self.db.execute("UPDATE jobs SET status='working',attempt=attempt+1,repairs=0,pid=NULL,error=NULL,updated=? WHERE issue=? AND status='blocked'", (now, issue))
                if cursor.rowcount:
                    attempt = self.db.execute('SELECT attempt FROM jobs WHERE issue=?', (issue,)).fetchone()[0]
                    self.db.execute('INSERT INTO work_events(kind,issue,attempt,at,payload) VALUES(?,?,?,?,?)',
                                    ('retry-reserved', issue, attempt, now, '{}'))
                    payload = {'cause': cause}
                    if detail:
                        payload.update(detail)
                    self.db.execute('INSERT OR IGNORE INTO work_events(kind,issue,attempt,at,source_key,payload) VALUES(?,?,?,?,?,?)',
                                    ('redispatch', issue, attempt, now,
                                     'redispatch:%s:%s' % (issue, attempt),
                                     json.dumps(payload, sort_keys=True)))
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

    # --- Lenovo QA findings lane -------------------------------------------

    def qa_report(self, run_id):
        row = self.db.execute('SELECT * FROM qa_reports WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def record_qa_report(self, run_id, sha, status, count=0, summary=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO qa_reports(run_id,sha,status,received,count,summary) '
                            'VALUES(?,?,?,?,?,?)',
                            (run_id, sha, status, time.time(), count, summary))

    def qa_finding(self, key):
        row = self.db.execute('SELECT * FROM qa_findings WHERE key=?', (key,)).fetchone()
        return dict(row) if row else None

    def qa_findings(self, statuses=None):
        query, args = 'SELECT * FROM qa_findings', []
        if statuses:
            args = list(statuses)
            query += ' WHERE status IN (' + ','.join('?' for _ in args) + ')'
        return [dict(r) for r in self.db.execute(query + ' ORDER BY created', args)]

    def upsert_qa_finding(self, finding, report, status):
        """Insert a new finding or re-observe an existing one.

        Re-observation refreshes the stored redacted payload and bumps the
        occurrence counter; lifecycle columns (status, issue, fix_sha, cycles)
        move only through set_qa_finding.
        """
        payload = json.dumps({**finding, '_run_id': report['run_id'],
                              '_sha': report['sha'], '_rig': report['rig'],
                              '_report_status': report.get('status', 'completed')})
        now = time.time()
        with self.db:
            cursor = self.db.execute(
                'UPDATE qa_findings SET kind=?,module=?,severity=?,confidence=?,title=?,'
                'sha=?,last_run=?,payload=?,occurrences=occurrences+1,updated=? WHERE key=?',
                (finding['kind'], finding['module'], finding['severity'],
                 finding['confidence'], finding['title'], report['sha'],
                 report['run_id'], payload, now, finding['key']))
            if not cursor.rowcount:
                self.db.execute(
                    'INSERT INTO qa_findings(key,kind,module,severity,confidence,title,'
                    'status,first_run,last_run,sha,payload,created,updated) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (finding['key'], finding['kind'], finding['module'],
                     finding['severity'], finding['confidence'], finding['title'],
                     status, report['run_id'], report['run_id'], report['sha'],
                     payload, now, now))

    def set_qa_finding(self, key, **fields):
        allowed = {'status', 'issue', 'fix_sha', 'cycles', 'title', 'sha'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid finding fields')
        fields['updated'] = time.time()
        with self.db:
            self.db.execute('UPDATE qa_findings SET ' + ','.join(k + '=?' for k in fields) + ' WHERE key=?',
                            (*fields.values(), key))

    def qa_count(self, statuses):
        args = list(statuses)
        return self.db.execute('SELECT COUNT(*) FROM qa_findings WHERE status IN ('
                               + ','.join('?' for _ in args) + ')', args).fetchone()[0]

    def pending_verifications(self):
        """Findings whose fix merged and still owe a hardware/sim verification."""
        return self.qa_findings(('fix-merged',))

    def qa_summary(self):
        counts = {r['status']: r['n'] for r in self.db.execute(
            'SELECT status, COUNT(*) AS n FROM qa_findings GROUP BY status')}
        return {'findings': counts,
                'pending_verifications': len(self.pending_verifications()),
                'reports': self.db.execute('SELECT COUNT(*) FROM qa_reports').fetchone()[0],
                'published_at': self.get('qa:published_at'),
                'publish_error': self.get('qa:publish_error')}
