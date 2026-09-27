"""The 2290_failover_refuse_retry leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_failover_refuse_retry, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'RefuseRetryTests.test_registered',
    'RefuseRetryTests.test_clean_rig_passes_and_validates',
    'RefuseRetryTests.test_gate_disarmed_fails',
    'RefuseRetryTests.test_proof_voided_fails',
    'RefuseRetryTests.test_gate_preempted_fails',
    'RefuseRetryTests.test_refusal_silent_fails',
    'RefuseRetryTests.test_refusal_flooded_fails',
    'RefuseRetryTests.test_refusal_unnamed_fails',
    'RefuseRetryTests.test_refusal_under_counted_fails',
    'RefuseRetryTests.test_served_silent_fails',
    'RefuseRetryTests.test_operator_switch_fails',
    'RefuseRetryTests.test_never_restores_fails',
    'RefuseRetryTests.test_incumbent_claim_refused_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_island_absent_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_misses_stalled_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_starved_watch_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_served_read_dropped_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_field_advanced_reports_'
    'nondeterministic',
    'RefuseRetryTests.test_diverging_digests_report_'
    'nondeterministic',
    'RefuseRetryTests.test_silent_judge_reports_unchecked',
    'RefuseRetryTests.test_unreachable_pair_reports_inconclusive',
    'RefuseRetryTests.test_unarmed_pair_reports_inconclusive',
    'RefuseRetryTests.test_unconverged_pair_reports_inconclusive',
    'RefuseRetryTests.test_missing_plant_reports_inconclusive',
    'RefuseRetryTests.test_missing_journal_files_reports_'
    'inconclusive',
    'RefuseRetryTests.test_single_endpoint_reports_inconclusive',
    'RefuseRetryTests.test_two_runs_produce_identical_evidence',
})


class FakeClock:
    """The scenario's `time` module swapped for a deterministic
    clock: every `sleep` advances `now` by exactly its argument, so
    the refused-window watch collects a fixed row count and two
    whole runs emit byte-identical evidence."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class PlantStream:
    """One incumbent attachment on the stubbed sim-net endpoint —
    just enough of a connected stream for `_plant_request`'s
    request/response framing: `sendall` answers the request into
    `response`, `recv` drains it, and `close` drops the holder —
    the attachment drop that leaves the foreign claim standing
    dead-owned."""

    def __init__(self, feed):
        self.feed = feed
        self.response = b''
        self.holds = False
        self.closed = False

    def sendall(self, data):
        body = self.feed._plant_answer(
            json.loads(data.decode()), self)
        self.response = (json.dumps(body) + '\n').encode()

    def recv(self, count):
        chunk, self.response = \
            self.response[:count], self.response[count:]
        return chunk

    def settimeout(self, _seconds):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.feed._attachment_dropped(self)


