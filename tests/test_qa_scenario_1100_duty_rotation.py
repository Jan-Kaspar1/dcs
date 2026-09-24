"""The 1100_duty_rotation leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_duty_rotation, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DutyRotationTests.test_registered_in_scenarios',
    'DutyRotationTests.test_clean_feed_passes_and_validates',
    'DutyRotationTests.test_two_runs_produce_identical_evidence',
    'DutyRotationTests.test_mirrored_layout_passes',
    'DutyRotationTests.test_no_active_peer_fails',
    'DutyRotationTests.test_unconverged_pair_is_inconclusive',
    'DutyRotationTests.test_missing_wiring_is_inconclusive',
    'DutyRotationTests.test_undeclared_inflow_is_inconclusive',
    'DutyRotationTests.test_level_never_rising_is_inconclusive',
    'DutyRotationTests.test_duty_never_moving_is_inconclusive',
    'DutyRotationTests.test_group_never_staging_fails',
    'DutyRotationTests.test_holder_never_stopping_fails',
    'DutyRotationTests.test_rotation_never_alternating_fails',
    'DutyRotationTests.test_holdout_never_banked_fails',
    'DutyRotationTests.test_run_hours_never_banked_fails',
    'DutyRotationTests.test_promoted_peer_resetting_fails',
    'DutyRotationTests.test_promotion_never_settling_fails',
    'DutyRotationTests.test_restore_never_completing_fails',
    'DutyRotationTests.test_journal_never_recording_fails',
    'DutyRotationTests.test_settlements_never_journaling_fails',
    'DutyRotationTests.test_unack_latch_never_clearing_fails',
})


class RotationPlant:
    """The field half of the duty-rotation rig: the lane dynamics'
    ambient inflow — the net-flow sum's declared bias — integrated
    against the pump draws, plus the field command points the
    field-owning peer writes. One owner scan steps one tick of process
    time."""

    START = 2.0      # the demand chain's asserted bound
    STOP = 1.0       # the demand chain's released bound

    def __init__(self):
        self.level = 0.8
        self.ambient = 0.02     # metres per owner scan, unopposed
        self.draw = 0.16        # metres per commanded pump per scan
        self.inflow_declared = True
        self.never_rises = False    # the declared inflow never reaches
        self.cmd = {100: False, 101: False}

    def step(self):
        net = self.ambient if self.inflow_declared else 0.0
        for running in self.cmd.values():
            if running:
                net -= self.draw
        if not self.never_rises:
            self.level += net


class RotationLine:
    """One peer's checkpointed executor state: the pump-group's
    rotation position, timers, accumulators, and command flags, the
    chain's held demand, the writable inputs, the none-available
    alarm's latch, and the served output image (`values`)."""

    JOURNALED = (40, 41, 217, 302, 334, 1033, 1034)

    def __init__(self):
        self.duty_index = None      # 0 | 1 | None
        self.cursor = 0             # 0-based, mirrors rotation_cursor-1
        self.run_hours = [0, 0]
        self.held_until = [0, 0]
        self.commanded = [False, False]
        self.last_demand = 0
        self.last_start = None
        self.demand = 0
        self.oos = [False, False]
        self.ack = False
        self.alarm_ticks = 0
        self.unack = False
        self.values = {}            # served output point -> raw value

    def pump_state(self):
        """The pump-group's checkpointed StateMap — the carried
        vocabulary the scenario reads back."""
        return {
            'rotation': {'int': 0},
            'min_off_ticks': {'int': 2},
            'start_delay_ticks': {'int': 1},
            'duty': {'int': 0 if self.duty_index is None
                     else self.duty_index + 1},
            'rotation_cursor': {'int': self.cursor + 1},
            'last_demand': {'int': self.last_demand},
            'run_hours_1': {'int': self.run_hours[0]},
            'run_hours_2': {'int': self.run_hours[1]},
            'held_until_1': {'int': self.held_until[0]},
            'held_until_2': {'int': self.held_until[1]},
            'commanded_1': {'bool': self.commanded[0]},
            'commanded_2': {'bool': self.commanded[1]}}


class RotationPeer:
    """One endpoint of the rotation pair: role, tracking posture, the
    adopted/executed line, its journal, and its receipt log."""

    def __init__(self, name):
        self.name = name
        self.tick = 0
        self.role = 'standby'    # active | standby | demoting | promoting
        self.tracking = False
        self.line = RotationLine()
        self.journal = []
        self.next_seq = 1
        self.receipts = []


