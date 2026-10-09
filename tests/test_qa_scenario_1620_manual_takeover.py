"""The 1620_manual_takeover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_manual_takeover, following the
per-leg split convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class TakeoverPlantPeer(FakePlantPeer):
    """The takeover rig's plant half: run contacts 40/41 loop back the
    driven commands, the thermal contacts 60/61 ride the plant's
    stored bool, and FakePlantPeer's quality substitution carries the
    injected bad:device_fault the asserted-or-untrusted clause trips
    the interlock on."""

    def __init__(self):
        super().__init__()
        for point in (41, 60, 61):
            self.samples[point] = {'value': {'bool': False},
                                   'quality': 'good', 'tick': 0}


class TakeoverFeed:
    """A stubbed monitor pair for the manual-takeover scenario:
    ctrl-a runs a tiny executor over the rig's writable
    mode/hand/oos points, the `((group-cmd and auto) or (hand and
    mode and the held protection set)) and protections-ok` motor
    request, the interlock's asserted-or-untrusted contact trips and
    its in-service permissive, the min_off_ticks hand-leg holdout,
    the pump group's exclusion/staging semantics, and the per-pump
    fault and thermal managed alarms; ctrl-b only reports its
    tracking standby role. Every `http_json` call is one completed
    scan — the internal carriers deliver last scan's image, so the
    exclusion, the holdout re-arm, and the delivered suppression copy
    each land their own hop exactly like the deployed model.
    Declared-journaled points record point_changed and every
    accepted command settles its receipt at the next scan's
    boundary. Fault flags stage each named failure the issue calls
    out."""

    LEVEL, NET, INFLOW = 200, 13, 12
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED, BELOW = 210, 211, 214
    START_DELAY, MIN_OFF, MOTOR_FAULT_TICKS = 1, 2, 2
    CUTOFF, STOP, START, LAG_START = 0.5, 1.0, 2.0, 3.0
    # Slow dynamics: both pumps running drifts the level ~0.04/scan
    # so the whole leg sequence stays clear of the dry-run clause —
    # a served protect-tripped then names its real cause.
    BIAS, DRAW = 0.2, 0.12
    JOURNALED = (40, 41, 214, 300, 302, 312, 316, 318, 328,
                 332, 334, 344, 348, 350, 360,
                 1073, 1074, 1076, 1077, 1083, 1084,
                 1103, 1104, 1106, 1107, 1113, 1114)
    WRITABLE = (300, 301, 302, 332, 333, 334, 1080, 1110)

    @staticmethod
    def base(index):
        """The 0-based pump's internal point base — 300 + 32*i."""
        return 300 + 32 * index

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []
        self.level = 2.2
        self.demand_held = 0
        self.duty_index = None
        self.cursor = 0
        self.last_demand = 0
        self.commanded = [False, False]
        self.held_until = [0, 0]
        self.last_start = None
        self.disagree = [0, 0]
        self.ok_streak = [0, 0]      # protections-ok standing scans
        self.alarms = {1070: {'state': False, 'latched': False},
                       1100: {'state': False, 'latched': False},
                       1080: {'state': False, 'latched': False},
                       1110: {'state': False, 'latched': False}}
        self.values = {200: 2.2, 13: self.BIAS, 12: 0.0,
                       204: 0, 205: 0, 210: 0, 211: 0, 214: False}
        for index in (0, 1):
            base = self.base(index)
            self.values.update({
                40 + index: False, 60 + index: False,
                100 + index: False,
                base: False, base + 1: False, base + 2: False,
                base + 3: False, base + 5: True,
                base + 12: False, base + 16: False,
                base + 18: True, base + 24: True,
                base + 26: True, base + 28: True,
                base + 30: False, base + 31: False})
            for point in (1070, 1073, 1074, 1076, 1077,
                          1080, 1083, 1084):
                self.values[point + 30 * index] = False
        self.jseen = {}
        # Fault injection for the named-failure cases.
        self.no_active = False           # ctrl-a never reports active
        self.no_tracking = False         # the peer never tracks
        self.bare_signals = False        # the leg's wiring absent
        self.auto_sticks = False         # the auto leg never drops
        self.hand_never_runs = False     # the hand leg never drives
        self.protection_mute = False     # the contact never trips
        self.thermal_mute = False        # the thermal alarm's `in`
                                         # never reads the trip
        self.never_rearms = False        # the cleared command never
                                         # re-asserts
        self.oos_mute = False            # the oos permissive never
                                         # drops the interlock
        self.managed_mute = False        # the fault alarm's managed
                                         # flags never assert
        self.step_on_rejoin = False      # cmd steps off the group
                                         # request on rejoin
        self._saw_inhibit = False        # the oos hold has stood
        self.latch_sticks = False        # the ack never clears the
                                         # latch
        self.no_journal = False          # point_changed never lands
        self.no_receipts = False         # command_settled never lands
        self.write_refused = False       # the command path rejects
        self.role_moves = False          # the pair's roles move
        self.flags_unserved = False      # the managed flag points
                                         # absent

    @staticmethod
    def _wrap(value):
        if isinstance(value, bool):
            return {'bool': value}
        if isinstance(value, int):
            return {'int': value}
        return {'float': value}

    def _entry(self, event):
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})
        self.seq += 1

    def _journal_values(self):
        for point in self.JOURNALED:
            value = self.values[point]
            previous = self.jseen.get(point)
            if point in self.jseen and previous == value:
                continue
            self.jseen[point] = value
            if not self.no_journal:
                self._entry({'point_changed': {
                    'point': point,
                    'from': None if previous is None
                    else self._wrap(previous),
                    'to': self._wrap(value)}})

    def _thermal_trip(self, index):
        """The asserted-or-untrusted contact clause: a True read or a
        non-Good quality trips the pump's thermal input."""
        served = self.plant.served(60 + index)
        return self.protection_mute is not True and (
            served['value'].get('bool') is True
            or served.get('quality') != 'good')

    def _effective(self, index):
        return bool(self.values[self.base(index) + 28]) \
            and not bool(self.values[self.base(index) + 12])

    def _assign(self, effective):
        for offset in range(2):
            index = (self.cursor + offset) % 2
            if effective[index]:
                self.duty_index = index
                self.cursor = (index + 1) % 2
                return
        self.duty_index = None

    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            self.values[write['point']] = write['value']['bool']
            if not self.no_receipts:
                self._entry({'command_settled':
                             {'receipt': dict(receipt)}})
        # The internal carriers: the delivered suppression copy and
        # the demand input hop last scan's image.
        for index in (0, 1):
            base = self.base(index)
            self.values[base + 31] = self.values[base + 30]
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        # The combinational layer per pump: the auto inversion, the
        # declared protections, the holdout, the two motor-request
        # legs, the availability aggregate, and the suppression copy.
        for index in (0, 1):
            base = self.base(index)
            thermal_trip = self._thermal_trip(index)
            self.values[base + 24] = not thermal_trip
            permissive = not bool(self.values[base + 2])
            tripped = thermal_trip or bool(self.values[self.BELOW]) \
                or (not permissive and not self.oos_mute)
            self.values[base + 16] = bool(tripped)
            self.values[base + 18] = not tripped
            self.ok_streak[index] = self.ok_streak[index] + 1 \
                if not tripped else 0
            auto = not bool(self.values[base])
            if self.auto_sticks:
                auto = True
            self.values[base + 5] = auto
            hand_armed = self.ok_streak[index] >= self.MIN_OFF \
                and not self.never_rearms
            hand_leg = bool(self.values[base + 1]) \
                and bool(self.values[base]) and hand_armed
            if self.hand_never_runs:
                hand_leg = False
            auto_leg = bool(self.values[base + 3]) and auto
            drive = (auto_leg or hand_leg) and not tripped
            self.values[100 + index] = drive
            self._saw_inhibit = self._saw_inhibit \
                or bool(self.values[base + 2])
            self.values[base + 30] = bool(self.values[base + 2])
            self.values[base + 28] = auto and permissive \
                and self.values[base + 24] \
                and bool(self.values[base + 26])
        # The delivered command loopback and the motor's fault
        # accounting.
        for index in (0, 1):
            drive = bool(self.values[100 + index])
            self.plant.samples[40 + index]['value'] = {'bool': drive}
            served = self.plant.served(40 + index)
            run = served['value'].get('bool')
            self.values[40 + index] = bool(run)
            good = served.get('quality') == 'good'
            agree = run == drive and good
            self.disagree[index] = 0 if agree \
                else self.disagree[index] + 1
            self.values[self.base(index) + 12] = \
                self.disagree[index] >= self.MOTOR_FAULT_TICKS
        # The pump group: effective availability, duty, staging, and
        # the per-pump request carriers.
        effective = [self._effective(index) for index in (0, 1)]
        if self.duty_index is not None \
                and not effective[self.duty_index]:
            self._assign(effective)
        elif self.last_demand >= 1 \
                and self.values[self.DEMAND_IN] == 0:
            self._assign(effective)
        if self.duty_index is None:
            self._assign(effective)
        self.values[self.DUTY] = 0 if self.duty_index is None \
            else self.duty_index + 1
        lead = self.duty_index if self.duty_index is not None \
            else self.cursor
        demand = self.values[self.DEMAND_IN]
        demand = demand if isinstance(demand, int) \
            and not isinstance(demand, bool) else 0
        targets = []
        for offset in range(2):
            if len(targets) >= demand:
                break
            index = (lead + offset) % 2
            if effective[index] and (self.commanded[index]
                                     or self.tick
                                     >= self.held_until[index]):
                targets.append(index)
        group_cmds = [index in targets for index in (0, 1)]
        for index in range(2):
            if self.commanded[index] and not group_cmds[index]:
                self.held_until[index] = self.tick + self.MIN_OFF
            self.commanded[index] = group_cmds[index]
            self.values[self.base(index) + 3] = group_cmds[index]
        if self.step_on_rejoin and self._saw_inhibit:
            # The named failure: a restored pump steps its delivered
            # command opposite the group's request — the no-step
            # contract's served image is cmd != group-cmd.
            for index in range(2):
                base = self.base(index)
                if not (self.values[base] or self.values[base + 1]
                        or self.values[base + 2]):
                    self.values[100 + index] = not group_cmds[index]
        self.values[self.STAGED] = sum(
            bool(self.values[100 + index]) for index in (0, 1))
        self.last_demand = demand
        # The threshold chain on the integrated level.
        if self.level <= self.STOP:
            self.demand_held = 0
        elif self.demand_held == 2 and self.level <= self.START:
            self.demand_held = 1
        elif self.level >= self.LAG_START:
            self.demand_held = 2
        elif self.demand_held == 0 and self.level >= self.START:
            self.demand_held = 1
        self.values[self.DEMAND] = self.demand_held
        self.values[self.BELOW] = self.level <= self.CUTOFF
        net = self.BIAS - self.DRAW * sum(
            bool(self.values[100 + index]) for index in (0, 1))
        self.values[self.NET] = net
        # The managed alarms: the fault alarm's `in` the proven flag
        # with oos/suppress declared, the thermal alarm's `in` the
        # contact guard's asserted-or-untrusted trip.
        inputs = {}
        for index in (0, 1):
            inputs[1070 + 30 * index] = \
                self.values[self.base(index) + 12]
            inputs[1080 + 30 * index] = \
                False if self.thermal_mute \
                else self._thermal_trip(index)
        for ack_point, inp in inputs.items():
            alarm = self.alarms[ack_point]
            fresh = bool(inp) and not alarm['state']
            alarm['state'] = bool(inp)
            alarm['latched'] = (alarm['latched'] or fresh) \
                and not self.values[ack_point] \
                and not self.latch_sticks
            self.values[ack_point + 3] = alarm['state']
            self.values[ack_point + 4] = alarm['latched']
            if ack_point in (1070, 1100):
                index = 0 if ack_point == 1070 else 1
                base = self.base(index)
                self.values[ack_point + 6] = \
                    bool(self.values[base + 31]) \
                    if not self.managed_mute else False
                self.values[ack_point + 7] = \
                    bool(self.values[base + 2]) \
                    if not self.managed_mute else False
        self._journal_values()
        self.level += net
        self.values[self.LEVEL] = self.level

    def _signals(self):
        def sig(point, name, direction='out', value_type='bool',
                writable=False):
            return {'point': point, 'signal': 10000 + point,
                    'name': name, 'direction': direction,
                    'value_type': value_type, 'writable': writable}
        points = [
            sig(12, 'inflow', 'in', 'float'),
            sig(13, 'net-flow', 'in', 'float'),
            sig(200, 'level-selected', 'out', 'float'),
            sig(204, 'demand', 'out', 'int'),
            sig(205, 'demand-in', 'in', 'int'),
            sig(210, 'duty', 'out', 'int'),
            sig(211, 'staged', 'out', 'int'),
            sig(214, 'below-cutoff')]
        for index, tag in ((0, 'p101'), (1, 'p102')):
            base = self.base(index)
            points += [
                sig(40 + index, tag + '-run', 'in'),
                sig(60 + index, tag + '-thermal', 'in'),
                sig(100 + index, tag + '-cmd'),
                sig(base, tag + '-mode', 'in', writable=True),
                sig(base + 1, tag + '-hand', 'in', writable=True),
                sig(base + 2, tag + '-oos', 'in', writable=True),
                sig(base + 3, tag + '-group-cmd'),
                sig(base + 5, tag + '-auto'),
                sig(base + 12, tag + '-fault'),
                sig(base + 16, tag + '-protect-tripped'),
                sig(base + 18, tag + '-protections-ok'),
                sig(base + 24, tag + '-thermal-ok'),
                sig(base + 26, tag + '-moisture-ok'),
                sig(base + 28, tag + '-avail'),
                sig(1073 + 30 * index, tag + '-fault-alarm'),
                sig(1074 + 30 * index,
                    tag + '-fault-unacknowledged'),
                sig(1076 + 30 * index, tag + '-fault-suppressed'),
                sig(1077 + 30 * index,
                    tag + '-fault-out-of-service'),
                sig(1080 + 30 * index, tag + '-thermal-ack', 'in',
                    writable=True),
                sig(1083 + 30 * index, tag + '-thermal-alarm'),
                sig(1084 + 30 * index,
                    tag + '-thermal-unacknowledged')]
        return points

    def _snapshot_body(self):
        flag_points = {1073, 1074, 1076, 1077, 1083, 1084,
                       1103, 1104, 1106, 1107, 1113, 1114}
        points = []
        for point, value in sorted(self.values.items()):
            if self.flags_unserved and point in flag_points:
                continue
            quality = 'good'
            if point in (40, 41, 60, 61):
                quality = self.plant.served(point).get('quality',
                                                       'good')
            points.append({
                'point': point,
                'direction': 'in',
                'sample': {'value': self._wrap(value),
                           'quality': quality, 'tick': self.tick}})
        return {'tick': self.tick, 'points': points}

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                sync = 'unsynchronized' if self.no_tracking \
                    else {'tracking': {'aligned': self.tick}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            if self.no_active \
                    or (self.role_moves and self.plant.faults):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = self._signals()
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot_body()
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            point = (write or {}).get('point')
            if write and point in self.WRITABLE \
                    and not self.write_refused:
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {'point': point}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class ManualTakeoverTests(unittest.TestCase):
    """scenario_manual_takeover against the stubbed rig: the feed's
    scan-per-call timing is deterministic so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the manual selection, the hand drive, the protection defeat, the
    holdout re-arm, the oos inhibit and its managed state, the
    no-step rejoin, the latch, the journal contract, and the
    inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = TakeoverPlantPeer()
        self.feed = TakeoverFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'TAKEOVER_POLL', 0.001), \
                patch.object(scenarios, 'TAKEOVER_DEADLINE', 3.0), \
                patch.object(scenarios, 'TAKEOVER_CYCLE_DEADLINE',
                             3.0):
            return scenarios.scenario_manual_takeover(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The takeover leg follows the duty-handover leg in the
        # restored window ahead of standby-loss.
        self.assertEqual(
            order.index(scenarios.scenario_duty_handover) + 1,
            order.index(scenarios.scenario_manual_takeover))
        self.assertIs(verify.case_function('manual-takeover'),
                      scenarios.scenario_manual_takeover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented restore: the held pump's mode/hand/oos and
        # the ack input all re-armed, the contact fault cleared —
        # the fake's baseline duty is always p101.
        self.assertFalse(self.plant.faults.get(60))
        for point in (300, 301, 302, 1080):
            self.assertFalse(self.feed.values[point], point)
        # Every submission settled applied, attributed.
        writes = [entry for entry in self.feed.journal
                  if 'command_settled' in entry.get('event', {})]
        self.assertEqual(len(writes), 8)   # mode, hand, oos, three
        # restores, ack, ack-restore
        for entry in writes:
            receipt = entry['event']['command_settled']['receipt']
            self.assertEqual(receipt.get('actor'), 'qa-lane')
            self.assertIn('applied', receipt.get('outcome') or {})

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = TakeoverPlantPeer()
        self.addCleanup(plant2.close)
        feed2 = TakeoverFeed(plant2)
        ctx2 = self._ctx()
        ctx2['plant'] = plant2.address
        ctx2['plant_ctl'] = plant2.ctl
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_mode_hand_oos_journaled(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        changes = {}
        for entry in self.feed.journal:
            change = entry.get('event', {}).get('point_changed')
            if change:
                changes.setdefault(change['point'], []).append(
                    change['to'])
        # The held pump's holds and releases, the two protection
        # trips and clears, the avail drop and rejoin, the managed
        # flags, and the thermal lifecycle all journaled in order —
        # the fake's held pump is p101: oos 302, protect-tripped 316,
        # avail 328, the managed flags and thermal lifecycle.
        for point in (302, 316, 328, 1076, 1077, 1083, 1084):
            seq = changes.get(point, [])
            self.assertIn({'bool': True}, seq, point)
            self.assertIn({'bool': False}, seq, point)
        # The mode point's assert/release either under p101 or p102.
        mode_seq = changes.get(300, []) + changes.get(332, [])
        self.assertIn({'bool': True}, mode_seq)
        self.assertIn({'bool': False}, mode_seq)

    def test_auto_never_dropping_fails(self):
        self.feed.auto_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_hand_never_running_fails(self):
        self.feed.hand_never_runs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_protection_never_tripping_fails(self):
        self.feed.protection_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_annunciating_fails(self):
        self.feed.thermal_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_rearming_fails(self):
        self.feed.never_rearms = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_oos_never_inhibiting_fails(self):
        self.feed.oos_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_managed_flags_never_asserting_fails(self):
        self.feed.managed_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_cmd_step_on_rejoin_fails(self):
        self.feed.step_on_rejoin = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_never_clearing_fails(self):
        self.feed.latch_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_transitions_fail(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_receipts_fail(self):
        self.feed.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_write_fails(self):
        self.feed.write_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_roles_fail(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('takeover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_tracking_pair_is_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn("leg's wiring", record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_seam_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_ctl']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant_ctl', record.get('detail', ''))
        report.validate_scenario(record)

    def test_flags_unserved_is_inconclusive(self):
        self.feed.flags_unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
