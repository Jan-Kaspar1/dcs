"""The 2483_cyclic_exchange_failure_legs leg's scenario unit coverage —
the feed fake and TestCase class for
scenario_cyclic_exchange_failure_legs, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape: the lane's device server serves a
lane-derived **two-station** `sim-cyclic` document, one born seat
stands on it, and the shipped `dcs-sim-bus-ctl` seam queues the four
exchange outcomes the seat's next scans consume — one `miss` whose
held image ages without the scan's outputs moving, two more misses that
reach the declared threshold and escalate every field input, one
`short-station` that degrades only the derived station's point, and one
`complete` that re-enters on the boundary with the counters accounting
for exactly the queued misses. The fault flags stage each named
defect and each named instability, plus the absent capabilities the leg
declines on."""
import json
import tempfile
import unittest
from pathlib import Path

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CyclicExchangeLegsTests.test_registered',
    'CyclicExchangeLegsTests.test_clean_passes_validate_and_tear_down',
    'CyclicExchangeLegsTests.test_two_runs_produce_identical_digests',
    'CyclicExchangeLegsTests.test_a_missed_exchange_moving_the_outputs_fails',
    'CyclicExchangeLegsTests.test_a_missed_exchange_not_counted_fails',
    'CyclicExchangeLegsTests.test_a_skipped_exchange_fails',
    'CyclicExchangeLegsTests.test_a_held_image_that_degraded_early_fails',
    'CyclicExchangeLegsTests.test_a_restamped_held_sample_fails',
    'CyclicExchangeLegsTests.test_an_absent_escalation_fails',
    'CyclicExchangeLegsTests.test_a_partial_escalation_fails',
    'CyclicExchangeLegsTests.test_a_healthy_link_while_missing_fails',
    'CyclicExchangeLegsTests.test_a_misattributed_shortfall_fails',
    'CyclicExchangeLegsTests.test_a_shortfall_never_counted_fails',
    'CyclicExchangeLegsTests.test_an_absent_recovery_fails',
    'CyclicExchangeLegsTests.test_a_stuck_failure_streak_fails',
    'CyclicExchangeLegsTests.test_counter_drift_fails',
    'CyclicExchangeLegsTests.test_an_accumulated_mismatch_fails',
    'CyclicExchangeLegsTests.test_recovery_leaving_points_degraded_fails',
    'CyclicExchangeLegsTests.test_a_single_station_field_is_inconclusive',
    'CyclicExchangeLegsTests.test_a_point_wise_field_is_inconclusive',
    'CyclicExchangeLegsTests.test_an_underivable_document_is_inconclusive',
    'CyclicExchangeLegsTests.test_absent_device_server_is_inconclusive',
    'CyclicExchangeLegsTests.test_an_unsettled_seat_is_inconclusive',
    'CyclicExchangeLegsTests.test_an_unreachable_rig_is_inconclusive',
    'CyclicExchangeLegsTests.test_missing_seams_are_inconclusive',
    'CyclicExchangeLegsTests.test_a_stage_failure_is_nondeterministic',
    'CyclicExchangeLegsTests.test_a_tool_refusal_is_nondeterministic',
    'CyclicExchangeLegsTests.test_a_starved_window_is_nondeterministic',
    'CyclicExchangeLegsTests.test_an_unreadable_point_set_is_nondeterministic',
    'CyclicExchangeLegsTests.test_a_device_that_stopped_serving_is_nondeterministic',
    'CyclicExchangeLegsTests.test_pair_moves_is_nondeterministic',
    'CyclicExchangeLegsTests.test_divergent_digests_are_nondeterministic',
    'CyclicExchangeLegsTests.test_an_unchecked_self_check_fails',
    'CyclicExchangeLegsTests.test_self_check_is_complete',
})


