"""Unified admission control for every managed invocation.

One lease must be reserved before any agent spawn — worker, retry,
repair, planner, or reviewer. Leases are durable SQLite rows, so a
reservation is atomic across supervisor connections and survives
restarts for reconciliation. Inference capacity (per quota group) is
accounted separately from workspace ownership: a PR awaiting CI keeps
its clone lease in `jobs` while releasing its inference lease here.

Quota feedback is scoped to the affected group: a rate-limit receipt
cools down only the models sharing that provider budget, other groups
keep dispatching, and recovery reopens with a single probe session
instead of a retry wave. Authentication and credit failures block the
group until an explicit operator reset — sleeping cannot fix them.
"""
import json
import math
import random
import re
import time

AUTH_WORDS = ('authentication failed', 'authentication error', 'unauthorized',
              'invalid api key', 'invalid token', 'http 401', '401 unauthorized')
CREDIT_WORDS = ('insufficient credits', 'credit balance', 'payment required',
                'http 402', 'billing')
RATE_WORDS = ('rate limit', 'ratelimit', 'quota exceeded', 'too many requests',
              'http 429', '429 too many')
ENDPOINT_WORDS = ('endpoint is unavailable', 'unexpected server error',
                  'internal server error', 'bad gateway', 'service unavailable',
                  'connection refused', 'econnrefused', 'http 500', 'http 502',
                  'http 503', 'http 504')
RETRY_AFTER = re.compile(r'retry[-_ ]?after[^0-9]{0,10}(\d{1,6})', re.IGNORECASE)


def classify(receipt, text):
    """Categorize a finished invocation for quota-group feedback.

    Returns (category, retry_after_seconds). Provider signals in the
    transcript tail are honored only for failed invocations — a clean
    exit is 'success' even if its output merely mentions rate limits.
    A timeout receipt wins over transcript wording, except when the tail
    carries a provider 'stream error' event — opencode freezes after a
    rate-limited stream and the runner's timeout then masks the real
    cause, so that specific event still scopes the cooldown.
    """
    receipt = receipt or {}
    if receipt.get('status') == 'timeout':
        tail = (text or '')[-16000:].lower()
        if 'stream error' in tail:
            if any(word in tail for word in RATE_WORDS):
                return 'rate', None
            if any(word in tail for word in ENDPOINT_WORDS):
                return 'endpoint', None
        return 'timeout', None
    code = receipt.get('returncode', receipt.get('exit_code', -1))
    if code == 0:
        return 'success', None
    tail = (text or '')[-16000:].lower()
    hint = RETRY_AFTER.search(tail)
    retry_after = int(hint.group(1)) if hint else None
    if any(word in tail for word in AUTH_WORDS):
        return 'auth', None
    if any(word in tail for word in CREDIT_WORDS):
        return 'credits', None
    if any(word in tail for word in RATE_WORDS) or retry_after is not None:
        return 'rate', retry_after
    if any(word in tail for word in ENDPOINT_WORDS):
        return 'endpoint', retry_after
    return 'failure', None


