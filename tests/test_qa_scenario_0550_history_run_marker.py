"""The 0550_history_run_marker leg's scenario unit coverage — the
feed fake and TestCase classes for scenario_history_run_marker,
following the module-per-leg convention (#928, #940). The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'HistoryRunMarkerTests.test_registered_in_scenarios',
    'HistoryRunMarkerTests.test_clean_rig_passes_and_validates',
    'HistoryRunMarkerTests.test_run_bumps_on_resumed_restart',
    'HistoryRunMarkerTests.test_resumed_restart_continues_the_axis',
    'HistoryRunMarkerTests.test_cold_restart_restarts_the_axis',
    'HistoryRunMarkerTests.test_held_cursor_sees_run_on_empty_answers',
    'HistoryRunMarkerTests.test_stale_marker_fails',
    'HistoryRunMarkerTests.test_resumed_axis_regression_fails',
    'HistoryRunMarkerTests.test_cold_axis_continuation_fails',
    'HistoryRunMarkerTests.test_flapping_marker_is_nondeterministic',
    'HistoryRunMarkerTests.test_unjournaled_lifetime_fails',
    'HistoryRunMarkerTests.test_spurious_promotion_fails',
    'HistoryRunMarkerTests.test_predating_rig_is_inconclusive',
    'HistoryRunMarkerTests.test_unreachable_rig_is_inconclusive',
    'HistoryRunMarkerTests.test_failed_action_is_inconclusive',
    'HistoryRunMarkerTests.test_unreturned_monitor_is_inconclusive',
    'HistoryRunMarkerTests.test_missing_action_is_inconclusive',
    'HistoryRunMarkerTests.test_two_runs_produce_identical_evidence',
})


class HistoryRunMarkerFeed:
    """A stubbed pair for the history-run-marker scenario. ctrl-a owns
    the field and ctrl-b tracks its checkpoint stream; every answered
    monitor request is one scan, so each peer's tick climbs per poll
    and its volatile history ring appends the tick. Each peer's
    --journal-file is a real append-only file the feed marks per
    lifetime, and each served /history envelope stamps the process's
    own run ordinal — the file's run_boundary count — over its own
    tick domain. The runner-owned restart actions cycle the
    container: the warm restart keeps the state file so the tick
    domain continues while `run` bumps; the cold restart drops it —
    the real runner action's unlink — so the axis restarts at 1 under
    the new run. Fault flags stage the leg's named defects."""

    POINT = 10

    def __init__(self, journal_a, journal_b):
        self.a_tick = 400
        self.b_tick = 400
        self.aligned = 400       # ctrl-b's applied stream alignment
        self.a_up = True
        self.b_up = True
        self.a_down = 0          # refused-request window while down
        self.b_down = 0
        self.run_a = 1           # the process's real lifetime ordinal
        self.run_b = 1
        self.serve_a = 1         # the ordinal its envelopes stamp
        self.serve_b = 1
        self.ring_a = []         # the volatile retained seqs
        self.ring_b = []
        self.restarts = []
        self.cold_restarts = []
        self.journal_a = Path(journal_a)
        self.journal_b = Path(journal_b)
        # Fault flags staging the named failures and inconclusives.
        self.down = False            # every request refused
        self.predates = False        # ctrl-b's envelopes carry run 0
        self.marker_stale = False    # envelopes keep stamping run-1
        self.flapping = False        # the stamped run alternates
        self.resumes_cold = False    # the warm restart drops the axis
        self.cold_continues = False  # the cold restart keeps the axis
        self.promotes = False        # ctrl-b promotes inside the gap
        self.journal_lag = False     # the boundary marker never lands
        self.returns = True          # False: the restart action fails
        self.serves = True           # False: the monitor never returns
        self.applies = True          # ctrl-b's checkpoint applies land
        self._flap = 0
        self._append(self.journal_a,
                     {'run_boundary': {'run': 1, 'tick': 0}})
        self._append(self.journal_b,
                     {'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    # The runner-owned actions' simulated halves: the container cycles,
    # the run bumps, the journal gains a boundary marker, and the axis
    # follows the resume form — the real actions' docker and
    # state-file halves run in the test's ctx wiring.
    def restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        assert name == 'standby'
        self.b_up = False
        self.b_down = 2
        self.run_b += 1
        self.ring_b = []             # the volatile ring dies
        if not self.marker_stale:
            self.serve_b = self.run_b
        self._flap = 0
        if self.resumes_cold:
            # The defect: the resume lost the domain AND the run never
            # rejoins the stream — the fresh axis climbs from the
            # floor where tracking would have carried it.
            self.b_tick = 0
            self.applies = False
        if not self.journal_lag:
            self._append(self.journal_b,
                         {'run_boundary': {'run': self.run_b,
                                           'tick': self.b_tick}})

    def cold_restart(self, name):
        self.cold_restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        assert name == 'active'
        self.a_up = False
        self.a_down = 2
        self.run_a += 1
        self.ring_a = []
        if not self.marker_stale:
            self.serve_a = self.run_a
        self._flap = 0
        if not self.cold_continues:
            self.a_tick = 0
        if not self.journal_lag:
            self._append(self.journal_a,
                         {'run_boundary': {'run': self.run_a,
                                           'tick': self.a_tick}})

    def _scan_b(self):
        """One tracking cycle on ctrl-b: the pull applies under the
        never-rewind rule — a regressed stream lands at the run tick —
        then the scan's own tick advance appends to the ring."""
        if self.a_up and self.applies:
            ckpt = self.a_tick
            if self.aligned is None or ckpt >= self.aligned:
                self.b_tick = max(self.b_tick, ckpt)
            self.aligned = ckpt
        self.b_tick += 1
        self.ring_b.append(self.b_tick)
        self.ring_b[:] = self.ring_b[-200:]

    def _scan_a(self):
        self.a_tick += 1
        self.ring_a.append(self.a_tick)
        self.ring_a[:] = self.ring_a[-200:]

    def _served_run(self, side):
        serve = self.serve_b if side == 'b' else self.serve_a
        run = self.run_b if side == 'b' else self.run_a
        if self.flapping and run > 1:
            serve = run - (self._flap % 2)
            self._flap += 1
        if self.predates and side == 'b':
            serve = 0
        return serve

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        params = dict(pair.split('=', 1)
                      for pair in query.split('&') if pair)
        side = 'b' if host == 'ctrl-b:2' else 'a'
        if self.down:
            raise urllib.error.URLError('connection refused')
        if side == 'b':
            if not self.b_up:
                self.b_down -= 1
                if self.b_down <= 0 and self.serves:
                    self.b_up = True
                else:
                    raise urllib.error.URLError('connection refused')
            self._scan_b()
            if (method, route) == ('GET', '/role'):
                role = 'standby'
                if self.promotes and not self.a_up:
                    role = 'promoting'
                sync = {'tracking': {'aligned': self.aligned}} \
                    if self.a_up else {'degraded': {'detail':
                                                  'checkpoint pull '
                                                  'failed'}}
                return 200, {'role': role, 'tick': self.b_tick,
                             'sync': sync}
        else:
            if not self.a_up:
                self.a_down -= 1
                if self.a_down <= 0 and self.serves:
                    self.a_up = True
                else:
                    raise urllib.error.URLError('connection refused')
            self._scan_a()
            if (method, route) == ('GET', '/role'):
                return 200, {'role': 'active', 'tick': self.a_tick,
                             'sync': None}
        if (method, route) == ('GET', '/history'):
            since = int(params.get('since', '0'))
            point = int(params.get('point', str(self.POINT)))
            if point != self.POINT:
                return 200, []
            ring = self.ring_b if side == 'b' else self.ring_a
            return 200, [{'point': self.POINT,
                          'run': self._served_run(side),
                          'samples': [
                              {'seq': seq,
                               'sample': {'tick': seq,
                                          'value': {'bool': True},
                                          'quality': 'good'}}
                              for seq in ring if seq > since]}]
        raise AssertionError('unexpected request %s %s' % (method, url))


class HistoryRunMarkerTests(unittest.TestCase):
    """scenario_history_run_marker against the stubbed pair: the
    tracking peer's warm restart bumps the served `run` while its
    seq axis continues the persisted tick domain, the field owner's
    cold restart bumps `run` again while the restarted axis hides
    behind the held since-cursor — every answer stamped, empty pages
    included — each journal file records one boundary marker per
    lifetime, and the launch roles are restored."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{"tick": 400}')
            self.dirs[name] = directory
        self.journal_a = self.dirs['a'] / 'journal.jsonl'
        self.journal_b = self.dirs['b'] / 'journal.jsonl'
        self.feed = HistoryRunMarkerFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return None

        def restart(name):
            runner.restart_controller(
                'qa-1', name,
                lambda event, detail=None: events.append(event))
            feed.restart(name)

        def cold_restart(name):
            # The real runner action — the state.json drop under the
            # bounded run dir — then the feed's simulated cold start.
            runner.cold_restart_controller(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event))
            feed.cold_restart(name)

        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence),
                'restart_controller': restart,
                'cold_restart_controller': cold_restart,
                'failover_misses': 120,
                'journal_files': {'active': str(self.journal_a),
                                  'standby': str(self.journal_b)}}
        if ctx is not None:
            base.update(ctx)
        patches = {'POLL_INTERVAL': 0.001, 'MARKER_SETTLE': 0.5,
                   'MARKER_RETURN': 0.5, 'MARKER_POLL': 0.001,
                   'MARKER_JOURNAL': 0.3}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(runner, 'docker', fake_docker):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_history_run_marker(base)
        return record, calls, events

    def _pass_evidence(self, number):
        name = 'history-run-marker-pass-' + str(number) + '.json'
        return json.loads((self.evidence / name).read_text())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_history_run_marker,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record, calls, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # The real runner actions ran: each pass warm-restarts the
        # tracked peer and cold-restarts the field owner.
        self.assertEqual(self.feed.restarts, ['standby', 'standby'])
        self.assertEqual(self.feed.cold_restarts, ['active', 'active'])
        self.assertEqual(calls.count(('start', 'dcs-hw-qa-1-b')), 2)
        self.assertEqual(calls.count(('start', 'dcs-hw-qa-1-a')), 2)
        self.assertIn('controller-restarted', events)
        self.assertIn('controller-cold-restarted', events)
        self.assertFalse((self.dirs['a'] / 'state.json').exists())
        self.assertTrue((self.dirs['b'] / 'state.json').exists())
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Three lifetimes per journal, each boundary marker named.
        for path, ticks in ((self.journal_a, [0, 0, 0]),):
            bounds = [item['run_boundary'] for item in
                      scenarios._journal_entries(path)
                      if 'run_boundary' in item]
            self.assertEqual([b['run'] for b in bounds], [1, 2, 3])
            self.assertEqual([b['tick'] for b in bounds], ticks)
        bounds = [item['run_boundary'] for item in
                  scenarios._journal_entries(self.journal_b)
                  if 'run_boundary' in item]
        self.assertEqual([b['run'] for b in bounds], [1, 2, 3])
        self.assertGreater(bounds[1]['tick'], 0)
        self.assertGreater(bounds[2]['tick'], bounds[1]['tick'])
        # The normalized digests: both passes identical.
        digests = [self._pass_evidence(n)['digest'] for n in (1, 2)]
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], {
            'resume_run': 'advanced', 'resume_axis': 'continued',
            'cold_run': 'advanced', 'cold_axis': 'restarted',
            'cold_cursor': 'marked-empty', 'roles': 'restored'})

    def test_run_bumps_on_resumed_restart(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for number in (1, 2):
            resume = self._pass_evidence(number)['resume']
            self.assertEqual(resume['before']['run'], number)
            self.assertTrue(all(a['run'] == number + 1
                                for a in resume['answers']))
            self.assertEqual(resume['before']['sibling_run'],
                             resume['sibling_after'])

    def test_resumed_restart_continues_the_axis(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        resume = self._pass_evidence(1)['resume']
        cursor = resume['before']['cursor']
        # The held cursor sees samples again — the resumed axis
        # rejoins it — and the fresh ring's head never dropped back.
        self.assertTrue(any(a['samples'] for a in resume['answers']))
        self.assertGreaterEqual(resume['axis']['head'][0], cursor - 8)

    def test_cold_restart_restarts_the_axis(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        cold = self._pass_evidence(1)['cold']
        self.assertEqual(cold['axis']['run'], 2)
        self.assertLess(cold['axis']['head'][0],
                        cold['before']['cursor'])
        self.assertEqual(cold['journal'][-1]['tick'], 0)

    def test_held_cursor_sees_run_on_empty_answers(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        cold = self._pass_evidence(1)['cold']
        empty = [a for a in cold['answers'] if not a['samples']]
        self.assertGreaterEqual(len(empty), 2)
        self.assertTrue(all(a['run'] == 2 for a in cold['answers']))

    def test_stale_marker_fails(self):
        # The pre-#886 defect: the restarted process's envelopes keep
        # stamping the old lifetime — a held cursor reads a
        # phantom-idle stream.
        self.feed.marker_stale = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('history-run-marker-failed',
                      record.get('detail', ''))
        self.assertIn('phantom-idle', record.get('detail', ''))
        report.validate_scenario(record)

    def test_resumed_axis_regression_fails(self):
        # The warm restart drops the persisted domain: the held cursor
        # never sees a sample again and the fresh ring restarts low.
        self.feed.resumes_cold = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('history-run-marker-failed',
                      record.get('detail', ''))
        self.assertIn('axis', record.get('detail', ''))
        report.validate_scenario(record)

    def test_cold_axis_continuation_fails(self):
        # The cold start kept the tick domain: the held cursor sees
        # samples immediately, so the restart is indistinguishable.
        self.feed.cold_continues = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('history-run-marker-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_flapping_marker_is_nondeterministic(self):
        self.feed.flapping = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('history-run-marker-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_lifetime_fails(self):
        self.feed.journal_lag = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('history-run-marker-failed',
                      record.get('detail', ''))
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_spurious_promotion_fails(self):
        self.feed.promotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promot', record.get('detail', ''))
        report.validate_scenario(record)

    def test_predating_rig_is_inconclusive(self):
        self.feed.predates = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the run-marker contract',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.down = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.returns = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_action_is_inconclusive(self):
        record, _, _ = self.run_scenario(
            ctx={'cold_restart_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no documented seam', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs over the same rig
        # transitions record the same report and evidence bytes — the
        # feed's behavior is call-count keyed, never wall-clock.
        runs = []
        for _index in range(2):
            for path in (self.journal_a, self.journal_b):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            for directory in self.dirs.values():
                (directory / 'state.json').write_text('{"tick": 400}')
            feed = HistoryRunMarkerFeed(self.journal_a, self.journal_b)
            record, _, _ = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
