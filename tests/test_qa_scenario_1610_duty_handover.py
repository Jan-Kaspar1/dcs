"""The 1610_duty_handover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_duty_handover, following the per-leg
split convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DutyHandoverTests.test_registered_in_scenarios',
    'DutyHandoverTests.test_clean_feed_passes_and_validates',
    'DutyHandoverTests.test_two_runs_produce_identical_evidence',
    'DutyHandoverTests.test_handover_exclusion_and_annunciation',
    'DutyHandoverTests.test_allout_rollup_annunciation',
    'DutyHandoverTests.test_fault_never_proving_fails',
    'DutyHandoverTests.test_duty_never_handing_over_fails',
    'DutyHandoverTests.test_avail_collapsing_fails',
    'DutyHandoverTests.test_alarm_never_annunciating_fails',
    'DutyHandoverTests.test_rollup_never_annunciating_fails',
    'DutyHandoverTests.test_unbounded_excursion_fails',
    'DutyHandoverTests.test_fault_never_clearing_fails',
    'DutyHandoverTests.test_unjournaled_transitions_fail',
    'DutyHandoverTests.test_unjournaled_receipts_fail',
    'DutyHandoverTests.test_refused_write_fails',
    'DutyHandoverTests.test_moved_roles_fail',
    'DutyHandoverTests.test_no_active_is_failed',
    'DutyHandoverTests.test_no_tracking_pair_is_inconclusive',
    'DutyHandoverTests.test_missing_wiring_is_inconclusive',
    'DutyHandoverTests.test_missing_descriptors_is_inconclusive',
    'DutyHandoverTests.test_no_plant_seam_is_inconclusive',
    'DutyHandoverTests.test_flags_unserved_is_inconclusive',
})


class HandoverPlantPeer(FakePlantPeer):
    """The handover rig's plant half: run contacts 40/41 loop back the
    driven commands — the feed mirrors each commanded point into the
    plant's stored value — and FakePlantPeer's quality substitution
    carries the injected bad:device_fault the feedback-discrepancy
    proof treats as the proven failure."""

    def __init__(self):
        super().__init__()
        self.samples[41] = {'value': {'bool': False},
                            'quality': 'good', 'tick': 0}


class HandoverFeed:
    """A stubbed monitor pair for the duty-handover scenario: ctrl-a
    runs a tiny executor over the rig's run/cmd loopback, the motor's
    fault_ticks feedback-discrepancy proof, the pump group's
    exclusion/staging semantics, the threshold chain on an integrated
    level, and the four managed alarms the leg annunciates; ctrl-b
    only reports its tracking standby role. Every `http_json` call is
    one completed scan — the internal carriers deliver last scan's
    image, so the fault proof, the exclusion, and the roll-ups land
    their own carrier hops exactly like the deployed model. Declared-
    journaled points record point_changed and every accepted command
    settles its receipt at the next scan's boundary. Fault flags
    stage each named failure the issue calls out."""

    LEVEL, NET, INFLOW = 200, 13, 12
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED = 210, 211
    BELOW, NONE, ALL = 214, 217, 218
    NONE_ACK, NONE_ALARM, NONE_UNACK = 1030, 1033, 1034
    ALL_ACK, ALL_ALARM, ALL_UNACK = 1040, 1043, 1044
    START_DELAY, MIN_OFF, MOTOR_FAULT_TICKS = 1, 2, 2
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    BIAS, DRAW = 0.2, 1.0
    JOURNALED = (40, 41, 214, 217, 218, 312, 328, 344, 360,
                 1033, 1034, 1043, 1044,
                 1073, 1074, 1103, 1104)
    WRITABLE = (1030, 1040, 1070, 1100)

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
        self.level = 2.2            # just past start — a duty cycle
                                    # forms at once
        self.demand_held = 0
        self.duty_index = None
        self.cursor = 0
        self.last_demand = 0
        self.commanded = [False, False]
        self.held_until = [0, 0]
        self.last_start = None
        self.disagree = [0, 0]
        self.alarms = {point: {'state': False, 'latched': False,
                               'ack': point}
                       for point in (1030, 1040, 1070, 1100)}
        self.values = {200: 2.2, 13: self.BIAS, 12: 0.0,
                       204: 0, 205: 0, 210: 0, 211: 0,
                       214: False, 217: False, 218: False,
                       1033: False, 1034: False, 1043: False,
                       1044: False}
        for index in (0, 1):
            base = self.base(index)
            self.values.update({
                40 + index: False, 100 + index: False,
                base + 12: False, base + 28: True,
                1073 + 30 * index: False, 1074 + 30 * index: False})
        for point in (1030, 1040, 1070, 1100):
            self.values[point] = False
        self.jseen = {}
        # Fault injection for the named-failure cases.
        self.no_active = False           # ctrl-a never reports active
        self.no_tracking = False         # the peer never tracks
        self.bare_signals = False        # the leg's wiring absent
        self.no_descriptors = False      # the chain descriptor absent
        self.fault_never = False         # the injection never proves
        self.duty_sticks = False         # duty never hands over
        self.avail_collapses = False     # avail reports the exclusion
        self.managed_mute = False        # the fault alarm never
                                         # annunciates
        self.rollup_mute = False         # none/all never annunciate
        self.unbounded = False           # the level excursion runs
                                         # away
        self.fault_sticks = False        # the cleared fault never
                                         # clears
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

    def _effective(self, index):
        """The group's availability: avail_i standing Good and the
        proven fault_i clear — the honest-aggregate contract the leg
        asserts."""
        return bool(self.values[self.base(index) + 28]) \
            and not bool(self.values[self.base(index) + 12])

    def _assign(self, effective):
        """The alternate policy's duty designation over the effective
        pumps."""
        for offset in range(2):
            index = (self.cursor + offset) % 2
            if effective[index]:
                self.duty_index = index
                self.cursor = (index + 1) % 2
                return
        self.duty_index = None

    def _step_group(self):
        effective = [self._effective(index) for index in (0, 1)]
        if self.duty_index is not None \
                and not effective[self.duty_index]:
            if not self.duty_sticks:
                self._assign(effective)
        elif self.last_demand >= 1 \
                and self.values[self.DEMAND_IN] == 0:
            self._assign(effective)      # cycle end: alternate
        if self.duty_index is None:
            self._assign(effective)
        self.values[self.NONE] = not any(effective)
        self.values[self.ALL] = all(
            self.values[self.base(index) + 12] for index in (0, 1))
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
        commanded = [False, False]
        for index in targets:
            if self.commanded[index]:
                commanded[index] = True
            elif self.last_start is None \
                    or self.tick >= self.last_start \
                    + self.START_DELAY:
                commanded[index] = True
                self.last_start = self.tick
        for index in range(2):
            if self.commanded[index] and not commanded[index]:
                self.held_until[index] = self.tick + self.MIN_OFF
            self.commanded[index] = commanded[index]
            self.values[100 + index] = commanded[index]
        self.values[self.STAGED] = sum(commanded)
        self.last_demand = demand

    def _step_alarms(self):
        """The managed bool alarms the leg annunciates: `in` follows
        the standing condition, the latch arms on a fresh assertion,
        the receipted ack clears. The fault alarms' `in` is the proven
        fault flag; the station alarms' `in` the roll-ups."""
        feeds = {1070: self.values[self.base(0) + 12],
                 1100: self.values[self.base(1) + 12],
                 1030: self.values[self.NONE],
                 1040: self.values[self.ALL]}
        for ack_point, inp in feeds.items():
            alarm = self.alarms[ack_point]
            if ack_point in (1070, 1100) and self.managed_mute:
                inp = False
            if ack_point in (1030, 1040) and self.rollup_mute:
                inp = False
            fresh = bool(inp) and not alarm['state']
            alarm['state'] = bool(inp)
            alarm['latched'] = (alarm['latched'] or fresh) \
                and not self.values[ack_point]
            self.values[ack_point + 3] = alarm['state']
            self.values[ack_point + 4] = alarm['latched']

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
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        # The delivered command loopback and the motor's
        # feedback-discrepancy accounting — the proven fault the group
        # excludes on.
        for index in (0, 1):
            drive = bool(self.values[100 + index])
            self.plant.samples[40 + index]['value'] = {'bool': drive}
            served = self.plant.served(40 + index)
            run = served['value'].get('bool')
            self.values[40 + index] = bool(run)
            good = served.get('quality') == 'good'
            agree = run == drive and (good or self.fault_never)
            self.disagree[index] = 0 if agree \
                else self.disagree[index] + 1
            base = self.base(index)
            proven = self.disagree[index] >= self.MOTOR_FAULT_TICKS
            self.values[base + 12] = proven or (
                self.fault_sticks and self.values[base + 12])
            if self.avail_collapses:
                self.values[base + 28] = not self.values[base + 12]
        self._step_group()
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
        self.values[self.LEVEL] = self.level
        net = self.BIAS - self.DRAW * sum(
            bool(self.values[100 + index]) for index in (0, 1))
        self.values[self.NET] = net
        if self.unbounded and self.values[self.NONE]:
            # The runaway: the all-out level leaps past the declared
            # high bound on the scan the roll-ups stand, so the
            # served excursion clause reports the breach.
            self.level = self.HIGH + 1.0
        self._step_alarms()
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
            sig(217, 'none-available'),
            sig(218, 'all-faulted'),
            sig(1030, 'none-available-ack', 'in', writable=True),
            sig(1033, 'none-available-alarm'),
            sig(1034, 'none-available-unacknowledged'),
            sig(1040, 'all-faulted-ack', 'in', writable=True),
            sig(1043, 'all-faulted-alarm'),
            sig(1044, 'all-faulted-unacknowledged')]
        for index, tag in ((0, 'p101'), (1, 'p102')):
            base = self.base(index)
            points += [
                sig(40 + index, tag + '-run', 'in'),
                sig(100 + index, tag + '-cmd'),
                sig(base + 12, tag + '-fault'),
                sig(base + 28, tag + '-avail'),
                sig(1070 + 30 * index, tag + '-fault-ack', 'in',
                    writable=True),
                sig(1073 + 30 * index, tag + '-fault-alarm'),
                sig(1074 + 30 * index,
                    tag + '-fault-unacknowledged')]
        return points

    def _snapshot_body(self):
        points = []
        flag_points = {1033, 1034, 1043, 1044, 1073, 1074,
                       1103, 1104}
        for point, value in sorted(self.values.items()):
            if self.flags_unserved and point in flag_points:
                continue
            quality = 'good'
            if point in (40, 41):
                quality = self.plant.served(point).get('quality',
                                                       'good')
            points.append({
                'point': point,
                'direction': 'in',
                'sample': {'value': self._wrap(value),
                           'quality': quality, 'tick': self.tick}})
        body = {'tick': self.tick, 'points': points,
                'parameters': [
                    {'name': 'chain', 'values': {
                        'cutoff': {'float': self.CUTOFF},
                        'stop': {'float': self.STOP},
                        'start': {'float': self.START},
                        'lag_start': {'float': self.LAG_START},
                        'high': {'float': self.HIGH}}}]}
        if not self.no_descriptors:
            body['descriptors'] = [
                {'name': 'chain', 'kind': 'threshold-chain',
                 'ports': []}]
        return body

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


