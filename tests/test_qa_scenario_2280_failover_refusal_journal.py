"""The 2280_failover_refusal_journal leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_failover_refusal_journal, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'RefusalJournalTests.test_registered',
    'RefusalJournalTests.test_clean_rig_passes_and_validates',
    'RefusalJournalTests.test_refusal_silent_fails',
    'RefusalJournalTests.test_refusal_flooded_fails',
    'RefusalJournalTests.test_refusal_unnamed_fails',
    'RefusalJournalTests.test_refusal_under_counted_fails',
    'RefusalJournalTests.test_gate_promoted_fails',
    'RefusalJournalTests.test_role_walked_fails',
    'RefusalJournalTests.test_served_silent_fails',
    'RefusalJournalTests.test_never_restores_fails',
    'RefusalJournalTests.test_misses_stalled_reports_'
    'nondeterministic',
    'RefusalJournalTests.test_starved_watch_reports_'
    'nondeterministic',
    'RefusalJournalTests.test_served_read_dropped_reports_'
    'nondeterministic',
    'RefusalJournalTests.test_demote_refused_reports_'
    'nondeterministic',
    'RefusalJournalTests.test_promote_refused_reports_'
    'nondeterministic',
    'RefusalJournalTests.test_diverging_digests_report_'
    'nondeterministic',
    'RefusalJournalTests.test_silent_judge_reports_unchecked',
    'RefusalJournalTests.test_unreachable_pair_reports_inconclusive',
    'RefusalJournalTests.test_unarmed_pair_reports_inconclusive',
    'RefusalJournalTests.test_unconverged_pair_reports_inconclusive',
    'RefusalJournalTests.test_missing_stop_action_reports_'
    'inconclusive',
    'RefusalJournalTests.test_missing_journal_files_reports_'
    'inconclusive',
    'RefusalJournalTests.test_single_endpoint_reports_inconclusive',
    'RefusalJournalTests.test_two_runs_produce_identical_evidence',
})


class FakeClock:
    """The scenario's `time` module swapped for a deterministic
    clock: every `sleep` advances `now` by exactly its argument, so
    the voided-window watch collects a fixed row count and two whole
    runs emit byte-identical evidence."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class RefusalJournalFeed:
    """A stubbed pair for the refusal-journal leg: ctrl-a launched
    as the plain field owner with no tracking source, ctrl-b
    launched --standby ctrl-a and armed with --auto-promote — the
    pair's only failover gate. Every endpoint call on a peer is one
    completed scan; a standby scan pulls its configured source,
    counting produced-nothing pulls as heartbeat misses against the
    armed budget — exactly the miss accounting the leg's voided
    window climbs on.

    The tracking half models the contract the leg pins: the armed
    gate fires at the budget-th miss — refusing as not_converged
    while the proof stands voided, and journaling exactly one
    promotion_refused naming the cause and the fired count — and
    closes for the episode once misses run past it. A requested
    promote is the unconditional-claim takeover (the incumbent's
    fencing-loss demotes it in place); a requested demote releases
    the claim and resets the tracking session; the runner-owned
    stop/start actions hold ctrl-a's container down and bring it
    back — a restarted owner only claims a free field. Doctor flags
    stage each named defect the issue calls out."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    NAMES = {'active': 'a', 'standby': 'b'}
    BUDGET = 120

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        # The checkpoint source each standby pulls — ctrl-a launched
        # with none; ctrl-b's configured --standby pull aims at it.
        self.source = {'a': None, 'b': 'a'}
        self.sync = {'a': 'unsynchronized', 'b': 'tracking'}
        self.aligned = {'a': None, 'b': 100}
        self.converged = {'a': False, 'b': True}
        self.misses = {'a': 0, 'b': 0}
        self.armed = {'a': False, 'b': True}
        self.claim = 'a'      # the standing writer claim's owner
        self.budget = self.BUDGET
        self.seq = {'a': 0, 'b': 0}
        self.served = {'a': [], 'b': []}
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_failover = False      # /role serves no armed bundle —
                                      # a pre-contract release
        self.no_tracking = False      # the standby never converges
        self.promote_refused = False  # the arming promote answers 409
        self.demote_refused = False   # the voiding demote answers 409
        self.stall_misses = False     # produced-nothing pulls never
                                      # reach the miss accounting
        self.starve = False           # the peer's monitor goes silent
                                      # inside the voided window
        self.starving = False
        self.refusal_silent = False   # the refused fire journals
                                      # nothing — the finding itself
        self.refusal_flood = False    # the refused fire re-journals
                                      # per scan
        self.wrong_reason = False     # the row names a cause the
                                      # window never produced
        self.under_counted = False    # the row reports a fired count
                                      # below the budget it fired at
        self.gate_promotes = False    # the voided gate promotes anyway
        self.role_walked = False      # the window journals a
                                      # transition the refusal never
                                      # made
        self.served_silent = False    # the durable row never reaches
                                      # the served tail
        self.served_drops = False     # the served /journal read drops
                                      # once the refusal exists
        self.never_restores = False   # the restarted owner never
                                      # re-takes the field

    # ---- the served surface --------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _journal(self, peer, kind, body):
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick[peer],
                 'event': {kind: body}}
        if not (self.served_silent and kind == 'promotion_refused'):
            self.served[peer].append(entry)
        path = self.journal_a if peer == 'a' else self.journal_b
        with path.open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _role_changed(self, peer, to):
        self._journal(peer, 'role_changed',
                      {'from': self.role[peer], 'to': to})
        self.role[peer] = to

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] != 'active':
            sync = self.sync[peer]
            if sync == 'tracking':
                report['sync'] = {'tracking': {
                    'aligned': self.aligned[peer]}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {
                    'detail': 'checkpoint pull refused'}}
            else:
                report['sync'] = 'unsynchronized'
            if self.armed[peer] and not self.no_failover:
                report['failover'] = {
                    'converged': self.converged[peer],
                    'misses': self.misses[peer],
                    'budget': self.budget}
        return report

    # ---- the tracking model ---------------------------------------

    def _pace(self, peer):
        """The peer's autonomous scan boundary — role transitions
        settle and the run's own tick advances."""
        self.tick[peer] += 1
        if self.role[peer] == 'demoting':
            self._role_changed(peer, 'standby')
        elif self.role[peer] == 'promoting':
            self._role_changed(peer, 'active')

    def _refused_fire(self, peer):
        """The armed gate's fired-but-refused attempt at the miss
        boundary — the durable record it owes: one promotion_refused
        naming the voided convergence proof and the fired count."""
        body = {'error': {'not_converged': {
                    'sync': {'degraded': {
                        'detail': 'checkpoint pull refused'}}}},
                'misses': self.misses[peer]}
        if self.wrong_reason:
            body['error'] = {'field_claim_failed': {
                'detail': 'writer claim held'}}
        if self.under_counted:
            body['misses'] = 80
        if not self.refusal_silent:
            copies = 3 if self.refusal_flood else 1
            for _ in range(copies):
                self._journal(peer, 'promotion_refused', body)
        if self.role_walked:
            # A transition the refused attempt never made.
            self._journal(peer, 'role_changed',
                          {'from': 'standby', 'to': 'promoting'})

    def _missed(self, peer):
        """A produced-nothing pull: the in-flight miss counts toward
        the budget, the standing proof goes stale once misses run
        past it — and the armed gate reads the miss run, firing at
        the boundary exactly while the proof is still voided."""
        if not self.stall_misses:
            self.misses[peer] += 1
        if self.sync[peer] != 'diverged':
            self.sync[peer] = 'degraded'
        if self.misses[peer] > self.budget:
            self.converged[peer] = False
        if self.role[peer] != 'standby' or not self.armed[peer]:
            return
        due = self.misses[peer] >= self.budget and (
            self.misses[peer] == self.budget
            or self.converged[peer])
        if not due:
            return
        if self.converged[peer] or self.gate_promotes:
            # The unconditional-claim takeover: a proof-backed fire
            # — or the doctored voided one — lifts the gate.
            old = self.claim
            self.claim = peer
            self._role_changed(peer, 'promoting')
            if old in ('a', 'b') and old != peer \
                    and self.role[old] == 'active':
                self.sync[old] = 'unsynchronized'
                self._role_changed(old, 'demoting')
        else:
            self._refused_fire(peer)

    def _pull(self, peer):
        """One tracking pull of the peer's configured source: a
        completed pull on the field owner re-proves convergence and
        resets the heartbeat; a dead source is the produced-nothing
        miss the voided window is made of."""
        source = self.source[peer]
        if source is None:
            return
        if self.no_tracking and peer == 'b':
            self.sync[peer] = 'unsynchronized'
            self.converged[peer] = False
            return
        if not self.up[source]:
            self._missed(peer)
            return
        self._pace(source)
        if self.role[source] == 'active':
            self.misses[peer] = 0
            self.converged[peer] = True
            self.sync[peer] = 'tracking'
            self.aligned[peer] = self.tick[source]

    def _scan(self, peer):
        """One completed scan: role transitions settle at the
        boundary and a standby pulls its tracked source."""
        self._pace(peer)
        if self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the runner-owned lifecycle actions ------------------------

    def stop(self, name):
        self.up[self.NAMES[name]] = False

    def start(self, name):
        peer = self.NAMES[name]
        if peer == 'a':
            if self.claim not in (None, 'a'):
                # The startup claim only takes a free field — a live
                # incumbent's hold refuses the restart outright.
                return
            self.up[peer] = True
            if self.never_restores:
                return
            self.role[peer] = 'active'
            self.claim = 'a'
        else:
            self.up[peer] = True

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        if self.demote_refused:
            self._raise(409, {'not_converged': {
                'sync': 'unsynchronized'}})
        self._role_changed(peer, 'demoting')
        # The tracking session resets on demotion — a fresh
        # heartbeat with no standing sync, no convergence proof.
        self.sync[peer] = 'unsynchronized'
        self.aligned[peer] = None
        self.misses[peer] = 0
        self.converged[peer] = False
        self.claim = None
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.promote_refused:
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        if self.role[peer] != 'standby' \
                or self.sync[peer] in (None, 'unsynchronized'):
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        # The deliberate takeover: the unconditional claim preempts
        # the standing owner, whose fencing-loss demotes it in
        # place.
        old = self.claim
        self.claim = peer
        self._role_changed(peer, 'promoting')
        if old in ('a', 'b') and old != peer \
                and self.role[old] == 'active':
            self.sync[old] = 'unsynchronized'
            self._role_changed(old, 'demoting')
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch -------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/journal'):
            if self.served_drops and peer == 'b' and any(
                    'promotion_refused' in entry['event']
                    for entry in self.served['b']):
                raise urllib.error.URLError('journal read dropped')
            since = int(query.split('=', 1)[1]) if query else 0
            self._scan(peer)
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/role'):
            if self.starving and peer == 'b':
                raise urllib.error.URLError('monitor starved')
            self._scan(peer)
            report = self._report(peer)
            if self.starve and peer == 'b' \
                    and report['role'] == 'standby' \
                    and not self.up['a']:
                # Armed once the dead-source demote has settled —
                # the voided window's watch reads silence.
                self.starving = True
            return 200, report
        if (method, route) == ('POST', '/demote'):
            self._scan(peer)
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            self._scan(peer)
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class RefusalJournalTests(unittest.TestCase):
    """The failover-refusal-journal leg against the stubbed pair:
    a clean rig passes with identical digests — the arming promote
    fencing the incumbent, the dead-source demote voiding the
    proof, the miss climb firing the gate at the declared budget,
    exactly one promotion_refused naming not_converged and the
    fired count on both journal surfaces, no ownership transition
    beside it, and the restarted owner re-taking the field — each
    doctored contract breach reports
    failover-refusal-journal-failed, each instability reports
    failover-refusal-journal-nondeterministic, and an unreachable,
    unarmed, unconverged, or seam-less run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RefusalJournalFeed(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'journal_files': {
                   'active': str(feed.journal_a),
                   'standby': str(feed.journal_b)},
               'stop_controller': feed.stop,
               'start_controller': feed.start}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'time', self.clock), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.01), \
                patch.object(scenarios, 'REFUSAL_SETTLE', 1.0), \
                patch.object(scenarios, 'REFUSAL_WINDOW', 3.0), \
                patch.object(scenarios, 'REFUSAL_HOLD', 0.05), \
                patch.object(scenarios, 'REFUSAL_POLL', 0.01):
            return scenarios.scenario_failover_refusal_journal(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The refusal-journal leg's window: behind the
        # failover-proof leg whose armed-evidence contract it
        # extends, before the checkpoint-negotiation cases the
        # restored launch layout serves.
        self.assertLess(
            order.index(scenarios.scenario_failover_proof_report),
            order.index(scenarios.scenario_failover_refusal_journal))
        self.assertLess(
            order.index(scenarios.scenario_failover_refusal_journal),
            order.index(scenarios.scenario_checkpoint_negotiation))
        self.assertIs(
            verify.case_function('failover-refusal-journal'),
            scenarios.scenario_failover_refusal_journal)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = []
        for name in ('failover-refusal-journal-pass-1.json',
                     'failover-refusal-journal-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            passes.append(json.loads(path.read_text()))
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'armed': 'declared', 'switch': 'armed-owner',
             'void': 'dead-source', 'window': 'boundary',
             'gate': 'parked', 'row': 'one-named',
             'roles': 'restored'})
        first = passes[0]['record']
        # The voided window: every served row read standby while the
        # miss count climbed past the declared budget — the gate
        # fired, refused, and stayed parked.
        rows = first['window']
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row['role'], 'standby', row)
        self.assertGreaterEqual(
            max(row['misses'] for row in rows
                if isinstance(row['misses'], int)), 120)
        # The durable trail's own entry: exactly one
        # promotion_refused naming the voided proof and the fired
        # count — the row an auditor distinguishes 'refused' from
        # 'never armed' on — mirrored on the served tail.
        self.assertEqual(len(first['durable_refusals']), 1)
        body = first['durable_refusals'][0]
        self.assertIn('not_converged', body['error'])
        self.assertGreaterEqual(body['misses'], 120)
        self.assertEqual(first['served_refusals'],
                         first['durable_refusals'])
        self.assertEqual(first['role_walks'], [])
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_refusal_silent_fails(self):
        # The finding's own shape: the fired-but-refused gate left
        # the durable trail silent — indistinguishable from a peer
        # that never armed.
        self.feed.refusal_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('promotion_refused', record['detail'])
        report.validate_scenario(record)

    def test_refusal_flooded_fails(self):
        self.feed.refusal_flood = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        report.validate_scenario(record)

    def test_refusal_unnamed_fails(self):
        # A row that names a cause the voided window never produced
        # names nothing an auditor can use.
        self.feed.wrong_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('names', record['detail'])
        report.validate_scenario(record)

    def test_refusal_under_counted_fails(self):
        self.feed.under_counted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('budget', record['detail'])
        report.validate_scenario(record)

    def test_gate_promoted_fails(self):
        # The gate that promotes on a voided proof is the wrong
        # answer entirely — worse than a silent journal.
        self.feed.gate_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('standby', record['detail'])
        report.validate_scenario(record)

    def test_role_walked_fails(self):
        # A transition journaled beside the refusal is a move the
        # refused attempt never made.
        self.feed.role_walked = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('role', record['detail'])
        report.validate_scenario(record)

    def test_served_silent_fails(self):
        # The durable row that never reaches the served tail leaves
        # the /journal reader the same silence the finding names.
        self.feed.served_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('served', record['detail'])
        report.validate_scenario(record)

    def test_never_restores_fails(self):
        self.feed.never_restores = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-failed'), record['detail'])
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_misses_stalled_reports_nondeterministic(self):
        # The produced-nothing pulls never reached the miss
        # accounting — the armed gate never fired.
        self.feed.stall_misses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        self.assertIn('budget', record['detail'])
        report.validate_scenario(record)

    def test_starved_watch_reports_nondeterministic(self):
        # The peer's monitor goes silent inside the voided window —
        # the watch collects no rows to audit.
        self.feed.starve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_served_read_dropped_reports_nondeterministic(self):
        self.feed.served_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        self.assertIn('served', record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_promote_refused_reports_nondeterministic(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        self.assertIn('/promote', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_refusal_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_window',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-refusal-journal-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unarmed_pair_reports_inconclusive(self):
        # The standby serves no failover evidence — the staged
        # release predates the --auto-promote contract.
        self.feed.no_failover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('failover', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_missing_stop_action_reports_inconclusive(self):
        record = self.run_scenario(stop_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stop_controller', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: the fake clock fixes
        # every watch's row count, so two whole runs emit identical
        # reports and identical evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = RefusalJournalFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