#: The checked-in rig fixture the leg derives from — the pinned
#: `sim-cyclic` document with its one coupler station.
CYCLIC_FIXTURE = {
    'version': 1,
    'devices': [{
        'id': 1, 'kind': 'sim-cyclic',
        'channels': {'di1': {'direction': 'in', 'value_type': 'bool'},
                     'di2': {'direction': 'in', 'value_type': 'bool'},
                     'do1': {'direction': 'out', 'value_type': 'bool'},
                     'do2': {'direction': 'out', 'value_type': 'bool'}},
        'parameters': {'address': '__BUS_ADDR__',
                       'exchange_miss_threshold': 3,
                       'stations': {'750-354': {'di1': 2, 'di2': 3,
                                                'do1': 0, 'do2': 1}}}}],
    'io_points': [{'id': 1, 'direction': 'in', 'value_type': 'bool',
                   'channel': {'device': 1, 'name': 'di1'}},
                  {'id': 2, 'direction': 'in', 'value_type': 'bool',
                   'channel': {'device': 1, 'name': 'di2'}},
                  {'id': 3, 'direction': 'out', 'value_type': 'bool',
                   'channel': {'device': 1, 'name': 'do1'}},
                  {'id': 4, 'direction': 'out', 'value_type': 'bool',
                   'channel': {'device': 1, 'name': 'do2'}}],
    'signals': [], 'components': [], 'connections': []}

#: The declared threshold the fixture carries.
MISS_THRESHOLD = 3
#: The coupler station's input point, and the id the two-station
#: derivation gives the derived station's own point — one above the
#: mounted fixture's maximum.
COUPLER_POINT = 1
DERIVED_STATION = '750-354-b'
DERIVED_POINT = max(point['id'] for point in CYCLIC_FIXTURE['io_points']) + 1


