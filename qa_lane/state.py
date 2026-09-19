"""Durable QA lane state. Mirrors agent_pool.state conventions:
stdlib SQLite, WAL, JSON values in a settings table, and a runs table
whose records survive process death so reconciliation can complete
interrupted runs on the next start.

Budget semantics: a run's `day` is the UTC date on which it actually
begins executing — stamped by begin(), not by enqueue(). A run queued
at 23:55 that the supervisor starts at 00:30 counts once, against the
date it started. The day stored at enqueue time is only a hint for
queued records and is overwritten when the run begins.
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

# settings keys owned by the resource-bounds machinery:
#   cleanup_errors - {key: {detail, first_seen, last_seen}} named
#                    teardown/reclaim failures, cleared on success
#   preserve       - {'runs': [run_id], 'shas': [sha]} retention pins
#                    set by the findings/verification lane so evidence
#                    tied to unresolved findings is never reaped
#   blocked        - {reason, detail, since} while the lane refuses to
#                    start runs (fail-closed), cleared when gates pass


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

        Only the newest queued revision runs: every still-queued assessment
        record is superseded, and the new record's range_first carries the
        oldest untested revision so the report preserves the intervening
        range. Re-enqueueing the same SHA is a no-op. Dedicated run kinds
        (qav- verifications, qax- explorations) never participate: a
        running exploration must not suppress a real assessment, and a
        queued verification survives a newer dispatch.
        """
        for row in self.runs(('queued', 'running')):
            if row['run_id'].startswith('qa-') \
                    and row['attempted_sha'] == sha:
                return row
        queued = [r for r in self.runs(('queued',))
                  if r['run_id'].startswith('qa-')]
        range_first = None
        for row in queued:
            if range_first is None:
                range_first = row['range_first'] or row['attempted_sha']
        if range_first is None:
            last = [r for r in self.runs(('finished', 'interrupted'))
                    if r['run_id'].startswith('qa-')]
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

    def queue_dedicated(self, run_id, sha, now, day):
        """Queue one dedicated run record (qav- verifications, qax-
        explorations). Unlike enqueue() this never supersedes queued
        assessment runs and carries no range_first — dedicated kinds are
        additive work dispatched by their own lanes.
        """
        with self.db:
            self.db.execute(
                'INSERT INTO runs(run_id,attempted_sha,status,attempt,day,'
                "created) VALUES(?,?, 'queued', 1, ?, ?)",
                (run_id, sha, day, now))
        return self.run(run_id)

    def queue_verification(self, run_id, sha, now, day):
        """Queue a dedicated fix-verification run (run ids 'qav-*').

        Unlike enqueue() this never supersedes queued assessment runs:
        verification work is additive and is dispatched ahead of the
        newest-SHA assessment by the cycle.
        """
        return self.queue_dedicated(run_id, sha, now, day)

    def next_queued(self, prefix=None):
        queued = self.runs(('queued',))
        if prefix is not None:
            queued = [r for r in queued
                      if r['run_id'].startswith(prefix + '-')]
        return queued[-1] if queued else None

    def attempts_for(self, sha):
        row = self.db.execute(
            "SELECT COALESCE(MAX(attempt),0) FROM runs WHERE attempted_sha=? "
            "AND status IN ('running','finished','interrupted')",
            (sha,)).fetchone()
        return row[0]

    def started_today(self, day, prefix=None):
        """Runs that began on UTC `day`. `prefix` scopes the count to one
        run kind ('qa', 'qav', 'qax') so dedicated budgets never eat the
        assessment allowance or vice versa."""
        query = "SELECT COUNT(*) FROM runs WHERE day=? AND status IN " \
                "('running','finished','interrupted')"
        args = [day]
        if prefix is not None:
            query += " AND run_id LIKE ?"
            args.append(prefix + '-%')
        return self.db.execute(query, args).fetchone()[0]

    def last_started(self, prefix):
        """Newest start timestamp among runs of one kind, or None."""
        row = self.db.execute(
            "SELECT MAX(started) FROM runs WHERE run_id LIKE ?",
            (prefix + '-%',)).fetchone()
        return row[0]

    def begin(self, run_id, pid, now):
        """Mark a queued run executing. `day` is restamped to the UTC
        execution date so the daily budget counts the day a run
        actually ran, not the day it was dispatched."""
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='running', pid=?, started=?, "
                "day=? WHERE run_id=? AND status='queued'",
                (pid, now, day, run_id))
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

    def attach_report(self, run_id, path, now):
        """Record a report path without changing lifecycle status —
        used when reconciliation writes an interrupted run's report."""
        with self.db:
            self.db.execute(
                'UPDATE runs SET report=?, finished=? WHERE run_id=?',
                (path, now, run_id))

    def last_attempted_sha(self):
        """The newest revision the lane was asked to assess. 'blocked'
        runs never attempted the revision, so they do not suppress
        redispatch of the same SHA. Dedicated run kinds (qav-, qax-)
        do not move this pointer — the WSL relay keys new-SHA dispatch
        off it and an exploration of an older revision must never
        regress it into a redundant re-queue."""
        rows = [r for r in
                self.runs(('queued', 'running', 'finished', 'interrupted'))
                if r['run_id'].startswith('qa-') and r['outcome'] != 'blocked']
        return rows[-1]['attempted_sha'] if rows else None

    def latest_verdicted_sha(self):
        """The newest revision whose deterministic assessment produced a
        verdict (passed/failed with completed_sha) — the exploration
        lane's target. Runs are ordered by creation, so the last
        verdicted assessment names the latest gate-covered revision."""
        rows = [r for r in self.runs(('finished',))
                if r['run_id'].startswith('qa-')
                and r['outcome'] in ('passed', 'failed')
                and r['completed_sha']]
        return rows[-1]['completed_sha'] if rows else None

    # -- cleanup-failure ledger -------------------------------------------
    # Failures are keyed so a later success clears exactly the failure it
    # resolved. The ledger is the durable record: run reports and status
    # surface it, and the cycle fails closed while teardown leftovers
    # remain.

    def cleanup_errors(self):
        return self.get('cleanup_errors', {})

    def record_cleanup_error(self, key, detail, now=None):
        """Record a named cleanup failure (idempotent per key)."""
        now = time.time() if now is None else now
        errors = self.cleanup_errors()
        entry = errors.get(key, {})
        entry['detail'] = detail[:500]
        entry.setdefault('first_seen', now)
        entry['last_seen'] = now
        errors[key] = entry
        self.set('cleanup_errors', errors)

    def clear_cleanup_error(self, key):
        errors = self.cleanup_errors()
        if key in errors:
            del errors[key]
            self.set('cleanup_errors', errors)

    # -- retention pins -----------------------------------------------------

    def preserved(self):
        preserve = self.get('preserve', {})
        return {'runs': list(preserve.get('runs', [])),
                'shas': list(preserve.get('shas', []))}

    def set_preserve(self, kind, value, on=True):
        """Pin ('run'/'sha') or unpin evidence for retention. The
        findings/verification lane calls `qa_lane preserve` to keep
        run dirs, reports, source trees, and images alive while a
        finding or fix verification remains unresolved."""
        if kind not in ('run', 'sha'):
            raise ValueError('preserve kind must be run or sha')
        plural = kind + 's'
        preserve = self.get('preserve', {})
        entries = list(preserve.get(plural, []))
        if on and value not in entries:
            entries.append(value)
        elif not on:
            entries = [e for e in entries if e != value]
        preserve[plural] = entries
        self.set('preserve', preserve)
        return self.preserved()
