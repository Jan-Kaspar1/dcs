"""The 1500_source_failover leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_source_failover, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SourceFailoverTests.test_registered_in_scenarios',
    'SourceFailoverTests.test_clean_feed_passes_and_validates',
    'SourceFailoverTests.test_two_runs_produce_identical_evidence',
    'SourceFailoverTests.test_backup_active_never_asserting_fails',
    'SourceFailoverTests.test_selection_tracking_the_degraded_source_fails',
    'SourceFailoverTests.test_frozen_last_known_fails',
    'SourceFailoverTests.test_alarm_never_standing_fails',
    'SourceFailoverTests.test_unacknowledged_never_latching_fails',
    'SourceFailoverTests.test_stalled_demand_fails',
    'SourceFailoverTests.test_frozen_demand_fails',
    'SourceFailoverTests.test_journaled_role_change_fails',
    'SourceFailoverTests.test_transitions_never_journaling_fails',
    'SourceFailoverTests.test_role_move_under_primary_fault_fails',
    'SourceFailoverTests.test_never_reselecting_primary_fails',
    'SourceFailoverTests.test_latch_releasing_with_the_condition_fails',
    'SourceFailoverTests.test_ack_refusal_fails',
    'SourceFailoverTests.test_ack_never_applying_fails',
    'SourceFailoverTests.test_ack_settlement_never_journaling_fails',
    'SourceFailoverTests.test_unattributed_ack_fails',
    'SourceFailoverTests.test_no_active_is_failed',
    'SourceFailoverTests.test_no_failover_select_is_inconclusive',
    'SourceFailoverTests.test_no_wired_alarm_is_inconclusive',
    'SourceFailoverTests.test_no_threshold_chain_is_inconclusive',
    'SourceFailoverTests.test_single_source_is_inconclusive',
    'SourceFailoverTests.test_unwritable_ack_is_inconclusive',
    'SourceFailoverTests.test_bare_signals_is_inconclusive',
    'SourceFailoverTests.test_unhealthy_backup_is_inconclusive',
    'SourceFailoverTests.test_backup_point_unserved_is_inconclusive',
    'SourceFailoverTests.test_no_plant_endpoint_is_inconclusive',
})


class SourceFailoverFeed:
    """A stubbed monitor pair for the source-failover scenario: a tiny
    executor over the failover-select, the wired managed bool-latching
    alarm on its backup_active flag, and the threshold chain on the
    selected level, against a real FakePlantPeer. Every `http_json`
    call is one completed scan: the internal carriers deliver last
    tick's image one scan later (the chain's level input and the
    alarm's `in`), the selector serves the backup sample verbatim once
    the primary's served quality degrades, the chain advances its
    held demand on the failover-fed level under the declared
    setpoints, and the alarm stands on `in`, latches unacknowledged on
    the edge, and releases it on ack's rising edge. Declared-journaled
    points record point_changed; the carriers do not. Fault flags
    stage each named failure the issue calls out."""

    PRIMARY, BACKUP, SELECTED, CHAIN_IN = 10, 11, 200, 201
    DEMAND, DUTY_CALL, LAG_CALL = 204, 212, 213
    BELOW_CUTOFF, HIGH_LEVEL = 214, 215
    BACKUP_ACTIVE, CARRIER, UNHEALTHY = 216, 219, 222
    ACK, ALARM, UNACK = 1020, 1023, 1024
    JOURNALED = (214, 215, 216, 222, 1023, 1024)
    INS = (201, 219, 1020)
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    ON_BAD = 0

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 201, 'signal': 10201, 'name': 'level-chain',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 204, 'signal': 10204, 'name': 'demand',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 212, 'signal': 10212, 'name': 'duty-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 213, 'signal': 10213, 'name': 'lag-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 214, 'signal': 10214, 'name': 'below-cutoff',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 215, 'signal': 10215, 'name': 'high-level',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 216, 'signal': 10216, 'name': 'backup-active',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 219, 'signal': 10219, 'name': 'backup-active-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 222, 'signal': 10222, 'name': 'backup-unhealthy',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1020, 'signal': 11020, 'name': 'backup-active-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1023, 'signal': 11023, 'name': 'backup-active-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1024, 'signal': 11024,
         'name': 'backup-active-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []
        self.ack = False
        self.ack_seen = False   # the last-observed ack level
        self.state = False      # the alarm's tracked `in`
        self.latched = False    # the unacknowledged latch
        self.ever_faulted = False
        # The well sits above `start`: demand 1 is the honest
        # evaluation, so a stalled or fallback answer reads as a
        # divergence the scenario can name.
        self.demand = 1
        self.selected = {'value': {'float': 2.5}, 'quality': 'good',
                         'tick': 0}
        self.chain_in = dict(self.selected)
        self.frozen_demand_tick = None
        self.values = {212: True, 213: False, 214: False, 215: False,
                       216: False, 219: False, 222: False,
                       1023: False, 1024: False}
        self._role_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False          # ctrl-a never reports active
        self.bare_schema = False        # no failover-select declared
        self.bare_signals = False       # the signal index is a stub
        self.no_alarm_wired = False     # no managed alarm on the flag
        self.no_chain = False           # no threshold-chain declared
        self.shared_source = False      # primary and backup coincide
        self.bad_ack_signal = False     # the ack point is unwritable
        self.mute_active = False        # backup_active never asserts
        self.tracks_degraded = False    # out keeps the degraded source
        self.holds_last_known = False   # out freezes the last image
        self.mute_alarm = False         # the alarm never stands
        self.mute_latch = False         # unacknowledged never latches
        self.stall_demand = False       # demand reads the fallback
                                        # under a healthy level
        self.freeze_demand = False      # demand's sample tick stalls
        self.unlatches_on_clear = False # the latch drops with the
                                        # condition — the lifecycle
                                        # changed by the excursion
        self.role_moves = False         # the fault reads as peer loss
        self.journals_role = False      # a role_changed entry lands
        self.no_journal = False         # transitions never journal
        self.ack_rejected = False       # the ack submission is refused
        self.ack_never_applies = False  # the accepted ack never settles
        self.ack_never_journals = False # the settled ack never journals
        self.ack_unattributed = False   # the settlement loses its actor
        self.never_recovers = False     # the clear never re-selects

    def _entry(self, event):
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})
        self.seq += 1

    def _drive(self, point, value):
        previous = self.values[point]
        self.values[point] = value
        if previous != value and point in self.JOURNALED \
                and not self.no_journal:
            self._entry({'point_changed': {
                'point': point, 'from': {'bool': previous},
                'to': {'bool': value}}})

    def _source_sample(self, point):
        sample = dict(self.plant.samples.get(point) or
                      {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0})
        fault = self.plant.faults.get(point)
        if isinstance(fault, dict) and 'quality' in fault:
            sample['quality'] = fault['quality']
        if self.never_recovers and point == self.PRIMARY \
                and self.ever_faulted:
            sample['quality'] = {'bad': 'device_fault'}
        return sample

    def _chain_advance(self, held, level):
        if level <= self.STOP:
            return 0
        if held == 2 and level <= self.START:
            return 1
        if level >= self.LAG_START:
            return 2
        if held == 0 and level >= self.START:
            return 1
        return held

    # One completed scan: the boundary settles queued commands, then
    # the carriers, the selector, the chain, and the alarm step in the
    # wired order.
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
                self.ack = write['value']['bool']
            if not self.ack_never_journals and not self.no_journal:
                body = receipt
                if self.ack_unattributed:
                    body = {'command': receipt['command'],
                            'outcome': receipt['outcome']}
                self._entry({'command_settled': {'receipt': body}})
        # The carriers deliver last scan's image — the one-scan lag
        # the wired scan order implies.
        self.chain_in = dict(self.selected)
        self.values[self.CARRIER] = self.values[self.BACKUP_ACTIVE]
        # The selector: the backup serves while the primary is
        # untrusted; the fault flags stage the value-path bugs the
        # scenario names.
        primary = self._source_sample(self.PRIMARY)
        backup = self._source_sample(self.BACKUP)
        on_backup = primary['quality'] != 'good'
        if on_backup:
            self.ever_faulted = True
        if not (self.holds_last_known and on_backup):
            source = primary if self.tracks_degraded \
                else (backup if on_backup else primary)
            self.selected = dict(source, tick=self.tick)
        self._drive(self.BACKUP_ACTIVE,
                    on_backup and not self.mute_active)
        self._drive(self.UNHEALTHY, backup['quality'] != 'good')
        # The chain evaluates the failover-fed image: a trusted level
        # advances the held demand, an untrusted one falls back to the
        # declared on_bad_demand — the fault flags stall or freeze the
        # evaluation outright.
        level = self.chain_in['value'].get('float', 0.0)
        trusted = self.chain_in['quality'] == 'good'
        if self.freeze_demand and on_backup:
            if self.frozen_demand_tick is None:
                self.frozen_demand_tick = self.tick
        elif self.stall_demand and on_backup:
            self.demand = self.ON_BAD
        elif not trusted:
            self.demand = self.ON_BAD
        else:
            self.demand = self._chain_advance(self.demand, level)
        self._drive(self.DUTY_CALL, self.demand >= 1)
        self._drive(self.LAG_CALL, self.demand >= 2)
        self._drive(self.BELOW_CUTOFF,
                    bool(trusted and level <= self.CUTOFF))
        self._drive(self.HIGH_LEVEL,
                    bool(trusted and level >= self.HIGH))
        # The wired managed alarm: `in` is the flag's carrier, the
        # latch holds until ack's rising edge.
        condition = self.values[self.CARRIER]
        fresh = condition and not self.state
        self.state = condition
        acknowledged = self.ack and not self.ack_seen
        self.ack_seen = self.ack
        self.latched = (self.latched and not acknowledged) or fresh
        if self.unlatches_on_clear and not condition:
            self.latched = False
        self._drive(self.ALARM, condition and not self.mute_alarm)
        self._drive(self.UNACK, self.latched and not self.mute_latch)
        if on_backup and self.journals_role \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})

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
                {'name': 'chain', 'interface': self._interface(
                    'threshold-chain')}]}
        select = [('primary', 10), ('backup', 11), ('out', 200)]
        if self.shared_source:
            select = [('primary', 10), ('backup', 10), ('out', 200)]
        interfaces = [
            {'name': 'select', 'interface': self._interface(
                'failover-select', select,
                [('backup_active', 216), ('backup_unhealthy', 222)])}]
        if not self.no_chain:
            interfaces.append({'name': 'chain', 'interface':
                               self._interface(
                                   'threshold-chain',
                                   [('level', 201), ('demand', 204)],
                                   [('duty_call', 212),
                                    ('lag_call', 213),
                                    ('below_cutoff', 214),
                                    ('high_level', 215)])})
        if not self.no_alarm_wired:
            interfaces.append({'name': 'ba-alarm', 'interface':
                               self._interface(
                                   'managed-bool-latching-alarm',
                                   [('in', 219)],
                                   [('ack', 1020), ('alarm', 1023),
                                    ('unacknowledged', 1024)])})
        return {'interfaces': interfaces}

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': {'tracking': {'aligned':
                                                   self.tick}}}
            if self.no_active \
                    or (self.role_moves and
                        self._source_sample(self.PRIMARY)['quality']
                        != 'good'):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'backup-active-ack'
                          else entry for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/schema'):
            return 200, self._schema()
        if (method, route) == ('GET', '/snapshot'):
            demand_tick = self.tick
            if self.freeze_demand \
                    and self.frozen_demand_tick is not None:
                demand_tick = self.frozen_demand_tick
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': dict(self._source_sample(self.PRIMARY),
                                tick=self.tick)},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': dict(self._source_sample(self.BACKUP),
                                tick=self.tick)},
                {'point': self.SELECTED, 'direction': 'out',
                 'sample': dict(self.selected)},
                {'point': self.CHAIN_IN, 'direction': 'in',
                 'sample': dict(self.chain_in, tick=self.tick)},
                {'point': self.DEMAND, 'direction': 'out',
                 'sample': {'value': {'int': self.demand},
                            'quality': 'good', 'tick': demand_tick}},
                {'point': self.ACK, 'direction': 'in',
                 'sample': {'value': {'bool': self.ack},
                            'quality': 'good', 'tick': self.tick}}]
            for point in sorted(self.values):
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': {'bool': self.values[point]},
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'parameters': [
                             {'name': 'chain', 'values': {
                                 'cutoff': {'float': self.CUTOFF},
                                 'stop': {'float': self.STOP},
                                 'start': {'float': self.START},
                                 'lag_start': {'float': self.LAG_START},
                                 'high': {'float': self.HIGH},
                                 'on_bad_demand': {'int': self.ON_BAD}}}]}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
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


class SourceFailoverTests(unittest.TestCase):
    """scenario_source_failover against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the failover assertions, the alarmed transition, the demand
    continuity, the recovery re-selection, the acknowledgment leg, and
    the inconclusive paths the issue declares."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        # Distinct live values on the two sources so the served
        # selection's tracking is observable: 2.5 sits above `start`,
        # holding demand at 1.
        self.plant.samples = {
            10: {'value': {'float': 2.5}, 'quality': 'good', 'tick': 0},
            11: {'value': {'float': 2.4}, 'quality': 'good', 'tick': 0}}
        self.feed = SourceFailoverFeed(self.plant)

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
                patch.object(scenarios, 'SOURCE_FAILOVER_DEADLINE', 2.0):
            return scenarios.scenario_source_failover(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same settled tracking window as the backup-health leg —
        # the complementary primary-faulted half — ahead of the
        # lag-staging leg's shared window.
        self.assertEqual(
            order.index(scenarios.scenario_backup_health) + 1,
            order.index(scenarios.scenario_source_failover))
        self.assertEqual(
            order.index(scenarios.scenario_source_failover) + 1,
            order.index(scenarios.scenario_lag_staging))
        self.assertIs(verify.case_function('source-failover'),
                      scenarios.scenario_source_failover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: one injection and its clear,
        # and the run left no fault standing.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertEqual(ops.count('inject_fault'), 1)
        self.assertGreaterEqual(ops.count('clear_fault'), 1)
        self.assertEqual(self.plant.faults, {})
        # The ack point was written true then restored false through
        # the receipted path, and the latch released through it.
        self.assertFalse(self.feed.ack)
        self.assertFalse(self.feed.latched)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = FakePlantPeer()
        self.addCleanup(plant2.close)
        plant2.samples = dict(self.plant.samples)
        feed2 = SourceFailoverFeed(plant2)
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

    def test_backup_active_never_asserting_fails(self):
        # The named failover clause: the selection moved but
        # backup_active stayed deasserted — a dead flag beside a live
        # switch.
        self.feed.mute_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('backup_active never asserted',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_selection_tracking_the_degraded_source_fails(self):
        self.feed.tracks_degraded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking the degraded source or a frozen '
                      'last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_frozen_last_known_fails(self):
        self.feed.holds_last_known = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking the degraded source or a frozen '
                      'last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_standing_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the wired alarm never stood',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never latched unacknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stalled_demand_fails(self):
        # The chain answers the untrusted-input fallback while the
        # failover-fed level serves healthy — demand stalled.
        self.feed.stall_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demand stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_frozen_demand_fails(self):
        self.feed.freeze_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demand evaluation froze',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_fails(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role_changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_transitions_never_journaling_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never journaled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_move_under_primary_fault_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the active role',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_reselecting_primary_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never re-selected the primary',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_releasing_with_the_condition_fails(self):
        # The excursion changed the two-flag lifecycle: the
        # unacknowledged latch dropped with the alarm condition
        # instead of standing for the declared ack.
        self.feed.unlatches_on_clear = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('latch released before the declared ack',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the standing unacknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_settlement_never_journaling_fails(self):
        self.feed.ack_never_journals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('CommandSettled never journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_ack_fails(self):
        self.feed.ack_unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_failover_select_is_inconclusive(self):
        self.feed.bare_schema = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no failover-select instance',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_wired_alarm_is_inconclusive(self):
        self.feed.no_alarm_wired = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no managed alarm is wired',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_threshold_chain_is_inconclusive(self):
        self.feed.no_chain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no threshold-chain', record.get('detail', ''))
        report.validate_scenario(record)

    def test_single_source_is_inconclusive(self):
        # The model declares no second source to switch between —
        # nothing the leg may prove.
        self.feed.shared_source = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('distinct pair', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool input', record.get('detail', ''))
        report.validate_scenario(record)

    def test_bare_signals_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a served float field input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unhealthy_backup_is_inconclusive(self):
        # The field's own census shows the backup already degraded —
        # no two distinct healthy field source points to switch
        # between.
        self.plant.faults[11] = {'quality': {'bad': 'device_fault'}}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('healthy field source points',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_point_unserved_is_inconclusive(self):
        self.plant.samples.pop(11)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a field in-point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