class CtlAnswer:
    """The shipped control tool's own answer: the exit the leg
    classifies, with the printed response and its stderr."""

    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CyclicLegsFeed:
    """A stubbed rig for the cyclic-exchange failure legs.

    The device server serves a lane-derived two-station `sim-cyclic`
    document; the born seat exchanges against it once per scan, and the
    shipped `dcs-sim-bus-ctl` seam queues the outcomes the next scans
    consume. Every fault flag doctors one named defect or one named
    instability, so the leg's own judge must catch it."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-d:5': 'driven'}
    OWNER_TOKEN = 424247

    def __init__(self, model, source):
        self.model = Path(model)
        self.source = Path(source)
        self.deployed = {'active': 900, 'standby': 900}
        self.field = None
        self.seats = {}
        self.queue = []
        self.calls = []
        # The point id the lane's own two-station derivation gives the
        # derived station — the leg derives the served document from the
        # same mounted fixture, so both ends agree on it.
        self.derived_point = DERIVED_POINT
        self.moved = 0        # the field's published-image moves counter
        self.pair_moves = False
        self.pair_moved = False
        # Staging failures and absent surfaces.
        self.stage_fails = False
        self.launch_fails = False
        self.silent_rig = False
        self.no_claim = False
        self.no_source = False
        self.single_station = False
        self.not_cyclic = False
        self.underivable = False
        # The contract defects.
        self.outputs_move = False
        self.uncounted_miss = False
        self.skipped_exchange = False
        self.early_degrade = False
        self.restamped = False
        self.no_escalation = False
        self.partial_escalation = False
        self.link_stays_up = False
        self.misattributed = False
        self.unrecorded_short = False
        self.no_recovery = False
        self.stuck_streak = False
        self.counter_drift = False
        self.accumulated_mismatch = False
        self.degraded_after_recovery = False
        # The instabilities.
        self.tool_refused = False
        self.starves = False
        self.starve_window = False
        self.unreadable_points = False
        self.device_silent = False

    # --- the staged documents ---------------------------------------

    def _write_source(self):
        self.source.parent.mkdir(parents=True, exist_ok=True)
        document = json.loads(json.dumps(CYCLIC_FIXTURE))
        if self.underivable:
            # A document the two-station derivation cannot extend.
            document['devices'][0]['kind'] = 'sim-di'
            document['devices'][0]['parameters'].pop('stations')
        self.source.write_text(json.dumps(document))

    def _write_model(self):
        """The staged document the server serves — the derived
        two-station model with the bridge address bound."""
        self.model.parent.mkdir(parents=True, exist_ok=True)
        document = json.loads(json.dumps(CYCLIC_FIXTURE))
        device = document['devices'][0]
        if self.not_cyclic:
            device['kind'] = 'sim-bus'
        device['parameters']['address'] = 'dcs-hw-qa-1-bus:9005'
        stations = device['parameters']['stations']
        if self.single_station:
            pass
        else:
            device['channels']['di3'] = {'direction': 'in',
                                         'value_type': 'bool'}
            stations[DERIVED_STATION] = {'di3': 4}
            document['io_points'].append(
                {'id': self.derived_point, 'direction': 'in',
                 'value_type': 'bool',
                 'channel': {'device': 1, 'name': 'di3'}})
            document['signals'].append(
                {'id': 10003, 'name': 'rig-di3',
                 'source': self.derived_point, 'unit': '',
                 'description': 'lane-derived second-station input',
                 'group': 'wago-rig'})
        self.model.write_text(json.dumps(document))

    # --- the shipped control tool's seam, faked ---------------------

    def ctl(self, *args):
        self.calls.append(('sim_bus_ctl',) + args)
        command = args[0] if args else ''
        if command == 'script-exchange':
            if self.tool_refused:
                return CtlAnswer('', 1, 'device is busy')
            self.queue.extend(args[1:])
            return CtlAnswer('{\n  "result": "done"\n}')
        if command == 'list':
            if self.device_silent or self.field is None:
                return CtlAnswer('', 1, 'connection refused')
            if self.outputs_move:
                # The image a scan publishes moves: the counter stands
                # for the exchange that moved it, so the two reads the
                # aged window takes disagree.
                self.moved += 1
                return CtlAnswer('{"registers": [{"register": 0, '
                                 '"value": %d}]}' % self.moved)
            return CtlAnswer('{"registers": [{"register": 0, '
                             '"value": 0}]}')
        return CtlAnswer('', 1, 'unknown command ' + repr(command))

    # --- the runner's register-protocol levers, faked ---------------

    def start_device(self, fixture=None, timeout_ms=None):
        self.calls.append(('start_sim_bus_device', fixture))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self._write_model()
        self.field = 'serving'
        self.queue = []
        if self.pair_moves:
            self.pair_moved = True
            self.deployed = {'active': 0, 'standby': 0}
        return {'container': 'dcs-hw-qa-1-bus', 'device': 1, 'port': 9005,
                'address': 'dcs-hw-qa-1-bus:9005',
                'model': str(self.model)}

    def stop_device(self):
        self.calls.append(('stop_sim_bus_device',))
        self.field = None
        self.queue = []

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        assert remote is None, 'the cyclic model carries no --remote'
        assert document == str(self.model), \
            'the seat mounts the device server\'s staged document'
        owns = not self.no_claim
        self.seats[seat] = {
            'seat': seat, 'role': 'active', 'token': self.OWNER_TOKEN,
            'claim': owns, 'tick': 100, 'attempted': 10, 'succeeded': 10,
            'failed': 0, 'mismatches': 0, 'streak': 0,
            'link': 'connected', 'short': None,
            'latched': 100, 'degraded': False}
        return {'container': 'dcs-hw-qa-1-' + seat, 'seat': seat,
                'address': 'dcs-hw-qa-1-' + seat + ':8082',
                'remote': None, 'peer': None, 'standby': None,
                'model': document,
                'monitor': 'http://127.0.0.1:18084'}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': seat in self.seats, 'exit': None,
                'logs': '', 'absent': seat not in self.seats}

    # --- the scans the queued outcomes land on ----------------------

    def _scan(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        peer['tick'] += 1
        if self.skipped_exchange:
            # The scan aborted before its exchange — the shape the
            # aged-not-aborted clause forbids.
            peer['failed'] += 1
            peer['streak'] = max(1, peer['streak'])
            return peer
        peer['attempted'] += 1
        outcome = self.queue.pop(0) if self.queue else 'complete'
        if outcome == 'miss':
            if not self.uncounted_miss:
                peer['failed'] += 1
            peer['streak'] += 1
            if peer['streak'] >= MISS_THRESHOLD and not self.link_stays_up:
                peer['degraded'] = True
                peer['link'] = 'disconnected'
            return peer
        peer['succeeded'] += 1
        # A completed exchange latches the input image at its own tick;
        # a missed one leaves the held image and its stamp alone.
        peer['latched'] = peer['tick']
        peer['streak'] = 0
        if not self.link_stays_up:
            peer['link'] = 'connected'
        peer['degraded'] = False
        if outcome.startswith('short-station:'):
            if not self.unrecorded_short:
                peer['mismatches'] += 1
            peer['short'] = outcome.split(':', 1)[1]
        else:
            peer['short'] = None
        if self.accumulated_mismatch and peer['short'] is None \
                and peer['mismatches']:
            peer['mismatches'] += 1
        if self.counter_drift:
            peer['succeeded'] += 1
        if self.no_recovery:
            peer['link'] = 'disconnected'
            peer['streak'] = 1
            peer['degraded'] = True
        if self.degraded_after_recovery and peer['short'] is None \
                and peer['mismatches']:
            peer['degraded'] = True
        if self.stuck_streak:
            peer['streak'] = 1
        return peer

    def _quality(self, peer, point):
        """The served sample for `point`: Good while the bus is
        exchanging, Bad past the declared threshold or while the point's
        own station is the one the shortfall named."""
        if self.early_degrade and peer.get('failed', 0) >= 1:
            return 'bad'
        if peer.get('short') == DERIVED_STATION and point == 13:
            return 'bad'
        if self.misattributed and peer.get('short'):
            return 'bad'
        if peer.get('degraded') or self.degraded_after_recovery \
                and peer.get('short') is None and False:
            return 'bad'
        if peer.get('degraded'):
            return 'bad'
        if self.no_escalation:
            return 'good'
        return 'good'

    def _io(self, peer):
        exchange = {
            'attempted': peer['attempted'], 'succeeded': peer['succeeded'],
            'working_counter_mismatches': peer['mismatches'],
            'last_exchange_tick': peer['tick'], 'missed_deadlines': 0,
            'buses': [{'device': 1, 'attempted': peer['attempted'],
                       'succeeded': peer['succeeded'],
                       'failed_exchanges': peer['failed'],
                       'link': peer['link']}]}
        return {'failed_exchanges': peer['failed'], 'failed_reads': 0,
                'failed_writes': 0,
                'consecutive_failures': peer['streak'],
                'driver': {'link': peer['link'], 'last_error': None,
                           'exchange': exchange}}

    def _quality(self, peer, point):
        """The served quality for `point`: Good while the bus is
        exchanging, Bad past the declared threshold or while the point's
        own station is the one the shortfall named."""
        if self.early_degrade and peer['failed'] >= 1:
            return 'bad'
        if self.partial_escalation and peer.get('degraded') \
                and point != COUPLER_POINT:
            return 'good'
        if peer['short'] == DERIVED_STATION and point == self.derived_point:
            return 'bad'
        if self.misattributed and peer['short']:
            return 'bad'
        if self.partial_escalation and point != COUPLER_POINT:
            return 'good'
        if peer.get('degraded'):
            if self.no_escalation:
                return 'good'
            return 'bad'
        return 'good'

    def _snapshot(self, seat):
        """One served snapshot: one scan, then the io_health and the
        point samples that scan produced."""
        if self.starve_window and self.queue:
            # The monitor goes quiet with a scripted outcome outstanding:
            # the window the leg waits for is unread, which is the
            # instability its own diagnostic names.
            return None
        peer = self._scan(seat)
        if peer is None:
            return None
        points = []
        for pid in (COUPLER_POINT, 2, self.derived_point):
            if self.unreadable_points and pid == self.derived_point:
                continue
            sample = {'value': False,
                      'quality': self._quality(peer, pid),
                      'tick': peer['latched']}
            if self.restamped and peer['failed'] == 1:
                sample['tick'] = peer['tick']
            points.append({'point': pid, 'sample': sample})
        return {'tick': peer['tick'], 'points': points,
                'io_health': self._io(peer)}

    def _role(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        return {'role': peer['role'], 'tick': peer['tick'],
                'field_claim': 'held' if peer['claim'] else 'unclaimed',
                'sync': None}

    def _deployed(self, host):
        """The deployed pair's own role report — advancing, with the
        peer tracking, unless the leg's staging disturbed them."""
        key = self.HOSTS[host]
        tick = self.deployed[key]
        self.deployed[key] += 1
        role = 'active' if self.pair_moved else (
            'active' if key == 'active' else 'standby')
        report = {'role': role, 'tick': tick}
        if report['role'] != 'active':
            report['sync'] = {'tracking': {'aligned': tick}}
        return report

    # --- the monitor channel — replaces scenarios.http_json ---------

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if host not in self.HOSTS:
            raise urllib.error.URLError('connection refused')
        seat = self.HOSTS[host]
        if seat in ('active', 'standby'):
            if (method, route) == ('GET', '/role'):
                return 200, self._deployed(host)
            raise AssertionError('unexpected request %s %s' % (method, url))
        if seat not in self.seats:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(seat) or {'role': None}
        if (method, route) == ('GET', '/snapshot'):
            served = self._snapshot(seat)
            if served is None:
                raise urllib.error.URLError('connection refused')
            return 200, served
        raise AssertionError('unexpected request %s %s' % (method, url))


