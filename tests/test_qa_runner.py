import json
import os
import select
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from qa_lane import revision, runner, scenarios, state as qa_state


SHA_A = 'a' * 40
DAY = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

# The shipped sim-bus device server the lane's bounded build compiles
# and the controller image carries (#1368). The lane's own image build
# produces it inside the builder container, so a checkout that has not
# compiled the workspace has no binary to serve the staged fixture
# with; the check that needs it declines there rather than standing up a
# double in its place.
SIM_BUS_DEVICE_BIN = os.environ.get('DCS_SIM_BUS_DEVICE') or next(
    (str(path) for path in (
        Path(__file__).resolve().parents[1] / 'target' / profile
        / 'dcs-sim-bus-device'
        for profile in ('release', 'debug'))
     if path.is_file()), None)

# The register protocol's wire vocabulary as the staged-fixture check
# speaks it: a u16 big-endian length prefix plus one byte-tagged
# request payload, answered by one byte-tagged response payload
# (crates/dcs-sim-bus/src/lib.rs records the whole contract — this is
# the read/write/claim/exchange slice the device server serves).
BUS_READ, BUS_WRITE, BUS_LIST, BUS_CLAIM, BUS_EXCHANGE = (
    0x01, 0x02, 0x03, 0x05, 0x09)
BUS_SAMPLE, BUS_WRITTEN, BUS_REGISTERS, BUS_DONE, BUS_EXCHANGED = (
    0x01, 0x02, 0x03, 0x06, 0x07)
BUS_BOOL, BUS_FLOAT = 0x01, 0x03


def _bus_send(stream, payload):
    """One request frame out, one response frame back, decoded."""
    stream.sendall(struct.pack('>H', len(payload)) + payload)
    header = stream.recv(2)
    if len(header) < 2:
        raise AssertionError('the device server closed the connection')
    length = struct.unpack('>H', header)[0]
    body = b''
    while len(body) < length:
        chunk = stream.recv(length - len(body))
        if not chunk:
            raise AssertionError('truncated response frame')
        body += chunk
    return body


def _bus_value(kind, value):
    if kind == BUS_BOOL:
        return struct.pack('>B', 1 if value else 0)
    return struct.pack('>d', float(value))


def _bus_write(register, kind, value):
    return (bytes([BUS_WRITE]) + struct.pack('>H', register)
            + struct.pack('>B', kind) + _bus_value(kind, value))


def _bus_read(register):
    return bytes([BUS_READ]) + struct.pack('>H', register)


def _bus_exchange(outputs):
    payload = bytes([BUS_EXCHANGE]) + struct.pack('>H', len(outputs))
    for register, kind, value in outputs:
        payload += struct.pack('>H', register) + struct.pack('>B', kind) \
            + _bus_value(kind, value)
    return payload


def _bus_sample(body):
    """(kind, value, tick) of one sample starting at body[0]."""
    kind = body[0]
    size = 1 if kind == BUS_BOOL else 8
    raw = body[1:1 + size]
    value = bool(raw[0]) if kind == BUS_BOOL \
        else struct.unpack('>d', raw)[0]
    return kind, value, struct.unpack('>Q', body[1 + size:9 + size])[0]


def _bus_census(body, offset):
    """The register index → value map of a `registers`/`exchanged`
    census starting at `offset` (the register count)."""
    count = struct.unpack('>H', body[offset:offset + 2])[0]
    offset += 2
    census = {}
    for _ in range(count):
        register = struct.unpack('>H', body[offset:offset + 2])[0]
        kind, value, _ = _bus_sample(body[offset + 2:])
        census[register] = value
        # register + kind + value + tick + the Good quality severity
        offset += 2 + 1 + (1 if kind == BUS_BOOL else 8) + 8 + 1
    return census


# The bus model the sim-bus staging seam is checked against: a
# complete, servable model document declaring one `sim-bus` device
# whose address carries the placeholder the lane binds — five channels
# on registers 0..4, two of them outputs, so the staged bank serves
# reads, writes, and an exchange the shipped server publishes.
BUS_MODEL = {
    'version': 1,
    'devices': [{
        'id': 1,
        'kind': 'sim-bus',
        'parameters': {'address': '__BUS_ADDR__',
                       'registers': {'level_raw': 0, 'pump_run': 1,
                                     'valve_fb': 2, 'valve_cmd': 3,
                                     'pump_cmd': 4}},
        'channels': {'level_raw': {'direction': 'in',
                                   'value_type': 'float'},
                     'pump_run': {'direction': 'in',
                                  'value_type': 'bool'},
                     'valve_fb': {'direction': 'in',
                                  'value_type': 'float'},
                     'valve_cmd': {'direction': 'out',
                                   'value_type': 'float'},
                     'pump_cmd': {'direction': 'out',
                                  'value_type': 'bool'}},
    }],
    'io_points': [
        {'id': 10, 'direction': 'in', 'value_type': 'float',
         'channel': {'device': 1, 'name': 'level_raw'}},
        {'id': 11, 'direction': 'in', 'value_type': 'bool',
         'channel': {'device': 1, 'name': 'pump_run'}},
        {'id': 12, 'direction': 'in', 'value_type': 'float',
         'channel': {'device': 1, 'name': 'valve_fb'}},
        {'id': 20, 'direction': 'out', 'value_type': 'float',
         'channel': {'device': 1, 'name': 'valve_cmd'}},
        {'id': 21, 'direction': 'out', 'value_type': 'bool',
         'channel': {'device': 1, 'name': 'pump_cmd'}},
    ],
    'signals': [],
    'components': [],
    'connections': [],
}

# The cyclic counterpart the fencing-loss leg stages through the
# launch's fixture override: one `sim-cyclic` device declaring its
# register image as a station map — the `stations` partition the kind
# requires — on the same placeholder address.
CYCLIC_BUS_MODEL = {
    'version': 1,
    'devices': [{
        'id': 1,
        'kind': 'sim-cyclic',
        'parameters': {'address': '__BUS_ADDR__',
                       'exchange_miss_threshold': 3,
                       'stations': {'field': {'di1': 0, 'do1': 1}}},
        'channels': {'di1': {'direction': 'in', 'value_type': 'bool'},
                     'do1': {'direction': 'out', 'value_type': 'bool'}},
    }],
    'io_points': [
        {'id': 10, 'direction': 'in', 'value_type': 'bool',
         'channel': {'device': 1, 'name': 'di1'}},
        {'id': 20, 'direction': 'out', 'value_type': 'bool',
         'channel': {'device': 1, 'name': 'do1'}},
    ],
    'signals': [],
    'components': [],
    'connections': [],
}


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


@unittest.skipUnless(os.name == 'posix',
                     'runner.cycle serializes through a POSIX flock')
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


