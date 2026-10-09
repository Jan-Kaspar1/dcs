"""The 1630_low_level_cutoff leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_low_level_cutoff, following
the per-leg split convention (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class CutoffPlantPeer(StagingPlantPeer):
    """The cutoff rig's plant half: the shared-claim peer plus the
    write-refusal lever the refused-drive case stages."""

    def __init__(self, owner):
        super().__init__(owner)
        self.refuse_writes = False   # the field refuses driven writes

    def dispatch(self, request):
        if self.refuse_writes and request.get('op') == 'write':
            self.requests.append(request)
            return {'result': 'error', 'error': {
                'kind': 'fenced',
                'detail': 'the write window is not held'}}
        return super().dispatch(request)


class CutoffFeed:
    """A stubbed monitor pair for the low-level-cutoff scenario:
    ctrl-a runs a tiny executor over the station's threshold chain,
    the pump group's delayed staging, the cutoff clamp, and the
    managed `lal` alarm's two-flag lifecycle, against a real
    plant-protocol peer whose `inflow` point the scenario writes
    through the shared writer claim. Every `http_json` call is one
    completed scan — the internal carriers deliver the last image one
    scan later, the chain holds its demand between the hysteresis
    bands, and the dynamics integrate the driven inflow minus each
    commanded pump's draw. Declared-journaled points record
    point_changed and every accepted command settles its receipt at
    the next scan's boundary. Fault flags stage each named failure
    the issue calls out."""

    LEVEL_IN, SELECTED = 201, 200
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED = 210, 211
    DUTY_CALL, LAG_CALL = 212, 213
    BELOW = 214
    P1_RUN, P2_RUN = 40, 41
    P1_CMD, P2_CMD = 100, 101
    LAL_ACK, LAL_ALARM, LAL_UNACK = 1010, 1013, 1014
    LAL_SHELVED, LAL_SUPPRESSED, LAL_OOS = 1015, 1016, 1017
    INFLOW = 12
    JOURNALED = (40, 41, 214, 1013, 1014, 1015, 1016, 1017)
    WRITABLE = (1010,)
    INS = (12, 1010)
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    LAL_LOW, LAL_HYST = 0.5, 0.1
    BIAS, DRAW, DT = 0.2, 1.0, 0.1   # ambient fill, per-pump draw,
                                    # the sim's dt per scan
    START_DELAY = 1

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []
        self.demand_held = 0
        self.duty_index = 0
        self.cmd = [False, False]
        self.last_start = None
        self.lal_state = 'clear'
        self.latched = False
        self.level = 2.2
        self._saw_clamp = False
        self.values = {
            12: 0.0, 200: 2.2, 201: 2.2,
            204: 0, 205: 0, 210: 1, 211: 0,
            212: False, 213: False, 214: False,
            40: False, 41: False, 100: False, 101: False,
            1010: False, 1013: False, 1014: False, 1015: False,
            1016: False, 1017: False}
        self.jseen = {}
        # Fault injection for the named-failure cases.
        self.no_active = False         # ctrl-a never reports active
        self.no_tracking = False       # the peer never tracks
        self.bare_signals = False      # the leg's wiring absent
        self.unwritable_ack = False    # lal-ack served non-writable
        self.no_chain = False          # no threshold-chain descriptor
        self.bad_setpoints = False     # the served table unordered
        self.no_lal = False            # no managed alarm binds lal
        self.bad_lal_params = False    # lal reports no low_limit
        self.frozen_level = False      # the drain never moves the well
        self.no_clamp = False          # below_cutoff never asserts
        self.mute_lal = False          # the alarm never annunciates
        self.mute_unack = False        # the latch never stands
        self.latch_sticks = False      # the ack never clears it
        self.sticky_lal = False        # the alarm holds past its
                                       # hysteresis release
        self.no_resume = False         # demand stays 0 past start
        self.role_moves = False        # the field write reads as
                                       # peer loss
        self.no_journal = False        # point_changed never lands
        self.no_receipts = False       # command_settled never lands
        self.write_refused = False     # the command path rejects
        self.flags_unserved = False    # the lal flag points absent

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

    def _step_chain(self):
        level = self.values[self.LEVEL_IN]
        if self.no_resume and self._saw_clamp:
            self.demand_held = 0
        elif level <= self.STOP:
            self.demand_held = 0
        elif self.demand_held == 2 and level <= self.START:
            self.demand_held = 1
        elif level >= self.LAG_START:
            self.demand_held = 2
        elif self.demand_held == 0 and level >= self.START:
            self.demand_held = 1
        demand = self.demand_held
        for point, value in (
                (self.DEMAND, demand),
                (self.DUTY_CALL, demand >= 1),
                (self.LAG_CALL, demand >= 2)):
            self.values[point] = value
        below = level <= self.CUTOFF and not self.no_clamp
        self._saw_clamp = self._saw_clamp or below
        self.values[self.BELOW] = below

    def _step_group(self):
        demand = self.values[self.DEMAND_IN]
        demand = demand if isinstance(demand, int) \
            and not isinstance(demand, bool) else 0
        order = [self.duty_index, (self.duty_index + 1) % 2]
        targets = order[:max(0, min(2, demand))]
        for index in targets:
            if not self.cmd[index] and (
                    self.last_start is None
                    or self.tick >= self.last_start
                    + self.START_DELAY):
                self.cmd[index] = True
                self.last_start = self.tick
        for index in range(2):
            if index not in targets:
                self.cmd[index] = False
        for index, point in ((0, self.P1_CMD), (1, self.P2_CMD),
                             (0, self.P1_RUN), (1, self.P2_RUN)):
            self.values[point] = self.cmd[index]
        self.values[self.DUTY] = self.duty_index + 1
        self.values[self.STAGED] = int(sum(self.cmd))

    def _step_lal(self):
        """The low alarm's two-flag lifecycle: trips at low_limit,
        holds through the declared hysteresis, the unacknowledged
        latch standing until the receipted ack dominates."""
        pv = self.level
        previous = self.lal_state
        if previous == 'low':
            released = pv > self.LAL_LOW + self.LAL_HYST \
                and not self.sticky_lal
            state = 'clear' if released else 'low'
        else:
            state = 'low' if pv <= self.LAL_LOW else 'clear'
        self.lal_state = state
        fresh = state == 'low' and previous != 'low'
        self.latched = self.latched or fresh
        if self.values[self.LAL_ACK] and not self.latch_sticks:
            self.latched = False
        self.values[self.LAL_ALARM] = state == 'low' \
            and not self.mute_lal
        self.values[self.LAL_UNACK] = self.latched \
            and not self.mute_unack

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
        # The internal carriers deliver last tick's image.
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        self.values[self.LEVEL_IN] = self.level
        self._step_chain()
        self._step_group()
        self._step_lal()
        self._journal_values()
        # The dynamics step: the driven inflow plus the ambient bias
        # minus each commanded pump's draw — a missing or
        # type-confused held value reads as still water.
        inflow = (self.plant.samples.get(self.INFLOW) or {}) \
            .get('value', {}).get('float', 0.0)
        if not self.frozen_level:
            self.level += (inflow + self.BIAS
                           - self.DRAW * sum(self.cmd)) * self.DT
        self.values[self.SELECTED] = self.level
        self.values[self.INFLOW] = inflow

    def _signals(self):
        def sig(point, name, direction='out', value_type='bool',
                writable=False):
            return {'point': point, 'signal': 10000 + point,
                    'name': name, 'direction': direction,
                    'value_type': value_type, 'writable': writable}
        return [
            sig(12, 'inflow', 'in', 'float'),
            sig(200, 'level-selected', 'out', 'float'),
            sig(204, 'demand', 'out', 'int'),
            sig(205, 'demand-in', 'in', 'int'),
            sig(210, 'duty', 'out', 'int'),
            sig(211, 'staged', 'out', 'int'),
            sig(212, 'duty-call'),
            sig(213, 'lag-call'),
            sig(214, 'below-cutoff'),
            sig(40, 'p101-run', 'in'),
            sig(41, 'p102-run', 'in'),
            sig(100, 'p101-cmd'),
            sig(101, 'p102-cmd'),
            sig(1010, 'lal-ack', 'in', 'bool',
                not self.unwritable_ack),
            sig(1013, 'lal-alarm'),
            sig(1014, 'lal-unacknowledged'),
            sig(1015, 'lal-shelved'),
            sig(1016, 'lal-suppressed'),
            sig(1017, 'lal-out-of-service')]

    def _descriptors(self):
        entries = []
        if not self.no_chain:
            entries.append({
                'name': 'chain', 'kind': 'threshold-chain',
                'ports': [{'name': 'level', 'point': 201},
                          {'name': 'demand', 'point': 204}]})
        if not self.no_lal:
            entries.append({
                'name': 'lal', 'kind': 'managed-latching-alarm',
                'ports': [{'name': 'in', 'point': 203},
                          {'name': 'alarm', 'point': self.LAL_ALARM},
                          {'name': 'ack', 'point': self.LAL_ACK},
                          {'name': 'unacknowledged',
                           'point': self.LAL_UNACK}]})
        return entries

    def _parameters(self):
        table = ({'cutoff': self.CUTOFF, 'stop': self.STOP,
                  'start': self.START, 'lag_start': self.LAG_START,
                  'high': self.HIGH}
                 if not self.bad_setpoints
                 else {'cutoff': self.START, 'stop': self.STOP,
                       'start': self.CUTOFF,
                       'lag_start': self.LAG_START,
                       'high': self.HIGH})
        lal_values = {'high_limit': {'float': 1e9},
                      'hysteresis': {'float': self.LAL_HYST}}
        if not self.bad_lal_params:
            lal_values['low_limit'] = {'float': self.LAL_LOW}
        return [
            {'name': 'chain',
             'values': {key: {'float': value}
                        for key, value in table.items()}},
            {'name': 'lal', 'values': lal_values}]

    def _snapshot_body(self):
        flag_points = {self.LAL_ALARM, self.LAL_UNACK,
                       self.LAL_SHELVED, self.LAL_SUPPRESSED,
                       self.LAL_OOS}
        points = []
        for point, value in sorted(self.values.items()):
            if self.flags_unserved and point in flag_points:
                continue
            points.append({
                'point': point,
                'direction': 'in' if point in self.INS else 'out',
                'sample': {'value': self._wrap(value),
                           'quality': 'good', 'tick': self.tick}})
        return {'tick': self.tick, 'points': points,
                'descriptors': self._descriptors(),
                'parameters': self._parameters()}

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
                    or (self.role_moves
                        and self.plant.write_count >= 1):
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


class LowLevelCutoffTests(unittest.TestCase):
    """scenario_low_level_cutoff against the stubbed rig: the feed's
    scan-per-call timing is deterministic so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the drain, the clamp, the lifecycle's ack-while-standing and its
    hysteresis release, the demand resume, the journal contract, and
    the inconclusive paths."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = CutoffPlantPeer(self.OWNER)
        self.feed = CutoffFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.OWNER,
                                'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CUTOFF_POLL', 0.001), \
                patch.object(scenarios, 'CUTOFF_DEADLINE', 3.0), \
                patch.object(scenarios, 'CUTOFF_TRAVERSE', 3.0):
            return scenarios.scenario_low_level_cutoff(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The cutoff leg follows the manual-takeover leg in the
        # restored window ahead of standby-loss.
        self.assertEqual(
            order.index(scenarios.scenario_manual_takeover) + 1,
            order.index(scenarios.scenario_low_level_cutoff))
        self.assertIs(verify.case_function('low-level-cutoff'),
                      scenarios.scenario_low_level_cutoff)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim
        # attachment, one read for the baseline, the drain/hold/
        # restore writes.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertEqual(ops.count('write'), 3)
        # The driven input restored, the ack re-armed, the alarm
        # stood down, and the well back in the demand band.
        self.assertEqual(
            self.plant.samples[12]['value'], {'float': 0.0})
        self.assertFalse(self.feed.latched)
        self.assertFalse(self.feed.values[self.feed.LAL_ACK])
        self.assertEqual(self.feed.lal_state, 'clear')
        self.assertGreater(self.feed.values[self.feed.DEMAND], 0)
        # Both receipted ack submissions settled applied, attributed.
        writes = [entry for entry in self.feed.journal
                  if 'command_settled' in entry.get('event', {})]
        self.assertEqual(len(writes), 2)
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
        plant2 = CutoffPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = CutoffFeed(plant2)
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

    def test_lifecycle_journaled_in_order(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        changes = {}
        for entry in self.feed.journal:
            change = entry.get('event', {}).get('point_changed')
            if change:
                changes.setdefault(change['point'], []).append(
                    change['to'])
        # The clamp's assert/release, the lal lifecycle's
        # annunciate/latch/ack/return, and the stopped pump's run
        # contact all recorded.
        for point in (self.feed.BELOW, self.feed.LAL_ALARM,
                      self.feed.LAL_UNACK):
            seq = changes.get(point, [])
            self.assertIn({'bool': True}, seq, point)
            self.assertIn({'bool': False}, seq, point)
        self.assertIn({'bool': False},
                      changes.get(self.feed.P1_RUN, []))

    def test_level_never_draining_fails(self):
        self.feed.frozen_level = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_clamp_never_engaging_fails(self):
        self.feed.no_clamp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_annunciating_fails(self):
        self.feed.mute_lal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_never_clearing_fails(self):
        self.feed.latch_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_returning_fails(self):
        self.feed.sticky_lal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demand_never_resuming_fails(self):
        self.feed.no_resume = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_move_under_drive_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('active role moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_transitions_fail(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_receipts_fail(self):
        self.feed.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cutoff-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_write_refused_fails(self):
        self.feed.write_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack', record.get('detail', ''))
        report.validate_scenario(record)

    def test_inflow_write_refused_fails(self):
        self.plant.refuse_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('inflow', record.get('detail', ''))
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

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.unwritable_ack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool ack input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_chain_is_inconclusive(self):
        self.feed.no_chain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('threshold-chain', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unordered_setpoints_is_inconclusive(self):
        self.feed.bad_setpoints = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('cutoff<stop<start',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unbound_lal_is_inconclusive(self):
        self.feed.no_lal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('managed-latching-alarm',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_lal_params_is_inconclusive(self):
        self.feed.bad_lal_params = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('low_limit', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_owner_token_is_inconclusive(self):
        ctx = self._ctx()
        ctx['plant_owner'] = {}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('owner token', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_writer_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('refused the shared attachment',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_inflow_not_field_is_inconclusive(self):
        del self.plant.samples[12]
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a field in-point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_bad_inflow_baseline_is_inconclusive(self):
        self.plant.samples[12] = {'value': {'int': 0},
                                  'quality': 'good', 'tick': 0}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no float baseline',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_flags_unserved_is_inconclusive(self):
        self.feed.flags_unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
