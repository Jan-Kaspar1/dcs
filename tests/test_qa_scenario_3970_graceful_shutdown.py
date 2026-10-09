"""The 3970_graceful_shutdown leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_graceful_shutdown, split out
of the test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class ShutdownFeed:
    """A stubbed pair for scenario_graceful_shutdown. ctrl-a owns the
    field — every request on it is one completed scan, advancing its
    tick — while ctrl-b tracks. The ctx signal/start/state wrappers
    drive the container verdicts the leg audits: the first signal
    stops the container with the staged exit and log marker while
    persisting the state file, the start relaunches it onto that
    file, and the impeded mount parks the first-phase-three signal in
    the flush so only the second forces the prompt exit. The fault
    flags stage each named defect the leg's diagnostics cover."""

    def __init__(self, dirs):
        self.dirs = {key: Path(value) for key, value in dirs.items()}
        self.a_tick = 400
        self.b_tick = 400
        self.a_running = True
        self.b_running = True
        self.a_role = 'active'
        self.b_role = 'standby'
        self.b_tracking = True
        self.a_exit = None
        self.a_logs = ''
        self.stalled = False
        self.signals = []
        self.phase3 = 0
        # Fault flags staging the named failures.
        self.down = False                # the rig is unreachable
        self.untracked = False           # the pair never settles
        self.kill_hangs = False          # the signal never stops it
        self.kill_exit = 0               # the signaled container's exit
        self.kill_marker = True          # its log names the shutdown
        self.state_lags = False          # the flush loses the run
        self.state_unreadable = False    # the state file cannot be read
        self.start_raises = False        # the relaunch never happens
        self.resume_stalls = False       # the relaunch never serves
        self.resume_rewinds = False      # the resume adopts stale state
        self.reconverge_fails = False    # the pair never reconverges
        self.stall_ignores = False       # the first signal exits anyway
        self.phase3_kill = False         # phase three kills like an old rev
        self.second_hangs = False        # the second signal never exits
        self.second_exit = 143           # the prompt exit's status
        self.impede_raises = False       # the lever's stage half errs
        self.restore_raises = False      # the lever's restore half errs

    def _advance(self):
        self.a_tick += 1
        return self.a_tick

    def _state_path(self):
        return self.dirs['a'] / 'state.json'

    def _persist(self):
        """The signal-time checkpoint write — the stopped run's tick,
        or a lagging one under the staged defect."""
        tick = self.a_tick - 50 if self.state_lags else self.a_tick
        self._state_path().write_text(json.dumps({'tick': tick}))

    def close(self):
        pass

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _ = path.partition('?')
        if self.down:
            raise urllib.error.URLError('connection refused')
        if host == 'ctrl-a:1':
            if not self.a_running:
                raise urllib.error.URLError('connection refused')
            tick = self._advance()
            if (method, route) == ('GET', '/role'):
                return 200, {'role': self.a_role, 'tick': tick,
                             'sync': None}
            if (method, route) == ('GET', '/snapshot'):
                return 200, {'tick': tick, 'points': []}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if host == 'ctrl-b:2':
            if not self.b_running:
                raise urllib.error.URLError('connection refused')
            self.b_tick += 1
            if (method, route) == ('GET', '/role'):
                tracking = None if self.untracked \
                    or not self.b_tracking else \
                    {'tracking': {'aligned': self.a_tick}}
                return 200, {'role': self.b_role,
                             'tick': self.b_tick, 'sync': tracking}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        raise AssertionError('unexpected request %s %s' % (method, url))

    def signal(self, name, signal='SIGTERM'):
        """The ctx['signal_controller'] stub: the first-phase signal
        stops the field owner with the staged verdict and persists
        the state file; the impeded phase-three signal parks instead,
        and only the second forces the prompt exit."""
        self.signals.append((name, signal))
        if name != 'active':
            return
        if not self.stalled:
            if self.kill_hangs:
                return
            self.a_running = False
            self.a_exit = self.kill_exit
            self.a_logs = ('graceful shutdown on SIGTERM: scan loop '
                           'stopped at tick %d; checkpoint flushed; '
                           'field write claim released' % self.a_tick) \
                if self.kill_marker else 'terminated'
            if not self.state_unreadable:
                self._persist()
            return
        self.phase3 += 1
        if self.phase3 == 1:
            # The impeded first signal parks in the flush — unless a
            # defect exits it early.
            if self.stall_ignores:
                self.a_running = False
                self.a_exit = 0
                self.a_logs = ('graceful shutdown on SIGTERM: scan loop '
                               'stopped')
                return
            if self.phase3_kill:
                self.a_running = False
                self.a_exit = 143
                self.a_logs = 'terminated'
                return
            return
        if self.second_hangs:
            return
        self.a_running = False
        self.a_exit = self.second_exit

    def start(self, name):
        """The ctx['start_controller'] stub: relaunches the field
        owner onto its state file — the granted claim and the resumed
        tick — or stages the refusal/stall/rewind defects."""
        if self.start_raises:
            raise RuntimeError('docker start failed')
        if self.resume_stalls:
            return
        try:
            persisted = json.loads(
                self._state_path().read_text())['tick']
        except (OSError, ValueError, KeyError):
            persisted = 0
        self.a_running = True
        self.a_role = 'active'
        self.a_exit = None
        self.a_tick = persisted - 5 if self.resume_rewinds \
            else persisted
        if self.reconverge_fails:
            self.b_tracking = False

    def controller_state(self, name):
        """The ctx['controller_state'] probe's stub."""
        running = self.a_running if name == 'active' \
            else self.b_running
        return {'container': 'ctrl-' + name, 'running': running,
                'exit': None if running else self.a_exit,
                'logs': '' if running else self.a_logs,
                'absent': False}