class DutyHandoverTests(unittest.TestCase):
    """scenario_duty_handover against the stubbed rig: the feed's
    scan-per-call timing is deterministic so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the proven fault, the handover, the honest availability aggregate,
    the managed annunciation, the roll-ups, the bounded excursion, the
    restores, the journal contract, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = HandoverPlantPeer()
        self.feed = HandoverFeed(self.plant)

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
                patch.object(scenarios, 'HANDOVER_POLL', 0.001), \
                patch.object(scenarios, 'HANDOVER_DEADLINE', 3.0), \
                patch.object(scenarios, 'HANDOVER_CYCLE_DEADLINE',
                             3.0):
            return scenarios.scenario_duty_handover(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The handover leg rides the same settled tracking window the
        # pump-fault legs use, between the chain staging leg and the
        # standby-loss leg.
        self.assertEqual(
            order.index(scenarios.scenario_lag_staging) + 1,
            order.index(scenarios.scenario_duty_handover))
        self.assertIs(verify.case_function('duty-handover'),
                      scenarios.scenario_duty_handover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented restore: every injected channel cleared and
        # every ack input re-armed at idle.
        self.assertFalse(self.plant.faults.get(40))
        self.assertFalse(self.plant.faults.get(41))
        for point in (1030, 1040, 1070, 1100):
            self.assertFalse(self.feed.values[point])
        # The four receipted ack writes settled applied, attributed.
        writes = [entry for entry in self.feed.journal
                  if 'command_settled' in entry.get('event', {})]
        self.assertEqual(len(writes), 8)   # four acks + four restores
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
        plant2 = HandoverPlantPeer()
        self.addCleanup(plant2.close)
        feed2 = HandoverFeed(plant2)
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

    def test_handover_exclusion_and_annunciation(self):
        # The clean run's journaled record: the proven fault's assert
        # and clear, the managed alarm's two-flag lifecycle, the
        # station roll-ups' assert and return.
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        changes = {}
        for entry in self.feed.journal:
            change = entry.get('event', {}).get('point_changed')
            if change:
                changes.setdefault(change['point'], []).append(
                    change['to'])
        for point in (312, 344, 217, 218, 1073, 1074, 1103, 1104,
                      1033, 1034, 1043, 1044):
            seq = changes.get(point, [])
            self.assertIn({'bool': True}, seq, point)
            self.assertIn({'bool': False}, seq, point)

    def test_allout_rollup_annunciation(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # The all-out leg stood every pump out: both fault flags and
        # both roll-ups journaled true.
        changes = {}
        for entry in self.feed.journal:
            change = entry.get('event', {}).get('point_changed')
            if change:
                changes.setdefault(change['point'], []).append(
                    change['to'])
        for point in (217, 218, 1033, 1043):
            self.assertIn({'bool': True}, changes.get(point, []),
                          point)

    def test_fault_never_proving_fails(self):
        self.feed.fault_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_never_handing_over_fails(self):
        self.feed.duty_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_avail_collapsing_fails(self):
        # An availability aggregate that drops with the proven fault
        # breaches the honest-aggregate clause — the exclusion is the
        # fault, not an availability collapse.
        self.feed.avail_collapses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_annunciating_fails(self):
        self.feed.managed_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rollup_never_annunciating_fails(self):
        self.feed.rollup_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unbounded_excursion_fails(self):
        self.feed.unbounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unbounded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fault_never_clearing_fails(self):
        self.feed.fault_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_transitions_fail(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_receipts_fail(self):
        self.feed.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_write_fails(self):
        self.feed.write_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_roles_fail(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('handover-failed', record.get('detail', ''))
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

    def test_missing_descriptors_is_inconclusive(self):
        self.feed.no_descriptors = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('threshold-chain', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_seam_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_ctl']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant_ctl', record.get('detail', ''))
        report.validate_scenario(record)

    def test_flags_unserved_is_inconclusive(self):
        # The managed flag outputs absent from the served snapshot —
        # the leg cannot see the annunciation it must assert, so the
        # case is inconclusive rather than failed.
        self.feed.flags_unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
