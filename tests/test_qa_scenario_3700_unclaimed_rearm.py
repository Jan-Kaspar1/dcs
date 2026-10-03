"""The 3700_unclaimed_rearm leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_unclaimed_rearm, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'UnclaimedRearmTests.test_registered',
    'UnclaimedRearmTests.test_clean_pair_passes_and_validates',
    'UnclaimedRearmTests.test_demoting_window_reports_failed',
    'UnclaimedRearmTests.test_rearm_dropping_write_reports_failed',
    'UnclaimedRearmTests.test_phantom_rearm_reports_failed',
    'UnclaimedRearmTests.test_open_field_reports_failed',
    'UnclaimedRearmTests.test_ledger_growth_reports_failed',
    'UnclaimedRearmTests.test_journaled_claim_loss_reports_failed',
    'UnclaimedRearmTests.test_journaled_role_change_reports_failed',
    'UnclaimedRearmTests.test_peer_role_move_reports_failed',
    'UnclaimedRearmTests.test_shared_claim_reports_nondeterministic',
    'UnclaimedRearmTests.test_diverging_digests_report_nondeterministic',
    'UnclaimedRearmTests.test_release_refusal_reports_inconclusive',
    'UnclaimedRearmTests.test_no_active_reports_failed',
    'UnclaimedRearmTests.test_unsettled_pair_reports_inconclusive',
    'UnclaimedRearmTests.test_unreachable_plant_reports_inconclusive',
    'UnclaimedRearmTests.test_missing_plant_endpoint_reports_inconclusive',
    'UnclaimedRearmTests.test_two_runs_produce_identical_evidence',
})


class RearmPlantPeer(FakePlantPeer):
    """The unclaimed-rearm rig's plant half: FakePlantPeer plus the
    full write-ownership arbitration the scenario induces — per-
    attachment holder tracking, `claim_writer`'s unconditional
    preemption, `ensure_writer`'s conditional grant, `release_writer`
    dropping only this attachment's hold (the last-holder release
    unclaiming the field), disconnect dropping the hold but never the
    claim, and `write`/`step` fencing against the standing claim —
    the server's contract shape. The feed drives the controller
    side's scan writes through `owner_write`, the RemoteDriver re-arm
    contract applied: fenced under a foreign claim, an unclaimed
    field re-arming the recorded token inline before the write lands."""

    IN, OUT = 10, 100

    def __init__(self, owner):
        super().__init__()
        self.samples = {
            self.IN: {'value': {'float': 0.8}, 'quality': 'good',
                      'tick': 0},
            self.OUT: {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0}}
        self.owner = owner
        self.claim = {'owner': owner, 'holders': {'controller'},
                      'phantom': False}   # the standing owner claim
        self.plant_tick = 0
        # The doctors staging each named defect.
        self.down = False           # every attachment drops unanswered
        self.open_field = False     # mutations ignore the claim
        self.phantom_rearm = False  # a re-armed claim never fences
        self.shared = False         # claims answer claimed_shared
        self.release_refuses = False
        self.dropped = False        # the re-arm keeps dropping writes

    def _holders(self):
        return (self.claim or {}).get('holders') or set()

    def owner_write(self, demote_on_unclaimed=False,
                    rearm_drops=False):
        """One controller-scan field write through the RemoteDriver's
        inline re-arm: 'fenced' under a foreign claim (the caller
        demotes); an unclaimed field re-arms the recorded token in
        place and the write lands — unless the doctor drops it."""
        if self.claim is None:
            if demote_on_unclaimed:
                return 'fenced'   # the defect: demote on Unclaimed
            self.claim = {'owner': self.owner,
                          'holders': {'controller'},
                          'phantom': self.phantom_rearm}
            if rearm_drops:
                self.dropped = True
                return 'rearmed'  # claim re-armed, write never landed
        elif 'controller' not in self._holders():
            return 'fenced'
        if self.dropped:
            return 'rearmed'      # the writes keep dropping
        self.plant_tick += 1
        self.samples[self.OUT].update(tick=self.plant_tick,
                                      value={'float': 1.5})
        return 'landed'

    def release_conn(self, conn):
        # Disconnect drops the attachment's hold, never the claim —
        # the dead-owner fencing the standing claim provides.
        if self.claim is not None:
            self.claim['holders'].discard(conn)

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if op == 'release_writer':
            if self.release_refuses:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'release refused'}}
            if self.claim is not None:
                self.claim['holders'].discard(conn)
                if not self.claim['holders']:
                    self.claim = None
            return {'result': 'done'}
        shared = self.claim is not None \
            and self.claim['owner'] == owner \
            and any(h is not conn for h in self._holders())
        if op == 'ensure_writer' and self.claim is not None \
                and self.claim['owner'] != owner \
                and not self.claim.get('phantom'):
            return {'result': 'error', 'error': {
                'kind': 'fenced',
                'detail': 'the field is owned by another attachment'}}
        if self.claim is None or self.claim['owner'] != owner:
            self.claim = {'owner': owner, 'holders': {conn},
                          'phantom': False}
        else:
            self.claim['holders'].add(conn)
        if shared or self.shared:
            return {'result': 'claimed_shared', 'owner': owner}
        return {'result': 'done'}

    def dispatch_for(self, conn, request):
        if self.down:
            raise OSError('the plant is down')
        op = request.get('op')
        if op in ('claim_writer', 'ensure_writer', 'release_writer'):
            self.requests.append(request)
            return self._claim_for(conn, request)
        if op in ('write', 'step'):
            self.requests.append(request)
            if self.open_field or conn in self._holders():
                if op == 'step':
                    self.plant_tick += 1
                    return {'result': 'stepped',
                            'tick': self.plant_tick}
                self.samples[request['point']].update(
                    value=request['value'], tick=self.plant_tick)
                return {'result': 'done'}
            if self.claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            if op == 'step':
                return {'result': 'error', 'error': {
                    'kind': 'fenced',
                    'detail': 'another attachment owns field writes'}}
            return {'result': 'error', 'error': {
                'kind': 'io', 'error': {'fenced': request['point']}}}
        if op == 'list_points':
            self.requests.append(request)
            return {'result': 'points', 'points': [
                {'point': p, 'sample': self.served(p),
                 'direction': 'out' if p == self.OUT else 'in',
                 'fault': self.faults.get(p)}
                for p in sorted(self.samples)]}
        return super().dispatch_for(conn, request)


class UnclaimedRearmFeed:
    """A stubbed pair for the unclaimed-rearm scenario: ctrl-a is the
    field owner — each served /snapshot is one scan's write attempted
    through the plant peer's claim arbitration — and ctrl-b is the
    tracking standby. The pair reports the settled active/standby
    layout the leg induces on, the journals and the io_health
    fencing-loss ledger the leg audits, and doctor flags stage each
    named defect the issue calls out."""

    OWNER = 424243

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.demoted = False
        self.failed_writes = 0
        self.journal = []
        self.seq = 0
        self.peer_role_calls = 0
        # The doctors staging each named defect.
        self.silent = False             # ctrl-a never reports
        self.demote_on_unclaimed = False  # the window demotes it
        self.rearm_drops = False        # the re-arm drops the write
        self.ledger_grows = False       # the fencing ledger grows
        self.claim_lost_journaled = False
        self.role_journaled = False
        self.peer_journaled = False
        self.peer_moves = False         # the standby reports a move
        self.peer_wrong_role = False    # the pair never settles
        self.peer_demoted = False       # the restore's demote landed

    def _scan(self):
        """One controller scan: the field write attempted through the
        plant's claim arbitration — the fencing verdict demotes the
        owner through the settled fencing-loss contract."""
        self.tick += 1
        if self.demoted:
            return
        if self.plant.owner_write(
                demote_on_unclaimed=self.demote_on_unclaimed,
                rearm_drops=self.rearm_drops) == 'fenced':
            self.demoted = True
            self.failed_writes += 1
            for event in ({'field_claim_lost':
                           {'point': self.plant.OUT}},
                          {'role_changed': {'from': 'active',
                                            'to': 'standby'}}):
                self.seq += 1
                self.journal.append({'seq': self.seq,
                                     'tick': self.tick,
                                     'event': event})
        if self.ledger_grows:
            self.failed_writes += 1

    def _journal(self, since):
        entries = [dict(e) for e in self.journal if e['seq'] > since]
        if self.claim_lost_journaled:
            entries.append({'seq': since + 1, 'tick': self.tick,
                            'event': {'field_claim_lost':
                                      {'point': self.plant.OUT}}})
        if self.role_journaled:
            entries.append({'seq': since + 2, 'tick': self.tick,
                            'event': {'role_changed':
                                      {'from': 'active',
                                       'to': 'standby'}}})
        return entries

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        if host.startswith('ctrl-b'):
            if route == '/role':
                self.peer_role_calls += 1
                role = 'standby'
                if self.peer_moves and self.peer_role_calls > 1 \
                        and not self.peer_demoted:
                    role = 'promoting'
                if self.peer_wrong_role:
                    role = 'active'
                return 200, {'role': role, 'tick': 0,
                             'sync': {'tracking': {'aligned': 1}}}
            if route == '/demote':
                self.peer_demoted = True
                return 200, {'role': 'standby'}
            if route == '/journal':
                entries = []
                if self.peer_journaled:
                    entries = [{'seq': since + 1, 'tick': 0,
                                'event': {'role_changed':
                                          {'from': 'standby',
                                           'to': 'promoting'}}}]
                return 200, entries
            raise AssertionError('unhandled ' + url)
        if not host.startswith('ctrl-a'):
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent:
            raise urllib.error.URLError('unreachable')
        if route == '/role':
            return 200, {'tick': self.tick,
                         'role': 'standby' if self.demoted
                                 else 'active'}
        if route == '/snapshot':
            self._scan()
            return 200, {'tick': self.tick, 'points': [],
                         'io_health':
                         {'failed_writes': self.failed_writes,
                          'journal_errors': 0, 'journals_appended': 0}}
        if route == '/journal':
            return 200, self._journal(since)
        if route == '/promote':
            self.demoted = False
            return 200, {'role': 'active'}
        raise AssertionError('unhandled ' + url)


class UnclaimedRearmTests(unittest.TestCase):
    """The unclaimed-rearm scenario against the stubbed pair: a clean
    rig passes with identical digests and evidence, each doctored
    defect reports the named diagnostic, and the unreachable rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = RearmPlantPeer(UnclaimedRearmFeed.OWNER)
        self.feed = UnclaimedRearmFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.feed.OWNER,
                                'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'UNCLAIMED_REARM_SETTLE', 2), \
                patch.object(scenarios, 'UNCLAIMED_REARM_POLL', 0.001), \
                patch.object(scenarios, 'UNCLAIMED_REARM_DEADLINE', 2), \
                patch.object(scenarios, 'UNCLAIMED_REARM_ROUNDS', 4):
            return scenarios.scenario_unclaimed_rearm(ctx)

    def test_registered(self):
        self.assertIn(scenarios.scenario_unclaimed_rearm,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('unclaimed-rearm'),
                      scenarios.scenario_unclaimed_rearm)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue((self.evidence
                         / 'unclaimed-rearm-pass-1.json').is_file())
        self.assertTrue((self.evidence
                         / 'unclaimed-rearm-pass-2.json').is_file())
        report.validate_scenario(record)

    def test_demoting_window_reports_failed(self):
        self.feed.demote_on_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('role=active', record['detail'])
        report.validate_scenario(record)

    def test_rearm_dropping_write_reports_failed(self):
        self.feed.rearm_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('never landed', record['detail'])
        report.validate_scenario(record)

    def test_phantom_rearm_reports_failed(self):
        self.plant.phantom_rearm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('conditional claim', record['detail'])
        report.validate_scenario(record)

    def test_open_field_reports_failed(self):
        self.plant.open_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('no standing writer claim', record['detail'])
        report.validate_scenario(record)

    def test_ledger_growth_reports_failed(self):
        self.feed.ledger_grows = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('fencing-loss ledger', record['detail'])
        report.validate_scenario(record)

    def test_journaled_claim_loss_reports_failed(self):
        self.feed.claim_lost_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_journaled_role_change_reports_failed(self):
        self.feed.role_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('role transition', record['detail'])
        report.validate_scenario(record)

    def test_peer_role_move_reports_failed(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('tracking peer', record['detail'])
        report.validate_scenario(record)

    def test_shared_claim_reports_nondeterministic(self):
        self.plant.shared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'writes': 'landed'}, {}, {'pass': 1}),
                       ({'writes': 'stalled'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_unclaimed_rearm_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_release_refusal_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('hand-back', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_pair_reports_inconclusive(self):
        self.feed.peer_wrong_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_plant_reports_inconclusive(self):
        self.plant.down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_plant_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('claim ops', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = RearmPlantPeer(UnclaimedRearmFeed.OWNER)
            feed = UnclaimedRearmFeed(plant)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