class Admission:
    """Durable inference-capacity ledger over shared scheduler state."""

    def __init__(self, state, config, clock=time.time, jitter=None):
        self.state = state
        self.db = state.db
        self.clock = clock
        self.jitter = jitter or (lambda: random.uniform(0, 60))
        sched = config.get('scheduler') or {}
        self.quiet = sched.get('quiet_seconds', 2700)
        self.cooldown = sched.get('cooldown_seconds', 600)
        self.max_cooldown = sched.get('max_cooldown_seconds', 7200)
        self.max_quota_requeues = sched.get('max_quota_requeues', 4)
        self.groups = sched.get('groups') or self._derive_groups(config)

    @staticmethod
    def _derive_groups(config):
        """One quota group per configured model, seeded from model_caps."""
        caps = config.get('model_caps') or {}
        models = config.get('models') or [config.get('model', 'swe-2-high')]
        groups = {}
        for model in dict.fromkeys(models):
            initial = caps.get(model, 4)
            groups[model] = {'models': [model], 'initial': initial,
                             'ceiling': max(initial * 3, initial + 1),
                             'external_slots': 0}
        return groups

    def group_names(self, model):
        return [name for name, cfg in self.groups.items() if model in cfg['models']]

    def _tx(self):
        self.db.commit()
        self.db.execute('BEGIN IMMEDIATE')

    def _group(self, name):
        """Group state row, created from config on first use. Call inside a tx."""
        row = self.db.execute('SELECT * FROM admission_groups WHERE grp=?', (name,)).fetchone()
        if row:
            return dict(row)
        cfg = self.groups[name]
        self.db.execute('INSERT INTO admission_groups(grp,target,mode,cooldown_len,window_start,useful) '
                        'VALUES(?,?,?,?,0,0)', (name, cfg['initial'], 'normal', self.cooldown))
        return {'grp': name, 'target': cfg['initial'], 'mode': 'normal',
                'cooldown_until': None, 'cooldown_len': self.cooldown,
                'window_start': 0, 'loaded': None, 'useful': 0}

    def _active(self, name, probe=None):
        query = "SELECT COUNT(*) FROM admission_leases WHERE instr(grps, ?)"
        args = ['"' + name + '"']
        if probe is not None:
            query += ' AND probe=?'
            args.append(probe)
        return self.db.execute(query, args).fetchone()[0]

    def reserve(self, owner, model, clone, limit):
        """Atomically take an inference lease, or return False with the reason held.

        Denied when paused, the clone is already leased, the global bound is
        full, or any quota group covering this model is blocked, cooling down,
        probing (one outstanding probe), or at its adaptive target.
        """
        names = self.group_names(model)
        now = self.clock()
        try:
            self._tx()
            if self.state.paused():
                self.db.rollback()
                return False
            if self.db.execute('SELECT 1 FROM admission_leases WHERE owner=?', (owner,)).fetchone():
                self.db.commit()
                return True
            if clone is not None and self.db.execute(
                    'SELECT 1 FROM admission_leases WHERE clone=? LIMIT 1', (clone,)).fetchone():
                self.db.rollback()
                return False
            if self.db.execute('SELECT COUNT(*) FROM admission_leases').fetchone()[0] >= limit:
                self.db.rollback()
                return False
            probe_grant = False
            for name in names:
                group = self._group(name)
                effective = max(0, group['target'] - self.groups[name].get('external_slots', 0))
                active = self._active(name)
                denied = (group['mode'] == 'blocked'
                          or (group['cooldown_until'] is not None and now < group['cooldown_until']))
                if not denied and group['mode'] == 'probing':
                    denied = self._active(name, probe=1) >= 1 or active >= max(1, effective)
                    probe_grant = probe_grant or not denied
                elif not denied:
                    denied = active >= effective
                    if denied:
                        self.db.execute('UPDATE admission_groups SET loaded=? WHERE grp=?', (now, name))
                if denied:
                    self.db.commit()
                    return False
            for name in names:
                group = self._group(name)
                if (group['mode'] == 'normal' and group['useful'] >= 1
                        and group['loaded'] is not None and group['loaded'] >= group['window_start']
                        and now - group['window_start'] >= self.quiet):
                    self.db.execute('UPDATE admission_groups SET target=?,window_start=?,useful=0,loaded=NULL WHERE grp=?',
                                    (min(self.groups[name]['ceiling'], group['target'] + 1), now, name))
            self.db.execute('INSERT INTO admission_leases(owner,model,grps,clone,probe,updated) VALUES(?,?,?,?,?,?)',
                            (owner, model, json.dumps(names), clone, int(probe_grant), now))
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def attach(self, owner, meta):
        """Bind a reserved lease to its spawned invocation record."""
        with self.db:
            self.db.execute('UPDATE admission_leases SET invocation=?,started=?,updated=? WHERE owner=?',
                            (meta.get('invocation'), meta.get('started_at'), self.clock(), owner))

    def finish(self, owner, meta, category, retry_after=None):
        """Record one deduplicated outcome and release the inference lease.

        A repeated report for the same invocation never spends another
        penalty: congestion episodes decrease the group target once.
        """
        now = self.clock()
        try:
            self._tx()
            lease = self.db.execute('SELECT * FROM admission_leases WHERE owner=?', (owner,)).fetchone()
            lease = dict(lease) if lease else None
            invocation = meta.get('invocation') or (lease or {}).get('invocation')
            if invocation and self.db.execute(
                    'SELECT 1 FROM admission_outcomes WHERE invocation=?', (invocation,)).fetchone():
                self.db.execute('DELETE FROM admission_leases WHERE owner=?', (owner,))
                self.db.commit()
                return
            names = json.loads(lease['grps']) if lease else self.group_names(meta.get('model') or '')
            if lease:
                self.db.execute('DELETE FROM admission_leases WHERE owner=?', (owner,))
            self.db.execute('INSERT INTO admission_outcomes(invocation,owner,grps,category,at) VALUES(?,?,?,?,?)',
                            (invocation or owner + '@' + str(now), owner, json.dumps(names), category, now))
            for name in names:
                group = self._group(name)
                if category in ('rate', 'endpoint'):
                    if group['mode'] == 'normal':
                        cooldown_until = now + (retry_after or group['cooldown_len']) + self.jitter()
                        self.db.execute('UPDATE admission_groups SET target=?,mode=?,cooldown_until=?,'
                                        'window_start=?,useful=0,loaded=NULL WHERE grp=?',
                                        (max(1, math.ceil(group['target'] / 2)), 'probing',
                                         cooldown_until, now, name))
                    else:
                        length = retry_after or group['cooldown_len'] or self.cooldown
                        cooldown_until = max(group['cooldown_until'] or 0, now + length + self.jitter())
                        new_len = group['cooldown_len']
                        if lease and lease['probe']:
                            new_len = min(self.max_cooldown, max(new_len, self.cooldown) * 2)
                        self.db.execute('UPDATE admission_groups SET cooldown_until=?,cooldown_len=? WHERE grp=?',
                                        (cooldown_until, new_len, name))
                elif category in ('auth', 'credits'):
                    self.db.execute("UPDATE admission_groups SET mode='blocked',cooldown_until=NULL WHERE grp=?", (name,))
                elif category == 'success' and lease and lease['probe']:
                    self.db.execute("UPDATE admission_groups SET mode='normal',cooldown_until=NULL,cooldown_len=? WHERE grp=?",
                                    (self.cooldown, name))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def useful(self, meta):
        """Credit a verified useful completion to its quota groups' growth window."""
        try:
            self._tx()
            row = None
            if meta.get('invocation'):
                row = self.db.execute('SELECT * FROM admission_outcomes WHERE invocation=?',
                                      (meta['invocation'],)).fetchone()
            if row is None and meta.get('owner'):
                row = self.db.execute('SELECT * FROM admission_outcomes WHERE owner=? ORDER BY at DESC LIMIT 1',
                                      (meta['owner'],)).fetchone()
            if row and not row['useful']:
                self.db.execute('UPDATE admission_outcomes SET useful=1 WHERE invocation=?', (row['invocation'],))
                for name in json.loads(row['grps']):
                    self._group(name)
                    self.db.execute('UPDATE admission_groups SET useful=useful+1 WHERE grp=?', (name,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def release(self, owner):
        """Drop a lease without an outcome (launch abort, blocked job, stop)."""
        with self.db:
            self.db.execute('DELETE FROM admission_leases WHERE owner=?', (owner,))

    def reset(self, name):
        """Operator reset of a blocked or cooling quota group."""
        if name not in self.groups:
            raise ValueError('Unknown quota group: ' + str(name))
        try:
            self._tx()
            self._group(name)
            self.db.execute("UPDATE admission_groups SET mode='normal',cooldown_until=NULL,cooldown_len=?,"
                            'window_start=?,useful=0,loaded=NULL WHERE grp=?',
                            (self.cooldown, self.clock(), name))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def reconcile(self, owners):
        """Drop leases whose owner no longer exists after a restart."""
        with self.db:
            if owners:
                marks = ','.join('?' for _ in owners)
                self.db.execute('DELETE FROM admission_leases WHERE owner NOT IN (' + marks + ')', tuple(owners))
            else:
                self.db.execute('DELETE FROM admission_leases')

    def summary(self):
        """Operator view: per-group targets/mode and deduplicated outcome counts."""
        groups = {}
        for row in self.db.execute('SELECT * FROM admission_groups ORDER BY grp'):
            groups[row['grp']] = {'target': row['target'], 'mode': row['mode'],
                                  'active': self._active(row['grp']),
                                  'external_slots': self.groups.get(row['grp'], {}).get('external_slots', 0),
                                  'cooldown_until': row['cooldown_until'],
                                  'useful': row['useful'], 'loaded': row['loaded'],
                                  'window_start': row['window_start']}
        for name, cfg in self.groups.items():
            groups.setdefault(name, {'target': cfg['initial'], 'mode': 'normal',
                                     'active': self._active(name),
                                     'external_slots': cfg.get('external_slots', 0),
                                     'cooldown_until': None, 'useful': 0,
                                     'loaded': None, 'window_start': 0})
        outcomes = [dict(row) for row in self.db.execute(
            'SELECT category,COUNT(*) AS count,SUM(useful) AS useful FROM admission_outcomes '
            'GROUP BY category ORDER BY count DESC, category')]
        return {'groups': groups, 'outcomes': outcomes,
                'leases': self.db.execute('SELECT COUNT(*) FROM admission_leases').fetchone()[0],
                'paused': self.state.paused()}
