"""The 1600_lag_staging leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_lag_staging, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'LagStagingTests.test_registered_in_scenarios',
    'LagStagingTests.test_clean_feed_passes_and_validates',
    'LagStagingTests.test_two_runs_produce_identical_evidence',
    'LagStagingTests.test_demand_never_staging_fails',
    'LagStagingTests.test_staging_beyond_delay_bound_fails',
    'LagStagingTests.test_alarm_never_standing_fails',
    'LagStagingTests.test_unacknowledged_never_latching_fails',
    'LagStagingTests.test_out_of_order_release_fails',
    'LagStagingTests.test_transitions_never_journaling_fails',
    'LagStagingTests.test_journaled_role_change_fails',
    'LagStagingTests.test_nonjournaled_point_journaling_fails',
    'LagStagingTests.test_role_move_under_drive_fails',
    'LagStagingTests.test_ack_refusal_fails',
    'LagStagingTests.test_ack_never_applying_fails',
    'LagStagingTests.test_unrestored_inflow_fails',
    'LagStagingTests.test_moved_pump_operator_state_fails',
    'LagStagingTests.test_no_active_is_failed',
    'LagStagingTests.test_no_tracking_pair_is_inconclusive',
    'LagStagingTests.test_missing_wiring_is_inconclusive',
    'LagStagingTests.test_unwritable_ack_is_inconclusive',
    'LagStagingTests.test_missing_schema_instances_is_inconclusive',
    'LagStagingTests.test_unordered_setpoints_is_inconclusive',
    'LagStagingTests.test_mismatched_high_limit_is_inconclusive',
    'LagStagingTests.test_refused_writer_claim_is_inconclusive',
    'LagStagingTests.test_no_plant_endpoint_is_inconclusive',
    'LagStagingTests.test_no_owner_token_is_inconclusive',
    'LagStagingTests.test_demand_never_reporting_is_inconclusive',
    'LagStagingTests.test_output_never_reporting_is_inconclusive',
})


class LagStagingFeed:
    """A stubbed monitor pair for the lag-staging scenario: a tiny
    executor over the station's threshold chain, the pump group's
    delayed staging, and the managed latching alarm, against a real
    plant-protocol peer whose `inflow` point the scenario writes.
    Every `http_json` call is one completed scan — the internal
    carriers (level into the chain and the LAH, demand into the group)
    deliver the last image one scan later, the chain holds its demand
    between the hysteresis bands, the group's starts wait the declared
    start_delay_ticks, and the LAH trips at high_limit and releases on
    its hysteresis while the unacknowledged latch waits for the
    receipted ack. Declared-journaled points record point_changed —
    the first observed sample included — the carrier and staging
    points do not. Fault flags stage each named failure the issue
    calls out."""

    PRIMARY, BACKUP, SELECTED = 10, 11, 200
    CHAIN_IN, LAH_IN = 201, 202
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED = 210, 211
    DUTY_CALL, LAG_CALL = 212, 213
    BELOW_CUTOFF, HIGH_LEVEL = 214, 215
    ACK, ALARM, UNACK = 1000, 1003, 1004
    SHELVED, SUPPRESSED, OOS = 1005, 1006, 1007
    P1_RUN, P2_RUN = 40, 41
    P1_CMD, P2_CMD = 100, 101
    P1_MODE, P1_OOS, P2_MODE, P2_OOS = 300, 302, 332, 334
    INFLOW = 12
    JOURNALED = (40, 41, 214, 215, 300, 302, 332, 334,
                 1003, 1004, 1005, 1006, 1007)
    INS = (10, 11, 12, 201, 202, 205, 1000, 300, 302, 332, 334)
    # The declared setpoint chain, the staging delay, and the alarm
    # limits — the same table the deployed model serves.
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    START_DELAY = 1
    HIGH_LIMIT, HYSTERESIS = 4.0, 0.1
    DRAW, DT = -1.0, 0.1    # each running pump's draw, sim dt per scan

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 12, 'signal': 10012, 'name': 'inflow',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 201, 'signal': 10201, 'name': 'level-chain-in',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 202, 'signal': 10202, 'name': 'level-lah-in',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 204, 'signal': 10204, 'name': 'demand',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 205, 'signal': 10205, 'name': 'demand-in',
         'direction': 'in', 'value_type': 'int', 'writable': False},
        {'point': 210, 'signal': 10210, 'name': 'duty',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 211, 'signal': 10211, 'name': 'staged',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 212, 'signal': 10212, 'name': 'duty-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 213, 'signal': 10213, 'name': 'lag-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 214, 'signal': 10214, 'name': 'below-cutoff',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 215, 'signal': 10215, 'name': 'high-level',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1000, 'signal': 11000, 'name': 'lah-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1003, 'signal': 11003, 'name': 'lah-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1004, 'signal': 11004, 'name': 'lah-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1005, 'signal': 11005, 'name': 'lah-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1006, 'signal': 11006, 'name': 'lah-suppressed',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1007, 'signal': 11007, 'name': 'lah-out-of-service',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 40, 'signal': 10040, 'name': 'p101-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 41, 'signal': 10041, 'name': 'p102-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 300, 'signal': 10300, 'name': 'p101-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 302, 'signal': 10302, 'name': 'p101-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 332, 'signal': 10332, 'name': 'p102-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 334, 'signal': 10334, 'name': 'p102-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []          # accepted commands awaiting boundary
        self.demand_held = 0       # the chain's held stage count
        self.last_demand = 0       # the group's last Good demand
        self.cmd = [False, False]  # commanded pumps, 0-based
        self.last_start = None     # tick the last start issued
        self.pending_start = {}    # pump -> first target tick
        self.duty_index = 0        # pump 1 holds duty
        self.alarm_state = 'clear'
        self.latched = False
        self.level = 0.8
        self.values = {
            200: 0.8, 201: 0.8, 202: 0.8,
            204: 0, 205: 0, 210: 0, 211: 0,
            212: False, 213: False, 214: False, 215: False,
            1000: False, 1003: False, 1004: False, 1005: False,
            1006: False, 1007: False,
            40: False, 41: False, 100: False, 101: False,
            300: False, 302: False, 332: False, 334: False}
        self.jseen = {}            # last journaled value per point
        self.history = {}          # point -> [{'seq','sample'}]
        self.hseq = {}
        self._role_journaled = False
        self._demand_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False         # ctrl-a never reports active
        self.no_tracking = False       # the peer never reports tracking
        self.bare_signals = False      # the staging wiring absent
        self.bad_ack_signal = False    # lah-ack served non-writable
        self.bare_schema = False       # schema lacks the group/alarm
        self.bad_setpoints = False     # served chain not increasing
        self.bad_high_limit = False    # lah high_limit != chain high
        self.mute_chain = False        # demand never advances
        self.missing_demand = False    # 204 absent from the snapshot
        self.missing_ack = False       # 1000 absent from the snapshot
        self.slow_staging = False      # starts land beyond the delay
        self.mute_alarm = False        # the alarm never stands
        self.mute_unack = False        # the latch never latches
        self.sticky_alarm = False      # alarm clears past its order
        self.role_moves = False        # the level drive reads as peer loss
        self.journals_role = False     # a role_changed entry lands
        self.journals_demand = False   # a non-journaled point journals
        self.no_journal = False        # transitions never journal
        self.ack_rejected = False      # the ack submission is refused
        self.ack_never_applies = False # the accepted ack never settles
        self.moves_pump_state = False  # p1_oos flips mid-leg

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
        """The recorder's per-scan diff over declared-journaled points:
        the first observed sample lands `from: null` like the real
        recorder's creation record."""
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

    # The pump group's step on the delivered demand: duty first then
    # rotation order, each fresh start gated on start_delay_ticks since
    # the last one, the tail de-staging first on falling demand.
    def _step_group(self):
        demand_eff = max(0, min(2, int(self.values[self.DEMAND_IN])))
        if self.last_demand >= 1 and demand_eff == 0:
            self.duty_index = (self.duty_index + 1) % 2  # alternate
        self.last_demand = demand_eff
        order = [self.duty_index, (self.duty_index + 1) % 2]
        targets = order[:demand_eff]
        for index in targets:
            if self.cmd[index]:
                continue
            self.pending_start.setdefault(index, self.tick)
            if self.slow_staging:
                delay = self.START_DELAY + 3
                allowed = self.tick >= \
                    self.pending_start[index] + delay
            else:
                allowed = self.last_start is None \
                    or self.tick >= self.last_start + self.START_DELAY
            if allowed:
                self.cmd[index] = True
                self.last_start = self.tick
                self.pending_start.pop(index, None)
        for index in range(2):
            if index not in targets:
                self.cmd[index] = False
                self.pending_start.pop(index, None)
        staged = int(sum(self.cmd))
        for point, value in ((self.STAGED, staged),
                             (self.DUTY, self.duty_index + 1),
                             (self.P1_CMD, self.cmd[0]),
                             (self.P2_CMD, self.cmd[1]),
                             (self.P1_RUN, self.cmd[0]),
                             (self.P2_RUN, self.cmd[1])):
            self.values[point] = value

    # The LAH's two-flag lifecycle on the delivered level: trips at
    # high_limit, holds through the hysteresis, and the unacknowledged
    # latch stands until the receipted ack dominates.
    def _step_alarm(self):
        pv = self.values[self.LAH_IN]
        previous = self.alarm_state
        if self.sticky_alarm:
            state = 'high' if pv >= self.HIGH_LIMIT \
                or (previous == 'high' and pv >= self.STOP) else 'clear'
        elif previous == 'high':
            state = 'high' \
                if pv >= self.HIGH_LIMIT - self.HYSTERESIS else 'clear'
        else:
            state = 'high' if pv >= self.HIGH_LIMIT else 'clear'
        self.alarm_state = state
        fresh = state == 'high' and previous != 'high'
        self.latched = (self.latched or fresh) \
            and not self.values[self.ACK]
        self.values[self.ALARM] = state == 'high' \
            and not self.mute_alarm
        self.values[self.UNACK] = self.latched and not self.mute_unack

    # One completed scan: boundary-settled commands first, then the
    # carriers' one-scan delivery, the components in order, the journaled
    # diffs, the per-point history append, and the plant's dynamics step.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            if self.ack_never_applies:
                self.pending.append(receipt)
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if write['point'] == self.ACK:
                self.values[self.ACK] = write['value']['bool']
            if not self.no_journal:
                self._entry({'command_settled': {'receipt':
                                                 dict(receipt)}})
        # The internal carriers deliver last tick's image.
        self.values[self.CHAIN_IN] = self.values[self.SELECTED]
        self.values[self.LAH_IN] = self.values[self.SELECTED]
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        # failover-select serves the primary; the backup never engages.
        self.values[self.SELECTED] = self.level
        level = self.values[self.CHAIN_IN]
        if not self.mute_chain:
            if level <= self.STOP:
                self.demand_held = 0
            elif self.demand_held == 2 and level <= self.START:
                self.demand_held = 1
            elif level >= self.LAG_START:
                self.demand_held = 2
            elif self.demand_held == 0 and level >= self.START:
                self.demand_held = 1
        demand = self.demand_held
        for point, value in ((self.DEMAND, demand),
                             (self.DUTY_CALL, demand >= 1),
                             (self.LAG_CALL, demand >= 2),
                             (self.BELOW_CUTOFF, level <= self.CUTOFF),
                             (self.HIGH_LEVEL, level >= self.HIGH)):
            self.values[point] = value
        self._step_group()
        self._step_alarm()
        self._journal_values()
        # Injected journal-contract violations.
        if self.journals_role and self.values[self.ALARM] \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})
        if self.journals_demand and demand >= 1 \
                and not self._demand_journaled:
            self._demand_journaled = True
            self._entry({'point_changed': {
                'point': self.DEMAND, 'from': {'int': 0},
                'to': {'int': demand}}})
        # Operator state the leg never drove must not move.
        if self.moves_pump_state and demand >= 2:
            self.values[self.P1_OOS] = True
        for point, value in self.values.items():
            self.hseq[point] = self.hseq.get(point, 0) + 1
            self.history.setdefault(point, []).append({
                'seq': self.hseq[point],
                'sample': {'value': self._wrap(value),
                           'quality': 'good', 'tick': self.tick}})
        # The plant's dynamics step: inflow plus each commanded pump's
        # draw integrates into the level the next scan serves. A
        # type-confused held value reads as still water.
        inflow = self.plant.samples[self.INFLOW]['value'] \
            .get('float', 0.0)
        self.level += (inflow + self.DRAW * sum(self.cmd)) * self.DT

    def _parameters(self):
        if self.bad_setpoints:
            table = {'cutoff': 0.5, 'stop': 1.0, 'start': 3.0,
                     'lag_start': 2.0, 'high': 4.0}
        else:
            table = {'cutoff': self.CUTOFF, 'stop': self.STOP,
                     'start': self.START, 'lag_start': self.LAG_START,
                     'high': self.HIGH}
        high_limit = 4.5 if self.bad_high_limit else self.HIGH_LIMIT
        return [
            {'name': 'select', 'values': {}},
            {'name': 'chain', 'values': dict(
                {key: {'float': value}
                 for key, value in table.items()},
                on_bad_demand={'int': 0})},
            {'name': 'group', 'values': {
                'rotation': {'int': 0},
                'start_delay_ticks': {'int': self.START_DELAY},
                'restage_delay_ticks': {'int': 0},
                'min_off_ticks': {'int': 2}}},
            {'name': 'lah', 'values': {
                'high_limit': {'float': high_limit},
                'hysteresis': {'float': self.HYSTERESIS},
                'low_limit': {'float': -1000000000.0}}},
            {'name': 'lal', 'values': {
                'high_limit': {'float': 1000000000.0},
                'hysteresis': {'float': self.HYSTERESIS},
                'low_limit': {'float': 0.5}}}]

    @staticmethod
    def _interface(kind, measurements=(), state=()):
        return {'kind': kind,
                'measurements': [{'name': name, 'point': point}
                                 for name, point in measurements],
                'state': [{'name': name, 'point': point}
                          for name, point in state],
                'configuration': [], 'commands': [], 'events': []}

    def _schema(self):
        if self.bare_schema:
            return {'interfaces': [
                {'name': 'chain',
                 'interface': self._interface('threshold-chain')}]}
        return {'interfaces': [
            {'name': 'select', 'interface': self._interface(
                'failover-select', [('out', 200)])},
            {'name': 'chain', 'interface': self._interface(
                'threshold-chain', [('level', 201), ('demand', 204)],
                [('duty_call', 212), ('lag_call', 213),
                 ('below_cutoff', 214), ('high_level', 215)])},
            {'name': 'group', 'interface': self._interface(
                'pump-group', [('demand', 205), ('staged', 211)],
                [('duty', 210)])},
            {'name': 'lah', 'interface': self._interface(
                'managed-latching-alarm', [('in', 202)],
                [('alarm', 1003), ('unacknowledged', 1004),
                 ('shelved', 1005), ('suppressed', 1006),
                 ('out_of_service', 1007)])},
            {'name': 'lal', 'interface': self._interface(
                'managed-latching-alarm', [('in', 203)],
                [('alarm', 1013)])}]}

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
                    or (self.role_moves and self.values[self.ALARM]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'lah-ack' else entry
                          for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/schema'):
            return 200, self._schema()
        if (method, route) == ('GET', '/snapshot'):
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': {'value': {'float': self.level},
                            'quality': 'good', 'tick': self.tick}},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': {'value': {'float': self.level},
                            'quality': 'good', 'tick': self.tick}}]
            for point, value in sorted(self.values.items()):
                if self.missing_demand and point == self.DEMAND:
                    continue
                if self.missing_ack and point == self.ACK:
                    continue
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': self._wrap(value),
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'parameters': self._parameters()}
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
            if write and write.get('point') == self.ACK:
                if self.ack_rejected:
                    return 200, {'command': body['command'],
                                 'outcome': {'rejected': {'reason': {
                                     'not_writable': {
                                         'point': write['point']}}}},
                                 'actor': body.get('actor')}
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {'point':
                                              (write or {})
                                              .get('point')}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class LagStagingTests(unittest.TestCase):
    """scenario_lag_staging against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    each staging leg, the delay bound, the annunciation lifecycle, the
    journal contract, the falling-edge order, the restore, and the
    inconclusive paths."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = StagingPlantPeer(self.OWNER)
        self.feed = LagStagingFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.OWNER, 'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LAG_STAGING_POLL', 0.001), \
                patch.object(scenarios, 'LAG_STAGING_DEADLINE', 3.0):
            return scenarios.scenario_lag_staging(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same settled tracking window as the backup-health and
        # source-failover legs, ahead of the tune case and the
        # failover switch.
        self.assertEqual(
            order.index(scenarios.scenario_source_failover) + 1,
            order.index(scenarios.scenario_lag_staging))
        self.assertEqual(
            order.index(scenarios.scenario_lag_staging) + 1,
            order.index(scenarios.scenario_standby_loss))
        # The standby-loss, demote-settle, and demote-carry legs share
        # the same restored window and still run ahead of the tune
        # case's a->b switch.
        self.assertEqual(
            order.index(scenarios.scenario_standby_loss) + 1,
            order.index(scenarios.scenario_demote_settle_uniqueness))
        self.assertEqual(
            order.index(scenarios.scenario_demote_settle_uniqueness)
            + 1,
            order.index(scenarios.scenario_demote_carry_settle))
        self.assertEqual(
            order.index(scenarios.scenario_demote_carry_settle) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertEqual(
            order.index(scenarios.scenario_peer_announce) + 1,
            order.index(scenarios.scenario_demote_forged_standby_source))
        # The stale-island leg's driven third controller shares the
        # same restored window and still clears before the tune case's
        # a->b switch — the journal-boundary flood and suspended-alias
        # legs run between them in the same launch-layout window.
        self.assertEqual(
            order.index(scenarios.scenario_demote_forged_standby_source)
            + 1,
            order.index(scenarios.scenario_stale_island_resolution))
        self.assertEqual(
            order.index(scenarios.scenario_stale_island_resolution)
            + 1,
            order.index(scenarios.scenario_journal_boundary_flood))
        self.assertEqual(
            order.index(scenarios.scenario_journal_boundary_flood)
            + 1,
            order.index(scenarios.scenario_suspended_alias_audit))
        self.assertEqual(
            order.index(scenarios.scenario_suspended_alias_audit)
            + 1,
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('lag-staging'),
                      scenarios.scenario_lag_staging)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim attachment,
        # one read for the baseline, the rise write and its restore.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertEqual(ops.count('write'), 2)
        # The driven input restored and the roles never moved.
        self.assertEqual(
            self.plant.samples[12]['value'], {'float': 0.0})
        self.assertFalse(self.feed.latched)
        self.assertFalse(self.feed.values[self.feed.ACK])
        self.assertEqual(self.feed.demand_held, 0)
        self.assertEqual(sum(self.feed.cmd), 0)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = StagingPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = LagStagingFeed(plant2)
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

    def test_demand_never_staging_fails(self):
        # The chain never advances: the start crossing's served output
        # never lands.
        self.feed.mute_chain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('staging-failed', record.get('detail', ''))
        self.assertIn('duty-call leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_staging_beyond_delay_bound_fails(self):
        # The group answers the consumed demand later than
        # start_delay_ticks — the named nondeterminism the bound exists
        # to catch.
        self.feed.slow_staging = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('beyond start_delay_ticks', detail)
        report.validate_scenario(record)

    def test_alarm_never_standing_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciated leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciated leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_out_of_order_release_fails(self):
        # The alarm holding past the lag's de-stage point breaks the
        # declared falling-edge order.
        self.feed.sticky_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('did not release in order', detail)
        report.validate_scenario(record)

    def test_transitions_never_journaling_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never recorded the declared point_changed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_fails(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('role_changed', detail)
        report.validate_scenario(record)

    def test_nonjournaled_point_journaling_fails(self):
        self.feed.journals_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('non-journaled point', detail)
        report.validate_scenario(record)

    def test_role_move_under_drive_fails(self):
        # A process drive that reads as peer loss — the scenario's
        # role-stability check must catch it.
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('active role moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('acknowledged leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrestored_inflow_fails(self):
        # The restore write reports done but the field reads back a
        # type-confused value — the restore audit must catch it.
        self.plant.lying_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not restore', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_pump_operator_state_fails(self):
        self.feed.moves_pump_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved pump operator state',
                      record.get('detail', ''))
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
        self.assertIn('staging wiring', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool ack input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_schema_instances_is_inconclusive(self):
        self.feed.bare_schema = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('lacks the threshold-chain',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unordered_setpoints_is_inconclusive(self):
        self.feed.bad_setpoints = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not strictly increasing',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mismatched_high_limit_is_inconclusive(self):
        self.feed.bad_high_limit = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('high_limit is not the chain',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_writer_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer claim refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_owner_token_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_owner']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant-writer owner token',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_demand_never_reporting_is_inconclusive(self):
        # The chain's own output never reports a sample — the
        # baseline's settled check can never trust it.
        self.feed.missing_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('settled low-level baseline',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_output_never_reporting_is_inconclusive(self):
        # A leg whose awaited output is absent from the served snapshot
        # is inconclusive, not a staging failure — the scenario's own
        # never-reported split.
        self.feed.missing_ack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('outputs never reported',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