class ColdRestartActionTests(unittest.TestCase):
    """The scenario-callable cold restart: stop, drop the named
    controller's host-side state.json inside the bounded run dir —
    the journal file stays, its run-boundary marker part of the
    evidence — then start, both docker halves on the timeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.a_dir = self.run_dir / 'controllers' / 'a'
        self.b_dir = self.run_dir / 'controllers' / 'b'
        for directory in (self.a_dir, self.b_dir):
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{"tick": 400}')
            (directory / 'journal.jsonl').write_text(
                '{"run_boundary": {"run": 1, "tick": 0}}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def _cold_restart(self, name='active', docker=None, events=None):
        calls = []
        if docker is None:
            def docker(*args, timeout=120, check=True):
                calls.append(args)
                return Result('')
        seen = events if events is not None else []
        with patch.object(runner, 'docker', docker):
            runner.cold_restart_controller(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: seen.append((event, detail)))
        return calls, seen

    def test_stop_remove_start_recorded_on_timeline(self):
        calls, events = self._cold_restart()
        self.assertEqual(
            calls, [('stop', '--time', '2', 'dcs-hw-qa-1-a'),
                    ('start', 'dcs-hw-qa-1-a')])
        self.assertEqual([event for event, _ in events],
                         ['controller-cold-restart',
                          'controller-cold-restarted'])
        self.assertIn('dcs-hw-qa-1-a', events[0][1])
        self.assertFalse((self.a_dir / 'state.json').exists())
        self.assertTrue((self.a_dir / 'journal.jsonl').exists())

    def test_standby_endpoint_drops_b_state_only(self):
        calls, _ = self._cold_restart(name='standby')
        self.assertEqual(calls[0][-1], 'dcs-hw-qa-1-b')
        self.assertEqual(calls[1], ('start', 'dcs-hw-qa-1-b'))
        self.assertFalse((self.b_dir / 'state.json').exists())
        self.assertTrue((self.b_dir / 'journal.jsonl').exists())
        self.assertTrue((self.a_dir / 'state.json').exists())

    def test_state_removal_stays_inside_the_run_dir(self):
        # The removed path resolves under the bounded run dir — the
        # peer's sibling state and journal survive, and nothing outside
        # the run dir is touched.
        before_b = (self.b_dir / 'state.json').read_text()
        self._cold_restart()
        self.assertFalse((self.a_dir / 'state.json').exists())
        self.assertEqual((self.b_dir / 'state.json').read_text(),
                         before_b)
        touched = sorted(p for p in self.run_dir.rglob('*'))
        self.assertNotIn(self.a_dir / 'state.json', touched)

    def test_missing_state_file_still_restarts(self):
        (self.a_dir / 'state.json').unlink()
        calls, events = self._cold_restart()
        self.assertEqual(calls[0][0], 'stop')
        self.assertEqual(calls[1], ('start', 'dcs-hw-qa-1-a'))
        self.assertIn('already absent', events[1][1])

    def test_failed_stop_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'stop' and check:
                raise RuntimeError('docker stop failed: no such')
            return Result('')

        with self.assertRaises(RuntimeError):
            self._cold_restart(docker=raising, events=events)
        self.assertEqual([event for event, _ in events],
                         ['controller-cold-restart'])
        self.assertTrue((self.a_dir / 'state.json').exists())

    def test_scenario_ctx_carries_the_cold_restart_action(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        record = {'run_id': 'qa-1', 'attempted_sha': SHA_A}
        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                dict(runner.DEFAULT_CONFIG), record, Path('src'),
                self.run_dir, 'evidence', 0,
                lambda event, detail=None: events.append(event))
            ctx['cold_restart_controller']('standby')
        self.assertEqual(calls, [('stop', '--time', '2',
                                  'dcs-hw-qa-1-b'),
                                 ('start', 'dcs-hw-qa-1-b')])
        self.assertFalse((self.b_dir / 'state.json').exists())
        self.assertTrue((self.b_dir / 'journal.jsonl').exists())
        self.assertIn('controller-cold-restart', events)
        self.assertIn('controller-cold-restarted', events)


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


class RelaunchActionTests(unittest.TestCase):
    """The scenario-callable flag-doctoring relaunch: `docker rm -f`
    on the pair member's container, then a fresh `docker run`
    rebuilding the launch from the run config — same mounts, labels,
    published port, owner-token pin, failover budget, and pair token —
    with the tracking-source argument optionally doctored: `--peer`
    on the launched active (the --standby name an owning run
    carries), `--standby`'s target on the launched standby."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        for name in ('a', 'b', 'probe-a', 'probe-b'):
            (self.run_dir / 'controllers' / name).mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.model = self.src / self.cfg['model_fixture']
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text('{}')
        self.record = {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def tearDown(self):
        self.tmp.cleanup()

    def _relaunch(self, name='active', track=None, pair='deployed',
                  docker=None, events=None):
        calls = []
        if docker is None:
            def docker(*args, timeout=120, check=True):
                calls.append(args)
                return Result('')
        seen = events if events is not None else []
        with patch.object(runner, 'docker', docker):
            runner.relaunch_controller(
                self.cfg, self.record, self.run_dir, self.model, name,
                lambda event, detail=None: seen.append((event, detail)),
                track, pair)
        return calls, seen

    def _launch(self, calls, container):
        return next(c for c in calls
                    if c[0] == 'run' and container in c)

    def test_rm_then_run_rebuilds_the_active_command(self):
        calls, events = self._relaunch()
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-a'))
        launch = self._launch(calls, 'dcs-hw-qa-1-a')
        self.assertEqual([event for event, _ in events],
                         ['controller-relaunch',
                          'controller-relaunched'])
        self.assertIn('docker rm -f dcs-hw-qa-1-a', events[0][1])
        # The launch command rebuilt from the run config — the same
        # command _start_rig launched ctrl-a with, flag-for-flag.
        index = launch.index('/model/plant.json')
        self.assertEqual(
            launch[index:],
            ('/model/plant.json',
             '--remote', 'dcs-hw-qa-1-plant:9001',
             '--owner-token',
             str(self.cfg['plant_owner_tokens']['active']),
             '--scan-ms', '100', '--listen', '0.0.0.0:8080',
             '--state-file', runner.CONTAINER_STATE_FILE,
             '--journal-file', runner.CONTAINER_JOURNAL_FILE,
             '--history-file', runner.CONTAINER_HISTORY_FILE,
             '--pair-token', self.cfg['pair_token']))
        self.assertNotIn('--peer', launch)
        self.assertNotIn('--standby', launch)
        self.assertNotIn('--auto-promote', launch)

    def test_active_relaunch_keeps_mounts_port_labels_and_network(self):
        calls, _ = self._relaunch()
        launch = self._launch(calls, 'dcs-hw-qa-1-a')
        self.assertIn('--network', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertIn('127.0.0.1:18080:8080', launch)
        self.assertIn(str(self.model) + ':/model/plant.json:ro',
                      launch)
        directory = self.run_dir / 'controllers' / 'a'
        self.assertIn(str(directory) + ':'
                      + runner.CONTAINER_RUN_DIR, launch)
        self.assertIn('--restart', launch)
        self.assertIn('no', launch)
        labels = [launch[i + 1]
                  for i, arg in enumerate(launch) if arg == '--label']
        self.assertIn(runner.MANAGED_LABEL + '=1', labels)
        self.assertIn(runner.RUN_LABEL + '=qa-1', labels)
        self.assertIn('dcs-hwtest/controller:' + SHA_A, launch)

    def test_doctored_track_becomes_peer_on_the_active(self):
        calls, events = self._relaunch(
            track='dcs-peer-down.invalid:8080')
        launch = self._launch(calls, 'dcs-hw-qa-1-a')
        index = launch.index('--peer')
        self.assertEqual(launch[index + 1],
                         'dcs-peer-down.invalid:8080')
        self.assertNotIn('--standby', launch)
        self.assertNotIn('--auto-promote', launch)
        self.assertIn('--peer dcs-peer-down.invalid:8080',
                      events[0][1])

    def test_standby_relaunch_replaces_its_standby_target(self):
        calls, _ = self._relaunch(name='standby',
                                  track='dcs-peer-down.invalid:8080')
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-b'))
        launch = self._launch(calls, 'dcs-hw-qa-1-b')
        index = launch.index('--standby')
        self.assertEqual(launch[index + 1],
                         'dcs-peer-down.invalid:8080')
        index = launch.index('--auto-promote')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['failover_misses']))
        self.assertNotIn('--peer', launch)
        self.assertIn('127.0.0.1:18081:8081', launch)
        self.assertIn(str(self.cfg['plant_owner_tokens']['standby']),
                      launch)

    def test_standby_relaunch_restores_its_launch_target(self):
        calls, _ = self._relaunch(name='standby')
        launch = self._launch(calls, 'dcs-hw-qa-1-b')
        index = launch.index('--standby')
        self.assertEqual(launch[index + 1], 'dcs-hw-qa-1-a:8080')

    def test_absent_remove_is_tolerated_and_noted(self):
        recorded = []

        def docker(*args, timeout=120, check=True):
            recorded.append(args)
            if args[0] == 'rm':
                return Result('', returncode=1)
            return Result('')

        calls, events = self._relaunch(docker=docker)
        calls = recorded
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-a'))
        self.assertTrue(any(c[0] == 'run' for c in calls))
        self.assertIn('already absent', events[1][1])

    def test_failed_run_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'run' and check:
                raise RuntimeError('docker run failed: exit 125')
            return Result('')

        with self.assertRaises(RuntimeError):
            self._relaunch(docker=raising, events=events)
        self.assertEqual([event for event, _ in events],
                         ['controller-relaunch'])

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(RuntimeError):
            self._relaunch(name='driven')

    def test_probe_pair_maps_probe_containers_and_tokens(self):
        probe = self.cfg['probe_pair']
        calls, _ = self._relaunch(name='active', pair='probe',
                                  track='dcs-peer-down.invalid:8080')
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-probe-a'))
        launch = self._launch(calls, 'dcs-hw-qa-1-probe-a')
        index = launch.index('--peer')
        self.assertEqual(launch[index + 1],
                         'dcs-peer-down.invalid:8080')
        index = launch.index('--remote')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-probe-plant:'
                         + str(probe['plant_port']))
        index = launch.index('--owner-token')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['plant_owner_tokens']
                            ['probe_active']))
        index = launch.index('--pair-token')
        self.assertEqual(launch[index + 1], str(probe['pair_token']))
        self.assertIn('127.0.0.1:' + str(probe['active_port'])
                      + ':8080', launch)
        directory = self.run_dir / 'controllers' / 'probe-a'
        self.assertIn(str(directory) + ':'
                      + runner.CONTAINER_RUN_DIR, launch)

    def test_probe_standby_relaunch_restores_probe_target(self):
        calls, _ = self._relaunch(name='standby', pair='probe')
        launch = self._launch(calls, 'dcs-hw-qa-1-probe-b')
        index = launch.index('--standby')
        self.assertEqual(launch[index + 1], 'dcs-hw-qa-1-probe-a:8080')
        self.assertIn('127.0.0.1:'
                      + str(self.cfg['probe_pair']['standby_port'])
                      + ':8081', launch)

    def test_scenario_ctx_carries_the_relaunch_action(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                self.cfg, self.record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda event, detail=None: events.append(event))
            ctx['relaunch_controller']('standby',
                                       'dcs-peer-down.invalid:8080')
            ctx['relaunch_controller']('standby')
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-b'))
        doctored = self._launch(calls, 'dcs-hw-qa-1-b')
        index = doctored.index('--standby')
        self.assertEqual(doctored[index + 1],
                         'dcs-peer-down.invalid:8080')
        restored = [c for c in calls if c[0] == 'run'
                    and 'dcs-hw-qa-1-b' in c][-1]
        index = restored.index('--standby')
        self.assertEqual(restored[index + 1], 'dcs-hw-qa-1-a:8080')
        self.assertIn('controller-relaunch', events)
        self.assertIn('controller-relaunched', events)

    def test_probe_ctx_carries_the_relaunch_action(self):
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            ctx = runner._scenario_ctx(
                self.cfg, self.record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            ctx['probe']['relaunch_controller'](
                'active', 'dcs-peer-down.invalid:8080')
        self.assertEqual(calls[0], ('rm', '-f', 'dcs-hw-qa-1-probe-a'))
        launch = self._launch(calls, 'dcs-hw-qa-1-probe-a')
        self.assertIn('--peer', launch)

    def test_relaunch_rebuilds_the_members_launch_spec(self):
        # track=None is constructional: the relaunched argv is the
        # shared builder's launch spec for the member, byte for byte —
        # the guarantee that a flag added to the launch spec cannot
        # silently drop from the restore half.
        prefix = 'dcs-hw-' + self.record['run_id']
        for name, container in (('active', prefix + '-a'),
                                ('standby', prefix + '-b')):
            calls, _ = self._relaunch(name=name)
            launch = self._launch(calls, container)
            index = launch.index('/model/plant.json')
            self.assertEqual(
                list(launch[index:]),
                runner._controller_argv(self.cfg, 'deployed', name,
                                        prefix), name)

    def test_doctored_relaunch_is_the_spec_plus_its_delta(self):
        # track=X relaunches the same spec modulo only the doctored
        # flag: --peer <track> on the active, the --standby target on
        # the standby.
        prefix = 'dcs-hw-' + self.record['run_id']
        track = 'dcs-peer-down.invalid:8080'
        spec = runner._controller_argv(self.cfg, 'deployed',
                                       'active', prefix)
        calls, _ = self._relaunch(name='active', track=track)
        launch = self._launch(calls, prefix + '-a')
        index = launch.index('/model/plant.json')
        at = spec.index('--scan-ms')
        self.assertEqual(list(launch[index:]),
                         spec[:at] + ['--peer', track] + spec[at:])
        spec = runner._controller_argv(self.cfg, 'deployed',
                                       'standby', prefix)
        calls, _ = self._relaunch(name='standby', track=track)
        launch = self._launch(calls, prefix + '-b')
        index = launch.index('/model/plant.json')
        expected = list(spec)
        expected[spec.index('--standby') + 1] = track
        self.assertEqual(list(launch[index:]), expected)

    def test_spec_change_flows_to_launch_and_relaunch_alike(self):
        # The structural regression this consolidation removes: a flag
        # added to the launch spec reaches both the rig's initial
        # launch and the relaunch's restore with no second edit.
        real = runner._controller_argv

        def extended(*args, **kwargs):
            return real(*args, **kwargs) + ['--new-persistence-flag']

        self.cfg['probe_pair'] = None
        dynamics = self.src / self.cfg['dynamics_fixture']
        dynamics.parent.mkdir(parents=True, exist_ok=True)
        dynamics.write_text('{}')
        calls = []

        class FakeConn:
            def close(self):
                pass

        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')), \
                patch.object(runner, '_controller_argv', extended), \
                patch.object(runner.socket, 'create_connection',
                             return_value=FakeConn()):
            runner._start_rig(self.cfg, self.record, self.src,
                              self.run_dir, lambda e, d=None: None)
            runner.relaunch_controller(
                self.cfg, self.record, self.run_dir, self.model,
                'active', lambda e, d=None: None)
        launches = [c for c in calls
                    if c[:4] == ('run', '-d', '--name',
                                 'dcs-hw-qa-1-a')]
        self.assertEqual(len(launches), 2)
        for launch in launches:
            self.assertIn('--new-persistence-flag', launch)


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

    def test_pause_and_unpause_recorded_on_timeline(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            timeline = lambda event, detail=None: events.append(
                (event, detail))
            runner.pause_plant('qa-1', timeline)
            runner.unpause_plant('qa-1', timeline)
        self.assertEqual(
            calls, [('pause', 'dcs-hw-qa-1-plant'),
                    ('unpause', 'dcs-hw-qa-1-plant')])
        self.assertEqual([event for event, _ in events],
                         ['plant-pause', 'plant-paused',
                          'plant-unpause', 'plant-unpaused'])

    def test_failed_pause_raises_after_recording_the_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'pause' and check:
                raise RuntimeError('docker pause failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.pause_plant(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['plant-pause'])

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
        probe = self.cfg['probe_pair']
        for fixture in (self.cfg['model_fixture'],
                        self.cfg['dynamics_fixture'],
                        probe['model_fixture'],
                        probe['dynamics_fixture']):
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
            self.assertIn('--history-file', launch)
            self.assertIn(runner.CONTAINER_HISTORY_FILE, launch)

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
        for path in ctx['history_files'].values():
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


class OwnerTokenPinTests(unittest.TestCase):
    """The per-controller --owner-token pins the run config records:
    every controller the runner launches — the pair and the
    scenario-action third peers — carries its endpoint's pinned
    token, so a scenario plant-protocol attachment can share the
    standing owner's claim through ensure_writer (the designed
    harness path) instead of preempting it; ctx['plant_owner'] hands
    the cases the same recorded map."""

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
        dynamics = self.src / self.cfg['dynamics_fixture']
        dynamics.parent.mkdir(parents=True, exist_ok=True)
        dynamics.write_text('{}')
        # The staged probe pair's fixtures — the probe block may share
        # the deployed model path, so only create what's absent.
        for key in ('model_fixture', 'dynamics_fixture'):
            path = self.src / self.cfg['probe_pair'][key]
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    @staticmethod
    def _docker(calls):
        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')
        return fake_docker

    @staticmethod
    def _launch(calls, container):
        return next(c for c in calls
                    if c[0] == 'run' and container in c)

    @staticmethod
    def _owner_token(launch):
        return launch[launch.index('--owner-token') + 1]

    def test_pair_launches_carry_the_config_pins(self):
        calls = []

        class FakeConn:
            def close(self):
                pass

        with patch.object(runner, 'docker', self._docker(calls)), \
                patch.object(runner.socket, 'create_connection',
                             return_value=FakeConn()):
            runner._start_rig(self.cfg, self._record(), self.src,
                              self.run_dir, lambda e, d=None: None)
        tokens = self.cfg['plant_owner_tokens']
        for key, container in (('active', 'dcs-hw-qa-1-a'),
                               ('standby', 'dcs-hw-qa-1-b')):
            launch = self._launch(calls, container)
            self.assertEqual(self._owner_token(launch),
                             str(tokens[key]), key)
        self.assertNotEqual(tokens['active'], tokens['standby'])

    def test_pins_come_from_the_run_config_not_a_constant(self):
        # A config-file override is what the launch carries — the pin
        # is recorded in the run config, not buried in the code.
        self.cfg['plant_owner_tokens'] = {
            **self.cfg['plant_owner_tokens'], 'active': 424299}
        calls = []

        class FakeConn:
            def close(self):
                pass

        with patch.object(runner, 'docker', self._docker(calls)), \
                patch.object(runner.socket, 'create_connection',
                             return_value=FakeConn()):
            runner._start_rig(self.cfg, self._record(), self.src,
                              self.run_dir, lambda e, d=None: None)
        launch = self._launch(calls, 'dcs-hw-qa-1-a')
        self.assertEqual(self._owner_token(launch), '424299')

    def test_every_third_controller_launch_carries_its_pin(self):
        calls, events = [], []
        with patch.object(runner, 'docker', self._docker(calls)):
            runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby', lambda e, d=None: events.append((e, d)))
            runner.start_foreign_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby', lambda e, d=None: events.append((e, d)))
            runner.start_driven_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby', lambda e, d=None: events.append((e, d)))
        tokens = self.cfg['plant_owner_tokens']
        for key, container in (('revised', 'dcs-hw-qa-1-c'),
                               ('foreign', 'dcs-hw-qa-1-foreign'),
                               ('driven', 'dcs-hw-qa-1-d')):
            launch = self._launch(calls, container)
            self.assertEqual(self._owner_token(launch),
                             str(tokens[key]), key)
        self.assertEqual(
            len({tokens[key] for key in
                 ('revised', 'foreign', 'driven')}), 3)
        # Each launch record names the pin it carried.
        for event, detail in events:
            if event in ('model-revision-start', 'negotiation-start',
                         'driven-start'):
                self.assertIn('--owner-token', detail, event)

    def test_scenario_ctx_hands_cases_the_recorded_map(self):
        ctx = runner._scenario_ctx(
            self.cfg, self._record(), self.src, self.run_dir,
            self.run_dir / 'evidence', 0, lambda e, d=None: None)
        self.assertEqual(ctx['plant_owner'],
                         self.cfg['plant_owner_tokens'])
        self.assertEqual(set(ctx['plant_owner']),
                         set(runner.OWNER_TOKEN_ENDPOINTS))

    def test_a_duplicated_pin_fails_the_launch_loudly(self):
        # Two endpoints on one token would silently defeat the sim's
        # single-writer fencing — the config check refuses before any
        # container launches.
        self.cfg['plant_owner_tokens'] = {
            **self.cfg['plant_owner_tokens'],
            'standby': self.cfg['plant_owner_tokens']['active']}
        calls = []
        with patch.object(runner, 'docker', self._docker(calls)):
            with self.assertRaises(RuntimeError) as caught:
                runner._start_rig(self.cfg, self._record(), self.src,
                                  self.run_dir, lambda e, d=None: None)
            self.assertIn('distinct', str(caught.exception))
            with self.assertRaises(RuntimeError):
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby', lambda e, d=None: None)
        self.assertFalse(any(c[0] == 'run' for c in calls))

    def test_a_missing_pin_fails_the_launch_loudly(self):
        tokens = dict(self.cfg['plant_owner_tokens'])
        del tokens['revised']
        self.cfg['plant_owner_tokens'] = tokens
        with patch.object(runner, 'docker', self._docker([])):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby', lambda e, d=None: None)
        self.assertIn('revised', str(caught.exception))

    def test_a_non_integer_pin_fails_the_launch_loudly(self):
        self.cfg['plant_owner_tokens'] = {
            **self.cfg['plant_owner_tokens'], 'driven': 'qa-d'}
        with patch.object(runner, 'docker', self._docker([])):
            with self.assertRaises(RuntimeError):
                runner.start_driven_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby', lambda e, d=None: None)


class EndpointPlacementTests(unittest.TestCase):
    """The rig bridge-to-host reachability rule the qax-20260922-001,
    qax-20260922-005, and qax-20260923-001 exploration runs
    demonstrated, recorded in the run config: the host egress policy
    drops every rig-network packet aimed at a host socket, so an
    endpoint a rig peer must dial (forge/interposer/plant-probe) runs
    bridge-placed in a labeled rig-bridge container while host-side
    attachments reach rig services through the published loopback
    ports only. _scenario_ctx hands the cases the recorded map and
    the run's bridge name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def _ctx(self):
        return runner._scenario_ctx(
            self.cfg, self._record(), self.src, self.run_dir,
            self.run_dir / 'evidence', 0, lambda e, d=None: None)

    def test_default_config_records_every_lane_endpoint(self):
        placements = self.cfg['endpoint_placement']
        self.assertEqual(set(placements),
                         set(runner.PLACEMENT_ENDPOINTS))
        # The endpoints rig peers must dial are recorded 'bridge': a
        # host socket is unreachable from the rig network.
        for key in ('forge', 'interposer'):
            self.assertEqual(placements[key], 'bridge', key)
        for key in ('active', 'standby', 'revised', 'foreign',
                    'driven', 'plant'):
            self.assertEqual(placements[key], 'loopback', key)

    def test_scenario_ctx_hands_cases_the_recorded_map(self):
        ctx = self._ctx()
        self.assertEqual(ctx['endpoint_placement'],
                         self.cfg['endpoint_placement'])
        self.assertEqual(set(ctx['endpoint_placement']),
                         set(runner.PLACEMENT_ENDPOINTS))
        self.assertEqual(ctx['rig_network'], 'dcs-hwtest-qa-1')

    def test_selection_comes_from_the_run_config_not_a_constant(self):
        # A config-file override is what the ctx carries — the
        # placement is recorded in the run config, not buried in code.
        self.cfg['endpoint_placement'] = {
            **self.cfg['endpoint_placement'], 'forge': 'loopback'}
        self.assertEqual(self._ctx()['endpoint_placement']['forge'],
                         'loopback')

    def test_a_missing_placement_fails_loudly(self):
        placements = dict(self.cfg['endpoint_placement'])
        del placements['forge']
        self.cfg['endpoint_placement'] = placements
        with self.assertRaises(RuntimeError) as caught:
            self._ctx()
        self.assertIn('forge', str(caught.exception))

    def test_an_unknown_placement_fails_loudly(self):
        self.cfg['endpoint_placement'] = {
            **self.cfg['endpoint_placement'], 'interposer': 'host'}
        with self.assertRaises(RuntimeError):
            self._ctx()

    def test_start_rig_fails_closed_on_a_bad_record(self):
        # A malformed map stops the launch before any container moves.
        self.cfg['endpoint_placement'] = {'active': 'loopback'}
        for fixture in (self.cfg['model_fixture'],
                        self.cfg['dynamics_fixture']):
            path = self.src / fixture
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(RuntimeError):
                runner._start_rig(self.cfg, self._record(), self.src,
                                  self.run_dir, lambda e, d=None: None)
        self.assertFalse(any(c[0] == 'run' for c in calls))

    def test_start_rig_refuses_a_loopback_endpoint_marked_bridge(self):
        # The pair's monitor ports and the plant probe port publish on
        # host loopback — a config recording them 'bridge' describes a
        # rig this launch does not build, so the launch refuses.
        self.cfg['endpoint_placement'] = {
            **self.cfg['endpoint_placement'], 'plant': 'bridge'}
        for fixture in (self.cfg['model_fixture'],
                        self.cfg['dynamics_fixture']):
            path = self.src / fixture
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(RuntimeError) as caught:
                runner._start_rig(self.cfg, self._record(), self.src,
                                  self.run_dir, lambda e, d=None: None)
        self.assertIn('plant', str(caught.exception))
        self.assertFalse(any(c[0] == 'run' for c in calls))

    def test_tracking_source_auth_leg_references_the_note(self):
        # The demote-forged-standby-source docstring's
        # forged-checkpoint endpoint is placed per the recorded rule
        # — 'bridge', never a host socket the rig cannot reach.
        doc = scenarios.__doc__
        auth = doc[doc.index('demote-forged-standby-source'):]
        auth = auth[:auth.index('command-record audit')]
        self.assertIn('endpoint_placement', auth)
        self.assertIn('bridge', auth)

    def test_shared_claim_legs_reference_the_note(self):
        # The shared-claim paragraph records the plant-probe
        # attachments' published-loopback placement and the
        # bridge-placed alternative for rig-side attachments.
        doc = scenarios.__doc__
        self.assertGreaterEqual(doc.count('endpoint_placement'), 2)
        shared = doc[doc.index('shared-claim'):]
        self.assertIn('endpoint_placement', shared)
        self.assertIn('published loopback port', shared)


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
                                            'dcs-plant-ctl',
                                            'dcs-ctl',
                                            'dcs-forge',
                                            'dcs-sim-bus-device')):
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
                                            'dcs-plant-server',
                                            'dcs-plant-ctl'))):
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


class PlantCtlShipTests(unittest.TestCase):
    """The dcs-plant-ctl image seam: the bounded image build compiles
    the plant-side tool beside the image binaries, the generated plant
    image ships it beside dcs-plant-server with the entrypoint
    unchanged, _scenario_ctx hands the cases a `docker exec`
    invocation against the container's loopback listener, and a build
    that produces no tool binary fails loudly rather than leaving the
    cases to run against a phantom tool."""

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
                                            'dcs-plant-ctl',
                                            'dcs-ctl',
                                            'dcs-forge',
                                            'dcs-sim-bus-device')):
        def fake_docker(*args, timeout=120, check=True):
            calls.append((args, check))
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

    def _ctx(self):
        return runner._scenario_ctx(
            self.cfg, {'run_id': 'qa-1'}, self.src,
            self.run_dir, self.run_dir / 'evidence', 0,
            lambda e, d=None: None)

    def test_build_compiles_the_tool_beside_the_server(self):
        calls, events = [], []
        with patch.object(runner, 'docker', self._fake_docker(calls)):
            runner._build_images(
                self.src, self.cfg, self.run_dir,
                lambda event, detail=None: events.append(event), 'qa-1')
        build = next(args for args, _ in calls
                     if args[0] == 'run' and 'cargo' in str(args))
        self.assertIn('-p dcs-controller -p dcs-plant -p dcs-sim-net',
                      build[-1])
        dockerfile = (self.run_dir / 'image-plant'
                      / 'Dockerfile').read_text()
        self.assertIn('COPY dcs-plant-server '
                      '/usr/local/bin/dcs-plant-server', dockerfile)
        self.assertIn('COPY dcs-plant-ctl '
                      '/usr/local/bin/dcs-plant-ctl', dockerfile)
        self.assertIn('ENTRYPOINT ["dcs-plant-server"]', dockerfile)
        self.assertTrue(
            (self.run_dir / 'image-plant' / 'dcs-plant-ctl').is_file())
        controller = (self.run_dir / 'image-controller'
                      / 'Dockerfile').read_text()
        self.assertNotIn('dcs-plant-ctl', controller)

    def test_build_fails_loudly_without_the_tool(self):
        with patch.object(runner, 'docker',
                          self._fake_docker(
                              [], binaries=('dcs-controller',
                                            'dcs-plant-server',
                                            'dcs-ctl'))):
            with self.assertRaises(RuntimeError):
                runner._build_images(self.src, self.cfg, self.run_dir,
                                     lambda e, d=None: None, 'qa-1')

    def test_scenario_ctx_execs_the_tool_inside_the_container(self):
        calls = []
        with patch.object(runner, 'docker', self._fake_docker(calls)):
            answer = self._ctx()['plant_ctl']('list')
        self.assertEqual(calls, [
            (('exec', 'dcs-hw-qa-1-plant', 'dcs-plant-ctl',
              '127.0.0.1:' + str(self.cfg['plant_port']), 'list'),
             False)])
        self.assertEqual(answer.returncode, 0)

    def test_scenario_ctx_returns_refusals_without_raising(self):
        calls = []

        def refusing(*args, timeout=120, check=True):
            calls.append((args, check))
            return Result('', returncode=1)

        with patch.object(runner, 'docker', refusing):
            answer = self._ctx()['plant_ctl']('write', '10', '1.5')
        self.assertEqual(answer.returncode, 1)
        self.assertEqual(calls[0][1], False)


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
        self.assertIn(runner.CONTAINER_HISTORY_FILE, launch)
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

    def _retyped_model(self):
        """A mounted model carrying the checked-in spec's retype
        target — point 302 writable internal bool — so the
        incompatible derivation applies."""
        document = json.loads(self.model.read_text())
        document['io_points'].append(
            {'id': 302, 'direction': 'in', 'value_type': 'bool',
             'writable': True, 'journaled': True,
             'initial': {'bool': False}})
        self.model.write_text(json.dumps(document))

    def test_incompatible_variant_derives_and_launches(self):
        self._retyped_model()
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            info = runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby',
                lambda event, detail=None: events.append(
                    (event, detail)), incompatible=True)
        launch = next(c for c in calls if c[0] == 'run')
        document = str(self.run_dir
                       / 'model-revised-incompatible.json')
        self.assertIn(document + ':/model/revised.json:ro', launch)
        self.assertIn('--revised', launch)
        self.assertIn('--standby', launch)
        self.assertIn('dcs-hw-qa-1-b:8081', launch)
        self.assertEqual(info['retyped_point'], 302)
        self.assertEqual(info['document'], document)
        derived = json.loads(Path(document).read_text())
        points = {p['id']: p for p in derived['io_points']}
        self.assertEqual(points[302]['value_type'], 'int')
        self.assertEqual(points[900]['value_type'], 'bool')
        self.assertEqual(revision.lint(derived), [])
        self.assertIn('retyped point 302', events[0][1])

    def test_incompatible_derivation_without_the_point_raises(self):
        # The checked-in spec names point 302 — a mounted model
        # lacking it fails the derivation before any container moves.
        events = []

        def fake_docker(*args, timeout=120, check=True):
            raise AssertionError('docker must not run')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(revision.RevisionError):
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby',
                    lambda event, detail=None: events.append(event),
                    incompatible=True)
        self.assertEqual(events, [])

    def test_second_launch_replaces_the_degraded_third(self):
        # The incompatible scenario's leftover '-c' is removed before
        # the compatible control launch — after its served role proves
        # it does not own the field.
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps' and 'name=' in str(args):
                return Result('ccc\n')
            return Result('')

        directory = self.run_dir / 'controllers' / 'c'
        directory.mkdir(parents=True)
        for artifact in ('state.json', 'journal.jsonl', 'history.jsonl'):
            (directory / artifact).write_text('stale')
        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_revised_peer_role',
                             return_value={'role': 'standby'}):
            runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby',
                lambda event, detail=None: events.append(event))
        removed = [c for c in calls
                   if c[:2] == ('rm', '-f')
                   and 'dcs-hw-qa-1-c' in c]
        self.assertEqual(len(removed), 1)
        self.assertIn('model-revision-replace', events)
        # The runner-owned artifacts reset with the container so the
        # new lifetime starts cold.
        for artifact in ('state.json', 'journal.jsonl', 'history.jsonl'):
            self.assertFalse((directory / artifact).exists())

    def test_relaunch_refuses_a_field_owning_third(self):
        def fake_docker(*args, timeout=120, check=True):
            if args[0] == 'ps' and 'name=' in str(args):
                return Result('ccc\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_revised_peer_role',
                             return_value={'role': 'active'}):
            with self.assertRaises(RuntimeError):
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby', lambda e, d=None: None)

    def test_relaunch_replaces_an_unreachable_third(self):
        # A leftover whose monitor does not answer cannot own the
        # field — the replacement proceeds.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps' and 'name=' in str(args):
                return Result('ccc\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker), \
                patch.object(runner, '_revised_peer_role',
                             return_value=None):
            runner.start_revised_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby', lambda e, d=None: None)
        self.assertTrue(any(c[:2] == ('rm', '-f') for c in calls))

    def test_failed_listing_fails_closed(self):
        def fake_docker(*args, timeout=120, check=True):
            if args[0] == 'ps':
                return Result('boom', returncode=1)
            raise AssertionError('docker must not run past the '
                                 'listing')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(RuntimeError):
                runner.start_revised_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'standby', lambda e, d=None: None)

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
        self.assertIn(runner.CONTAINER_HISTORY_FILE, launch)
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


class DrivenActionTests(unittest.TestCase):
    """The scenario-callable driven-peer actions: the runner launches
    the run's labeled third controller on the same mounted model with
    --standby AND --driven — the externally paced standby whose pulls
    happen only inside POST /scan — and removes the container again
    for the case's teardown, both halves recorded on the run's action
    timeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.model = self.src / self.cfg['model_fixture']
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps({'version': 1}))

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def test_driven_peer_launches_standby_driven(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            info = runner.start_driven_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'active',
                lambda event, detail=None: events.append(
                    (event, detail)))
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-d', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertIn('--standby', launch)
        self.assertIn('dcs-hw-qa-1-a:8080', launch)
        # The externally paced seam: --driven, never --scan-ms —
        # every pull happens inside a POST /scan request.
        self.assertIn('--driven', launch)
        self.assertNotIn('--scan-ms', launch)
        self.assertNotIn('--revised', launch)
        # The keyed contract: the run's --pair-token signs the driven
        # peer's ?prove= answers — the line_proof an islanded peer's
        # orphan-resolution probe demands of its owner candidate.
        self.assertIn('--pair-token', launch)
        self.assertIn(self.cfg['pair_token'], launch)
        self.assertIn('127.0.0.1:' + str(self.cfg['driven_port'])
                      + ':8082', launch)
        self.assertIn(runner.CONTAINER_STATE_FILE, launch)
        self.assertIn(runner.CONTAINER_JOURNAL_FILE, launch)
        self.assertIn(runner.CONTAINER_HISTORY_FILE, launch)
        self.assertIn(str(self.model) + ':/model/plant.json:ro', launch)
        self.assertIn(str(self.run_dir / 'controllers' / 'd')
                      + ':' + runner.CONTAINER_RUN_DIR, launch)
        self.assertEqual(info['container'], 'dcs-hw-qa-1-d')
        self.assertEqual([event for event, _ in events],
                         ['driven-start', 'driven-up'])

    def test_tokenless_run_launches_unkeyed(self):
        # An unkeyed run carries no --pair-token — the driven peer's
        # ?prove= answers then run unsigned like the pair's own.
        self.cfg['pair_token'] = None
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            runner.start_driven_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'active', lambda e, d=None: None)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertNotIn('--pair-token', launch)
        self.assertIn('--driven', launch)

    def test_standby_endpoint_standbys_on_ctrl_b(self):
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            runner.start_driven_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'standby', lambda e, d=None: None)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-b:8081', launch)

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.start_driven_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'driven', lambda e, d=None: None)

    def test_failed_launch_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'run' and check:
                raise RuntimeError('docker run failed: name in use')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_driven_controller(
                    self.cfg, self._record(), self.run_dir, self.model,
                    'active',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['driven-start'])

    def test_stop_removes_the_driven_container(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.stop_driven_controller(
                'qa-1', lambda event, detail=None: events.append(
                    (event, detail)))
        self.assertEqual(calls,
                         [('rm', '-f', 'dcs-hw-qa-1-d')])
        self.assertEqual([event for event, _ in events],
                         ['driven-stop', 'driven-stopped'])

    def test_failed_teardown_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'rm' and check:
                raise RuntimeError('docker rm failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.stop_driven_controller(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['driven-stop'])

    def test_scenario_ctx_carries_driven_actions_and_endpoint(self):
        calls = []
        record = self._record()
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            info = ctx['start_driven']('active')
            ctx['stop_driven']()
        self.assertEqual(ctx['driven'],
                         'http://127.0.0.1:' + str(
                             self.cfg['driven_port']))
        self.assertEqual(info['container'], 'dcs-hw-qa-1-d')
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('--standby', launch)
        self.assertIn('--driven', launch)
        # The keyed run hands the launch the pair token the keyed
        # orphan-resolution probes require.
        self.assertIn('--pair-token', launch)
        self.assertIn(self.cfg['pair_token'], launch)
        self.assertEqual(calls[-1],
                         ('rm', '-f', 'dcs-hw-qa-1-d'))
        # The driven peer's state/journal paths sit inside the run dir.
        self.assertTrue(Path(ctx['journal_files']['driven'])
                        .is_relative_to(self.run_dir))
        self.assertTrue(Path(ctx['state_files']['driven'])
                        .is_relative_to(self.run_dir))


class BornActiveActionTests(unittest.TestCase):
    """The born-active startup-failure leg's staging levers: the
    runner's scratch sim-serve field — 'silent' launches a sleeping
    placeholder under the field's container name so the address
    resolves but nothing listens, 'serving' launches the plant server
    and waits for its listener — and the labeled seat launches that
    carry --remote plus the class's --peer/--standby wiring, cold by
    construction (the seat's state/journal/history reset with the
    launch), refused while the seat reports a field-owning role."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        for name in ('c', 'foreign', 'd'):
            (self.run_dir / 'controllers' / name).mkdir(
                parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.model = self.src / self.cfg['model_fixture']
        self.dynamics = self.src / self.cfg['dynamics_fixture']
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.dynamics.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps({'version': 1}))
        self.dynamics.write_text(json.dumps({}))

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def _field(self, mode, docker=None):
        calls, events = [], []
        if docker is None:
            def docker(*args, timeout=120, check=True):
                calls.append(args)
                return Result('')
        with patch.object(runner, 'docker', docker):
            info = runner.start_born_field(
                self.cfg, self._record(), self.run_dir, self.model,
                self.dynamics,
                lambda event, detail=None: events.append(
                    (event, detail)), mode)
        return calls, events, info

    def _launch(self, seat='revised', docker=None, **kw):
        calls, events = [], []
        if docker is None:
            def docker(*args, timeout=120, check=True):
                calls.append(args)
                return Result('')
        with patch.object(runner, 'docker', docker):
            info = runner.start_born_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                seat, 'dcs-hw-qa-1-born-plant:9003',
                lambda event, detail=None: events.append(
                    (event, detail)), **kw)
        return calls, events, info

    def _run(self, calls):
        return next(c for c in calls if c[0] == 'run')

    def test_silent_field_launches_placeholder(self):
        calls, events, info = self._field('silent')
        self.assertEqual(calls[0],
                         ('rm', '-f', 'dcs-hw-qa-1-born-plant'))
        launch = self._run(calls)
        self.assertIn('dcs-hw-qa-1-born-plant', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        index = launch.index('--entrypoint')
        self.assertEqual(launch[index + 1], 'sleep')
        self.assertEqual(launch[-1], 'infinity')
        self.assertIn('dcs-hwtest/controller:' + SHA_A, launch)
        # The unreachable-field induction needs a name that resolves
        # but serves nothing: no published port, no sim-serve argv.
        self.assertNotIn('-p', launch)
        self.assertNotIn('--listen', launch)
        self.assertEqual(
            info['remote'],
            'dcs-hw-qa-1-born-plant:'
            + str(runner.BORN_FIELD_PORT))
        self.assertEqual([event for event, _ in events],
                         ['born-field-start', 'born-field-up'])

    def test_serving_field_launches_plant_and_probes(self):
        calls, events, info = self._field('serving')
        launch = self._run(calls)
        self.assertIn('dcs-hw-qa-1-born-plant', launch)
        self.assertIn('dcs-hwtest/plant:' + SHA_A, launch)
        self.assertIn(str(self.model) + ':/model/plant.json:ro',
                      launch)
        self.assertIn(str(self.dynamics)
                      + ':/model/dynamics.json:ro', launch)
        self.assertIn('0.0.0.0:' + str(runner.BORN_FIELD_PORT), launch)
        probe = next(c for c in calls if c[0] == 'exec')
        self.assertEqual(
            probe,
            ('exec', 'dcs-hw-qa-1-born-plant', 'dcs-plant-ctl',
             '127.0.0.1:' + str(runner.BORN_FIELD_PORT), 'list'))
        self.assertEqual(info['mode'], 'serving')

    def test_foreign_field_serves_the_foreign_fixtures(self):
        foreign_model = self.src / self.cfg['foreign_model_fixture']
        foreign_dynamics = (self.src
                            / self.cfg['foreign_dynamics_fixture'])
        foreign_model.parent.mkdir(parents=True, exist_ok=True)
        foreign_dynamics.parent.mkdir(parents=True, exist_ok=True)
        foreign_model.write_text(json.dumps({'version': 2}))
        foreign_dynamics.write_text(json.dumps({}))
        calls, events = [], []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', docker):
            ctx = runner._scenario_ctx(
                self.cfg, self._record(), self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: events.append(e))
            info = ctx['start_born_field']('foreign')
        launch = self._run(calls)
        self.assertIn(str(foreign_model) + ':/model/plant.json:ro',
                      launch)
        self.assertIn(str(foreign_dynamics)
                      + ':/model/dynamics.json:ro', launch)
        self.assertNotIn(str(self.model) + ':', launch)
        self.assertEqual(info['mode'], 'foreign')
        self.assertEqual(info['remote'],
                         'dcs-hw-qa-1-born-plant:'
                         + str(runner.BORN_FIELD_PORT))
        # The foreign field is still a serving listener — the
        # correspondence refusal needs live claim arbitration — so
        # the same in-container probe gates the address handoff.
        probe = next(c for c in calls if c[0] == 'exec')
        self.assertEqual(
            probe,
            ('exec', 'dcs-hw-qa-1-born-plant', 'dcs-plant-ctl',
             '127.0.0.1:' + str(runner.BORN_FIELD_PORT), 'list'))

    def test_born_field_ctl_execs_the_tool_inside_the_field(self):
        calls = []

        def docker(*args, timeout=120, check=True):
            calls.append((args, check))
            return Result('{"result": "points", "points": []}')

        with patch.object(runner, 'docker', docker):
            ctx = runner._scenario_ctx(
                self.cfg, self._record(), self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            answer = ctx['born_field_ctl']('list')
            refused = ctx['born_field_ctl']('step', '0')
        self.assertEqual(
            calls,
            [(('exec', 'dcs-hw-qa-1-born-plant', 'dcs-plant-ctl',
               '127.0.0.1:' + str(runner.BORN_FIELD_PORT), 'list'),
              False),
             (('exec', 'dcs-hw-qa-1-born-plant', 'dcs-plant-ctl',
               '127.0.0.1:' + str(runner.BORN_FIELD_PORT), 'step', '0'),
              False)])
        self.assertEqual(answer.returncode, 0)
        self.assertEqual(refused.returncode, 0)

    def test_serving_field_retries_until_the_listener_binds(self):
        polls = []

        def docker(*args, timeout=120, check=True):
            polls.append(args)
            if args[0] == 'exec':
                return Result('', returncode=1 if len(
                    [p for p in polls if p[0] == 'exec']) < 3 else 0)
            return Result('')

        self._field('serving', docker=docker)
        self.assertEqual(
            len([p for p in polls if p[0] == 'exec']), 3)

    def test_serving_field_never_binding_raises(self):
        moments = iter((0.0, 0.0, 61.0))
        fake_time = type('T', (), {
            'monotonic': staticmethod(lambda: next(moments)),
            'sleep': staticmethod(lambda seconds: None)})

        def docker(*args, timeout=120, check=True):
            if args[0] == 'exec':
                return Result('', returncode=1)
            return Result('')

        with patch.object(runner, 'time', fake_time):
            with self.assertRaises(RuntimeError) as caught:
                self._field('serving', docker=docker)
        self.assertIn('never bound', str(caught.exception))

    def test_pause_unpause_and_stop_field(self):
        calls, events = [], []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        timeline = lambda event, detail=None: events.append(event)
        with patch.object(runner, 'docker', docker):
            runner.pause_born_field('qa-1', timeline)
            runner.unpause_born_field('qa-1', timeline)
            runner.stop_born_field('qa-1', timeline)
        self.assertEqual(calls, [
            ('pause', 'dcs-hw-qa-1-born-plant'),
            ('unpause', 'dcs-hw-qa-1-born-plant'),
            ('rm', '-f', 'dcs-hw-qa-1-born-plant')])
        self.assertEqual(events, [
            'born-field-pause', 'born-field-paused',
            'born-field-unpause', 'born-field-unpaused',
            'born-field-stop', 'born-field-stopped'])

    def test_born_active_launch_carries_remote_peer_and_cold_state(self):
        calls, events, info = self._launch(peer='foreign')
        self.assertEqual(calls[0][:3],
                         ('ps', '-a', '--filter'))
        launch = self._run(calls)
        self.assertIn('dcs-hw-qa-1-c', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        index = launch.index('--remote')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-born-plant:'
                         + str(runner.BORN_FIELD_PORT))
        index = launch.index('--peer')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-foreign:'
                         + str(runner.BORN_MONITOR_PORT))
        self.assertNotIn('--standby', launch)
        index = launch.index('--owner-token')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['plant_owner_tokens']
                            ['revised']))
        index = launch.index('--listen')
        self.assertEqual(launch[index + 1],
                         '0.0.0.0:' + str(runner.BORN_MONITOR_PORT))
        index = launch.index('--scan-ms')
        self.assertEqual(launch[index + 1], '100')
        self.assertIn(runner.CONTAINER_STATE_FILE, launch)
        self.assertIn(runner.CONTAINER_JOURNAL_FILE, launch)
        self.assertIn(runner.CONTAINER_HISTORY_FILE, launch)
        self.assertIn('--pair-token', launch)
        self.assertIn(self.cfg['pair_token'], launch)
        self.assertIn('--restart', launch)
        self.assertIn('no', launch)
        self.assertIn('127.0.0.1:' + str(self.cfg['revised_port'])
                      + ':' + str(runner.BORN_MONITOR_PORT), launch)
        directory = self.run_dir / 'controllers' / 'c'
        self.assertIn(str(directory) + ':'
                      + runner.CONTAINER_RUN_DIR, launch)
        self.assertIn(str(self.model) + ':/model/plant.json:ro',
                      launch)
        self.assertEqual(info['container'], 'dcs-hw-qa-1-c')
        self.assertEqual(info['monitor'],
                         'http://127.0.0.1:'
                         + str(self.cfg['revised_port']))
        self.assertEqual([event for event, _ in events],
                         ['born-start', 'born-up'])

    def test_born_standby_launch_replaces_peer_with_standby(self):
        calls, _, info = self._launch(seat='foreign',
                                    standby='revised')
        launch = self._run(calls)
        index = launch.index('--standby')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-c:'
                         + str(runner.BORN_MONITOR_PORT))
        self.assertNotIn('--peer', launch)
        self.assertIn('127.0.0.1:' + str(self.cfg['foreign_port'])
                      + ':' + str(runner.BORN_MONITOR_PORT), launch)
        index = launch.index('--owner-token')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['plant_owner_tokens']
                            ['foreign']))
        self.assertEqual(info['container'], 'dcs-hw-qa-1-foreign')

    def test_verbatim_peer_names_pass_through_undoctored(self):
        calls, _, _ = self._launch(
            seat='driven', peer='dcs-born-dead-peer:8082')
        launch = self._run(calls)
        index = launch.index('--peer')
        self.assertEqual(launch[index + 1],
                         'dcs-born-dead-peer:8082')
        index = launch.index('--owner-token')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['plant_owner_tokens']
                            ['driven']))

    def test_peer_and_standby_together_rejected(self):
        with self.assertRaises(RuntimeError):
            self._launch(peer='foreign', standby='revised')

    def test_a_document_addressed_launch_carries_no_remote(self):
        # The sim-bus rig legs point a seat at a field the staged
        # document declares: no --remote at all, and that document
        # mounted in place of the run's sim-tcp model, so both ends of
        # the register protocol read one declaration.
        bus_model = self.run_dir / 'sim-bus' / 'model.json'
        bus_model.parent.mkdir(parents=True, exist_ok=True)
        bus_model.write_text(json.dumps({'version': 1}))
        calls, events, info = [], [], []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', docker):
            info = runner.start_born_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'driven', None,
                lambda event, detail=None: events.append(
                    (event, detail)), standby='revised',
                document=bus_model)
        launch = self._run(calls)
        self.assertNotIn('--remote', launch)
        self.assertIn(str(bus_model) + ':/model/plant.json:ro', launch)
        self.assertNotIn(str(self.model) + ':/model/plant.json:ro',
                         launch)
        # The launch role is otherwise the rig shape unchanged: the
        # --standby target resolved to the seat's bridge monitor and
        # the seat's own claim-token pin rides along.
        index = launch.index('--standby')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-c:'
                         + str(runner.BORN_MONITOR_PORT))
        index = launch.index('--owner-token')
        self.assertEqual(launch[index + 1],
                         str(self.cfg['plant_owner_tokens']['driven']))
        self.assertIsNone(info['remote'])
        # The return carries the document actually mounted, so a leg
        # staging against a device server can evidence that both ends
        # read the one declaration it staged.
        self.assertEqual(info['model'], str(bus_model))
        self.assertEqual(
            events[0][1],
            'launch dcs-hw-qa-1-d on ' + str(bus_model)
            + ' --standby dcs-hw-qa-1-c:' + str(runner.BORN_MONITOR_PORT)
            + ' --owner-token '
            + str(self.cfg['plant_owner_tokens']['driven']))

    def test_a_launch_with_neither_remote_nor_document_rejected(self):
        with self.assertRaises(RuntimeError) as caught:
            runner.start_born_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'driven', None, lambda event, detail=None: None)
        self.assertIn('staged document of its own',
                      str(caught.exception))

    def test_unknown_seat_rejected(self):
        with self.assertRaises(RuntimeError):
            self._launch(seat='active')
        with self.assertRaises(RuntimeError):
            runner.stop_born_controller(
                'qa-1', 'standby', lambda e, d=None: None)

    def test_seat_replacement_requires_the_role_read(self):
        def docker(*args, timeout=120, check=True):
            if args[0] == 'ps':
                return Result('', returncode=1)
            return Result('')

        with self.assertRaises(RuntimeError) as caught:
            self._launch(docker=docker)
        self.assertIn('cannot prove', str(caught.exception))

    def test_owning_seat_refuses_replacement(self):
        def docker(*args, timeout=120, check=True):
            if args[0] == 'ps':
                return Result('abc123\n')
            return Result('')

        def urlopen(request, timeout=10):
            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self):
                    return json.dumps({'role': 'active',
                                       'tick': 9}).encode()
            return _Response()

        with patch.object(runner, 'docker', docker), \
                patch.object(runner.urllib.request, 'urlopen',
                             urlopen):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_born_controller(
                    self.cfg, self._record(), self.run_dir,
                    self.model, 'revised', 'dcs-hw-qa-1-x:9003',
                    lambda e, d=None: None)
        self.assertIn('refuses to replace', str(caught.exception))

    def test_quiet_seat_is_removed_and_relaunched(self):
        calls = []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('abc123\n')
            return Result('')

        def urlopen(request, timeout=10):
            class _Response:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self):
                    return json.dumps({'role': 'standby',
                                       'sync': 'unsynchronized',
                                       'tick': 9}).encode()
            return _Response()

        with patch.object(runner, 'docker', docker), \
                patch.object(runner.urllib.request, 'urlopen',
                             urlopen):
            runner.start_born_controller(
                self.cfg, self._record(), self.run_dir, self.model,
                'revised', 'dcs-hw-qa-1-x:9003',
                lambda e, d=None: None)
        self.assertIn(('rm', '-f', 'dcs-hw-qa-1-c'), calls)
        self.assertTrue(any(c[0] == 'run' for c in calls))

    def test_born_launch_resets_the_seat_artifacts(self):
        directory = self.run_dir / 'controllers' / 'c'
        for artifact in ('state.json', 'journal.jsonl',
                         'history.jsonl'):
            (directory / artifact).write_text('stale\n')
        self._launch()
        for artifact in ('state.json', 'journal.jsonl',
                         'history.jsonl'):
            self.assertFalse((directory / artifact).exists(),
                             artifact)

    def test_stop_born_controller_tolerates_absence(self):
        calls, events = [], []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'rm':
                return Result('no such container', returncode=1)
            return Result('')

        with patch.object(runner, 'docker', docker):
            runner.stop_born_controller(
                'qa-1', 'driven',
                lambda event, detail=None: events.append(event))
        self.assertEqual(calls, [('rm', '-f', 'dcs-hw-qa-1-d')])
        self.assertEqual(events, ['born-stop', 'born-stopped'])

    def test_born_controller_state_reports_the_process_verdict(self):
        def docker(*args, timeout=120, check=True):
            if args[0] == 'inspect':
                return Result('false 1\n')
            if args[0] == 'logs':
                result = Result('claim refused; no --peer was declared')
                result.stderr = ''
                return result
            return Result('')

        with patch.object(runner, 'docker', docker):
            state = runner.born_controller_state('qa-1', 'driven')
        self.assertFalse(state['running'])
        self.assertEqual(state['exit'], 1)
        self.assertFalse(state['absent'])
        self.assertIn('no --peer was declared', state['logs'])

    def test_born_controller_state_reports_absence(self):
        def docker(*args, timeout=120, check=True):
            if args[0] == 'inspect':
                return Result('no such container', returncode=1)
            return Result('')

        with patch.object(runner, 'docker', docker):
            state = runner.born_controller_state('qa-1', 'driven')
        self.assertFalse(state['running'])
        self.assertTrue(state['absent'])

    def test_scenario_ctx_carries_the_born_actions(self):
        calls, events = [], []

        def docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        bus_model = self.run_dir / 'sim-bus' / 'model.json'
        bus_model.parent.mkdir(parents=True, exist_ok=True)
        bus_model.write_text(json.dumps({'version': 1}))
        with patch.object(runner, 'docker', docker):
            ctx = runner._scenario_ctx(
                self.cfg, self._record(), self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: events.append(e))
            for key in ('start_born_field', 'pause_born_field',
                        'unpause_born_field', 'stop_born_field',
                        'start_born_controller', 'stop_born_controller',
                        'born_controller_state'):
                self.assertIsNotNone(ctx[key], key)
            info = ctx['start_born_field']('silent')
            self.assertEqual(info['remote'],
                             'dcs-hw-qa-1-born-plant:'
                             + str(runner.BORN_FIELD_PORT))
            launched = ctx['start_born_controller'](
                'driven', info['remote'], peer='revised')
            ctx['pause_born_field']()
            ctx['unpause_born_field']()
            ctx['stop_born_controller']('driven')
            ctx['stop_born_field']()
            state = ctx['born_controller_state']('driven')
            # The same lever takes a document-addressed launch: the
            # caller names the document to mount and omits the remote.
            bus = ctx['start_born_controller'](
                'revised', None, document=bus_model)
        self.assertEqual(launched['container'], 'dcs-hw-qa-1-d')
        launch = next(c for c in calls
                      if c[0] == 'run' and 'dcs-hw-qa-1-d' in c)
        index = launch.index('--peer')
        self.assertEqual(launch[index + 1],
                         'dcs-hw-qa-1-c:'
                         + str(runner.BORN_MONITOR_PORT))
        self.assertIsNone(bus['remote'])
        self.assertEqual(bus['model'], str(bus_model))
        bus_launch = next(c for c in calls
                          if c[0] == 'run' and 'dcs-hw-qa-1-c' in c)
        self.assertNotIn('--remote', bus_launch)
        self.assertIn(str(bus_model) + ':/model/plant.json:ro',
                      bus_launch)
        self.assertFalse(state['running'])


class ForgeEndpointTests(unittest.TestCase):
    """The scenario-callable forged-checkpoint endpoint: the runner
    launches the run's labeled rig-bridge container on the shipped
    dcs-forge binary out of the controller image — announcing itself
    to the named owner's monitor and serving a staged checkpoint
    document the leg rewrites between demote calls — and removes the
    container again for the case's teardown, both halves recorded on
    the run's action timeline. The keyed posture comes from the run
    config's --pair-token, the launch refuses a non-bridge placement,
    and _scenario_ctx hands the actions to the case."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.document = {'format_version': 1,
                         'generation': 7, 'tick': 42,
                         'source_owns_field': False}

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def test_forge_launches_keyed_on_the_rig_bridge(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            info = runner.start_forge_endpoint(
                self.cfg, self._record(), self.run_dir, self.document,
                'active',
                lambda event, detail=None: events.append(
                    (event, detail)))
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('dcs-hw-qa-1-forge', launch)
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        # Bridge placement: the run's rig network, no host port.
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertNotIn('-p', launch)
        # The shipped binary runs under --entrypoint on the controller
        # image, serving the staged document and ledgering its hits.
        self.assertIn('--entrypoint', launch)
        self.assertIn('dcs-forge', launch)
        self.assertIn('dcs-hwtest/controller:' + SHA_A, launch)
        self.assertIn('--listen', launch)
        self.assertIn('0.0.0.0:' + str(runner.FORGE_PORT), launch)
        self.assertIn('--document', launch)
        self.assertIn('/forge/checkpoint.json', launch)
        self.assertIn('--hits', launch)
        self.assertIn('/forge/hits.jsonl', launch)
        # The endpoint announces to the named owner's monitor so its
        # bridge address is the recorded tracking hint.
        self.assertIn('--announce', launch)
        self.assertIn('dcs-hw-qa-1-a:8080', launch)
        # Keyed: the run's --pair-token signs the ?prove= answers.
        self.assertIn('--pair-token', launch)
        self.assertIn(self.cfg['pair_token'], launch)
        self.assertTrue(info['keyed'])
        self.assertEqual(info['container'], 'dcs-hw-qa-1-forge')
        self.assertEqual(info['port'], runner.FORGE_PORT)
        # The staged document and hits ledger sit inside the run dir.
        self.assertEqual(json.loads(Path(info['document'])
                                    .read_text()), self.document)
        self.assertTrue(Path(info['document'])
                        .is_relative_to(self.run_dir))
        self.assertTrue(Path(info['hits'])
                        .is_relative_to(self.run_dir))
        self.assertEqual([event for event, _ in events],
                         ['forge-start', 'forge-up'])

    def test_unkeyed_launch_carries_no_pair_token(self):
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            info = runner.start_forge_endpoint(
                self.cfg, self._record(), self.run_dir, self.document,
                'standby', lambda e, d=None: None, keyed=False)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertNotIn('--pair-token', launch)
        self.assertIn('dcs-hw-qa-1-b:8081', launch)
        self.assertFalse(info['keyed'])

    def test_tokenless_run_launches_unkeyed_regardless(self):
        self.cfg['pair_token'] = None
        calls = []
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            info = runner.start_forge_endpoint(
                self.cfg, self._record(), self.run_dir, self.document,
                'active', lambda e, d=None: None)
        launch = next(c for c in calls if c[0] == 'run')
        self.assertNotIn('--pair-token', launch)
        self.assertFalse(info['keyed'])

    def test_non_bridge_placement_refuses_the_launch(self):
        self.cfg['endpoint_placement'] = {
            **self.cfg['endpoint_placement'], 'forge': 'loopback'}
        with self.assertRaises(RuntimeError):
            runner.start_forge_endpoint(
                self.cfg, self._record(), self.run_dir, self.document,
                'active', lambda e, d=None: None)

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(RuntimeError):
            runner.start_forge_endpoint(
                self.cfg, self._record(), self.run_dir, self.document,
                'forge', lambda e, d=None: None)

    def test_failed_launch_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'run' and check:
                raise RuntimeError('docker run failed: name in use')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.start_forge_endpoint(
                    self.cfg, self._record(), self.run_dir,
                    self.document, 'active',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['forge-start'])

    def test_stop_removes_the_forge_container(self):
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner.stop_forge_endpoint(
                'qa-1', lambda event, detail=None: events.append(
                    (event, detail)))
        self.assertEqual(calls,
                         [('rm', '-f', 'dcs-hw-qa-1-forge')])
        self.assertEqual([event for event, _ in events],
                         ['forge-stop', 'forge-stopped'])

    def test_failed_teardown_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'rm' and check:
                raise RuntimeError('docker rm failed: no such')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.stop_forge_endpoint(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['forge-stop'])

    def test_scenario_ctx_carries_forge_actions_and_token(self):
        calls = []
        record = self._record()
        with patch.object(runner, 'docker',
                          lambda *a, **k: calls.append(a)
                          or Result('')):
            ctx = runner._scenario_ctx(
                self.cfg, record, self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            info = ctx['start_forge'](self.document, 'active')
            ctx['stop_forge']()
        self.assertEqual(ctx['pair_token'], self.cfg['pair_token'])
        self.assertEqual(info['container'], 'dcs-hw-qa-1-forge')
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('--entrypoint', launch)
        self.assertIn('dcs-forge', launch)
        self.assertIn('--pair-token', launch)
        self.assertEqual(calls[-1],
                         ('rm', '-f', 'dcs-hw-qa-1-forge'))

    def test_controller_image_ships_the_forge_binary(self):
        calls, events = [], []
        target = Path(self.cfg['state_dir']) / 'build-cache' \
            / 'target' / 'release'

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'run' and 'cargo' in str(args):
                target.mkdir(parents=True, exist_ok=True)
                for binary in ('dcs-controller', 'dcs-plant-server',
                               'dcs-plant-ctl', 'dcs-ctl', 'dcs-forge',
                               'dcs-sim-bus-device'):
                    (target / binary).write_text('bin')
            if args[:2] == ('image', 'inspect'):
                return Result('sha256:' + 'a' * 64)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            runner._build_images(
                self.src, self.cfg, self.run_dir,
                lambda event, detail=None: events.append(event),
                'qa-1')
        build = next(args for args in calls
                     if args[0] == 'run' and 'cargo' in str(args))
        self.assertIn('--bin dcs-forge', build[-1])
        dockerfile = (self.run_dir / 'image-controller'
                      / 'Dockerfile').read_text()
        self.assertIn('COPY dcs-forge /usr/local/bin/dcs-forge',
                      dockerfile)
        self.assertIn('ENTRYPOINT ["dcs-controller"]', dockerfile)
        self.assertTrue(
            (self.run_dir / 'image-controller' / 'dcs-forge')
            .is_file())
        plant = (self.run_dir / 'image-plant'
                 / 'Dockerfile').read_text()
        self.assertNotIn('dcs-forge', plant)

    def test_build_fails_loudly_without_the_forge_binary(self):
        target = Path(self.cfg['state_dir']) / 'build-cache' \
            / 'target' / 'release'

        def fake_docker(*args, timeout=120, check=True):
            if args[0] == 'run' and 'cargo' in str(args):
                target.mkdir(parents=True, exist_ok=True)
                for binary in ('dcs-controller', 'dcs-plant-server',
                               'dcs-plant-ctl', 'dcs-ctl',
                               'dcs-sim-bus-device'):
                    (target / binary).write_text('bin')
            if args[:2] == ('image', 'inspect'):
                return Result('sha256:' + 'a' * 64)
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(RuntimeError):
                runner._build_images(self.src, self.cfg, self.run_dir,
                                     lambda e, d=None: None, 'qa-1')


class SimBusDeviceImageTests(unittest.TestCase):
    """The lane's sim-bus device server seam (#1368): the bounded image
    build compiles the shipped `dcs-sim-bus-device` binary and the
    controller image carries it beside dcs-controller, so a rig leg
    launches the real register-protocol server out of the revision
    under test — the dcs-plant-ctl precedent of #654 — instead of a
    second implementation of the wire protocol. The launch stages the
    run config's bus model inside the bounded run directory with the
    named device's `__BUS_ADDR__` placeholder bound to the device
    container's rig-bridge name, runs the binary under --entrypoint on
    the run's bridge with no published port, waits for the server's
    own bound-address report, and hands the leg the address its
    sim-bus attachments dial plus the document it mounts into the
    controller it points at the field. The plant image, the two
    reported digests, the controller entrypoint, and the host-side
    dcs-ctl seam are unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.spec = self.cfg['sim_bus_device']
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        self.fixture = self.src / self.spec['model_fixture']
        self.fixture.parent.mkdir(parents=True, exist_ok=True)
        self.fixture.write_text(json.dumps(BUS_MODEL, indent=1) + '\n')
        # The shipped binary's own report of the address it serves on,
        # the readiness signal start_sim_bus_device waits for.
        self.serving = ('serving device 1 on dcs-hw-qa-1-bus:9005 '
                        '(declared 0.0.0.0:9005)')

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    def _build_docker(self, calls, binaries=('dcs-controller',
                                            'dcs-plant-server',
                                            'dcs-plant-ctl',
                                            'dcs-ctl',
                                            'dcs-forge',
                                            'dcs-sim-bus-device')):
        target = Path(self.cfg['state_dir']) / 'build-cache' \
            / 'target' / 'release'

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'run' and 'cargo' in str(args):
                target.mkdir(parents=True, exist_ok=True)
                for binary in binaries:
                    (target / binary).write_text('bin')
            if args[:2] == ('image', 'inspect'):
                return Result('sha256:' + 'a' * 64)
            return Result('')
        return fake_docker

    def _serving_docker(self, calls, events=None):
        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'logs':
                return Result(self.serving)
            if args[0] == 'inspect':
                return Result('true')
            return Result('')
        return fake_docker

    def test_bounded_build_compiles_and_ships_the_device_binary(self):
        calls, events = [], []
        with patch.object(runner, 'docker',
                          self._build_docker(calls)):
            digests = runner._build_images(
                self.src, self.cfg, self.run_dir,
                lambda event, detail=None: events.append(event), 'qa-1')
        build = next(args for args in calls
                     if args[0] == 'run' and 'cargo' in str(args))
        # The compile set names the crate and the binary, the way the
        # dcs-ctl and dcs-forge seams name theirs.
        self.assertIn('-p dcs-sim-bus --bin dcs-sim-bus-device',
                      build[-1])
        # The ship map carries it into the controller image beside its
        # entrypoint — a dedicated bus image would change the reported
        # digests, so the existing image rides it.
        dockerfile = (self.run_dir / 'image-controller'
                      / 'Dockerfile').read_text()
        self.assertIn('COPY dcs-sim-bus-device '
                      '/usr/local/bin/dcs-sim-bus-device', dockerfile)
        self.assertIn('ENTRYPOINT ["dcs-controller"]', dockerfile)
        self.assertTrue((self.run_dir / 'image-controller'
                         / 'dcs-sim-bus-device').is_file())
        # The plant image and the two reported digests are untouched,
        # and the operator CLI stays a host-side binary.
        plant = (self.run_dir / 'image-plant' / 'Dockerfile').read_text()
        self.assertNotIn('dcs-sim-bus-device', plant)
        self.assertEqual(set(digests), {'controller', 'plant'})
        self.assertIn('tool-built', events)

    def test_build_fails_loudly_without_the_device_binary(self):
        with patch.object(runner, 'docker', self._build_docker(
                [], binaries=('dcs-controller', 'dcs-plant-server',
                              'dcs-plant-ctl', 'dcs-ctl', 'dcs-forge'))):
            with self.assertRaises(RuntimeError) as caught:
                runner._build_images(self.src, self.cfg, self.run_dir,
                                     lambda e, d=None: None, 'qa-1')
        self.assertIn('dcs-sim-bus-device', str(caught.exception))

    def test_launch_serves_the_staged_model_on_the_rig_bridge(self):
        calls, events = [], []
        with patch.object(runner, 'docker',
                          self._serving_docker(calls)):
            info = runner.start_sim_bus_device(
                self.cfg, self._record(), self.run_dir,
                lambda event, detail=None: events.append(
                    (event, detail)))
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn(runner.MANAGED_LABEL + '=1', launch)
        self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
        # Bridge placement: the run's rig network, dialed by container
        # name, no host-published port.
        self.assertIn('--network', launch)
        self.assertIn('dcs-hwtest-qa-1', launch)
        self.assertNotIn('-p', launch)
        # The shipped binary out of the controller image under
        # --entrypoint, serving the staged document.
        self.assertIn('--entrypoint', launch)
        self.assertIn('dcs-sim-bus-device', launch)
        self.assertIn('dcs-hwtest/controller:' + SHA_A, launch)
        self.assertIn(info['model'] + ':/model/plant.json:ro', launch)
        self.assertIn('/model/plant.json', launch)
        self.assertIn('--device', launch)
        self.assertIn('1', launch)
        self.assertIn('--listen', launch)
        self.assertIn('0.0.0.0:9005', launch)
        self.assertEqual(info, {'container': 'dcs-hw-qa-1-bus',
                                'address': 'dcs-hw-qa-1-bus:9005',
                                'port': 9005, 'device': 1,
                                'model': info['model']})
        self.assertTrue(Path(info['model']).is_relative_to(self.run_dir))
        self.assertEqual([event for event, _ in events],
                         ['sim-bus-start', 'sim-bus-up'])

    def test_the_staged_model_declares_the_bridge_address(self):
        # The document the leg mounts into its controller binds the
        # device's placeholder to the address the shipped server serves
        # on, so the attachment's declared address and the running
        # server's bind are one declaration.
        with patch.object(runner, 'docker',
                          self._serving_docker([])):
            info = runner.start_sim_bus_device(
                self.cfg, self._record(), self.run_dir,
                lambda e, d=None: None)
        staged = json.loads(Path(info['model']).read_text())
        device = next(d for d in staged['devices'] if d['id'] == 1)
        self.assertEqual(device['parameters']['address'],
                         info['address'])
        self.assertEqual(device['parameters']['registers']['valve_cmd'], 3)
        self.assertEqual(device['kind'], 'sim-bus')

    def test_a_never_serving_launch_fails_with_the_servers_report(self):
        def fake_docker(*args, timeout=120, check=True):
            if args[0] == 'logs':
                return Result('error: cannot bind 0.0.0.0:9005')
            if args[0] == 'inspect':
                return Result('false')
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertIn('cannot bind', str(caught.exception))

    def test_non_bridge_placement_refuses_the_launch(self):
        self.cfg['endpoint_placement'] = {
            **self.cfg['endpoint_placement'], 'sim_bus_device': 'loopback'}
        calls = []
        with patch.object(runner, 'docker', self._serving_docker(calls)):
            with self.assertRaises(RuntimeError):
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertEqual(calls, [])

    def test_unstaged_run_refuses_the_launch(self):
        self.cfg['sim_bus_device'] = None
        calls = []
        with patch.object(runner, 'docker', self._serving_docker(calls)):
            with self.assertRaises(RuntimeError):
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertEqual(calls, [])

    def test_an_undeclared_device_fails_before_a_container_exists(self):
        self.cfg['sim_bus_device'] = {**self.spec, 'device': 7}
        calls = []
        with patch.object(runner, 'docker', self._serving_docker(calls)):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertIn('declares no device 7', str(caught.exception))
        self.assertEqual(calls, [])

    def test_a_model_without_the_placeholder_fails_before_launch(self):
        # A document whose address is already bound belongs to
        # something else: serving it here would leave the attachments
        # dialing an endpoint the device server never bound.
        fixture = self.src / self.spec['model_fixture']
        document = json.loads(fixture.read_text())
        document['devices'][0]['parameters']['address'] = '127.0.0.1:9'
        fixture.write_text(json.dumps(document, indent=1) + '\n')
        calls = []
        with patch.object(runner, 'docker', self._serving_docker(calls)):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertIn(runner.BUS_ADDR_PLACEHOLDER, str(caught.exception))
        self.assertEqual(calls, [])

    def test_a_non_bus_device_fails_before_launch(self):
        fixture = self.src / self.spec['model_fixture']
        document = json.loads(fixture.read_text())
        document['devices'][0]['kind'] = 'sim'
        fixture.write_text(json.dumps(document, indent=1) + '\n')
        with patch.object(runner, 'docker', self._serving_docker([])):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None)
        self.assertIn('which the device server does not serve',
                      str(caught.exception))

    def test_malformed_blocks_fail_before_launch(self):
        cases = [
            ({**self.spec, 'port': '9005'}, 'device/port must be int'),
            ({**self.spec, 'device': 0}, 'device/port must be int'),
            ({key: value for key, value in self.spec.items()
              if key != 'model_fixture'}, 'stages no model_fixture'),
            ({**self.spec, 'cyclic_model': 9},
             'cyclic_model must name'),
            ('device-1', 'sim_bus_device must map'),
        ]
        for block, message in cases:
            with self.subTest(block=block):
                self.cfg['sim_bus_device'] = block
                calls = []
                with patch.object(runner, 'docker',
                                  self._serving_docker(calls)):
                    with self.assertRaises(RuntimeError) as caught:
                        runner.start_sim_bus_device(
                            self.cfg, self._record(), self.run_dir,
                            lambda e, d=None: None)
                self.assertIn(message, str(caught.exception))
                self.assertEqual(calls, [])

    def test_stop_removes_the_device_container(self):
        calls, events = [], []
        with patch.object(runner, 'docker',
                          self._serving_docker(calls)):
            runner.stop_sim_bus_device(
                'qa-1', lambda event, detail=None: events.append(
                    (event, detail)))
        self.assertEqual(calls[-1], ('rm', '-f', 'dcs-hw-qa-1-bus'))
        self.assertEqual([event for event, _ in events],
                         ['sim-bus-stop', 'sim-bus-stopped'])

    def test_failed_teardown_raises_after_recording_attempt(self):
        events = []

        def raising(*args, timeout=120, check=True):
            if args[0] == 'rm' and check:
                raise RuntimeError('docker rm failed: no such container')
            return Result('')

        with patch.object(runner, 'docker', raising):
            with self.assertRaises(RuntimeError):
                runner.stop_sim_bus_device(
                    'qa-1',
                    lambda event, detail=None: events.append(event))
        self.assertEqual(events, ['sim-bus-stop'])

    def test_a_fixture_override_stages_the_selected_model(self):
        # The fencing-loss leg's seam: the run config's cyclic_model
        # names the sim-cyclic document, and the launch stages it on
        # the same device block — the server serves either
        # register-protocol kind.
        cyclic = self.src / 'crates/dcs-demo/fixtures/cyclic.json'
        cyclic.parent.mkdir(parents=True, exist_ok=True)
        cyclic.write_text(json.dumps(CYCLIC_BUS_MODEL, indent=1) + '\n')
        with patch.object(runner, 'docker',
                          self._serving_docker([])):
            info = runner.start_sim_bus_device(
                self.cfg, self._record(), self.run_dir,
                lambda e, d=None: None,
                fixture='crates/dcs-demo/fixtures/cyclic.json')
        staged = json.loads(Path(info['model']).read_text())
        device = next(d for d in staged['devices'] if d['id'] == 1)
        self.assertEqual(device['kind'], 'sim-cyclic')
        self.assertEqual(device['parameters']['address'],
                         info['address'])

    def test_a_missing_override_fixture_fails_before_launch(self):
        calls = []
        with patch.object(runner, 'docker',
                          self._serving_docker(calls)):
            with self.assertRaises(RuntimeError) as caught:
                runner.start_sim_bus_device(
                    self.cfg, self._record(), self.run_dir,
                    lambda e, d=None: None,
                    fixture='crates/dcs-demo/fixtures/absent.json')
        self.assertIn('fixture missing', str(caught.exception))
        self.assertEqual(calls, [])

    def test_restart_severs_and_waits_on_the_fresh_lifetime(self):
        # The fencing-loss leg's sever: docker restart drops every
        # attachment's control connection and the same server comes
        # back — the readiness read is scoped to the restarted
        # process's logs, since the old lifetime's announcement stays
        # on the container log.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'inspect' and 'StartedAt' in str(args):
                return Result('2026-10-01T12:00:00Z')
            if args[0] == 'logs' and '--since' in args:
                return Result(self.serving)
            if args[0] == 'logs':
                # The previous lifetime's announcement — present but
                # scoped out of the readiness read.
                return Result(self.serving + '\nstale')
            if args[0] == 'inspect':
                return Result('true')
            return Result('')

        events = []
        with patch.object(runner, 'docker', fake_docker):
            info = runner.restart_sim_bus_device(
                self.cfg, 'qa-1', self.run_dir,
                lambda e, d=None: events.append(e))
        self.assertIn(('restart', 'dcs-hw-qa-1-bus'), calls)
        self.assertTrue(any(c[0] == 'logs' and '--since' in c
                            for c in calls))
        self.assertEqual(info['address'], 'dcs-hw-qa-1-bus:9005')
        self.assertEqual(info['model'],
                         str(self.run_dir / 'sim-bus' / 'model.json'))
        self.assertEqual(events, ['sim-bus-sever', 'sim-bus-severed'])

    def test_restart_without_a_staged_server_refuses(self):
        self.cfg['sim_bus_device'] = None
        calls = []
        with patch.object(runner, 'docker',
                          self._serving_docker(calls)):
            with self.assertRaises(RuntimeError) as caught:
                runner.restart_sim_bus_device(
                    self.cfg, 'qa-1', self.run_dir, lambda e, d=None: None)
        self.assertIn('nothing to sever', str(caught.exception))
        self.assertEqual(calls, [])

    def test_scenario_ctx_carries_the_device_actions(self):
        calls = []
        with patch.object(runner, 'docker',
                          self._serving_docker(calls)):
            ctx = runner._scenario_ctx(
                self.cfg, self._record(), self.src, self.run_dir,
                self.run_dir / 'evidence', 0,
                lambda e, d=None: None)
            info = ctx['start_sim_bus_device']()
            ctx['stop_sim_bus_device']()
        self.assertEqual(ctx['endpoint_placement']['sim_bus_device'],
                         'bridge')
        self.assertEqual(info['address'], 'dcs-hw-qa-1-bus:9005')
        launch = next(c for c in calls if c[0] == 'run')
        self.assertIn('--entrypoint', launch)
        self.assertIn('dcs-sim-bus-device', launch)
        self.assertEqual(calls[-1], ('rm', '-f', 'dcs-hw-qa-1-bus'))

    @unittest.skipUnless(
        SIM_BUS_DEVICE_BIN, 'the shipped dcs-sim-bus-device binary is '
        'not compiled in this checkout (DCS_SIM_BUS_DEVICE overrides '
        'where it is looked for)')
    def test_the_staged_model_answers_the_register_protocol(self):
        # The staged document really serves: the shipped binary out of
        # this checkout reads the runner's staged model — the same
        # bytes the rig mounts — and answers the claim/exchange traffic
        # a rig peer exchanges against it. The bind is loopback here
        # because the placement under test is the rig bridge's, not the
        # host's: the address the model declares is the device
        # container's name on that bridge.
        staged = runner._stage_bus_model(
            self.src / self.spec['model_fixture'],
            self.run_dir / 'model.json', 1, '127.0.0.1:0')
        process = subprocess.Popen(
            [SIM_BUS_DEVICE_BIN, staged, '--device', '1',
             '--listen', '127.0.0.1:0'],
            stderr=subprocess.PIPE, text=True)
        try:
            address = self._announced_address(process)
            with socket.create_connection(address, timeout=5) as stream:
                stream.settimeout(5)
                # The device's declared register map, served.
                answer = _bus_send(stream, bytes([BUS_LIST]))
                self.assertEqual(answer[0], BUS_REGISTERS)
                self.assertEqual(set(_bus_census(answer, 1)),
                                 {0, 1, 2, 3, 4})
                # The field claim a controller's attachment takes.
                answer = _bus_send(stream, bytes([BUS_CLAIM])
                                   + struct.pack('>Q', 0x6463))
                self.assertEqual(answer, bytes([BUS_DONE]))
                # A staged output write and the exchange that publishes
                # one, the traffic the register-protocol legs exercise.
                answer = _bus_send(stream,
                                   _bus_write(3, BUS_FLOAT, 12.5))
                self.assertEqual(answer[0], BUS_WRITTEN)
                answer = _bus_send(stream, _bus_read(3))
                self.assertEqual(answer[0], BUS_SAMPLE)
                self.assertEqual(_bus_sample(answer[1:])[1], 12.5)
                answer = _bus_send(stream, _bus_exchange([
                    (3, BUS_FLOAT, 20.0), (4, BUS_BOOL, True)]))
                self.assertEqual(answer[0], BUS_EXCHANGED)
                self.assertEqual(answer[1], 0)   # not late
                self.assertEqual(_bus_census(answer, 2)[3], 20.0)
                self.assertEqual(_bus_census(answer, 2)[4], True)
        finally:
            process.terminate()
            process.wait(timeout=30)
            process.stderr.close()

    def _announced_address(self, process):
        """The address the shipped server reports it serves on: its own
        readiness report, the same line the rig launch waits for."""
        deadline = time.monotonic() + 30
        report = ''
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stderr], [], [],
                                        deadline - time.monotonic())
            if not ready:
                break
            line = process.stderr.readline()
            if not line:
                break
            report += line
            if 'serving device 1 on ' in line:
                served = line.split('serving device 1 on ',
                                    1)[1].split(' ')[0]
                host, _, port = served.rpartition(':')
                return (host, int(port))
        self.fail('the shipped device server never reported a served '
                  'address: ' + (report.strip() or 'no output'))


class ProbePairTests(unittest.TestCase):
    """The lane-staged keyed probe pair (#1058): a second, always-keyed
    redundant pair bound to its own sim-serve plant, so the keyed
    announced-source legs exercise the contract per revision while
    the deployed pair runs whichever posture the run config gives
    it. The probe plant is bridge-placed with its own declared
    dynamics and field; the probe controllers share the block's
    --pair-token, carry the distinct probe_* owner-token pins, and
    publish their monitors on host loopback; ctx['probe'] hands the
    keyed legs the pair as their subject."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        self.probe = self.cfg['probe_pair']
        self.run_dir = Path(self.cfg['state_dir']) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.src = Path(self.cfg['src_dir']) / SHA_A
        for fixture in (self.cfg['model_fixture'],
                        self.cfg['dynamics_fixture'],
                        self.probe['model_fixture'],
                        self.probe['dynamics_fixture']):
            path = self.src / fixture
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        self.document = {'format_version': 1, 'generation': 7,
                         'tick': 42, 'source_owns_field': False}

    def tearDown(self):
        self.tmp.cleanup()

    def _record(self):
        return {'run_id': 'qa-1', 'attempted_sha': SHA_A}

    @staticmethod
    def _docker(calls):
        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return Result('')
        return fake_docker

    @staticmethod
    def _launch(calls, container):
        return next(c for c in calls
                    if c[0] == 'run' and container in c)

    def _rig(self, calls, events=None):
        class FakeConn:
            def close(self):
                pass
        with patch.object(runner, 'docker', self._docker(calls)), \
                patch.object(runner.socket, 'create_connection',
                             return_value=FakeConn()):
            runner._start_rig(
                self.cfg, self._record(), self.src, self.run_dir,
                lambda e, d=None: events is not None
                and events.append((e, d)))

    def _ctx(self):
        return runner._scenario_ctx(
            self.cfg, self._record(), self.src, self.run_dir,
            self.run_dir / 'evidence', 0, lambda e, d=None: None)

    def test_start_rig_stages_the_probe_pair_keyed(self):
        calls, events = [], []
        self._rig(calls, events)
        tokens = self.cfg['plant_owner_tokens']
        # The probe plant: its own sim-serve deployment — the run's
        # labels and rig bridge, its own dynamics declaration, no
        # host publish (bridge-placed).
        plant = self._launch(calls, 'dcs-hw-qa-1-probe-plant')
        self.assertIn(runner.MANAGED_LABEL + '=1', plant)
        self.assertIn(runner.RUN_LABEL + '=qa-1', plant)
        self.assertIn('dcs-hwtest-qa-1', plant)
        self.assertNotIn('-p', plant)
        self.assertIn(str(self.src / self.probe['model_fixture'])
                      + ':/model/plant.json:ro', plant)
        self.assertIn(str(self.src / self.probe['dynamics_fixture'])
                      + ':/model/dynamics.json:ro', plant)
        self.assertIn('--dynamics', plant)
        self.assertIn('0.0.0.0:' + str(self.probe['plant_port']),
                      plant)
        # Readiness runs through the shipped tool inside the probe
        # plant's own netns — no host port exists to probe.
        self.assertTrue(any(
            c[:3] == ('exec', 'dcs-hw-qa-1-probe-plant',
                      'dcs-plant-ctl')
            and '127.0.0.1:' + str(self.probe['plant_port']) in c
            for c in calls))
        # The keyed pair: shared --pair-token, distinct probe_*
        # owner-token pins, bound to the probe plant, monitors
        # published on host loopback, b tracking a.
        for key, container, publish in (
                ('probe_active', 'dcs-hw-qa-1-probe-a',
                 '127.0.0.1:' + str(self.probe['active_port'])
                 + ':8080'),
                ('probe_standby', 'dcs-hw-qa-1-probe-b',
                 '127.0.0.1:' + str(self.probe['standby_port'])
                 + ':8081')):
            launch = self._launch(calls, container)
            self.assertIn(runner.MANAGED_LABEL + '=1', launch)
            self.assertIn(runner.RUN_LABEL + '=qa-1', launch)
            self.assertIn(publish, launch)
            self.assertIn('--pair-token', launch)
            self.assertIn(self.probe['pair_token'], launch)
            self.assertIn('--remote', launch)
            self.assertIn(
                'dcs-hw-qa-1-probe-plant:'
                + str(self.probe['plant_port']), launch)
            self.assertEqual(launch[launch.index('--owner-token') + 1],
                             str(tokens[key]), key)
        probe_b = self._launch(calls, 'dcs-hw-qa-1-probe-b')
        self.assertIn('--standby', probe_b)
        self.assertIn('dcs-hw-qa-1-probe-a:8080', probe_b)
        self.assertIn('--auto-promote', probe_b)
        # The probe pair never touches the deployed pair's field or
        # claim tokens.
        for container in ('dcs-hw-qa-1-probe-a',
                          'dcs-hw-qa-1-probe-b'):
            launch = self._launch(calls, container)
            self.assertNotIn(
                'dcs-hw-qa-1-plant:' + str(self.cfg['plant_port']),
                launch)
            for deployed in ('active', 'standby', 'revised',
                             'foreign', 'driven'):
                self.assertNotEqual(
                    launch[launch.index('--owner-token') + 1],
                    str(tokens[deployed]), deployed)
        self.assertNotEqual(tokens['probe_active'],
                            tokens['probe_standby'])
        self.assertIn('probe-rig-up',
                      [event for event, _ in events])
        # Probe controllers' state dirs live under the run dir.
        self.assertTrue((self.run_dir / 'controllers' / 'probe-a')
                        .is_dir())
        self.assertTrue((self.run_dir / 'controllers' / 'probe-b')
                        .is_dir())

    def test_unkeyed_deployed_pair_still_stages_probe_keyed(self):
        # The posture split the issue calls for: the deployed pair
        # launches unkeyed while the probe pair still carries the
        # shared --pair-token.
        self.cfg['pair_token'] = None
        calls = []
        self._rig(calls)
        for container in ('dcs-hw-qa-1-a', 'dcs-hw-qa-1-b'):
            launch = self._launch(calls, container)
            self.assertNotIn('--pair-token', launch)
        for container in ('dcs-hw-qa-1-probe-a',
                          'dcs-hw-qa-1-probe-b'):
            launch = self._launch(calls, container)
            self.assertIn('--pair-token', launch)
            self.assertIn(self.probe['pair_token'], launch)
        # And the ctx hands the legs the keyed probe subject while
        # the deployed ctx stays honestly unkeyed.
        ctx = self._ctx()
        self.assertIsNone(ctx['pair_token'])
        self.assertEqual(ctx['probe']['pair_token'],
                         self.probe['pair_token'])
        self.assertIs(ctx['probe'],
                      scenarios._keyed_subject(ctx))

    def test_probe_block_validation_fails_loudly(self):
        for broken in ({'pair_token': 'x'},
                       'not-a-mapping',
                       dict(self.probe, pair_token=None),
                       dict(self.probe, active_port=0),
                       dict(self.probe, plant_port='9002'),
                       dict(self.probe, model_fixture='')):
            with self.assertRaises(RuntimeError):
                runner._probe_pair(
                    {**self.cfg, 'probe_pair': broken})
        self.assertIsNone(runner._probe_pair(
            {**self.cfg, 'probe_pair': None}))

    def test_no_probe_block_stages_no_probe_pair(self):
        self.cfg['probe_pair'] = None
        calls = []
        self._rig(calls)
        launched = [c for c in calls if c[0] == 'run']
        self.assertEqual(len(launched), 3)
        self.assertIsNone(self._ctx()['probe'])
        self.assertIsNone(scenarios._keyed_subject(
            {'pair_token': None, 'probe': None}))

    def test_probe_endpoints_require_their_recorded_placements(self):
        # The probe plant is rig-dialed only — a loopback record is
        # a rig this launch does not build; the probe monitors
        # publish on host loopback — a bridge record likewise.
        for key, value in (('probe_plant', 'loopback'),
                           ('probe_active', 'bridge'),
                           ('probe_standby', 'bridge'),
                           ('probe_driven', 'bridge')):
            self.cfg['endpoint_placement'] = {
                **self.cfg['endpoint_placement'], key: value}
            calls = []
            try:
                with patch.object(runner, 'docker',
                                  self._docker(calls)):
                    with self.assertRaises(RuntimeError) as caught:
                        runner._start_rig(
                            self.cfg, self._record(), self.src,
                            self.run_dir, lambda e, d=None: None)
            finally:
                self.cfg['endpoint_placement'] = dict(
                    runner.DEFAULT_CONFIG['endpoint_placement'])
            self.assertIn(key, str(caught.exception), key)
            self.assertFalse(any(c[0] == 'run' for c in calls), key)

    def test_probe_subject_ctx_rebinds_the_run_actions(self):
        calls = []
        with patch.object(runner, 'docker', self._docker(calls)):
            ctx = self._ctx()
            probe = ctx['probe']
            self.assertIsNotNone(probe)
            self.assertEqual(probe['active'],
                             'http://127.0.0.1:'
                             + str(self.probe['active_port']))
            self.assertEqual(probe['standby'],
                             'http://127.0.0.1:'
                             + str(self.probe['standby_port']))
            self.assertEqual(probe['driven'],
                             'http://127.0.0.1:'
                             + str(self.probe['driven_port']))
            # Bridge-placed field: no host-side plant attachment.
            self.assertIsNone(probe['plant'])
            self.assertEqual(probe['pair_token'],
                             self.probe['pair_token'])
            tokens = self.cfg['plant_owner_tokens']
            self.assertEqual(
                probe['plant_owner'],
                {'active': tokens['probe_active'],
                 'standby': tokens['probe_standby'],
                 'driven': tokens['probe_driven']})
            # Deployed-pair-only actions are absent rather than
            # rebound onto the wrong pair.
            for action in ('start_revised', 'start_foreign',
                           'stop_foreign'):
                self.assertIsNone(probe[action], action)
            # The probe pair's own journal/state paths.
            for key, peer in (('active', 'probe-a'),
                              ('standby', 'probe-b'),
                              ('driven', 'probe-d')):
                self.assertIn('controllers/' + peer,
                              probe['journal_files'][key], key)
                self.assertTrue(Path(probe['journal_files'][key])
                                .is_relative_to(self.run_dir), key)
                self.assertTrue(Path(probe['state_files'][key])
                                .is_relative_to(self.run_dir), key)
                self.assertTrue(Path(probe['history_files'][key])
                                .is_relative_to(self.run_dir), key)
            # The lifecycle actions take the probe containers.
            probe['restart_controller']('standby')
            probe['stop_plant']()
            probe['start_plant']()
            probe['plant_ctl']('list')
            info = probe['start_driven']('active')
            probe['stop_driven']()
            forge = probe['start_forge'](self.document, 'active')
            probe['stop_forge']()
        self.assertEqual(calls[0:2],
                         [('stop', '--time', '2',
                           'dcs-hw-qa-1-probe-b'),
                          ('start', 'dcs-hw-qa-1-probe-b')])
        self.assertEqual(calls[2],
                         ('stop', '--time', '2',
                          'dcs-hw-qa-1-probe-plant'))
        self.assertEqual(calls[3],
                         ('start', 'dcs-hw-qa-1-probe-plant'))
        self.assertEqual(
            calls[4],
            ('exec', 'dcs-hw-qa-1-probe-plant', 'dcs-plant-ctl',
             '127.0.0.1:' + str(self.probe['plant_port']), 'list'))
        self.assertEqual(info['container'], 'dcs-hw-qa-1-probe-d')
        driven = self._launch(calls, 'dcs-hw-qa-1-probe-d')
        self.assertIn('dcs-hw-qa-1-probe-a:8080', driven)
        self.assertIn('dcs-hw-qa-1-probe-plant:'
                      + str(self.probe['plant_port']), driven)
        self.assertIn('--pair-token', driven)
        self.assertIn(self.probe['pair_token'], driven)
        self.assertIn('127.0.0.1:' + str(self.probe['driven_port'])
                      + ':8082', driven)
        forge_launch = self._launch(calls, 'dcs-hw-qa-1-probe-forge')
        self.assertIn('--announce', forge_launch)
        self.assertIn('dcs-hw-qa-1-probe-a:8080', forge_launch)
        self.assertIn('--pair-token', forge_launch)
        self.assertIn(self.probe['pair_token'], forge_launch)
        self.assertTrue(forge['keyed'])
        self.assertEqual(forge['container'], 'dcs-hw-qa-1-probe-forge')
        self.assertIn('/probe-forge/', forge['document'])

    def test_probe_state_file_mount_lever_uses_probe_endpoints(self):
        # The declared mount levers map probe endpoint keys: the
        # probe subject's impede action stages the stall under the
        # probe controller's own state dir; an endpoint the config
        # never declared still refuses.
        self.cfg['state_file_mounts'] = {
            **self.cfg['state_file_mounts'], 'probe_active': 'fifo'}
        probe = self._ctx()['probe']
        self.assertIsNotNone(probe['impede_state_file'])
        directory = self.run_dir / 'controllers' / 'probe-a'
        directory.mkdir(parents=True)
        probe['impede_state_file']('active')
        self.assertTrue(os.path.exists(
            str(directory / runner.STATE_FILE_TMP)))
        with self.assertRaises(RuntimeError) as caught:
            probe['impede_state_file']('standby')
        self.assertIn('probe_standby', str(caught.exception))

    def test_keyed_subject_selects_deployed_or_probe(self):
        # The offline shape the @require-gated legs consume: the
        # deployed pair while the run config keys it, else the staged
        # probe pair.
        ctx = self._ctx()
        self.assertIs(scenarios._keyed_subject(ctx), ctx)
        self.cfg['pair_token'] = None
        ctx = self._ctx()
        self.assertIs(scenarios._keyed_subject(ctx), ctx['probe'])

    def test_wait_monitor_polls_every_staged_monitor(self):
        urls = []

        def fake_http(method, url, body=None, timeout=10):
            urls.append(url)
            return 200, {'role': 'active'}

        with patch.object(runner.scenarios, 'http_json', fake_http):
            self.assertTrue(runner._wait_monitor(
                self.cfg, lambda e, d=None: None))
        ports = {url.rsplit(':', 1)[1].split('/')[0] for url in urls}
        self.assertEqual(ports, {str(self.cfg['active_port']),
                                 str(self.cfg['standby_port']),
                                 str(self.probe['active_port']),
                                 str(self.probe['standby_port'])})

    def test_probe_objects_teardown_with_the_rig(self):
        # Probe containers carry the run labels like the rest of the
        # rig, so the shared label-driven teardown removes them —
        # leftovers become cleanup-ledger failures, never orphans.
        calls = []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            if args[0] == 'ps':
                return Result('pp1 qa-1\npp2 qa-1\npp3 qa-1\n'
                              'aaa qa-1\n')
            if args[:2] == ('network', 'ls'):
                return Result('nnn qa-1\n')
            return Result('')

        with patch.object(runner, 'docker', fake_docker):
            failures = runner._teardown_rig(
                'qa-1', lambda e, d=None: None)
        self.assertEqual(failures, [])
        removed = [c for c in calls if c[0] == 'rm']
        self.assertEqual(len(removed), 4)


if __name__ == '__main__':
    unittest.main()
