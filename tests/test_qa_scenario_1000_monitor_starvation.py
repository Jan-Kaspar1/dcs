"""The 1000_monitor_starvation leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_monitor_starvation, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'MonitorStarvationTests.test_registered_in_scenarios',
    'MonitorStarvationTests.test_armed_pair_passes_and_validates',
    'MonitorStarvationTests.test_starved_liveness_reads_report_failed',
    'MonitorStarvationTests.test_late_liveness_reads_report_nondeterministic',
    'MonitorStarvationTests.test_promoting_standby_reports_nondeterministic',
    'MonitorStarvationTests.test_stalled_pulls_report_failed',
    'MonitorStarvationTests.test_unfenced_probe_reports_nondeterministic',
    'MonitorStarvationTests.test_journaled_claim_loss_reports_nondeterministic',
    'MonitorStarvationTests.test_journaled_role_change_reports_nondeterministic',
    'MonitorStarvationTests.test_unrecovered_submission_lane_reports_failed',
    'MonitorStarvationTests.test_no_armed_tracking_standby_inconclusive',
    'MonitorStarvationTests.test_missing_plant_endpoint_inconclusive',
    'MonitorStarvationTests.test_two_runs_produce_identical_evidence',
})


class StarvationFeed:
    """A stubbed armed pair for the monitor-starvation leg. ctrl-a is
    the flooded active — each served /snapshot is one paced scan
    having landed — and ctrl-b is the armed tracking standby: each
    /role poll on it is one landed checkpoint pull, its reported
    tracking alignment advancing one scan, unless a doctor moves its
    sync or role mid-hold. The leg's raw-connection seam hands out
    FakeSockets; the plant fences every third-party probe while the
    writer claim stands."""
    POINT = 204

    def __init__(self):
        self.tick = 0         # the active's run tick
        self.aligned = 0      # the standby's landed checkpoint pulls
        self.polls = 0        # standby /role polls — the heartbeat
        self.receipts = []
        self.journal_a = []
        self.journal_b = []
        self.seq = 0
        self.sockets = []
        # The doctors for the named-failure cases.
        self.starve_reads = False    # the serving reads never answer
        self.late_reads = False      # they answer past the bound
        self.promote_after = None    # poll count the standby promotes at
        self.stall_after = None      # poll count its pulls stop landing
        self.never_tracks = False    # no armed tracking standby settles
        self.unfenced = False        # probes write through the claim
        self.claim_lost = False      # a field_claim_lost is journaled
        self.role_journaled = False  # a role_changed is journaled
        self.no_recover = False      # the submission lane never frees

    # --- the runner-owned transport seams ---

    def connect(self, base, timeout=5):
        stream = FakeSocket()
        self.sockets.append(stream)
        return stream

    def plant(self, ctx, request, timeout=5):
        if self.unfenced:
            return {'ok': {'stepped': True}}
        return {'error': {'kind': 'fenced'}}

    def request_status(self, method, url, body=None, timeout=5):
        if self.no_recover:
            raise urllib.error.URLError('timed out')
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a') and path == '/scan' \
                and method == 'POST':
            return 409, b'{"error": "paced"}'
        raise urllib.error.URLError('no route ' + path)

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.active(method, path, body)
        if host.startswith('ctrl-b'):
            return self.standby(method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def active(self, method, path, body):
        if method == 'GET' \
                and any(not stream.closed for stream in self.sockets):
            if self.starve_reads:
                raise urllib.error.URLError('timed out')
            if self.late_reads:
                time.sleep(0.5)   # past the patched bound — a late
                                  # answer, the bound contract broken
        if path == '/role':
            return 200, {'role': 'active', 'tick': self.tick}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'kind': 'bool', 'writable': True,
                 'direction': 'in', 'value_type': 'bool'}]}
        if path == '/snapshot':
            self.tick += 1   # one paced scan landed per served read
            return 200, {'tick': self.tick,
                         'points': [{'point': self.POINT,
                                     'sample': {'value': {
                                         'bool': False}}}]}
        if path == '/checkpoint':
            return 200, {'tick': self.tick,
                         'model_fingerprint': 'fp'}
        if path == '/receipts':
            for receipt in self.receipts:
                if 'accepted' in receipt['outcome']:
                    receipt['outcome'] = {'applied': {'tick': self.tick}}
                    self.seq += 1
                    self.journal_a.append({
                        'seq': self.seq, 'tick': self.tick,
                        'event': {'command_settled': {'receipt': {
                            'id': receipt['id'], 'actor': 'qa-lane',
                            'command': receipt['command'],
                            'outcome': {'applied': {}}}}}})
            return 200, list(self.receipts)
        if path.startswith('/journal'):
            entries = list(self.journal_a)
            if '?since=' in path and self.claim_lost:
                entries.append({'seq': 90, 'tick': self.tick,
                                'event': {'field_claim_lost': {
                                    'point': 9}}})
            return 200, entries
        if path == '/command' and method == 'POST':
            if self.no_recover:
                raise urllib.error.URLError('timed out')
            receipt = {'id': 'r' + str(len(self.receipts) + 1),
                       'command': body['command'],
                       'outcome': {'accepted': {}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise urllib.error.URLError('no route ' + path)

    def standby(self, method, path, body):
        if path == '/role':
            self.polls += 1
            if self.never_tracks:
                return 200, {'role': 'standby', 'tick': self.aligned,
                             'sync': {'unsynchronized': {}}}
            if self.promote_after is not None \
                    and self.polls >= self.promote_after:
                return 200, {'role': 'promoting', 'tick': self.aligned}
            if self.stall_after is not None \
                    and self.polls >= self.stall_after:
                return 200, {'role': 'standby', 'tick': self.aligned,
                             'sync': {'degraded': {'detail':
                                      'fetch failed: timed out'}}}
            self.aligned += 1   # one landed checkpoint pull
            return 200, {'role': 'standby', 'tick': self.aligned,
                         'sync': {'tracking': {'aligned': self.aligned}}}
        if path.startswith('/journal'):
            entries = list(self.journal_b)
            if '?since=' in path and self.role_journaled:
                entries.append({'seq': 91, 'tick': self.aligned,
                                'event': {'role_changed': {
                                    'from': 'standby',
                                    'to': 'promoting'}}})
            return 200, entries
        raise urllib.error.URLError('no route ' + path)


class MonitorStarvationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = StarvationFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': 'tcp://plant:9', 'failover_misses': 120,
               'evidence_dir': str(self.evidence)}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None, **patches):
        feed = feed or self.feed
        constants = {'POLL_INTERVAL': 0.001, 'LATENCY_BOUND': 0.3,
                     'STARVE_SETTLE': 1.0, 'STARVE_POLL': 0.001,
                     'STARVE_DEADLINE': 1.0, 'STARVE_PULL_ADVANCE': 3,
                     'STARVE_RECOVER': 1.0}
        constants.update(patches)
        with patch.multiple(scenarios, **constants), \
                patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_connect', feed.connect), \
                patch.object(scenarios, '_plant_probe', feed.plant), \
                patch.object(scenarios, '_request_status',
                             feed.request_status):
            return scenarios.scenario_monitor_starvation(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_monitor_starvation, order)
        # The leg sits in the armed pre-switch window — ahead of the
        # tune case's a->b switch, behind the dead-peer case that
        # restores the rig it shares the window with.
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_monitor_starvation))
        self.assertLess(
            order.index(scenarios.scenario_monitor_starvation),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('monitor-starvation'),
                      scenarios.scenario_monitor_starvation)

    def test_armed_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(record['observations'],
                         ['two starvation passes, identical digests'])
        self.assertEqual(len(self.feed.sockets),
                         scenarios.STARVE_CONNECTIONS * 2)
        self.assertTrue(all(stream.closed
                            for stream in self.feed.sockets))
        self.assertTrue(all(stream.sent in scenarios.STARVE_REQUESTS
                            for stream in self.feed.sockets))
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        digest = None
        for number in (1, 2):
            window = json.loads(
                (self.evidence
                 / ('monitor-starvation-pass-' + str(number)
                    + '.json')).read_text())
            self.assertEqual(window['digest']['reads'], {
                '/role': 'bounded', '/snapshot': 'bounded',
                '/checkpoint': 'bounded'})
            self.assertEqual(window['digest']['scans'], 'advanced')
            self.assertEqual(window['digest']['tracking'], 'advanced')
            self.assertEqual(window['digest']['fencing'], 'fenced')
            self.assertEqual(window['digest']['journal'], 'clean')
            self.assertEqual(window['digest']['recovery'], 'settled')
            self.assertEqual(window['digest']['roles'], 'unchanged')
            self.assertEqual(window['violations'], {})
            self.assertEqual(window['armed_miss_budget'], 120)
            if digest is not None:
                self.assertEqual(digest, window['digest'])
            digest = window['digest']

    def test_starved_liveness_reads_report_failed(self):
        self.feed.starve_reads = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        report.validate_scenario(record)

    def test_late_liveness_reads_report_nondeterministic(self):
        self.feed.late_reads = True
        record = self.run_scenario(STARVE_PULL_ADVANCE=1,
                                   STARVE_DEADLINE=4.0)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('past the', record['detail'])
        report.validate_scenario(record)

    def test_promoting_standby_reports_nondeterministic(self):
        self.feed.promote_after = 4
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('promoting', record['detail'])
        report.validate_scenario(record)

    def test_stalled_pulls_report_failed(self):
        self.feed.stall_after = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        report.validate_scenario(record)

    def test_unfenced_probe_reports_nondeterministic(self):
        self.feed.unfenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('wrote through', record['detail'])
        report.validate_scenario(record)

    def test_journaled_claim_loss_reports_nondeterministic(self):
        self.feed.claim_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_journaled_role_change_reports_nondeterministic(self):
        self.feed.role_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('role_changed', record['detail'])
        report.validate_scenario(record)

    def test_unrecovered_submission_lane_reports_failed(self):
        self.feed.no_recover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        self.assertIn('never answered a command', record['detail'])
        report.validate_scenario(record)

    def test_no_armed_tracking_standby_inconclusive(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no armed tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_missing_plant_endpoint_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('fencing probes', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = StarvationFeed()
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
