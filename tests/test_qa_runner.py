import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from qa_lane import revision, runner, state as qa_state


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


class LifecycleActionTests(unittest.TestCase):
    """The scenario-callable stop/start pair: each action records its
    own attempt and completion on the run's action timeline, so a
    scenario can hold a controller down across an observation window
    instead of taking the whole restart as one step."""

    def test_stop_and_start_record_their_own_events(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        timeline = lambda event, detail=None: events.append(event)
        with patch.object(runner, 'docker', fake_docker):
            runner.stop_controller('qa-1', 'active', timeline)
            runner.start_controller('qa-1', 'active', timeline)
        self.assertEqual(calls,
                         [('stop', '--time', '2', 'dcs-hw-qa-1-a'),
                          ('start', 'dcs-hw-qa-1-a')])
        self.assertEqual(events, ['controller-stop', 'controller-stopped',
                                  'controller-start',
                                  'controller-started'])

    def test_failed_start_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'start' and check:
                raise RuntimeError('docker start failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_controller(
                    'qa-1', 'active',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['controller-start'])


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
                dict(runner.DEFAULT_CONFIG), record, Path('src'),
                Path('run'), 'evidence', 0,
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

    def test_standby_launch_arms_the_failover_budget(self):
        # The declared freshness budget presents inside the writer-loss
        # window only because the armed --auto-promote bound keeps the
        # freeze finite — the standby carries it, the active does not.
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
        standby = next(c for c in calls
                       if c[0] == 'run' and 'dcs-hw-qa-1-b' in c)
        index = standby.index('--auto-promote')
        self.assertEqual(standby[index + 1],
                         str(self.cfg['failover_misses']))
        active = next(c for c in calls
                      if c[0] == 'run' and 'dcs-hw-qa-1-a' in c)
        self.assertNotIn('--auto-promote', active)

    def test_scenario_ctx_carries_restart_and_run_dir_paths(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        record = self._record()
        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda event, detail=None: events.append(event))
            ctx['restart_controller']('active')
            ctx['stop_controller']('standby')
            ctx['start_controller']('standby')
        for path in ctx['state_files'].values():
            self.assertTrue(Path(path).is_relative_to(self.run_dir))
        for path in ctx['journal_files'].values():
            self.assertTrue(Path(path).is_relative_to(self.run_dir))
        self.assertEqual(calls[0][0], 'stop')
        self.assertEqual(calls[1], ('start', 'dcs-hw-qa-1-a'))
        self.assertEqual(calls[2][-1], 'dcs-hw-qa-1-b')
        self.assertEqual(calls[3], ('start', 'dcs-hw-qa-1-b'))
        self.assertEqual(ctx['failover_misses'],
                         self.cfg['failover_misses'])
        self.assertIn('controller-restart', events)
        self.assertIn('controller-stopped', events)
        self.assertIn('controller-started', events)

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


class DcsCtlBuildTests(unittest.TestCase):
    """The dcs-ctl host-binary seam: the bounded image build compiles
    the operator CLI beside the image binaries, _scenario_ctx hands its
    path to the dcs-ctl case, and a build that produces no binary fails
    loudly rather than leaving the case to run against a phantom
    tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_docker(self, calls, binaries=('dcs-controller',
                                            'dcs-plant-server',
                                            'dcs-ctl')):
        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'run' and 'cargo' in str(args):
                target = Path(self.cfg['state_dir']) / 'build-cache' \
                    / 'target' / 'release'
                target.mkdir(parents=True, exist_ok=True)
                for binary in binaries:
                    (target / binary).write_text('bin')
            if args[:2] == ('image', 'inspect'):
                return Result('sha256:' + 'a' * 64)
            return Result('')
        return fake_docker

    def test_build_compiles_dcs_ctl_beside_the_images(self):
        calls, events = [], []
        with patch.object(runner, 'docker', self._fake_docker(calls)):
            digests = runner._build_images(
                self.src, self.cfg, self.run_dir,
                lambda event, detail=None: events.append(event), 'qa-1')
        build = next(args for args in calls
                     if args[0] == 'run' and 'cargo' in str(args))
        self.assertIn('-p dcs-monitor --bin dcs-ctl', build[-1])
        self.assertEqual(set(digests), {'controller', 'plant'})
        self.assertIn('tool-built', events)

    def test_build_fails_loudly_without_the_binary(self):
        with patch.object(runner, 'docker',
                          self._fake_docker(
                              [], binaries=('dcs-controller',
                                            'dcs-plant-server'))):
            with self.assertRaises(RuntimeError):
                runner._build_images(self.src, self.cfg, self.run_dir,
                                     lambda e, d=None: None, 'qa-1')

    def test_scenario_ctx_hands_the_binary_to_the_case(self):
        ctx = runner._scenario_ctx(self.cfg, {'run_id': 'qa-1'},
                                   self.src, self.run_dir,
                                   self.run_dir / 'evidence', 0,
                                   lambda e, d=None: None)
        self.assertEqual(ctx['dcs_ctl'],
                         str(Path(self.cfg['state_dir']) / 'build-cache'
                             / 'target' / 'release' / 'dcs-ctl'))


class ModelRevisionActionTests(unittest.TestCase):
    """The scenario-callable model-revision action: the runner derives
    the run's revised model document through the checked-in recipe and
    launches the run's third labeled controller on it with
    --standby --revised, both halves recorded on the run's action
    timeline and the container reconciled by the run-label teardown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.model = self.src / self.cfg['model_fixture']
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps(
            {'version': 1,
             'devices': [{'id': 1, 'kind': 'sim-di',
                          'channels': [{'name': 'ch0',
                                        'direction': 'in',
                                        'value_type': 'bool'}]}],
             'io_points': [
                 {'id': 10, 'direction': 'in', 'value_type': 'bool',
                  'channel': {'device': 1, 'channel': 'ch0'}},
                 {'id': 300, 'direction': 'in', 'value_type': 'bool',
                  'writable': True, 'initial': {'bool': False}}],
             'signals': [{'id': 10300, 'name': 'oos', 'source': 300}],
             'components': [], 'connections': []}))

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def test_third_controller_launches_standby_revised(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            info = runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby',
                lambda event, detail=None: events.append(
                    (event, detail)))
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-c', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertIn('--standby', launch)
        self.assertIn('dcs-hw-qa-1-b:8081', launch)
        self.assertIn('--revised', launch)
        self.assertIn('--listen', launch)
        self.assertIn('0.0.0.0:8082', launch)
        self.assertIn('127.0.0.1:' + str(self.cfg['revised_port'])
                      + ':8082', launch)
        self.assertIn(runner.CONTAINER_STATE_FILE, launch)
        self.assertIn(runner.CONTAINER_JOURNAL_FILE, launch)
        document = str(self.run_dir / 'model-revised.json')
        self.assertIn(document + ':/model/revised.json:ro', launch)
        self.assertIn('/model/revised.json', launch)
        self.assertIn(str(self.run_dir / 'controllers' / 'c')
                      + ':' + runner.CONTAINER_RUN_DIR, launch)
        self.assertEqual(info['container'], 'dcs-hw-qa-1-c')
        self.assertEqual(info['document'], document)
        self.assertEqual(info['added_points'], [900])
        self.assertEqual(info['added_signals'], [10900])
        revised = json.loads(Path(document).read_text())
        self.assertEqual(len(revised['io_points']), 3)
        self.assertEqual(len(revised['signals']), 2)
        self.assertEqual(revision.lint(revised), [])
        self.assertEqual([event for event, _ in events],
                         ['model-revision-start', 'model-revision-up'])
        self.assertIn('--revised', events[0][1])

    def test_active_endpoint_standbys_on_ctrl_a(self):
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'active', lambda e, d=None: None)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-a:8080', launch)

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'revised', lambda e, d=None: None)

    def test_failed_launch_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'run' and check:
                raise RuntimeError('docker run failed: name in use')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['model-revision-start'])
        # The derivation still ran — the document is on disk.
        self.assertTrue(
            (self.run_dir / 'model-revised.json').is_file())

    def test_teardown_reconciles_the_third_container(self):
        # The launched -c container carries the run label like the rest
        # of the rig, so the shared teardown removes it with them.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('aaa qa-1\nbbb qa-1\nccc qa-1\n'
                              'ppp qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('nnn qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            failures = runner._teardown_rig(
                'qa-1', lambda e, d=None: None)
        self.assertEqual(failures, [])
        removed = [c for c in calls if c[0] == 'rm']
        self.assertEqual(len(removed), 4)
        self.assertTrue(any('ccc' in c for c in removed))
        self.assertTrue(
            any(c[:2] == ('network', 'rm') for c in calls))

    def test_scenario_ctx_carries_revision_action_and_endpoint(self):
        calls = []
        record = self._record()
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            info = ctx['start_revised']('standby')
        self.assertEqual(ctx['revised'],
                         'http://127.0.0.1:' + str(
                             self.cfg['revised_port']))
        self.assertEqual(ctx['plant'],
                         '127.0.0.1:' + str(self.cfg['plant_host_port']))
        self.assertEqual(info['container'], 'dcs-hw-qa-1-c')
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('--revised', launch)
        # The third peer's state/journal paths sit inside the run dir.
        self.assertTrue(Path(ctx['journal_files']['revised'])
                        .is_relative_to(self.run_dir))
        self.assertTrue(Path(ctx['state_files']['revised'])
                        .is_relative_to(self.run_dir))


