import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from qa_lane import runner, state as qa_state


SHA_A = 'a' * 40
DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class Result:
    def __init__(self, stdout='', returncode=0):
        self.stdout = stdout
        self.stderr = ''
        self.returncode = returncode


def cfg_for(root):
    cfg = dict(runner.DEFAULT_CONFIG)
    cfg['state_dir'] = str(Path(root) / 'state')
    cfg['src_dir'] = str(Path(root) / 'state' / 'src')
    cfg['egress_required'] = False
    return cfg


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_dead_runner_marked_interrupted_and_orphans_removed(self):
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 999999, 2.0)
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('abc123 qa-1\ndef456 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('net9 qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_pid_alive', return_value=False):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        rec = self.st.run('qa-1')
        self.assertEqual(rec['status'], 'interrupted')
        self.assertEqual(rec['outcome'], 'interrupted')
        self.assertIn('abc123', str(calls))
        self.assertIn('net9', str(calls))
        self.assertTrue(any(a[:2] == ('network', 'rm') for a in calls))

    def test_live_runner_keeps_its_containers(self):
        self.st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        self.st.begin('qa-1', 4321, 2.0)
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('abc123 qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('net9 qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_pid_alive', return_value=True):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.run('qa-1')['status'], 'running')
        self.assertFalse(any('rm' in a for a in calls))

    def test_unlabeled_run_orphan_removed(self):
        # A container whose run label matches no record at all is reaped.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('dead0 qa-999\n')
            if args[:2] == ('network', 'ls'):
                return Result('')
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.reconcile(self.st, self.cfg, log=lambda m: None)
        self.assertTrue(any(a[0] == 'rm' and 'dead0' in a for a in calls))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_queue_cycle_is_quiet(self):
        logs = []
        with patch.object(runner, 'docker', lambda *a, **k: Result('')):
            runner.cycle(self.cfg, log=logs.append)
        self.assertTrue(any('nothing queued' in m for m in logs))

    def test_budget_blocks_new_runs(self):
        self.cfg['max_runs_per_day'] = 1
        st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        st.begin('qa-1', 1, DAY.timestamp())
        st.finish('qa-1', 'passed', SHA_A, '/r.json',
                  DAY.timestamp() + 60)
        st.enqueue('qa-2', 'b' * 40, DAY.timestamp() + 120,
                   '2026-09-15')
        st.close()
        logs = []
        with patch.object(runner, 'docker', lambda *a, **k: Result('')), \
                patch.object(runner, '_utcnow', return_value=DAY), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=logs.append)
        mock_run.assert_not_called()
        self.assertTrue(any('budget' in m for m in logs))

    def test_inconclusive_gets_one_retry(self):
        self.cfg['max_attempts_per_sha'] = 2
        st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
        st.begin('qa-1', 1, 1.0)
        st.finish('qa-1', 'inconclusive', None, '/r.json', 2.0)
        st.close()
        with patch.object(runner, 'docker', lambda *a, **k: Result('')), \
                patch.object(runner, '_utcnow', return_value=DAY), \
                patch.object(runner, 'run') as mock_run:
            runner.cycle(self.cfg, log=lambda m: None)
        mock_run.assert_called_once()
        record = mock_run.call_args[0][1]
        self.assertEqual(record['attempted_sha'], SHA_A)
        self.assertEqual(record['attempt'], 2)


class RestartActionTests(unittest.TestCase):
    """The scenario-callable controller restart: stop then start on the
    run's own container, recorded on the run's action timeline."""

    def test_stop_then_start_recorded_on_timeline(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.restart_controller(
                'qa-1', 'active',
                lambda event, detail=None: events.append(
                    (event, detail)))
        self.assertEqual(
            calls, [('stop', '--time', '2', 'dcs-hw-qa-1-a'),
                    ('start', 'dcs-hw-qa-1-a')])
        self.assertEqual([event for event, _ in events],
                         ['controller-restart', 'controller-restarted'])
        self.assertIn('dcs-hw-qa-1-a', events[0][1])

    def test_standby_endpoint_maps_to_b_container(self):
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a) or Result('')):
            runner.restart_controller('qa-1', 'standby',
                                      lambda e, d=None: None)
        self.assertEqual(calls[0][-1], 'dcs-hw-qa-1-b')
        self.assertEqual(calls[1], ('start', 'dcs-hw-qa-1-b'))

    def test_failed_stop_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'stop' and check:
                raise RuntimeError('docker stop failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.restart_controller(
                    'qa-1', 'active',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['controller-restart'])