class RefuseRetryFeed:
    """A stubbed pair for the refused-fire retry leg: ctrl-a
    launched as the plain field owner with no tracking source,
    ctrl-b launched --standby ctrl-a and armed with --auto-promote
    — the pair's only failover gate. Every endpoint call on a peer
    is one completed scan; a standby scan paces its tracked
    source and applies its served checkpoint — an ownerless one
    counts one orphaned miss and re-proves convergence, exactly
    the miss accounting the refused window climbs on.

    The plant half models the claim arbitration the leg stages: a
    foreign attachment's controller-marked `claim_writer`
    preempts the launched owner unconditionally — live and
    unyielded while its stream holds, dead-owned the moment the
    attachment drops — so the fenced owner's in-place demotion
    turns the armed peer's pulls orphaned, the budget-th fire's
    conditional claim refuses the live incumbent, and the drop's
    dead-owned claim is the retry's grant. A requested promote is
    the unconditional-claim takeover; a requested demote releases
    the claim yielded and resets the tracking session. Doctor
    flags stage each named defect the issue calls out."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    NAMES = {'active': 'a', 'standby': 'b'}
    BUDGET = 120
    TOKENS = {'a': 424243, 'b': 424244}
    FOREIGN = 0x7161_2d72_6574_7279

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        # The checkpoint source each standby pulls — ctrl-a
        # launched with none; ctrl-b's configured --standby pull
        # aims at it; a fenced owner's in-place demotion adopts
        # the surviving peer.
        self.source = {'a': None, 'b': 'a'}
        self.sync = {'a': 'unsynchronized', 'b': 'tracking'}
        self.aligned = {'a': None, 'b': 100}
        self.converged = {'a': False, 'b': True}
        self.misses = {'a': 0, 'b': 0}
        self.armed = {'a': False, 'b': True}
        self.budget = self.BUDGET
        self.refused_fire = {'a': False, 'b': False}
        # The simulated plant: one standing writer claim (token,
        # holding peer or attachment, controller mark, yielded
        # mark, live-holders mark) and the step clock the frozen-
        # field probe reads.
        self.plant_tick = 300
        self.claim = {'token': self.TOKENS['a'], 'peer': 'a',
                      'controller': True, 'yielded': False,
                      'live': True, 'holder': None}
        self.streams = []
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
        self.no_failover = False      # /role serves no armed bundle
                                      # — a pre-contract release
        self.no_tracking = False      # the standby never converges
        self.claim_refused = False    # the incumbent's claim_writer
                                      # is refused
        self.island_absent = False    # the fenced owner never demotes
                                      # — the line never goes
                                      # ownerless
        self.stall_misses = False     # orphaned applies never reach
                                      # the miss accounting
        self.starve = False           # the armed peer's monitor goes
                                      # silent inside the window
        self.starving = False
        self.field_advances = False   # the held-claim field moves —
                                      # a foreign write landed
        self.refusal_silent = False   # the refused fire journals
                                      # nothing — the finding itself
        self.refusal_flood = False    # the refused fire re-journals
                                      # per scan
        self.wrong_reason = False     # the row names a cause the
                                      # window never produced
        self.under_counted = False    # the row reports a fired count
                                      # below the budget it fired at
        self.gate_disarmed = False    # one refusal disarms the gate —
                                      # the exact-equality defect
        self.proof_voided = False     # the orphaned applies stop
                                      # re-proving past the budget
        self.gate_preempts = False    # the conditional claim
                                      # preempts the live incumbent
        self.operator_switch = False  # the retry's walk journals an
                                      # operator attribution
        self.served_silent = False    # the durable row never reaches
                                      # the served tail
        self.served_drops = False     # the served /journal read drops
                                      # once the refusal exists
        self.never_restores = False   # the launched owner never
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
        if not (self.served_silent
                and kind == 'promotion_refused'):
            self.served[peer].append(entry)
        path = self.journal_a if peer == 'a' else self.journal_b
        with path.open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _role_changed(self, peer, to, origin='operator'):
        change = {'from': self.role[peer], 'to': to,
                  'origin': origin}
        if origin == 'operator':
            change['actor'] = 'qa-operator'
        self._journal(peer, 'role_changed', change)
        self.role[peer] = to

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] != 'active':
            sync = self.sync[peer]
            if sync == 'tracking':
                report['sync'] = {'tracking': {
                    'aligned': self.aligned[peer]}}
            elif sync == 'orphaned':
                report['sync'] = {'orphaned': {
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

    # ---- the simulated plant's claim arbitration -------------------

    def plant_connect(self, ctx, timeout=5):
        stream = PlantStream(self)
        self.streams.append(stream)
        return stream

    def _attachment_dropped(self, stream):
        """The holder set losing the incumbent attachment: a claim
        its holder dropped stands dead-owned — the preemptable
        shape the retry takes."""
        if self.claim.get('holder') is stream:
            self.claim['live'] = False

    def _plant_answer(self, request, stream):
        op = request.get('op')
        if op == 'ping':
            if self.field_advances:
                self.plant_tick += 1
            return {'result': 'alive', 'tick': self.plant_tick}
        if op == 'claim_writer':
            if self.claim_refused:
                return {'result': 'error',
                        'error': {'kind': 'fenced',
                                  'owner': self.claim['token'],
                                  'monitor': None}}
            # The unconditional claim: preempts whatever stands and
            # lives while the attachment does.
            self.claim = {'token': request.get('owner'),
                          'peer': None,
                          'controller': request.get(
                              'controller', True),
                          'yielded': False, 'live': True,
                          'holder': stream}
            stream.holds = True
            return {'result': 'done'}
        return {'result': 'error', 'error': {'kind': 'unknown_op'}}

    def _claim_refuses_conditional(self, peer):
        """The standing claim a `claim_writer_unless_held` must
        refuse: a live attachment holds a different controller's
        unyielded claim. Dead-owned and yielded claims are
        preemptable."""
        return self.claim['live'] and self.claim['controller'] \
            and not self.claim['yielded'] \
            and self.claim['token'] != self.TOKENS[peer]

    # ---- the tracking model ----------------------------------------

    def _pace(self, peer):
        """The peer's autonomous scan boundary — role transitions
        settle, the run's own tick advances, and a fenced owner
        meets its in-place demotion."""
        self.tick[peer] += 1
        if self.role[peer] == 'demoting':
            self._role_changed(peer, 'standby', 'demote')
        elif self.role[peer] == 'promoting':
            origin = 'failover'
            if peer == 'a' or self.operator_switch:
                origin = 'operator'
            self._role_changed(peer, 'active', origin)
        if self.role[peer] == 'active' \
                and not self.island_absent \
                and self.claim['token'] != self.TOKENS[peer]:
            # The standing claim moved off this owner's token —
            # its next fenced write demotes it in place.
            self._journal(peer, 'field_claim_lost',
                          {'claimant': self.claim['token']})
            self.sync[peer] = 'unsynchronized'
            self.aligned[peer] = None
            self.misses[peer] = 0
            self.converged[peer] = False
            self._role_changed(peer, 'demoting', 'demote')
            if peer == 'a':
                self.source['a'] = 'b'   # the surviving peer's
                                         # adoption the demoted
                                         # ex-owner lands on

    def _orphan_apply(self, peer):
        """One ownerless checkpoint applied: the orphan apply counts
        the cycle's miss AND re-proves convergence — Orphaned is a
        promotable verdict — then the armed gate reads the miss run,
        firing its conditional orphan claim once the budget is due
        and the proof stands."""
        if not self.stall_misses:
            self.misses[peer] += 1
        self.sync[peer] = 'orphaned'
        self.aligned[peer] = self.tick[self.source[peer] or 'a']
        self.converged[peer] = \
            self.misses[peer] <= self.budget \
            if self.proof_voided else True
        if self.role[peer] != 'standby' or not self.armed[peer]:
            return
        if self.gate_disarmed and self.refused_fire[peer]:
            return  # the defect's own shape: one refused fire
                    # disarmed the gate for the whole episode
        if self.gate_disarmed:
            due = self.misses[peer] == self.budget
        else:
            due = self.misses[peer] >= self.budget and (
                self.misses[peer] == self.budget
                or self.converged[peer])
        if not (due and self.converged[peer]):
            return
        if not self.gate_preempts \
                and self._claim_refuses_conditional(peer):
            # The refused fire: one journaled promotion_refused
            # naming the live incumbent's held claim and the fired
            # count — a continuous same-cause streak dedups.
            body = {'error': {'field_claim_failed': {
                        'detail': 'writer claim held by '
                                  + str(self.claim['token'])}},
                    'misses': self.misses[peer]}
            if self.wrong_reason:
                body['error'] = {'not_converged': {
                    'sync': {'degraded': {
                        'detail': 'checkpoint pull refused'}}}}
            if self.under_counted:
                body['misses'] = 80
            if not self.refusal_silent \
                    and (self.refusal_flood
                         or not self.refused_fire[peer]):
                self._journal(peer, 'promotion_refused', body)
            self.refused_fire[peer] = True
            return
        # The granted retry: the conditional claim preempts the
        # dead-owned (or doctored live) claim and the peer walks
        # standby->promoting->active on failover origin.
        self.claim = {'token': self.TOKENS[peer], 'peer': peer,
                      'controller': True, 'yielded': False,
                      'live': True, 'holder': None}
        origin = 'operator' if self.operator_switch else 'failover'
        self._role_changed(peer, 'promoting', origin)

    def _pull(self, peer):
        """One tracking pull of the peer's source: a landed
        checkpoint from a field-owning source re-proves convergence
        and resets the heartbeat; an ownerless source's checkpoint
        is the orphaned apply the refused window is made of."""
        source = self.source[peer]
        if source is None:
            return
        if self.no_tracking and peer == 'b':
            self.sync[peer] = 'unsynchronized'
            self.converged[peer] = False
            return
        self._pace(source)
        if self.role[source] in ('active', 'promoting'):
            self.misses[peer] = 0
            self.converged[peer] = True
            self.sync[peer] = 'tracking'
            self.aligned[peer] = self.tick[source]
            self.refused_fire[peer] = False
            return
        self._orphan_apply(peer)

    def _scan(self, peer):
        """One completed scan: role transitions settle at the
        boundary and a standby pulls its tracked source."""
        self._pace(peer)
        if self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        self._role_changed(peer, 'demoting', 'demote')
        # The release leaves the claim standing yielded; the
        # tracking session resets on demotion — a fresh heartbeat
        # with no standing sync, no convergence proof.
        self.claim = {'token': self.TOKENS[peer], 'peer': peer,
                      'controller': True, 'yielded': True,
                      'live': False, 'holder': None}
        self.sync[peer] = 'unsynchronized'
        self.aligned[peer] = None
        self.misses[peer] = 0
        self.converged[peer] = False
        self.refused_fire[peer] = False
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if peer == 'a' and self.never_restores:
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        if self.role[peer] != 'standby' \
                or self.sync[peer] in (None, 'unsynchronized'):
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        # The deliberate takeover: an orphaned posture claims
        # conditionally, a converged one unconditionally — both
        # grant the preemptable claim this restore leaves standing.
        if self.sync[peer] == 'orphaned' \
                and self._claim_refuses_conditional(peer):
            self._raise(409, {'not_converged': {
                'sync': {'orphaned': {}}}})
        self.claim = {'token': self.TOKENS[peer], 'peer': peer,
                      'controller': True, 'yielded': False,
                      'live': True, 'holder': None}
        self._role_changed(peer, 'promoting', 'operator')
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch --------------------------------------

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
                    and self.sync['b'] == 'orphaned':
                # Armed once the island's orphaned climb starts —
                # the refused window's watch reads silence.
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


class RefuseRetryTests(unittest.TestCase):
    """The failover-refuse-retry leg against the stubbed pair: a
    clean rig passes with identical digests — the foreign
    attachment's controller-marked claim fencing the owner into
    its in-place demotion, the armed peer's orphaned miss run
    reaching the declared budget beside the standing proof, the
    live incumbent refusing the budget-th fire exactly once on
    both journal surfaces, the incumbent's drop leaving the claim
    dead-owned, the still-armed gate re-firing into an automatic
    failover-origin promotion, and the launch layout restoring —
    each doctored contract breach reports failover-retry-failed,
    each instability reports failover-retry-nondeterministic, and
    an unreachable, unarmed, unconverged, or seam-less run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RefuseRetryFeed(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': 'plant-host:5001',
               'evidence_dir': str(self.evidence),
               'journal_files': {
                   'active': str(feed.journal_a),
                   'standby': str(feed.journal_b)}}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_plant_connect',
                             feed.plant_connect), \
                patch.object(scenarios, 'time', self.clock), \
                patch.object(scenarios, 'RETRY_SETTLE', 1.0), \
                patch.object(scenarios, 'RETRY_WINDOW', 3.0), \
                patch.object(scenarios, 'RETRY_HOLD', 0.05), \
                patch.object(scenarios, 'RETRY_POLL', 0.01):
            return scenarios.scenario_failover_refuse_retry(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The refuse-retry leg's window: behind the refusal-journal
        # leg whose same armed gate it exercises one refusal cause
        # earlier, before the checkpoint-negotiation cases the
        # restored launch layout serves.
        self.assertLess(
            order.index(scenarios.scenario_failover_refusal_journal),
            order.index(scenarios.scenario_failover_refuse_retry))
        self.assertLess(
            order.index(scenarios.scenario_failover_refuse_retry),
            order.index(scenarios.scenario_checkpoint_negotiation))
        self.assertIs(
            verify.case_function('failover-refuse-retry'),
            scenarios.scenario_failover_refuse_retry)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = []
        for name in ('failover-refuse-retry-pass-1.json',
                     'failover-refuse-retry-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            passes.append(json.loads(path.read_text()))
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'armed': 'declared', 'incumbent': 'claimed',
             'island': 'orphaned', 'window': 'boundary',
             'proof': 'standing', 'row': 'one-named',
             'retry': 'promoted', 'roles': 'restored'})
        first = passes[0]['record']
        # The refused window: every served row read standby beside
        # the orphaned verdict and the standing proof while the
        # miss accounting climbed to and past the declared budget.
        rows = first['window']
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row['role'], 'standby', row)
            self.assertEqual(row['sync'], 'orphaned', row)
            self.assertIs(row['converged'], True, row)
        self.assertGreaterEqual(
            max(row['misses'] for row in rows
                if isinstance(row['misses'], int)), 120)
        # The refused fire's own trail: exactly one
        # promotion_refused naming the live incumbent's held claim
        # and the fired count — mirrored on the served tail.
        self.assertEqual(len(first['durable_refusals']), 1)
        body = first['durable_refusals'][0]['body']
        self.assertIn('field_claim_failed', body['error'])
        self.assertGreaterEqual(body['misses'], 120)
        self.assertEqual(
            first['served_refusals'],
            [row['body'] for row in first['durable_refusals']])
        # The retry: the gate re-fired once the incumbent's claim
        # stood dead-owned — the peer promoted automatically on
        # failover origin with no actor.
        self.assertIsNotNone(first['promoted'])
        self.assertEqual(
            [(row['from'], row['to']) for row in first['walk']],
            [('standby', 'promoting'), ('promoting', 'active')])
        for row in first['walk']:
            self.assertEqual(row['origin'], 'failover')
            self.assertFalse(row['actor'])
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_gate_disarmed_fails(self):
        # The issue's doctored negative: the gate asserted as
        # re-firing while it stays disarmed after the refusal —
        # the stranded peer is the defect's own shape.
        self.feed.gate_disarmed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('never promoted', record['detail'])
        report.validate_scenario(record)

    def test_proof_voided_fails(self):
        # Orphaned applies that stop re-proving past the budget —
        # the voided-proof disarm the fix removed.
        self.feed.proof_voided = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('voided', record['detail'])
        report.validate_scenario(record)

    def test_gate_preempted_fails(self):
        # The conditional claim preempting the live incumbent is
        # the stale-island breach the orphan claim exists to
        # refuse.
        self.feed.gate_preempts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('incumbent', record['detail'])
        report.validate_scenario(record)

    def test_refusal_silent_fails(self):
        # The refused fire that journals nothing reads identical
        # to a peer that never armed.
        self.feed.refusal_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('promotion_refused', record['detail'])
        report.validate_scenario(record)

    def test_refusal_flooded_fails(self):
        # A retry that journals once per refused scan floods the
        # trail the audit counts.
        self.feed.refusal_flood = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        report.validate_scenario(record)

    def test_refusal_unnamed_fails(self):
        # A row naming a cause the live-incumbent window never
        # produced names nothing an auditor can use.
        self.feed.wrong_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('names', record['detail'])
        report.validate_scenario(record)

    def test_refusal_under_counted_fails(self):
        self.feed.under_counted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('budget', record['detail'])
        report.validate_scenario(record)

    def test_served_silent_fails(self):
        # The durable row that never reaches the served tail
        # leaves the /journal reader the same silence.
        self.feed.served_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('served', record['detail'])
        report.validate_scenario(record)

    def test_operator_switch_fails(self):
        # An operator-attributed promotion walk is a requested
        # switch, not the armed gate's automatic retry.
        self.feed.operator_switch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('operator', record['detail'])
        report.validate_scenario(record)

    def test_never_restores_fails(self):
        self.feed.never_restores = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-failed'), record['detail'])
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_incumbent_claim_refused_reports_nondeterministic(self):
        self.feed.claim_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('claim', record['detail'])
        report.validate_scenario(record)

    def test_island_absent_reports_nondeterministic(self):
        # The fenced owner never demotes — the armed peer's pulls
        # keep landing owner checkpoints and the island never
        # forms.
        self.feed.island_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('orphaned', record['detail'])
        report.validate_scenario(record)

    def test_misses_stalled_reports_nondeterministic(self):
        # Orphaned applies that never reach the miss accounting —
        # the armed gate never fired.
        self.feed.stall_misses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('budget', record['detail'])
        report.validate_scenario(record)

    def test_starved_watch_reports_nondeterministic(self):
        # The peer's monitor goes silent inside the refused
        # window — the climb never reaches the budget on the
        # served rows the audit reads.
        self.feed.starve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_served_read_dropped_reports_nondeterministic(self):
        self.feed.served_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('served', record['detail'])
        report.validate_scenario(record)

    def test_field_advanced_reports_nondeterministic(self):
        # The field moving under the held foreign claim is a
        # foreign write landing — instability the leg does not
        # answer for.
        self.feed.field_advances = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('field', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_retry_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what
        # they name.
        with patch.object(scenarios, '_judge_retry',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'failover-retry-unchecked'), record['detail'])
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

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(plant=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant', record['detail'])
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
        # every watch's row count, so two whole runs emit
        # identical reports and identical evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = RefuseRetryFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
