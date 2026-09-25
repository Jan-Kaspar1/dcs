"""The 1200_pump_out_of_service leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_pump_out_of_service, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PumpOutOfServiceTests.test_registered_in_scenarios',
    'PumpOutOfServiceTests.test_clean_feed_passes_and_validates',
    'PumpOutOfServiceTests.test_two_runs_produce_identical_evidence',
    'PumpOutOfServiceTests.test_oos_exclusion_and_handover',
    'PumpOutOfServiceTests.test_avail_never_dropping_fails',
    'PumpOutOfServiceTests.test_duty_never_handing_over_fails',
    'PumpOutOfServiceTests.test_handover_beyond_the_bound_is_nondeterministic',
    'PumpOutOfServiceTests.test_duty_moving_before_the_drop_is_nondeterministic',
    'PumpOutOfServiceTests.test_held_command_reasserting_fails',
    'PumpOutOfServiceTests.test_managed_flags_never_asserting_fails',
    'PumpOutOfServiceTests.test_suppression_annunciating_fails',
    'PumpOutOfServiceTests.test_fault_never_proving_fails',
    'PumpOutOfServiceTests.test_avail_never_rejoining_fails',
    'PumpOutOfServiceTests.test_release_never_reannunciating_fails',
    'PumpOutOfServiceTests.test_never_rejoining_rotation_fails',
    'PumpOutOfServiceTests.test_unjournaled_transitions_fail',
    'PumpOutOfServiceTests.test_unjournaled_receipts_fail',
    'PumpOutOfServiceTests.test_refused_write_fails',
    'PumpOutOfServiceTests.test_moved_roles_fail',
    'PumpOutOfServiceTests.test_declared_thermal_suppress_passes',
    'PumpOutOfServiceTests.test_no_active_is_failed',
    'PumpOutOfServiceTests.test_no_tracking_pair_is_inconclusive',
    'PumpOutOfServiceTests.test_missing_wiring_is_inconclusive',
    'PumpOutOfServiceTests.test_missing_descriptors_is_inconclusive',
    'PumpOutOfServiceTests.test_non_alternate_policy_is_inconclusive',
    'PumpOutOfServiceTests.test_managed_flags_never_reporting_is_inconclusive',
})


class OosPlantPeer(FakePlantPeer):
    """The out-of-service rig's plant half: run contacts 40/41 loop
    back the driven commands — the feed mirrors each commanded point
    into the plant's stored value — and FakePlantPeer's quality
    substitution carries the injected run fault the mid-OOS truth leg
    proves with."""

    def __init__(self):
        super().__init__()
        self.samples[41] = {'value': {'bool': False},
                            'quality': 'good', 'tick': 0}


class OosFeed:
    """A stubbed monitor pair for the pump out-of-service scenario:
    ctrl-a runs a tiny executor over the rig's writable `oos` points,
    the in-service availability leg, the demand guard, the pump
    group's exclusion/staging semantics, and the six managed bool
    alarms; ctrl-b only reports its tracking standby role. Every
    `http_json` call is one completed scan — the internal carriers
    deliver last scan's image, so the oos-ok cone, the avail drop, the
    duty handover, and the delivered suppression copy each land their
    own carrier hop after the write, exactly like the deployed model.
    Declared-journaled points record point_changed — the first
    observed sample included — and every accepted command settles its
    receipt at the next scan's boundary. Fault flags stage each named
    failure the issue calls out."""

    LEVEL, INFLOW = 200, 12
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED, NONE_AVAIL = 210, 211, 217
    START_DELAY, MIN_OFF, MOTOR_FAULT_TICKS = 1, 2, 2
    CUTOFF, STOP, START, LAG_START = 0.5, 1.0, 2.0, 3.0
    RISE, DRAW = 0.2, 1.0       # the dynamics' ambient gain and draw
    KINDS = ('fault', 'thermal', 'moisture')
    JOURNALED = (40, 41, 217,
                 300, 302, 312, 328, 332, 334, 344, 360,
                 1073, 1074, 1075, 1076, 1077,
                 1083, 1084, 1085, 1086, 1087,
                 1093, 1094, 1095, 1096, 1097,
                 1103, 1104, 1105, 1106, 1107,
                 1113, 1114, 1115, 1116, 1117,
                 1123, 1124, 1125, 1126, 1127)
    WRITABLE = (300, 301, 302, 332, 333, 334,
                1070, 1080, 1090, 1100, 1110, 1120)

    @staticmethod
    def base(index):
        """The 0-based pump's internal point base — 300 + 32*i."""
        return 300 + 32 * index

    @staticmethod
    def alarm_base(index, kind):
        """Pump `index`'s `kind` alarm's ack point — the alarm region
        lays out three managed alarms per pump on 10-point strides."""
        return 1070 + 30 * index + 10 * kind

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []           # accepted commands awaiting apply
        self.level = 0.8
        self.demand_held = 0        # the threshold chain's held demand
        self.duty_index = None      # the group's duty holder, 0-based
        self.cursor = 0             # the rotation cursor
        self.last_demand = 0
        self.commanded = [False, False]
        self.held_until = [0, 0]
        self.last_start = None
        self.disagree = [0, 0]      # motor fault accounting per pump
        self.exclusion_wait = 0     # slow_handover's delaying counter
        self.ever_held = [False, False]
        self.alarms = {(index, kind): {'state': False,
                                       'latched': False,
                                       'prev_sup': False}
                       for index in (0, 1) for kind in range(3)}
        self.values = {200: 0.8, 12: 0.0, 204: 0, 205: 0,
                       210: 0, 211: 0, 217: False}
        for index in (0, 1):
            base = self.base(index)
            self.values.update({
                40 + index: False, 100 + index: False,
                base: False, base + 1: False, base + 2: False,
                base + 3: False, base + 4: False,
                base + 8: True, base + 9: True, base + 10: True,
                base + 12: False, base + 13: False, base + 14: False,
                base + 28: True, base + 29: True,
                base + 30: False, base + 31: False})
            for kind in range(3):
                base_a = self.alarm_base(index, kind)
                for offset in (0, 3, 4, 5, 6, 7):
                    self.values[base_a + offset] = False
        self.jseen = {}
        self.history = {}
        self.hseq = {}
        # Fault injection for the named-failure cases.
        self.no_active = False           # ctrl-a never reports active
        self.no_tracking = False         # the peer never tracks
        self.bare_signals = False        # the oos-leg wiring absent
        self.no_descriptors = False      # snapshot serves none
        self.non_alternate = False       # the group declares timed
        self.avail_sticks = False        # avail never drops on the hold
        self.duty_sticks = False         # duty never hands over
        self.slow_handover = False       # duty lands past the bound
        self.duty_before_avail = False   # duty moves before the drop
        self.managed_mute = False        # the held fault alarm's flags
                                         # never assert
        self.flags_unserved = False      # managed flag points absent
        self.thermal_suppress = False    # the thermal alarm declares
                                         # suppress — precedence proof
        self.suppress_annunciates = False  # the latch ignores suppress
        self.held_recommands = False     # the held cmd re-asserts
        self.no_return = False           # avail never rejoins
        self.no_reannunciate = False     # suppression's release lands
                                         # no fresh latch
        self.never_rejoins = False       # the group never re-admits
        self.fault_never = False         # the injected fault never
                                         # proves
        self.no_journal = False          # point_changed never lands
        self.no_receipts = False         # command_settled never lands
        self.write_refused = False       # the command path rejects
        self.role_moves = False          # the pair's roles move

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
                    'from': (None if point not in self.jseen
                             or previous is None
                             else self._wrap(previous)),
                    'to': self._wrap(value)}})

    def _assign(self, available):
        """The alternate-each-cycle policy: the next available pump in
        rotation order takes duty and advances the cursor."""
        for offset in range(2):
            index = (self.cursor + offset) % 2
            if available[index]:
                self.duty_index = index
                self.cursor = (index + 1) % 2
                return
        self.duty_index = None

    def _step_group(self):
        demand = self.values[self.DEMAND_IN]
        demand_eff = demand if isinstance(demand, int) \
            and not isinstance(demand, bool) else self.last_demand
        demand_eff = max(0, min(2, demand_eff))
        available = []
        for index in (0, 1):
            base = self.base(index)
            ok = bool(self.values[base + 29]) \
                and not bool(self.values[base + 13])
            if self.never_rejoins and self.ever_held[index]:
                ok = False
            available.append(ok)
        if self.duty_index is not None \
                and not available[self.duty_index]:
            # The exclusion hands duty over this scan — or slow_handover
            # banks it past the declared wiring bound.
            self.exclusion_wait += 1
            if not self.duty_sticks \
                    and (not self.slow_handover
                         or self.exclusion_wait > 10):
                self._assign(available)
        else:
            self.exclusion_wait = 0
            if self.last_demand >= 1 and demand_eff == 0:
                self._assign(available)      # cycle end: alternate
        if self.duty_index is None:
            self._assign(available)
        self.values[self.NONE_AVAIL] = not any(available)
        self.values[self.DUTY] = 0 if self.duty_index is None \
            else self.duty_index + 1
        lead = self.duty_index if self.duty_index is not None \
            else self.cursor
        targets = []
        for offset in range(2):
            if len(targets) >= demand_eff:
                break
            index = (lead + offset) % 2
            if available[index] and (self.commanded[index]
                                     or self.tick
                                     >= self.held_until[index]):
                targets.append(index)
        commanded = [False, False]
        for index in targets:
            if self.commanded[index]:
                commanded[index] = True
            elif self.last_start is None \
                    or self.tick >= self.last_start + self.START_DELAY:
                commanded[index] = True
                self.last_start = self.tick
        for index in range(2):
            if self.commanded[index] and not commanded[index]:
                self.held_until[index] = self.tick + self.MIN_OFF
            self.commanded[index] = commanded[index]
        self.last_demand = demand_eff
        for index in (0, 1):
            self.values[self.base(index) + 3] = commanded[index]
        self.values[self.STAGED] = sum(commanded)

    def _step_alarms(self):
        """Each managed bool alarm: state follows `in`, the latch arms
        on a fresh assertion or a suppression release under a standing
        condition, ack dominates, suppression withholds. The managed
        flags report their bound inputs' delivered levels — the fault
        alarm's `oos`/`suppress` bindings, plus the thermal alarm's
        `suppress` when thermal_suppress declares it."""
        for index in (0, 1):
            base = self.base(index)
            for kind, name in enumerate(self.KINDS):
                base_a = self.alarm_base(index, kind)
                state = self.alarms[(index, kind)]
                if name == 'fault':
                    inp = self.values[base + 14]     # fault-alarm-in
                    oos_in = bool(self.values[base + 2])
                    sup_in = bool(self.values[base + 31])
                elif name == 'thermal':
                    inp = False                       # the clear contact
                    oos_in = False
                    sup_in = bool(self.values[base + 31]) \
                        if self.thermal_suppress else False
                else:
                    inp = oos_in = sup_in = False
                if self.managed_mute and name == 'fault':
                    oos_in = sup_in = False
                suppressed = sup_in
                fresh = bool(inp) and (
                    not state['state']
                    or (state['prev_sup'] and not self.no_reannunciate))
                state['state'] = bool(inp)
                state['latched'] = (state['latched'] or fresh) \
                    and not self.values[base_a] \
                    and not (suppressed
                             and not self.suppress_annunciates)
                state['prev_sup'] = suppressed
                self.values[base_a + 3] = state['state']
                self.values[base_a + 4] = state['latched']
                self.values[base_a + 5] = False
                self.values[base_a + 6] = suppressed
                self.values[base_a + 7] = oos_in

    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            point = write['point']
            self.values[point] = write['value']['bool']
            if point in (302, 334) and write['value']['bool']:
                index = 0 if point == 302 else 1
                self.ever_held[index] = True
                if self.duty_before_avail:
                    self.duty_index = 1 - index
            if not self.no_receipts:
                self._entry({'command_settled':
                             {'receipt': dict(receipt)}})
        # The internal carriers deliver last scan's image.
        for index in (0, 1):
            base = self.base(index)
            self.values[base + 9] = self.values[base + 8]
            self.values[base + 10] = self.values[base + 8]
            self.values[base + 29] = self.values[base + 28]
            self.values[base + 31] = self.values[base + 30]
            self.values[base + 14] = self.values[base + 12]
            self.values[base + 13] = self.values[base + 12]
            self.values[base + 4] = self.values[base + 3]
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        # The combinational layer: the in-service inversion, the
        # suppression copy, the guarded motor request, the fault
        # accounting, and the availability gate.
        for index in (0, 1):
            base = self.base(index)
            self.values[base + 8] = not self.values[base + 2]
            self.values[base + 30] = self.values[base + 2]
            request = (self.values[base + 4]
                       and not self.values[base]) \
                or (self.values[base + 1] and self.values[base])
            drive = request and self.values[base + 10]
            if self.held_recommands and self.values[base + 2]:
                drive = True
            self.values[100 + index] = drive
            self.plant.samples[40 + index]['value'] = {'bool': drive}
            served = self.plant.served(40 + index)
            run = served['value'].get('bool')
            self.values[40 + index] = bool(run)
            good = served.get('quality') == 'good'
            agree = run == drive and (good or self.fault_never)
            self.disagree[index] = 0 if agree \
                else self.disagree[index] + 1
            self.values[base + 12] = \
                self.disagree[index] >= self.MOTOR_FAULT_TICKS
            avail = (not self.values[base]) \
                and bool(self.values[base + 9])
            if self.avail_sticks:
                avail = True
            if self.no_return and not self.values[base + 28]:
                avail = False
            self.values[base + 28] = avail
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
        self.values[self.LEVEL] = self.level
        self.values[self.INFLOW] = 0.0
        self._step_alarms()
        self._journal_values()
        for point, value in self.values.items():
            quality = 'good'
            if point in (40, 41):
                quality = self.plant.served(point).get('quality',
                                                       'good')
            self.hseq[point] = self.hseq.get(point, 0) + 1
            self.history.setdefault(point, []).append({
                'seq': self.hseq[point],
                'sample': {'value': self._wrap(value),
                           'quality': quality, 'tick': self.tick}})
        self.level += self.RISE \
            - self.DRAW * sum(bool(self.values[100 + index])
                              for index in (0, 1))

    def _signals(self):
        def sig(point, name, direction='out', value_type='bool',
                writable=False):
            return {'point': point, 'signal': 10000 + point,
                    'name': name, 'direction': direction,
                    'value_type': value_type, 'writable': writable}
        points = [
            sig(12, 'inflow', 'in', 'float'),
            sig(200, 'level-selected', 'out', 'float'),
            sig(204, 'demand', 'out', 'int'),
            sig(205, 'demand-in', 'in', 'int'),
            sig(210, 'duty', 'out', 'int'),
            sig(211, 'staged', 'out', 'int'),
            sig(217, 'none-available')]
        for index, tag in ((0, 'p101'), (1, 'p102')):
            base = self.base(index)
            points += [
                sig(40 + index, tag + '-run', 'in'),
                sig(100 + index, tag + '-cmd'),
                sig(base, tag + '-mode', 'in', writable=True),
                sig(base + 1, tag + '-hand', 'in', writable=True),
                sig(base + 2, tag + '-oos', 'in', writable=True),
                sig(base + 8, tag + '-oos-ok'),
                sig(base + 9, tag + '-oos-ok-avail-in', 'in'),
                sig(base + 10, tag + '-oos-ok-guard-in', 'in'),
                sig(base + 12, tag + '-fault'),
                sig(base + 14, tag + '-fault-alarm-in', 'in'),
                sig(base + 28, tag + '-avail'),
                sig(base + 29, tag + '-avail-in', 'in'),
                sig(base + 30, tag + '-fault-sup'),
                sig(base + 31, tag + '-fault-sup-in', 'in')]
            for kind, name in enumerate(self.KINDS):
                base_a = self.alarm_base(index, kind)
                points += [
                    sig(base_a, tag + '-' + name + '-ack', 'in',
                        writable=True),
                    sig(base_a + 3, tag + '-' + name + '-alarm'),
                    sig(base_a + 4,
                        tag + '-' + name + '-unacknowledged'),
                    sig(base_a + 5, tag + '-' + name + '-shelved'),
                    sig(base_a + 6, tag + '-' + name + '-suppressed'),
                    sig(base_a + 7,
                        tag + '-' + name + '-out-of-service')]
        return points

    def _descriptors(self):
        def port(name, point):
            return {'name': name, 'point': point}
        descriptors = [
            {'name': 'group', 'kind': 'pump-group',
             'ports': [port('demand', 205),
                       port('avail_1', 329), port('avail_2', 361),
                       port('fault_1', 313), port('fault_2', 345),
                       port('cmd_1', 303), port('cmd_2', 335),
                       port('run_1', 40), port('run_2', 41),
                       port('duty', 210), port('staged', 211),
                       port('none_available', 217)]}]
        for index in (0, 1):
            base = self.base(index)
            for kind, name in enumerate(self.KINDS):
                base_a = self.alarm_base(index, kind)
                ins = 314 + 32 * index if name == 'fault' \
                    else (60 + index if name == 'thermal'
                          else 80 + index)
                ports = [port('in', ins), port('ack', base_a),
                         port('alarm', base_a + 3),
                         port('unacknowledged', base_a + 4),
                         port('shelved', base_a + 5),
                         port('suppressed', base_a + 6),
                         port('out_of_service', base_a + 7)]
                if name == 'fault':
                    ports += [port('oos', base + 2),
                              port('suppress', base + 31)]
                elif name == 'thermal' and self.thermal_suppress:
                    ports.append(port('suppress', base + 31))
                descriptors.append({
                    'name': 'p10%d-%s-alarm' % (index + 1, name),
                    'kind': 'managed-bool-latching-alarm',
                    'ports': ports})
        return descriptors

    def _parameters(self):
        return [{'name': 'group', 'values': {
            'rotation': {'int': 1 if self.non_alternate else 0},
            'start_delay_ticks': {'int': self.START_DELAY},
            'restage_delay_ticks': {'int': 0},
            'min_off_ticks': {'int': self.MIN_OFF}}}]

    def _snapshot_body(self):
        points = []
        flag_points = set()
        for index in (0, 1):
            for kind in range(3):
                base_a = self.alarm_base(index, kind)
                flag_points.update(
                    base_a + offset for offset in (3, 4, 5, 6, 7))
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
                'parameters': self._parameters()}
        if not self.no_descriptors:
            body['descriptors'] = self._descriptors()
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
                    or (self.role_moves and any(self.ever_held)):
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
        if (method, route) == ('GET', '/history'):
            params = {}
            for pair in query.split('&'):
                key, _, val = pair.partition('=')
                params.setdefault(key, []).append(val)
            since = int(params.get('since', ['0'])[0])
            wanted = [int(point) for point in params.get('point', [])]
            if not wanted:
                wanted = sorted(self.history)
            return 200, [
                {'point': point,
                 'samples': [sample for sample in
                             self.history.get(point, [])
                             if sample['seq'] > since]}
                for point in wanted]
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