class RotationPair:
    """A stubbed redundant pair plus its simulated station for the
    duty-rotation scenario: two monitor endpoints over one plant.
    Every call on the field-owning peer is one completed scan — the
    plant steps, the chain holds demand between `start` and `stop`,
    and the group alternates duty, banks the declared holdout, and
    accrues run-hours exactly as the block does. Calls on the tracking
    peer adopt the owner's published line — the checkpoint pull — so
    its snapshot and checkpoint serve what the owner writes. Fault
    flags stage each named failure the issue calls out."""

    SIGNALS = [
        {'point': 12, 'name': 'inflow', 'direction': 'in',
         'value_type': 'float', 'writable': False},
        {'point': 40, 'name': 'p101-run', 'direction': 'in',
         'value_type': 'bool', 'writable': False},
        {'point': 41, 'name': 'p102-run', 'direction': 'in',
         'value_type': 'bool', 'writable': False},
        {'point': 100, 'name': 'p101-cmd', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 101, 'name': 'p102-cmd', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 200, 'name': 'level-selected', 'direction': 'out',
         'value_type': 'float', 'writable': False},
        {'point': 205, 'name': 'demand-in', 'direction': 'in',
         'value_type': 'int', 'writable': False},
        {'point': 210, 'name': 'duty', 'direction': 'out',
         'value_type': 'int', 'writable': False},
        {'point': 211, 'name': 'staged', 'direction': 'out',
         'value_type': 'int', 'writable': False},
        {'point': 217, 'name': 'none-available', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 302, 'name': 'p101-oos', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 334, 'name': 'p102-oos', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 1030, 'name': 'none-available-ack', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 1034, 'name': 'none-available-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.a = RotationPeer('a')
        self.b = RotationPeer('b')
        self.a.role = 'active'
        self.b.role = 'standby'
        self.b.tracking = True
        self.owner = 'a'
        self.switched_once = False
        # Fault injection for the named-failure cases.
        self.no_active = False       # no peer ever reports active
        self.no_tracking = False     # the standby never converges
        self.bare_signals = False    # the leg's wiring is absent
        self.duty_mute = False       # the duty output never moves
        self.never_stages = False    # the group never stages a pump
        self.never_stops = False     # a staged pump never de-stages
        self.no_alternate = False    # the cursor never advances
        self.no_holdout = False      # the stop banks no holdout
        self.peer_resets = False     # the promoted peer reinitializes
        self.run_hours_frozen = False  # run-hours never accrue
        self.promote_never = False   # every promote is refused
        self.restore_never = False   # the fail-back's promote refuses
        self.no_journal = False      # transitions never journal
        self.no_receipts = False     # settlements never journal
        self.ack_stuck = False       # the unack latch never clears

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _other(self, peer):
        return self.b if peer.name == 'a' else self.a

    def _mark(self, peer, event):
        if not self.no_journal:
            peer.journal.append({'seq': peer.next_seq,
                                 'tick': peer.tick, 'event': event})
            peer.next_seq += 1

    def _drive(self, peer, point, value):
        """A journaled output/input transition, mirroring the real
        driver's point_changed on declared-journaled bool points."""
        previous = peer.line.values.get(point)
        peer.line.values[point] = value
        if previous != value and point in RotationLine.JOURNALED:
            self._mark(peer, {'point_changed': {
                'point': point,
                'from': None if previous is None
                else {'bool': previous},
                'to': {'bool': value}}})

    def _assign(self, line):
        avail = [not held for held in line.oos]
        line.duty_index = None
        for offset in range(2):
            idx = (line.cursor + offset) % 2
            if avail[idx]:
                line.duty_index = idx
                break
        if line.duty_index is not None and not self.no_alternate:
            line.cursor = (line.duty_index + 1) % 2

    def _settle(self, peer):
        for receipt in peer.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted is None or peer.tick < accepted['apply_tick']:
                continue
            write = receipt['command']['write_value']
            value = write['value']['bool']
            if write['point'] == 302:
                peer.line.oos[0] = value
                self._drive(peer, 302, value)
            elif write['point'] == 334:
                peer.line.oos[1] = value
                self._drive(peer, 334, value)
            elif write['point'] == 1030:
                peer.line.ack = value
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            if not self.no_receipts:
                self._mark(peer, {'command_settled':
                                  {'receipt': dict(receipt)}})

    def _execute(self, peer):
        """One owner scan of the threshold chain plus the pump group,
        mirroring the real blocks' step ordering."""
        line, tick = peer.line, peer.tick
        if self.plant.level >= RotationPlant.START:
            line.demand = 1
        elif self.plant.level < RotationPlant.STOP:
            line.demand = 0
        avail = [not held for held in line.oos]
        if line.duty_index is not None and not avail[line.duty_index]:
            self._assign(line)
        elif line.last_demand >= 1 and line.demand == 0:
            self._assign(line)
        if line.duty_index is None:
            self._assign(line)
        lead = line.duty_index if line.duty_index is not None \
            else line.cursor
        targets = []
        if not self.never_stages:
            for offset in range(2):
                if len(targets) >= line.demand:
                    break
                idx = (lead + offset) % 2
                if avail[idx] and (line.commanded[idx]
                                   or tick >= line.held_until[idx]):
                    targets.append(idx)
        new = [False, False]
        for idx in targets:
            if line.commanded[idx]:
                new[idx] = True
            elif line.last_start is None \
                    or tick >= line.last_start + 1:
                new[idx] = True
                line.last_start = tick
        if self.never_stops:
            new = [old or on for old, on in zip(line.commanded, new)]
        for idx in range(2):
            if line.commanded[idx] and not new[idx]:
                line.held_until[idx] = tick if self.no_holdout \
                    else tick + 2
            line.commanded[idx] = new[idx]
            self.plant.cmd[100 + idx] = new[idx]
            self._drive(peer, 40 + idx, new[idx])
            if new[idx] and not self.run_hours_frozen:
                line.run_hours[idx] += 1
        line.last_demand = line.demand
        line.values[210] = 0 if self.duty_mute else (
            0 if line.duty_index is None else line.duty_index + 1)
        line.values[211] = sum(1 for on in new if on)
        line.values[205] = line.demand
        nav = not any(avail)
        self._drive(peer, 217, nav)
        line.alarm_ticks = line.alarm_ticks + 1 if nav else 0
        self._drive(peer, 1033, line.alarm_ticks >= 5)
        latched = line.unack or line.alarm_ticks >= 5
        line.unack = latched if self.ack_stuck \
            else latched and not line.ack
        self._drive(peer, 1034, line.unack)

    def _advance(self, peer):
        """One completed scan on `peer`: pending role transitions
        settle, due receipts apply and journal, and the peer either
        executes the field-owning line or adopts the other peer's —
        the announced/configured checkpoint pull."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True
            if self.owner == peer.name:
                self.owner = None
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            self.owner = peer.name
            self.switched_once = True
            if self.peer_resets:
                peer.line = RotationLine()
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        self._settle(peer)
        if peer.name == self.owner:
            self.plant.step()
            self._execute(peer)
        elif peer.role == 'standby' and peer.tracking:
            peer.line = copy.deepcopy(self._other(peer).line)

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _sample(self, peer, point, value, kind):
        return {'point': point, 'direction': 'in',
                'sample': {'value': {kind: value},
                           'quality': 'good', 'tick': peer.tick}}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance(peer)
        line = peer.line
        if (method, route) == ('GET', '/role'):
            role = peer.role
            if peer.name == 'a' and self.no_active:
                role = 'standby'
            report = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                report['sync'] = {'tracking': {'aligned': peer.tick}} \
                    if peer.tracking and not self.no_tracking \
                    else {'unsynchronized': {}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {
                'points': [{'point': 10, 'name': 'level-primary',
                            'direction': 'in', 'value_type': 'float',
                            'writable': False}]
                if self.bare_signals else self.SIGNALS,
                'components': []}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                # The inflow channel is undriven — the dynamics declare
                # the ambient inflow inside the net-flow sum, so point
                # 12 serves its unwritten zero.
                self._sample(peer, 12, 0.0, 'float'),
                self._sample(peer, 40, line.values.get(40, False),
                             'bool'),
                self._sample(peer, 41, line.values.get(41, False),
                             'bool'),
                self._sample(peer, 100, self.plant.cmd[100], 'bool'),
                self._sample(peer, 101, self.plant.cmd[101], 'bool'),
                self._sample(peer, 200, self.plant.level, 'float'),
                self._sample(peer, 205, line.values.get(205, 0),
                             'int'),
                self._sample(peer, 210, line.values.get(210, 0),
                             'int'),
                self._sample(peer, 211, line.values.get(211, 0),
                             'int'),
                self._sample(peer, 217, line.values.get(217, False),
                             'bool'),
                self._sample(peer, 302, line.oos[0], 'bool'),
                self._sample(peer, 334, line.oos[1], 'bool'),
                self._sample(peer, 1030, line.ack, 'bool'),
                self._sample(peer, 1033, line.values.get(1033, False),
                             'bool'),
                self._sample(peer, 1034, line.unack, 'bool')]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'tick': peer.tick, 'components':
                         {'pump-group:3': line.pump_state()}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            if not write or write.get('point') not in (302, 334, 1030):
                return 200, {'command': (body or {}).get('command'),
                             'outcome': {'rejected': {'reason': {
                                 'not_writable': {
                                     'point': (write or {})
                                     .get('point')}}}},
                             'actor': (body or {}).get('actor')}
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}},
                       'actor': body.get('actor')}
            peer.receipts.append(receipt)
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if self.promote_never \
                    or (self.restore_never and self.switched_once) \
                    or not peer.tracking:
                self._raise(409, {'not_converged':
                                  {'sync': {'unsynchronized': {}}}})
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s' % (method, url))


class DutyRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = RotationPlant()
        self.pair = RotationPair(self.plant)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.pair.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ROTATION_POLL', 0.001), \
                patch.object(scenarios, 'ROTATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_RISE_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_CYCLE_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_SWITCH_DEADLINE', 2.0):
            return scenarios.scenario_duty_rotation(ctx)

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The restored pre-switch window behind the dead-peer case —
        # the settled tracking pair ahead of the force case's legs,
        # with the monitor-starvation leg sharing the window in front.
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_duty_rotation))
        self.assertEqual(
            order.index(scenarios.scenario_duty_rotation) + 1,
            order.index(scenarios.scenario_pump_out_of_service))
        self.assertEqual(
            order.index(scenarios.scenario_pump_out_of_service) + 1,
            order.index(scenarios.scenario_force_carryover))
        self.assertIs(verify.case_function('duty-rotation'),
                      scenarios.scenario_duty_rotation)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Every writable point and the pair's roles restored.
        self.assertEqual(self.pair.a.line.oos, [False, False])
        self.assertFalse(self.pair.a.line.ack)
        self.assertEqual(self.pair.a.role, 'active')
        self.assertEqual(self.pair.b.role, 'standby')
        self.assertTrue(self.pair.b.tracking)
        self.assertEqual(self.plant.cmd, {100: False, 101: False})
        notes = ' '.join(record['observations'])
        self.assertIn('the rotation alternated', notes)
        self.assertIn('the rotation position carried', notes)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {path.name: path.read_bytes()
                 for path in self.evidence.iterdir()}
        pair, plant = self.pair, self.plant
        self.pair, self.plant = RotationPair(RotationPlant()), None
        try:
            second_run = self.run_scenario()
        finally:
            self.pair, self.plant = pair, plant
        self.assertEqual(second_run['outcome'], 'passed', second_run)
        second = {path.name: path.read_bytes()
                  for path in self.evidence.iterdir()}
        self.assertEqual(first, second)

    def test_mirrored_layout_passes(self):
        # The post-failover layout: ctrl-b owns the field, ctrl-a
        # follows — the same legs mirrored, restored b-active.
        self.pair.a.role = 'standby'
        self.pair.a.tracking = True
        self.pair.b.role = 'active'
        self.pair.owner = 'b'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.pair.b.role, 'active')
        self.assertEqual(self.pair.a.role, 'standby')
        report.validate_scenario(record)

    def test_no_active_peer_fails(self):
        self.pair.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unconverged_pair_is_inconclusive(self):
        self.pair.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported tracking convergence',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.pair.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('lacks the rotation leg', record.get('detail', ''))
        report.validate_scenario(record)

    def test_undeclared_inflow_is_inconclusive(self):
        # No ambient inflow declared in the net-flow sum: the held
        # level never rises unopposed past start.
        self.plant.inflow_declared = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('rotation-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('never raised the level', record.get('detail', ''))
        report.validate_scenario(record)

    def test_level_never_rising_is_inconclusive(self):
        self.plant.never_rises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('rotation-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('never raised the level',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_never_moving_is_inconclusive(self):
        # The staged command reaches the field but the served duty
        # output never moves off zero — the rig cannot prove rotation.
        self.pair.duty_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('duty never moved off', record.get('detail', ''))
        report.validate_scenario(record)

    def test_group_never_staging_fails(self):
        self.pair.never_stages = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('never staged a duty pump',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_holder_never_stopping_fails(self):
        self.pair.never_stops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('never stopped at stop', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rotation_never_alternating_fails(self):
        self.pair.no_alternate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('did not rotate', record.get('detail', ''))
        report.validate_scenario(record)

    def test_holdout_never_banked_fails(self):
        self.pair.no_holdout = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('does not extend past the journaled stop',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_run_hours_never_banked_fails(self):
        self.pair.run_hours_frozen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('no run-hours banked', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_resetting_fails(self):
        # The promoted peer reinitializes instead of adopting the
        # checkpoint — duty, the in-flight command, and the banked
        # run-hours all reset.
        self.pair.peer_resets = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('lost the rotation position',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_never_settling_fails(self):
        self.pair.promote_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promotion never settled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_restore_never_completing_fails(self):
        self.pair.restore_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not restored', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_never_recording_fails(self):
        self.pair.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('no journaled run-contact stop',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_settlements_never_journaling_fails(self):
        self.pair.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled evidence missing',
                      record.get('detail', ''))
        self.assertIn('no settled receipt journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unack_latch_never_clearing_fails(self):
        self.pair.ack_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unacknowledged latch never cleared',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