class NegotiationActionTests(unittest.TestCase):
    """The scenario-callable checkpoint-negotiation actions: the runner
    derives the same recipe-revised document and launches the run's
    labeled foreign peer on it with --standby but WITHOUT --revised —
    the negotiation-refusal leg — and removes the container again for
    the case's teardown, both halves recorded on the run's action
    timeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.model = self.src / self.cfg['model_fixture']
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps(
            {'version': 1,
             'devices': [{'id': 1, 'kind': 'sim-di',
                          'channels': [{'name': 'ch0',
                                        'direction': 'in',
                                        'value_type': 'bool'}]}],
             'io_points': [
                 {'id': 10, 'direction': 'in', 'value_type': 'bool',
                  'channel': {'device': 1, 'channel': 'ch0'}},
                 {'id': 300, 'direction': 'in', 'value_type': 'bool',
                  'writable': True, 'initial': {'bool': False}}],
             'signals': [{'id': 10300, 'name': 'oos', 'source': 300}],
             'components': [], 'connections': []}))

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def test_foreign_peer_launches_standby_without_revised(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            info = runner.start_foreign_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby',
                lambda event, detail=None: events.append(
                    (event, detail)))
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-foreign', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertIn('--standby', launch)
        self.assertIn('dcs-hw-qa-1-b:8081', launch)
        # The negotiation-refusal leg: no --revised opt-in, so the
        # foreign fingerprint must degrade the peer.
        self.assertNotIn('--revised', launch)
        self.assertIn('127.0.0.1:' + str(self.cfg['foreign_port'])
                      + ':8082', launch)
        self.assertIn(runner.CONTAINER_STATE_FILE, launch)
        self.assertIn(runner.CONTAINER_JOURNAL_FILE, launch)
        document = str(self.run_dir / 'model-foreign.json')
        self.assertIn(document + ':/model/foreign.json:ro', launch)
        self.assertIn('/model/foreign.json', launch)
        self.assertIn(str(self.run_dir / 'controllers' / 'foreign')
                      + ':' + runner.CONTAINER_RUN_DIR, launch)
        self.assertEqual(info['container'], 'dcs-hw-qa-1-foreign')
        self.assertEqual(info['document'], document)
        self.assertEqual(info['added_points'], [900])
        self.assertEqual(info['added_signals'], [10900])
        foreign = json.loads(Path(document).read_text())
        self.assertEqual(len(foreign['io_points']), 3)
        self.assertEqual(revision.lint(foreign), [])
        self.assertEqual([event for event, _ in events],
                         ['negotiation-start', 'negotiation-up'])

    def test_active_endpoint_standbys_on_ctrl_a(self):
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            runner.start_foreign_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'active', lambda e, d=None: None)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-a:8080', launch)

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.start_foreign_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'foreign', lambda e, d=None: None)

    def test_failed_launch_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'run' and check:
                raise RuntimeError('docker run failed: name in use')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_foreign_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['negotiation-start'])
        # The derivation still ran — the document is on disk.
        self.assertTrue(
            (self.run_dir / 'model-foreign.json').is_file())

    def test_stop_removes_the_foreign_container(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.stop_foreign_controller(
                'qa-1', lambda event, detail=None: events.append(
                    (event, detail)))
        self.assertEqual(calls,
                         [('rm', '-f', 'dcs-hw-qa-1-foreign')])
        self.assertEqual([event for event, _ in events],
                         ['negotiation-stop', 'negotiation-stopped'])

    def test_failed_teardown_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'rm' and check:
                raise RuntimeError('docker rm failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.stop_foreign_controller(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['negotiation-stop'])

    def test_scenario_ctx_carries_negotiation_actions_and_endpoint(self):
        calls = []
        record = self._record()
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            info = ctx['start_foreign']('standby')
            ctx['stop_foreign']()
        self.assertEqual(ctx['foreign'],
                         'http://127.0.0.1:' + str(
                             self.cfg['foreign_port']))
        self.assertEqual(info['container'], 'dcs-hw-qa-1-foreign')
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('--standby', launch)
        self.assertNotIn('--revised', launch)
        self.assertEqual(calls[-1],
                         ('rm', '-f', 'dcs-hw-qa-1-foreign'))
        # The foreign peer's state/journal paths sit inside the run dir.
        self.assertTrue(Path(ctx['journal_files']['foreign'])
                        .is_relative_to(self.run_dir))
        self.assertTrue(Path(ctx['state_files']['foreign'])
                        .is_relative_to(self.run_dir))


if __name__ == '__main__':
    unittest.main()