class GracefulShutdownTests(unittest.TestCase):
    """scenario_graceful_shutdown against the stubbed pair: the
    signal/start/state actions are feed-driven while the impede and
    restore halves run the real runner lever against the bounded run
    dir — the staged FIFO genuinely parks until restore pairs it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            self.dirs[name] = directory
        self.feed = ShutdownFeed(
            {'a': self.dirs['a'], 'b': self.dirs['b']})

    def tearDown(self):
        self.feed.close()
        self.tmp.cleanup()

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        self.addCleanup(feed.close)
        evidence = evidence if evidence is not None else self.evidence
        events = []
        mounts = {'active': 'fifo', 'standby': 'fifo'}

        def impede(name):
            if feed.impede_raises:
                raise RuntimeError('mkfifo failed')
            runner.impede_state_file(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event),
                mounts)
            feed.stalled = True

        def restore(name):
            if feed.restore_raises:
                raise RuntimeError('reader attach failed')
            runner.restore_state_file(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event))
            feed.stalled = False

        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence),
                'signal_controller': feed.signal,
                'start_controller': feed.start,
                'controller_state': feed.controller_state,
                'impede_state_file': impede,
                'restore_state_file': restore,
                'state_files': {
                    'active': str(self.dirs['a'] / 'state.json'),
                    'standby': str(self.dirs['b'] / 'state.json')}}
        if ctx is not None:
            base.update(ctx)
        patches = {'GRACEFUL_POLL': 0.005, 'GRACEFUL_SETTLE': 5,
                   'GRACEFUL_EXIT_BOUND': 3, 'GRACEFUL_RETURN': 5,
                   'GRACEFUL_ADVANCE': 5, 'GRACEFUL_RECONVERGE': 5,
                   'GRACEFUL_STALL_OBSERVE': 0.5,
                   'GRACEFUL_PROMPT_BOUND': 3}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(runner, 'STATE_FILE_ATTACH_GRACE', 0.5), \
                patch.object(runner, 'STATE_FILE_BOUND', 3):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_graceful_shutdown(base)
        return record, events

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_graceful_shutdown,
                      scenarios.SCENARIOS)
        # The graceful-shutdown leg sits between the alarm cluster's
        # close and the schedule's closing observation case.
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_cause_alarm_quality) + 1,
            order.index(scenarios.scenario_graceful_shutdown))
        self.assertEqual(
            order.index(scenarios.scenario_graceful_shutdown) + 1,
            order.index(scenarios.scenario_dcs_ctl))

    def test_clean_rig_passes_and_validates(self):
        record, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # Both signals went out; the stall staged and released
        # through the real lever.
        self.assertEqual([name for name, _ in self.feed.signals],
                         ['active'] * 3)
        self.assertIn('state-file-impeded', events)
        self.assertIn('state-file-restored', events)
        # The flushed checkpoint survived the prompt exit's
        # unflushed stop: the final resume re-read it.
        persisted = json.loads(
            (self.dirs['a'] / 'state.json').read_text())['tick']
        self.assertGreaterEqual(persisted, 400)
        # The found posture is restored: the signaled key active,
        # the peer tracking.
        self.assertTrue(self.feed.a_running)
        self.assertEqual(self.feed.a_role, 'active')
        self.assertTrue(self.feed.b_tracking)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/graceful-shutdown-before.json',
            'evidence/graceful-shutdown-stop.json',
            'evidence/graceful-shutdown-resumed.json',
            'evidence/graceful-shutdown-second-signal.json'})
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_killed_exit_without_marker_is_predated(self):
        self.feed.kill_exit = 143
        self.feed.kill_marker = False
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_graceful_exit_without_marker_fails(self):
        self.feed.kill_marker = False
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('marker', record['detail'])

    def test_exit_timeout_fails(self):
        self.feed.kill_hangs = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('never exited', record['detail'])

    def test_stale_state_file_fails(self):
        self.feed.state_lags = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('state file', record['detail'])

    def test_unreadable_state_file_fails(self):
        self.feed.state_unreadable = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])

    def test_relaunch_refused_fails(self):
        self.feed.start_raises = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('relaunched', record['detail'])

    def test_resumed_tick_rewind_is_nondeterministic(self):
        self.feed.resume_rewinds = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-nondeterministic',
                      record['detail'])
        self.assertIn('rewound', record['detail'])

    def test_resumed_run_stalls_fails(self):
        self.feed.resume_stalls = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])

    def test_reconverge_fails(self):
        self.feed.reconverge_fails = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('reconverge', record['detail'])

    def test_first_signal_exits_despite_stall_fails(self):
        self.feed.stall_ignores = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('stalled', record['detail'])

    def test_killed_first_signal_is_predated(self):
        self.feed.phase3_kill = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_second_signal_hangs_fails(self):
        self.feed.second_hangs = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('prompt', record['detail'])

    def test_second_signal_wrong_exit_fails(self):
        self.feed.second_exit = 0
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('graceful-shutdown-failed', record['detail'])
        self.assertIn('143', record['detail'])

    def test_missing_signal_action_is_inconclusive(self):
        record, _ = self.run_scenario(
            ctx={'signal_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('signal', record['detail'])

    def test_missing_state_probe_is_inconclusive(self):
        record, _ = self.run_scenario(
            ctx={'controller_state': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_missing_state_path_is_inconclusive(self):
        record, _ = self.run_scenario(ctx={'state_files': {}})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('state-file', record['detail'])

    def test_missing_mount_lever_is_inconclusive(self):
        record, _ = self.run_scenario(
            ctx={'impede_state_file': None,
                 'restore_state_file': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('mount lever', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.down = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.untracked = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_failed_impede_is_inconclusive(self):
        self.feed.impede_raises = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('lever', record['detail'])

    def test_failed_restore_reports(self):
        self.feed.restore_raises = True
        record, _ = self.run_scenario()
        # The mount never restores but the container relaunches and
        # the pair reconverges — the lever failure reports
        # inconclusive rather than failing the graceful path.
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never restored', record['detail'])

    def test_scenario_ctx_carries_the_signal_action(self):
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG), record, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: None)
        self.assertTrue(callable(ctx['signal_controller']))
        self.assertTrue(callable(ctx['probe']['signal_controller']))

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two passes over the same
        # staged transitions record the same report and evidence
        # bytes — the feed is call-count keyed, never wall-clock.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = ShutdownFeed(
                {'a': self.dirs['a'], 'b': self.dirs['b']})
            record, _ = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