class CyclicExchangeLegsTests(unittest.TestCase):
    """The leg's pass, failure, and inconclusive outcomes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.model = self.root / 'sim-bus' / 'model.json'
        # The revision tree the leg resolves the spec's `cyclic_model`
        # against: `src_dir` plus the spec's src-relative path.
        self.source = (self.root / 'src'
                       / 'crates/dcs-demo/fixtures/cyclic.json')
        self.feed = CyclicLegsFeed(self.model, self.source)
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.feed._write_source()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        ctx = {
            'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
            'revised': 'http://ctrl-c:3', 'driven': 'http://ctrl-d:5',
            'evidence_dir': str(self.evidence),
            'sim_bus_device': {
                'device': 1, 'port': 9005,
                'model_fixture': 'crates/dcs-demo/fixtures/pump_station.json',
                'cyclic_model': 'crates/dcs-demo/fixtures/cyclic.json'},
            'sim_bus_ctl': feed.ctl,
            'start_sim_bus_device': feed.start_device,
            'stop_sim_bus_device': feed.stop_device,
            'start_born_controller': feed.start_controller,
            'stop_born_controller': feed.stop_controller,
            'born_controller_state': feed.state,
            'src_dir': str(self.root / 'src'),
        }
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'LEGS_SETTLE_BOUND', 1.0), \
                patch.object(scenarios, 'LEGS_BOUND', 1.0), \
                patch.object(scenarios, 'LEGS_SETTLE_POLL', 0.001), \
                patch.object(scenarios, 'LEGS_POLL', 0.001):
            return scenarios.scenario_cyclic_exchange_failure_legs(ctx)

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('cyclic-exchange-failure-legs-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_cyclic_exchange_failure_legs,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('cyclic-exchange-failure-legs'),
            scenarios.scenario_cyclic_exchange_failure_legs)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_cyclic_exchange_failure_legs)
        self.assertLess(
            order.index(
                scenarios.scenario_sim_bus_scripted_miss_claim_hold), mine)
        self.assertLess(
            mine, order.index(scenarios.scenario_reclaim_convergence_gate))

    def test_clean_passes_validate_and_tear_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = sorted(entry['ref'] for entry in record['evidence'])
        self.assertEqual(
            refs,
            ['evidence/cyclic-exchange-failure-legs-pass-1.json',
             'evidence/cyclic-exchange-failure-legs-pass-2.json'])
        first = self._pass(1)
        self.assertEqual(first['violations'], {})
        self.assertEqual(
            first['digest'],
            {'aged': 'held', 'escalation': 'escalated',
             'attribution': 'named', 'recovery': 're-entered',
             'device': 'serving', 'pair': 'held', 'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The four queued outcomes, in order.
        self.assertEqual(first['script_miss']['outcomes'], ['miss'])
        self.assertEqual(first['script_escalate']['outcomes'],
                         ['miss', 'miss'])
        self.assertEqual(first['script_short']['outcomes'],
                         ['short-station:' + DERIVED_STATION])
        self.assertEqual(first['script_complete']['outcomes'], ['complete'])
        # The derived document is the lane's own, and the staged server
        # serves it with both stations.
        self.assertEqual(first['field']['derived']['station'],
                         DERIVED_STATION)
        self.assertEqual(first['field']['stations'],
                         ['750-354', DERIVED_STATION])
        self.assertEqual(first['field']['mounted'], str(self.model))
        # Each pass ends torn down.
        self.assertEqual(self.feed.seats, {})
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_sim_bus_device'), 2)
        self.assertEqual(kinds.count('stop_sim_bus_device'), 2)
        self.assertEqual(kinds.count('start_born_controller'), 2)
        self.assertEqual(kinds.count('stop_born_controller'), 2)

    def test_two_runs_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])

    def _fails(self, diagnostic):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn(diagnostic, record['detail'])

    def _set(self, **flags):
        for name, value in flags.items():
            setattr(self.feed, name, value)
        return self.run_scenario()

    def test_a_missed_exchange_moving_the_outputs_fails(self):
        self._set(outputs_move=True)
        record = self._pass(1)
        self.assertIn('aged-moved-outputs', record['violations'])

    def test_a_missed_exchange_not_counted_fails(self):
        self._set(uncounted_miss=True)
        # The miss is queued but its counter never advances, so the
        # window cannot find the boundary the contract counts.
        self.assertEqual(self._pass(1)['violations'].get('aged-count'),
                         'cyclic-exchange-legs-failed')

    def test_a_skipped_exchange_fails(self):
        self._set(skipped_exchange=True)
        self.assertIn('aged-not-attempted', self._pass(1)['violations'])

    def test_a_held_image_that_degraded_early_fails(self):
        self._set(early_degrade=True)
        self.assertIn('aged-hold', self._pass(1)['violations'])

    def test_a_restamped_held_sample_fails(self):
        self._set(restamped=True)
        self.assertIn('aged-restamped', self._pass(1)['violations'])

    def test_an_absent_escalation_fails(self):
        self._set(no_escalation=True)
        self.assertIn('no-escalation', self._pass(1)['violations'])

    def test_a_partial_escalation_fails(self):
        self._set(partial_escalation=True)
        self.assertIn('partial-escalation', self._pass(1)['violations'])

    def test_a_healthy_link_while_missing_fails(self):
        self._set(link_stays_up=True)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)

    def test_a_misattributed_shortfall_fails(self):
        self._set(misattributed=True)
        self.assertIn('station-misattributed',
                      self._pass(1)['violations'])

    def test_a_shortfall_never_counted_fails(self):
        self._set(unrecorded_short=True)
        self.assertIn('attribution-count', self._pass(1)['violations'])

    def test_an_absent_recovery_fails(self):
        self._set(no_recovery=True)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)

    def test_a_stuck_failure_streak_fails(self):
        self._set(stuck_streak=True)
        self.assertIn('streak-not-cleared', self._pass(1)['violations'])

    def test_counter_drift_fails(self):
        self._set(counter_drift=True)
        self.assertIn('counter-drift', self._pass(1)['violations'])

    def test_an_accumulated_mismatch_fails(self):
        self._set(accumulated_mismatch=True)
        self.assertIn('mismatch-accumulated', self._pass(1)['violations'])

    def test_recovery_leaving_points_degraded_fails(self):
        self._set(degraded_after_recovery=True)
        self.assertIn('recovery-degraded', self._pass(1)['violations'])

    def test_a_single_station_field_is_inconclusive(self):
        self.feed.single_station = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('station', record['detail'])

    def test_a_point_wise_field_is_inconclusive(self):
        self.feed.not_cyclic = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_an_underivable_document_is_inconclusive(self):
        self.feed.underivable = True
        self.feed._write_source()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_absent_device_server_is_inconclusive(self):
        ctx = self._ctx()
        ctx['sim_bus_device'] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sim_bus_device', record['detail'])

    def test_an_unsettled_seat_is_inconclusive(self):
        self.feed.no_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_an_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_missing_seams_are_inconclusive(self):
        for key in ('start_sim_bus_device', 'sim_bus_ctl', 'evidence_dir',
                    'stop_born_controller'):
            ctx = self._ctx()
            ctx[key] = None
            record = self.run_scenario(ctx)
            self.assertEqual(record['outcome'], 'inconclusive', record)
            self.assertIn(key, record['detail'])

    def test_a_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_a_tool_refusal_is_nondeterministic(self):
        self.feed.tool_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_a_starved_window_is_nondeterministic(self):
        self.feed.starve_window = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_an_unreadable_point_set_is_nondeterministic(self):
        # The attributed window carries no sample for one of the two
        # stations: the leg cannot tell attribution from a blur.
        self.feed.unreadable_points = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_a_device_that_stopped_serving_is_nondeterministic(self):
        self.feed.device_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_pair_moves_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-nondeterministic',
                      record['detail'])

    def test_divergent_digests_are_nondeterministic(self):
        # The second pass's staged document drops the derived station, so
        # the two passes cannot agree on a digest.
        original = self.feed._write_model

        def drifting():
            if len([call for call in self.feed.calls
                    if call[0] == 'start_sim_bus_device']) > 1:
                self.feed.single_station = True
            return original()

        self.feed._write_model = drifting
        record = self.run_scenario()
        self.assertIn(record['outcome'], ('failed', 'inconclusive'))

    def test_an_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_legs_self_check',
                          lambda: ['planted-negative']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-exchange-legs-unchecked', record['detail'])

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._legs_self_check(), [])