class PlantActionTests(unittest.TestCase):
    """The scenario-callable plant stop/start: the run's shared-plant
    container cycled mid-run, each half recorded on the run's action
    timeline."""

    def test_stop_and_start_recorded_on_timeline(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            timeline = lambda event, detail=None: events.append(
                (event, detail))
            runner.stop_plant('qa-1', timeline)
            runner.start_plant('qa-1', timeline)
        self.assertEqual(
            calls, [('stop', '--time', '2', 'dcs-hw-qa-1-plant'),
                    ('start', 'dcs-hw-qa-1-plant')])
        self.assertEqual([event for event, _ in events],
                         ['plant-stop', 'plant-stopped',
                          'plant-start', 'plant-started'])
        self.assertIn('dcs-hw-qa-1-plant', events[0][1])

    def test_failed_stop_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'stop' and check:
                raise RuntimeError('docker stop failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.stop_plant(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['plant-stop'])

    def test_failed_start_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'start' and check:
                raise RuntimeError('docker start failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_plant(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['plant-start'])

    def test_scenario_ctx_carries_plant_actions_and_address(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        record = {'run_id': 'qa-1', 'attempted_sha': SHA_A}
        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                dict(runner.DEFAULT_CONFIG), record, Path('run'),
                'evidence', 0,
                lambda event, detail=None: events.append(event))
            ctx['stop_plant']()
            ctx['start_plant']()
        self.assertEqual(ctx['plant'], '127.0.0.1:19001')
        self.assertEqual(
            calls, [('stop', '--time', '2', 'dcs-hw-qa-1-plant'),
                    ('start', 'dcs-hw-qa-1-plant')])
        self.assertEqual(events, ['plant-stop', 'plant-stopped',
                                  'plant-start', 'plant-started'])


class RigStateFileTests(unittest.TestCase):
    """The rig's per-controller --state-file/--journal-file paths live
    inside the bounded run directory on runner-owned mounts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        for fixture in (self.cfg['model_fixture'],
                        self.cfg['dynamics_fixture']):
            path = self.src / fixture
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def test_controllers_get_state_journal_mounts_inside_run_dir(self):
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        class FakeConn:
            def close(self):
                pass

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner.socket, 'create_connection',
                             return_value=FakeConn()):
            runner._start_rig(self.cfg, self._record(), self.src,
                              self.run_dir, lambda e, d=None: None)
        for name, container in (('a', 'dcs-hw-qa-1-a'),
                                ('b', 'dcs-hw-qa-1-b')):
            directory = self.run_dir / 'controllers' / name
            self.assertTrue(directory.is_dir())
            launch = next(c for c in calls
                          if c[0] == 'run' and container in c)
            self.assertIn(str(directory) + ':'
                          + runner.CONTAINER_RUN_DIR, launch)
            self.assertIn('--state-file', launch)
            self.assertIn(runner.CONTAINER_STATE_FILE, launch)
            self.assertIn('--journal-file', launch)
            self.assertIn(runner.CONTAINER_JOURNAL_FILE, launch)

    def test_scenario_ctx_carries_restart_and_run_dir_paths(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        record = self._record()
        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda event, detail=None: events.append(event))
            ctx['restart_controller']('active')
        for path in ctx['state_files'].values():
            self.assertTrue(Path(path).is_relative_to(self.run_dir))
        for path in ctx['journal_files'].values():
            self.assertTrue(Path(path).is_relative_to(self.run_dir))
        self.assertEqual(calls[0][0], 'stop')
        self.assertEqual(calls[1], ('start', 'dcs-hw-qa-1-a'))
        self.assertIn('controller-restart', events)

    def test_controller_state_files_reaped_with_the_run_dir(self):
        # The state/journal mounts sit under runs/<id>/, so the
        # retention reconciler removes them with the run directory.
        directory = self.run_dir / 'controllers' / 'a'
        directory.mkdir(parents=True)
        (directory / 'state.json').write_text('{}')
        (directory / 'journal.jsonl').write_text('{}\n')
        self.cfg['runs_keep'] = 0
        st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')
        try:
            st.enqueue('qa-1', SHA_A, 1.0, '2026-09-15')
            st.begin('qa-1', 1, 2.0)
            st.finish('qa-1', 'passed', SHA_A,
                      str(self.run_dir / 'report.json'), 3.0)
            with patch.object(runner, 'docker',
                              lambda *a, **k: Result('')):
                runner.reclaim(st, self.cfg, log=lambda m: None)
        finally:
            st.close()
        self.assertFalse(self.run_dir.exists())


if __name__ == '__main__':
    unittest.main()
