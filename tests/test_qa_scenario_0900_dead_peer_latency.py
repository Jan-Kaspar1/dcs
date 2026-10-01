"""The 0900_dead_peer_latency leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_dead_peer_latency, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DeadPeerLatencyTests.test_registered_in_scenarios',
    'DeadPeerLatencyTests.test_dead_peer_window_passes_and_validates',
    'DeadPeerLatencyTests.test_refusing_pull_path_also_passes',
    'DeadPeerLatencyTests.test_slow_survivor_read_fails_by_name',
    'DeadPeerLatencyTests.test_driven_reads_starving_mid_batch_fail',
    'DeadPeerLatencyTests.test_admission_rejection_fails_by_name',
    'DeadPeerLatencyTests.test_mid_window_batch_starving_fails',
    'DeadPeerLatencyTests.test_missing_grace_fault_fails_by_name',
    'DeadPeerLatencyTests.test_no_reconvergence_fails',
    'DeadPeerLatencyTests.test_missing_driven_actions_inconclusive',
    'DeadPeerLatencyTests.test_failed_launch_inconclusive',
    'DeadPeerLatencyTests.test_driven_never_serving_inconclusive',
    'DeadPeerLatencyTests.test_failed_stop_inconclusive',
    'DeadPeerLatencyTests.test_identical_evidence_across_runs',
})


class LatencyFeed:
    """A stubbed rig for the dead-peer-latency scenario. ctrl-a owns
    the field; ctrl-b is the tracking standby whose every monitor read
    is one paced scan cycle — while the owner is stopped each read is
    another failed pull, reported as the degraded sync the pair view
    names. ctrl-d is the run's driven third peer: never paced, so it
    reports 'unsynchronized' until a POST /scan batch runs its
    per-scan pulls — each pull waits pull_wait seconds while the batch
    occupies its worker, lands 'degraded' while the source is dead,
    and 'tracking' once the source answers again. Every transition is
    call-count keyed — never wall-clock — so two scenario runs emit
    identical evidence; the pull waits are the only sleeps, staging
    the dead peer's fetch wait and the batch's hold on its worker.
    Fault flags stage each named failure the issue calls out."""

    POINT = 210

    def __init__(self):
        self.owner_up = True
        self.driven_up = False
        self.misses = 0
        self.saw_isolation = False
        self.pulls = 0
        self.driven_sync = 'unsynchronized'
        self.batches = []          # the scans counts posted to ctrl-d
        self.pull_wait = 0.05      # each pull's wait inside a batch
        self.in_flight = 0         # batches currently on their worker
        self.in_flight_reads = 0   # driven reads served mid-batch
        self.tick = 30
        self.stops = []
        self.starts = []
        self.launches = []
        self.removes = []
        self.commands = 0
        # Fault flags stage each named failure the issue calls out.
        self.pulls_refuse = False      # the dead peer refuses, not hangs
        self.slow_survivor = False     # survivor reads exceed the bound
        self.slow_driven = False       # driven reads starve mid-batch
        self.batch_refused = False     # the mid-window batch answers 500
        self.never_unsynced = False    # ctrl-d reports converged at launch
        self.never_reconverge = False  # ctrl-b stays degraded
        self.queue_full = False        # the command is refused at admission
        self.stop_fails = False
        self.start_fails = False
        self.launch_fails = False
        self.teardown_fails = False
        self.driven_never_serves = False

    # --- the runner-owned actions the ctx carries ---

    def stop(self, name):
        if self.stop_fails:
            raise RuntimeError('docker stop failed')
        self.stops.append(name)
        self.owner_up = False

    def start(self, name):
        if self.start_fails:
            raise RuntimeError('docker start failed')
        self.starts.append(name)
        self.owner_up = True

    def launch(self, name):
        if self.launch_fails:
            raise RuntimeError('docker run failed')
        self.launches.append(name)
        if not self.driven_never_serves:
            self.driven_up = True
        return {'container': 'dcs-hw-qa-1-d'}

    def teardown(self):
        if self.teardown_fails:
            raise RuntimeError('docker rm failed')
        self.removes.append('d')
        self.driven_up = False

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.owner(method, path, body)
        if host.startswith('ctrl-b'):
            return self.survivor(method, path, body)
        if host.startswith('ctrl-d'):
            return self.driven(method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def owner(self, method, path, body):
        if not self.owner_up:
            raise urllib.error.URLError('connection refused')
        if path == '/role':
            return 200, {'role': 'active', 'tick': self.tick}
        raise urllib.error.URLError('no route ' + path)

    def survivor(self, method, path, body):
        if self.slow_survivor:
            time.sleep(0.5)   # past the patched bound: starvation
        # one paced cycle: the pull the loop runs before its scan —
        # a failed pull is the degraded sync the pair view names
        if self.owner_up:
            self.misses = 0
        else:
            self.misses += 1
            self.saw_isolation = True
        self.tick += 1
        if path == '/role':
            if self.misses == 0 and not (self.never_reconverge
                                         and self.saw_isolation):
                sync = {'tracking': {'aligned': self.tick}}
            else:
                sync = {'degraded': {'detail': 'fetch failed: refused'}}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': sync}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'kind': 'bool', 'writable': True,
                 'direction': 'in', 'value_type': 'bool'}]}
        if path == '/snapshot':
            return 200, {'tick': self.tick}
        if path.startswith('/journal'):
            return 200, [{'seq': self.commands, 'tick': self.tick}]
        if path == '/command' and method == 'POST':
            self.commands += 1
            if self.queue_full:
                reason = {'queue_full': {}}
            else:
                reason = {'not_active': {}}
            return 200, {'id': 'r' + str(self.commands),
                         'outcome': {'rejected': {'reason': reason}}}
        raise urllib.error.URLError('no route ' + path)

    def driven(self, method, path, body):
        if not self.driven_up:
            raise urllib.error.URLError('connection refused')
        if self.in_flight:
            if self.slow_driven:
                time.sleep(0.5)   # the batch starves the endpoint
            else:
                self.in_flight_reads += 1
        if path == '/role':
            sync = {'tracking': {'aligned': self.tick}} \
                if self.never_unsynced else self.driven_sync
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': sync}
        if path == '/snapshot':
            return 200, {'tick': self.tick}
        if path.startswith('/journal'):
            return 200, [{'seq': self.pulls}]
        if path == '/scan' and method == 'POST':
            scans = int((body or {}).get('scans') or 0)
            self.batches.append(scans)
            if self.batch_refused and not self.owner_up:
                return 500, {'error': 'no worker free'}
            self.in_flight += 1
            try:
                for _ in range(scans):
                    self.pulls += 1
                    if self.pull_wait:
                        time.sleep(self.pull_wait)
                    if self.owner_up:
                        self.driven_sync = {'tracking':
                                            {'aligned': self.tick}}
                    elif self.pulls_refuse:
                        self.driven_sync = {'degraded':
                                            {'detail': 'refused'}}
                    else:
                        self.driven_sync = {'degraded':
                                            {'detail': 'fetch timed '
                                             'out'}}
            finally:
                self.in_flight -= 1
            return 200, {'scanned': scans, 'tick': self.tick}
        raise urllib.error.URLError('no route ' + path)


class DeadPeerLatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = LatencyFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'evidence_dir': str(self.evidence),
               'stop_controller': feed.stop,
               'start_controller': feed.start,
               'start_driven': feed.launch,
               'stop_driven': feed.teardown}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LATENCY_BOUND', 0.3), \
                patch.object(scenarios, 'LATENCY_POLL', 0.001), \
                patch.object(scenarios, 'LATENCY_WINDOW', 2.0), \
                patch.object(scenarios, 'LATENCY_GRACE', 0.0), \
                patch.object(scenarios, 'LATENCY_SERVE_DEADLINE', 0.5), \
                patch.object(scenarios, 'LATENCY_SETTLE_DEADLINE', 1.0), \
                patch.object(scenarios, 'LATENCY_HEALTHY_SCANS', 6):
            return scenarios.scenario_dead_peer_latency(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_dead_peer_latency, order)
        # The same restored pre-switch window as the force case —
        # ahead of the tune case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_stale_freshness),
            order.index(scenarios.scenario_dead_peer_latency))
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('dead-peer-latency'),
                      scenarios.scenario_dead_peer_latency)

    def test_dead_peer_window_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(self.feed.stops, ['active'])
        self.assertEqual(self.feed.starts, ['active'])
        self.assertEqual(self.feed.launches, ['active'])
        self.assertEqual(self.feed.removes, ['d'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        window = json.loads(
            (self.evidence / 'dead-peer-latency-window.json')
            .read_text())
        for label, entry in window['endpoints'].items():
            self.assertTrue(entry['answered'], label)
            self.assertTrue(entry['within_bound'], label)
        self.assertEqual(window['command']['status'], 200)
        self.assertEqual(window['command']['outcome'],
                         'rejected:not_active')
        self.assertIn('unsynchronized', window['grace_fault'])
        self.assertTrue(window['batch']['completed'])
        self.assertEqual(window['batch']['status'], 200)
        # The batch occupied its worker while the driven monitor kept
        # answering the sampled reads.
        self.assertGreater(self.feed.in_flight_reads, 0)
        health = json.loads(
            (self.evidence / 'dead-peer-latency-health.json')
            .read_text())
        self.assertEqual(health['faults'], [])
        self.assertEqual(health['roles']['driven'], 'standby')
        batch = json.loads(
            (self.evidence / 'dead-peer-latency-healthy-batch.json')
            .read_text())
        self.assertTrue(batch['completed'])
        self.assertEqual(batch['status'], 200)
        self.assertEqual(batch['scans'], 6)
        for label, entry in batch['endpoints'].items():
            self.assertTrue(entry['answered'], label)
            self.assertTrue(entry['within_bound'], label)

    def test_refusing_pull_path_also_passes(self):
        self.feed.pulls_refuse = True
        self.feed.pull_wait = 0
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        window = json.loads(
            (self.evidence / 'dead-peer-latency-window.json')
            .read_text())
        self.assertTrue(window['batch']['completed'])

    def test_slow_survivor_read_fails_by_name(self):
        self.feed.slow_survivor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_driven_reads_starving_mid_batch_fail(self):
        self.feed.slow_driven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_admission_rejection_fails_by_name(self):
        self.feed.queue_full = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('admission', record['detail'])
        report.validate_scenario(record)

    def test_mid_window_batch_starving_fails(self):
        self.feed.batch_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('batch starved', record['detail'])
        report.validate_scenario(record)

    def test_missing_grace_fault_fails_by_name(self):
        self.feed.never_unsynced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unsynchronized', record['detail'])
        report.validate_scenario(record)

    def test_no_reconvergence_fails(self):
        self.feed.never_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_missing_driven_actions_inconclusive(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'stop_controller': self.feed.stop,
               'start_controller': self.feed.start}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no documented seam', record['detail'])
        report.validate_scenario(record)

    def test_failed_launch_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch never completed', record['detail'])

    def test_driven_never_serving_inconclusive(self):
        self.feed.driven_never_serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered /role', record['detail'])

    def test_failed_stop_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('induction never completed', record['detail'])

    def test_identical_evidence_across_runs(self):
        runs = []
        for _ in range(2):
            feed = LatencyFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed,
                                       ctx=self._ctx(feed))
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
