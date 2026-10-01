"""The 0600_stale_freshness leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_stale_freshness, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'StaleFreshnessTests.test_registered_in_scenarios',
    'StaleFreshnessTests.test_freeze_stale_recovery_passes_and_validates',
    'StaleFreshnessTests.test_stale_never_presenting_fails',
    'StaleFreshnessTests.test_comparison_presenting_stale_fails',
    'StaleFreshnessTests.test_stale_reverting_to_healthy_fails',
    'StaleFreshnessTests.test_no_recovery_after_restart_fails',
    'StaleFreshnessTests.test_rewound_history_ticks_fail',
    'StaleFreshnessTests.test_promoted_peer_never_recovering_fails',
    'StaleFreshnessTests.test_unfrozen_induction_is_inconclusive',
    'StaleFreshnessTests.test_failed_restart_action_is_inconclusive',
    'StaleFreshnessTests.test_missing_lifecycle_actions_are_inconclusive',
    'StaleFreshnessTests.test_two_runs_produce_identical_evidence',
})


class FreshnessFeed:
    """A stubbed pair for the stale-freshness scenario. ctrl-a is the
    field writer: while it is up the shared plant steps and the
    dynamics-driven stamp advances with it. ctrl-b is the tracking
    standby — its own scan tick advances per snapshot read, its sync
    reports `degraded` while the writer's checkpoint pulls miss, and the
    budgeted point's served quality follows the declared five-tick
    freshness rule: stale once the driver report has gone unchanged for
    more than the budget in the run's own tick domain, fresh again on
    the first change. The writer's restart realigns the standby's state
    at its own tick — the run clock never rewinds — the resumed
    stepping's changed reports clearing the staleness, and /history
    keeps the recorded interval. Fault flags stage each named outcome
    the issue calls out."""

    BUDGET = 5
    B_POINT = 13   # net-flow — the model's declared stale_after_ticks
    C_POINT = 10   # level-primary — the undeclared comparison
    PROMOTE_MISSES = 12  # the armed failover budget, past first stale

    def __init__(self):
        self.plant = 100     # the dynamics-driven driver stamp
        self.b_tick = 100    # the standby's own scan tick
        self.writer_up = True
        self.misses = 0
        self.promoted = False
        self.stale_seen = False
        self.relapsed = False
        # Change-detection freshness in the run's own tick domain: the
        # driver report last observed and the scan tick it changed at.
        self.observed = self.plant
        self.since = self.b_tick
        self.blind = False       # a never-recovering peer's frozen view
        self.rewind_ring = False  # the realign rewinds the run's tick
        self.hist = {self.B_POINT: [], self.C_POINT: []}
        self.seq = 1
        self.stops = []
        self.starts = []
        # Fault flags for the named outcomes.
        self.never_stale = False   # the budgeted point never presents
        self.leak = False          # the undeclared point presents stale
        self.relapse = False       # stale flips back to good mid-freeze
        self.no_recover = False    # the restart never realigns the peer
        self.freeze_takes = True   # False: the stop never freezes stamps
        self.promote = False       # the armed failover budget fires
        self.start_fails = False   # the restart action never completes

    # The runner-owned lifecycle actions — replace ctx's
    # stop_controller/start_controller.
    def stop(self, name):
        self.stops.append(name)
        self.writer_up = False

    def start(self, name):
        self.starts.append(name)
        if self.start_fails:
            raise RuntimeError('docker start failed: no such container')
        self.writer_up = True
        if self.rewind_ring and not self.promoted:
            # The finding's defect: the realign rewinds the run's tick
            # axis onto the resumed stream's — later ring samples stamp
            # inside the retained freeze window, double-covering it.
            self.b_tick = self.plant
            self.since = self.b_tick
        if self.no_recover and not self.promoted:
            # The named defect: the resumed stream never lands and the
            # peer's reads never refresh — the staleness never clears.
            self.blind = True

    def _frozen(self):
        # Stamps freeze while no peer steps the plant. A promoted
        # standby steps it itself; a stop that never took leaves the
        # writer effectively running.
        return not self.writer_up and not self.promoted \
            and self.freeze_takes

    def _b_scan(self):
        """One standby scan: its own tick advances, the plant's stamp
        advances only while a writer steps it, and a downed writer's
        pulls miss — the armed budget promoting at the configured
        count. Freshness is judged on the driver report's last observed
        change in the run's own tick domain, never on a cross-domain
        stamp comparison."""
        self.b_tick += 1
        if not self._frozen():
            self.plant += 1
        if self.writer_up or not self.freeze_takes:
            self.misses = 0
        else:
            self.misses += 1
            if self.promote and self.misses >= self.PROMOTE_MISSES:
                # The promoted run steps the plant again — but the
                # staged defect latches the freshness view: its reads
                # never re-observe the resumed field, so the budgeted
                # point never returns Good.
                self.promoted = True
                self.blind = True
        if not self.blind and self.plant != self.observed:
            self.observed = self.plant
            self.since = self.b_tick

    def _b_quality(self):
        lag = self.b_tick - self.since
        if lag > self.BUDGET and not self.never_stale:
            if self.relapse and self.stale_seen and not self.relapsed:
                self.relapsed = True
                return 'good'
            self.stale_seen = True
            return {'uncertain': 'stale'}
        return 'good'

    def _c_quality(self):
        lag = self.b_tick - self.since
        if self.leak and lag > self.BUDGET:
            return {'uncertain': 'stale'}
        return 'good'

    def _record(self, point, quality):
        self.hist[point].append({'seq': self.seq, 'sample': {
            'value': {'float': 1.0}, 'quality': quality,
            'tick': self.b_tick}})
        self.seq += 1

    def _standby(self, method, route, query):
        if (method, route) == ('GET', '/role'):
            if self.promoted:
                return 200, {'role': 'active', 'tick': self.b_tick}
            sync = {'degraded': {'misses': self.misses}} if self.misses \
                else {'tracking': {'aligned': self.plant}}
            return 200, {'role': 'standby', 'tick': self.b_tick,
                         'sync': sync}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': self.B_POINT, 'signal': None,
                 'name': 'net-flow', 'direction': 'in',
                 'value_type': 'float', 'writable': False},
                {'point': self.C_POINT, 'signal': None,
                 'name': 'level-primary', 'direction': 'in',
                 'value_type': 'float', 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._b_scan()
            qb, qc = self._b_quality(), self._c_quality()
            self._record(self.B_POINT, qb)
            self._record(self.C_POINT, qc)
            return 200, {'tick': self.b_tick, 'points': [
                {'point': self.B_POINT, 'sample': {
                    'value': {'float': 1.0}, 'quality': qb,
                    'tick': self.b_tick}},
                {'point': self.C_POINT, 'sample': {
                    'value': {'float': 1.0}, 'quality': qc,
                    'tick': self.b_tick}}]}
        if (method, route) == ('GET', '/history'):
            params = [part.split('=', 1) for part in query.split('&')]
            wanted = [int(v) for k, v in params if k == 'point']
            since = next((int(v) for k, v in params if k == 'since'), 0)
            return 200, [{'point': point,
                          'samples': [s for s in self.hist[point]
                                      if s['seq'] > since]}
                         for point in wanted]
        raise AssertionError('unexpected request %s ctrl-b%s'
                             % (method, route))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if host == 'ctrl-b:2':
            return self._standby(method, route, query)
        if not self.writer_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.plant}
        raise AssertionError('unexpected request %s %s' % (method, url))


class StaleFreshnessTests(unittest.TestCase):
    """scenario_stale_freshness against the stubbed pair: stopping the
    writer freezes the plant's stamps, the tracking standby's scans
    outrun them, and the declared budget presents stale per-point while
    the undeclared comparison keeps Good; the writer's restart realigns
    the peer inside the failover bound and /history preserves the
    interval."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = FreshnessFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, ctx=None, feed=None, evidence=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence),
                'stop_controller': feed.stop,
                'start_controller': feed.start,
                'failover_misses': 120}
        if ctx is not None:
            base.update(ctx)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'STALE_FRESHNESS_POLL', 0.001), \
                patch.object(scenarios, 'STALE_WALL_DEADLINE', 5.0), \
                patch.object(scenarios, 'STALE_RECOVER_DEADLINE', 1.0), \
                patch.object(scenarios, 'STALE_RETURN_DEADLINE', 1.0):
            return scenarios.scenario_stale_freshness(base)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_stale_freshness,
                      scenarios.SCENARIOS)

    def test_freeze_stale_recovery_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.stops, ['active'])
        self.assertEqual(self.feed.starts, ['active'])
        self.assertTrue(self.feed.stale_seen)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        interval = json.loads(
            (self.evidence / 'stale-freshness-history.json').read_text())
        self.assertIsNotNone(interval['budgeted']['interval'])
        self.assertFalse(interval['budgeted']['interval']['good_inside'])

    def test_stale_never_presenting_fails(self):
        self.feed.never_stale = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never presented Uncertain(Stale)',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_comparison_presenting_stale_fails(self):
        self.feed.leak = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('leaked past its declaration',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_reverting_to_healthy_fails(self):
        self.feed.relapse = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('healthy last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_recovery_after_restart_fails(self):
        self.feed.no_recover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not return Good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rewound_history_ticks_fail(self):
        # The finding's defect: the realign rewinds the run's tick
        # axis, so the post-restart ring samples stamp inside the
        # retained freeze window — a double-covered tick range.
        self.feed.rewind_ring = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('double-covered', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_never_recovering_fails(self):
        # The armed failover bound firing mid-freeze: the promoted peer
        # reclaims the writer and resumes stepping, but the staged
        # latch leaves its freshness view frozen — the point never
        # returns Good.
        self.feed.promote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-promotion', record.get('detail', ''))
        self.assertTrue(self.feed.promoted)
        report.validate_scenario(record)

    def test_unfrozen_induction_is_inconclusive(self):
        # The writer-stop never took: the plant's stamps keep advancing
        # and the peer keeps tracking, so a missing stale presentation
        # cannot be attributed to the induction.
        self.feed.freeze_takes = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never took effect', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_restart_action_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_lifecycle_actions_are_inconclusive(self):
        record = self.run_scenario(ctx={'stop_controller': None,
                                        'start_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no documented', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # against the same rig state record the same report and the
        # same evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('evidence-' + str(index))
            evidence.mkdir()
            record = self.run_scenario(feed=FreshnessFeed(),
                                       evidence=evidence)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
