"""The 3550_remote_driver_recovery leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_remote_driver_recovery. The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'RemoteDriverRecoveryTests.test_registered',
    'RemoteDriverRecoveryTests.test_clean_cycles_pass_and_validate',
    'RemoteDriverRecoveryTests.test_evidence_entries_cover_both_cycles',
    'RemoteDriverRecoveryTests.test_lingering_error_fails',
    'RemoteDriverRecoveryTests.test_late_clear_fails',
    'RemoteDriverRecoveryTests.test_baseline_linger_fails',
    'RemoteDriverRecoveryTests.test_never_named_fails',
    'RemoteDriverRecoveryTests.test_uncounted_outage_fails',
    'RemoteDriverRecoveryTests.test_link_never_degrades_fails',
    'RemoteDriverRecoveryTests.test_second_outage_never_named_fails',
    'RemoteDriverRecoveryTests.test_streak_standing_fails',
    'RemoteDriverRecoveryTests.test_reset_history_fails',
    'RemoteDriverRecoveryTests.test_promotion_during_outage_fails',
    'RemoteDriverRecoveryTests.test_owner_role_move_fails',
    'RemoteDriverRecoveryTests.test_aborted_run_fails',
    'RemoteDriverRecoveryTests.test_aborted_recovery_fails',
    'RemoteDriverRecoveryTests.test_never_reattached_fails',
    'RemoteDriverRecoveryTests.test_rewound_tick_reports_nondeterministic',
    'RemoteDriverRecoveryTests.test_diverging_digests_report_nondeterministic',
    'RemoteDriverRecoveryTests.test_unsettled_restore_fails',
    'RemoteDriverRecoveryTests.test_inert_stop_is_inconclusive',
    'RemoteDriverRecoveryTests.test_unreturned_plant_is_inconclusive',
    'RemoteDriverRecoveryTests.test_failed_stop_is_inconclusive',
    'RemoteDriverRecoveryTests.test_failed_start_is_inconclusive',
    'RemoteDriverRecoveryTests.test_missing_actions_is_inconclusive',
    'RemoteDriverRecoveryTests.test_unreachable_rig_is_inconclusive',
    'RemoteDriverRecoveryTests.test_unsettled_pair_is_inconclusive',
    'RemoteDriverRecoveryTests.test_silent_peer_is_inconclusive',
    'RemoteDriverRecoveryTests.test_precontract_surface_is_inconclusive',
    'RemoteDriverRecoveryTests.test_legacy_driver_is_inconclusive',
    'RemoteDriverRecoveryTests.test_unclassifiable_outage_is_inconclusive',
    'RemoteDriverRecoveryTests.test_probe_fallback_covers_raw_plant_seam',
    'RemoteDriverRecoveryTests.test_two_runs_produce_identical_evidence',
})


class RemoteDriverFeed:
    """A stubbed pair plus shared plant for the remote-driver-
    recovery scenario. ctrl-a owns the field; ctrl-b is the tracking
    standby. `stop`/`start` replace ctx['stop_plant']/ctx['start_plant']
    — the runner-owned plant lifecycle actions — and flip `plant_up`;
    every /snapshot read on ctrl-a is one completed scan whose
    io_health carries the remote backend's DriverDiagnostics: the link
    disconnected with the severing failure named in last_error while
    the plant is down, the first successful exchange clearing the
    standing record once it answers again — the #990 contract the leg
    exists to prove. Doctor flags stage each named defect and each
    inconclusive rig state the issue calls out."""

    LINK_ERROR = 'no live connection to the plant server'

    def __init__(self):
        self.tick = 0
        self.plant_up = True
        self.active_up = True
        self.calls = []           # the lifecycle actions run
        self.failed_reads = 0
        self.failed_writes = 0
        self.consecutive = 0
        self.io_fault = None      # io_health's recorded boundary fault
        self.driver_error = None  # the backend's standing last_error
        self.connected_scans = 0  # healthy scans since the last loss
        # The doctors staging each named defect and rig state.
        self.lingering = False      # the record never clears
        self.late_clear = None      # clears only after N healthy scans
        self.never_names = False    # disconnected, last_error empty
        self.flat_health = False    # the boundary streak never moves
        self.healthy_through = False  # the link reports connected anyway
        self.link_stuck = False     # never reports connected again
        self.stuck_streak = False   # the streak stands past recovery
        self.resets_health = False  # io_health zeroes on recovery
        self.inert_stop = False     # the stop lever never bites
        self.never_returns = False  # start leaves the plant dead
        self.stop_raises = False    # the stop action itself fails
        self.start_raises = False   # the start action itself fails
        self.promoted = False       # the standby reports active
        self.owner_moves = False    # the owner demotes on field loss
        self.demoted_end = False    # the owner leaves active at the end
        self.peer_down = False      # the standby never answers /role
        self.peer_untracked = False  # the standby never tracks
        self.peer_wrong_role = False  # the standby reports active always
        self.silent_rig = False     # every endpoint refuses
        self.aborts = False         # the monitor dies on field loss
        self.aborts_on_recovery = False  # the monitor dies past restore
        self.tick_regress = False   # the active re-serves tick 0
        self.no_driver = False      # io_health carries no backend
        self.legacy_driver = False  # the backend lacks last_error
        self.weak_second = False    # cycle two's outage never names

    # The runner-owned lifecycle actions — replace
    # ctx['stop_plant']/ctx['start_plant'].
    def stop(self):
        self.calls.append('stop')
        if self.stop_raises:
            raise RuntimeError('docker stop failed: no such container')
        if self.inert_stop:
            return  # the lever ran but the field kept serving
        self.plant_up = False
        if self.aborts:
            self.active_up = False

    def start(self):
        self.calls.append('start')
        if self.start_raises:
            raise RuntimeError('docker start failed: no such container')
        if not self.never_returns:
            self.plant_up = True
        if self.tick_regress:
            self.tick = 0      # a restarted process re-serves tick 0
        if self.aborts_on_recovery:
            self.active_up = False

    def _stops(self):
        return self.calls.count('stop')

    def _down(self):
        return not self.plant_up

    def _link(self):
        if self.healthy_through:
            return 'connected'
        if self.link_stuck and self._stops():
            return 'disconnected'
        return 'disconnected' if self._down() else 'connected'

    # One completed scan per /snapshot read: the severed link counts
    # the boundary failures and stands the named record; the first
    # healthy exchange clears it — unless a doctor holds it.
    def _scan(self):
        self.tick += 1
        if self._down():
            self.connected_scans = 0
            if not self.flat_health:
                self.failed_reads += 5
                self.failed_writes += 2
                self.consecutive += 7
                self.io_fault = {'tick': self.tick, 'point': 10,
                                 'direction': 'in',
                                 'error': {'disconnected': 10}}
            if not self.never_names \
                    and not (self.weak_second and self._stops() >= 2):
                self.driver_error = self.LINK_ERROR
            return
        self.connected_scans += 1
        if self.late_clear and self.connected_scans >= self.late_clear:
            self.driver_error = None
        elif not self.lingering and not self.late_clear:
            self.driver_error = None
        self.consecutive = max(self.consecutive, 3) \
            if self.stuck_streak else 0
        if self.resets_health:
            self.failed_reads = 0
            self.failed_writes = 0
            self.io_fault = None

    def _driver(self):
        if self.no_driver:
            return None
        driver = {'link': self._link()}
        if self.legacy_driver:
            return driver   # the served surface predates last_error
        driver['last_error'] = self.driver_error
        driver['exchange'] = None
        return driver

    def _health(self):
        health = {'failed_reads': self.failed_reads,
                  'failed_writes': self.failed_writes,
                  'consecutive_failures': self.consecutive,
                  'last_error': self.io_fault,
                  'scan_overruns': 0}
        driver = self._driver()
        if driver is not None:
            health['driver'] = driver
        return health

    # The plant wire protocol — replaces scenarios._plant_probe and
    # backs ctx['plant_ctl'] through the shipped tool's wrap.
    def plant_request(self, ctx, request, timeout=5):
        if not self.plant_up:
            raise urllib.error.URLError('connection refused')
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': 10, 'direction': 'in',
                 'sample': {'value': {'float': 1.5},
                            'quality': 'good', 'tick': 0},
                 'fault': None}]}
        raise AssertionError('unexpected plant request %s' % request)

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.plant_request(None, request), *args)

    # The monitor surface — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                if self.peer_down:
                    raise urllib.error.URLError('connection refused')
                role = 'standby'
                if self.peer_wrong_role \
                        or (self.promoted and not self.plant_up):
                    role = 'active'
                report = {'role': role, 'tick': self.tick}
                if role == 'standby':
                    report['sync'] = {'tracking': {'aligned': self.tick}} \
                        if not self.peer_untracked \
                        else {'degraded': {'reason': 'test'}}
                return 200, report
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.active_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            role = 'active'
            if self.owner_moves and not self.plant_up:
                role = 'standby'
            if self.demoted_end and self.calls.count('start') >= 2 \
                    and self.connected_scans >= 1:
                role = 'standby'
            return 200, {'role': role, 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            return 200, {'tick': self.tick, 'points': [],
                         'io_health': self._health()}
        raise AssertionError('unexpected request %s %s' % (method, url))


class RemoteDriverRecoveryTests(unittest.TestCase):
    """scenario_remote_driver_recovery against the stubbed feed: the
    stop/start actions cycle the shared plant while the monitor's
    served io_health carries the backend's standing-failure record —
    named on the interrupted link, cleared on the first exchange after
    the link's return, twice, with roles and cadence held."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RemoteDriverFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': '127.0.0.1:9',
                'plant_ctl': feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'stop_plant': feed.stop,
                'start_plant': feed.start}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = self._ctx(feed) if ctx is None else ctx
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_plant_probe',
                             feed.plant_request), \
                patch.object(scenarios, 'REMOTE_RECOVERY_SETTLE', 0.5), \
                patch.object(scenarios, 'REMOTE_RECOVERY_POLL', 0.001), \
                patch.object(scenarios, 'REMOTE_RECOVERY_DEGRADE', 0.5), \
                patch.object(scenarios, 'REMOTE_RECOVERY_RESTORE', 0.5):
            return scenarios.scenario_remote_driver_recovery(ctx)

    def _cycle(self, number):
        return json.loads((self.evidence
                           / ('remote-driver-recovery-cycle-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_remote_driver_recovery,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('remote-driver-recovery'),
                      scenarios.scenario_remote_driver_recovery)

    def test_clean_cycles_pass_and_validate(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.calls,
                         ['stop', 'start', 'stop', 'start'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        first = self._cycle(1)
        outage = first['outage'][-1]
        self.assertEqual(outage['link'], 'disconnected')
        self.assertEqual(outage['last_error'], RemoteDriverFeed.LINK_ERROR)
        self.assertGreater(outage['consecutive'], 0)
        self.assertTrue(outage['fault'])
        connected = first['first_connected']
        self.assertEqual(connected['driver']['link'], 'connected')
        self.assertIsNone(connected['driver']['last_error'])
        self.assertEqual(connected['io_health']
                         ['consecutive_failures'], 0)
        self.assertGreaterEqual(connected['io_health']['failed_reads'],
                                outage['failed_reads'])
        self.assertTrue(connected['io_health']['last_error'])
        digest = json.loads((self.evidence
                             / 'remote-driver-recovery-digest.json'
                             ).read_text())
        self.assertEqual(len(digest['cycles']), 2)
        self.assertEqual(digest['cycles'][0], digest['cycles'][1])
        self.assertEqual(self._cycle(1)['digest'],
                         self._cycle(2)['digest'])

    def test_evidence_entries_cover_both_cycles(self):
        record = self.run_scenario()
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(refs, [
            'evidence/remote-driver-recovery-cycle-1.json',
            'evidence/remote-driver-recovery-cycle-2.json',
            'evidence/remote-driver-recovery-digest.json'])
        report.validate_scenario(record)

    def test_lingering_error_fails(self):
        self.feed.lingering = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'remote-driver-recovery-failed'), record['detail'])
        self.assertIn('lingered', record['detail'])
        report.validate_scenario(record)

    def test_late_clear_fails(self):
        # The record clearing on a later serve is still the contract
        # missed: the first connected serve already carried it.
        self.feed.late_clear = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('lingered', record['detail'])
        report.validate_scenario(record)

    def test_baseline_linger_fails(self):
        # A standing record the healthy exchanges never clear — the
        # defect caught ahead of any staging.
        self.feed.lingering = True
        self.feed.driver_error = 'a refuse record left standing'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'remote-driver-recovery-failed'), record['detail'])
        self.assertIn('ahead of the staged interruption',
                      record['detail'])
        report.validate_scenario(record)

    def test_never_named_fails(self):
        self.feed.never_names = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the standing failure',
                      record['detail'])
        self.assertEqual(self.feed.calls, ['stop', 'start'])
        report.validate_scenario(record)

    def test_uncounted_outage_fails(self):
        self.feed.flat_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never moved io_health', record['detail'])
        report.validate_scenario(record)

    def test_link_never_degrades_fails(self):
        self.feed.healthy_through = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never surfaced', record['detail'])
        report.validate_scenario(record)

    def test_second_outage_never_named_fails(self):
        self.feed.weak_second = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the standing failure',
                      record['detail'])
        self.assertEqual(self.feed.calls,
                         ['stop', 'start', 'stop', 'start'])
        report.validate_scenario(record)

    def test_streak_standing_fails(self):
        self.feed.stuck_streak = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('failure streak still stood', record['detail'])
        report.validate_scenario(record)

    def test_reset_history_fails(self):
        self.feed.resets_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('counted history reset', record['detail'])
        report.validate_scenario(record)

    def test_promotion_during_outage_fails(self):
        self.feed.promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'remote-driver-recovery-failed'), record['detail'])
        self.assertIn('roles did not hold', record['detail'])
        report.validate_scenario(record)

    def test_owner_role_move_fails(self):
        self.feed.owner_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('roles did not hold', record['detail'])
        report.validate_scenario(record)

    def test_aborted_run_fails(self):
        self.feed.aborts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never answered', record['detail'])
        report.validate_scenario(record)

    def test_aborted_recovery_fails(self):
        self.feed.aborts_on_recovery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never answered', record['detail'])
        report.validate_scenario(record)

    def test_never_reattached_fails(self):
        self.feed.link_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never re-attached', record['detail'])
        report.validate_scenario(record)

    def test_rewound_tick_reports_nondeterministic(self):
        self.feed.tick_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'remote-driver-recovery-nondeterministic'),
            record['detail'])
        self.assertIn('rewound', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'interrupted': 'severed'}, {}, {'cycle': 1}),
                       ({'interrupted': 'skipped'}, {}, {'cycle': 2})])
        with patch.object(scenarios, '_remote_driver_cycle',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'remote-driver-recovery-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_restore_fails(self):
        self.feed.demoted_end = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not settle back on its launch roles',
                      record['detail'])
        report.validate_scenario(record)

    def test_inert_stop_is_inconclusive(self):
        self.feed.inert_stop = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reached the plant link', record['detail'])
        self.assertEqual(self.feed.calls, ['stop', 'start'])
        report.validate_scenario(record)

    def test_unreturned_plant_is_inconclusive(self):
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never served again', record['detail'])
        report.validate_scenario(record)

    def test_failed_stop_is_inconclusive(self):
        self.feed.stop_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stop lever', record['detail'])
        report.validate_scenario(record)

    def test_failed_start_is_inconclusive(self):
        self.feed.start_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start lever', record['detail'])
        report.validate_scenario(record)

    def test_missing_actions_is_inconclusive(self):
        ctx = self._ctx(self.feed)
        del ctx['stop_plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('link-staging lever', record['detail'])
        ctx = self._ctx(self.feed)
        del ctx['start_plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.peer_wrong_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_silent_peer_is_inconclusive(self):
        self.feed.peer_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered', record['detail'])
        report.validate_scenario(record)

    def test_precontract_surface_is_inconclusive(self):
        self.feed.no_driver = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the contract', record['detail'])
        report.validate_scenario(record)

    def test_legacy_driver_is_inconclusive(self):
        self.feed.legacy_driver = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the contract', record['detail'])
        report.validate_scenario(record)

    def test_unclassifiable_outage_is_inconclusive(self):
        # The staged interruption never surfaces degraded and no
        # probe seam can say whether the lever ever bit.
        self.feed.healthy_through = True
        ctx = self._ctx(self.feed)
        del ctx['plant_ctl']
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant probe seam', record['detail'])
        report.validate_scenario(record)

    def test_probe_fallback_covers_raw_plant_seam(self):
        # Without the shipped tool's seam the raw ctx['plant'] probe
        # classifies the same rig states — here, the lever that left
        # the field serving.
        self.feed.inert_stop = True
        ctx = self._ctx(self.feed)
        del ctx['plant_ctl']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reached the plant link', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = RemoteDriverFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