class PumpOutOfServiceTests(unittest.TestCase):
    """scenario_pump_out_of_service against the stubbed rig: the
    feed's carrier-hop timing is call-count keyed so each run emits
    identical evidence, and every fault flag stages a named
    acceptance failure — the exclusion, the handover bound, the
    command hold, the managed-state assertion and its precedence,
    the mid-OOS truth leg, the manual return, the rejoin, the
    journal contract, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = OosPlantPeer()
        self.feed = OosFeed(self.plant)

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
                patch.object(scenarios, 'OOS_POLL', 0.001), \
                patch.object(scenarios, 'OOS_DEADLINE', 3.0), \
                patch.object(scenarios, 'OOS_CYCLE_DEADLINE', 3.0):
            return scenarios.scenario_pump_out_of_service(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The managed-state leg rides the same settled tracking window
        # as the duty-rotation case, ahead of the force-carryover leg.
        self.assertEqual(
            order.index(scenarios.scenario_duty_rotation) + 1,
            order.index(scenarios.scenario_pump_out_of_service))
        self.assertIs(verify.case_function('pump-out-of-service'),
                      scenarios.scenario_pump_out_of_service)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented restore: oos cleared, the ack input clear,
        # the run fault lifted, both pumps available and uncommanded
        # at idle.
        self.assertEqual(self.feed.values[302], False)
        self.assertEqual(self.feed.values[1070], False)
        self.assertFalse(self.plant.faults.get(40))
        self.assertTrue(self.feed.values[328])
        self.assertTrue(self.feed.values[360])
        # The receipted writes the leg submitted all settled applied.
        writes = [entry for entry in self.feed.journal
                  if 'command_settled' in entry.get('event', {})]
        self.assertEqual(len(writes), 4)   # hold, release, ack, ack-off
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
        plant2 = OosPlantPeer()
        self.addCleanup(plant2.close)
        feed2 = OosFeed(plant2)
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

    def test_oos_exclusion_and_handover(self):
        # The clean run's journaled record is the exclusion evidence:
        # the attributed receipt beside the ordered point_changed
        # entries — oos true->false, avail false->true, the managed
        # flags' assert and release.
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        changes = {}
        for entry in self.feed.journal:
            change = entry.get('event', {}).get('point_changed')
            if change:
                changes.setdefault(change['point'], []).append(
                    change['to'])
        for point in (302, 328, 312, 1073, 1074, 1076, 1077):
            seq = changes.get(point, [])
            self.assertIn({'bool': True}, seq, point)
            self.assertIn({'bool': False}, seq, point)
        # The suppressed pump's thermal/moisture flags never moved.
        for point in (1083, 1084, 1085, 1086, 1087,
                      1093, 1094, 1095, 1096, 1097):
            self.assertNotIn({'bool': True}, changes.get(point, []),
                             point)

    def test_avail_never_dropping_fails(self):
        self.feed.avail_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        self.assertIn('exclusion', record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_never_handing_over_fails(self):
        self.feed.duty_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_handover_beyond_the_bound_is_nondeterministic(self):
        self.feed.slow_handover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_moving_before_the_drop_is_nondeterministic(self):
        self.feed.duty_before_avail = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_held_command_reasserting_fails(self):
        self.feed.held_recommands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_managed_flags_never_asserting_fails(self):
        self.feed.managed_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_suppression_annunciating_fails(self):
        # A latch that ignores the standing suppress annunciates the
        # mid-OOS fault — the named-without-annunciating contract's
        # breach.
        self.feed.suppress_annunciates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fault_never_proving_fails(self):
        self.feed.fault_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_avail_never_rejoining_fails(self):
        self.feed.no_return = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        self.assertIn('manual return', record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_never_reannunciating_fails(self):
        self.feed.no_reannunciate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_rejoining_rotation_fails(self):
        self.feed.never_rejoins = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        self.assertIn('rejoined the duty rotation',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_transitions_fail(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_receipts_fail(self):
        self.feed.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_write_fails(self):
        self.feed.write_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_roles_fail(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('oos-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_thermal_suppress_passes(self):
        # A thermal alarm declaring `suppress` bound to the delivered
        # copy reports suppressed-but-not-out-of-service — the
        # descriptor-driven precedence reading the scenario asserts.
        self.feed.thermal_suppress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
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
        self.assertIn('pump-group', record.get('detail', ''))
        report.validate_scenario(record)

    def test_non_alternate_policy_is_inconclusive(self):
        self.feed.non_alternate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('alternate-each-cycle',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_managed_flags_never_reporting_is_inconclusive(self):
        # The managed flag outputs absent from the served snapshot —
        # the leg cannot see the states it must assert, so the case is
        # inconclusive rather than failed.
        self.feed.flags_unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
