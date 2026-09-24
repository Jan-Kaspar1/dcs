"""The 1400_backup_health leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_backup_health, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'BackupHealthTests.test_registered_in_scenarios',
    'BackupHealthTests.test_clean_feed_passes_and_validates',
    'BackupHealthTests.test_two_runs_produce_identical_evidence',
    'BackupHealthTests.test_unhealthy_never_asserting_fails',
    'BackupHealthTests.test_alarm_never_standing_fails',
    'BackupHealthTests.test_unacknowledged_never_latching_fails',
    'BackupHealthTests.test_selection_moving_to_the_backup_fails',
    'BackupHealthTests.test_role_move_under_backup_fault_fails',
    'BackupHealthTests.test_journaled_role_change_fails',
    'BackupHealthTests.test_transitions_never_journaling_fails',
    'BackupHealthTests.test_ack_refusal_fails',
    'BackupHealthTests.test_ack_never_applying_fails',
    'BackupHealthTests.test_ack_settlement_never_journaling_fails',
    'BackupHealthTests.test_unattributed_ack_fails',
    'BackupHealthTests.test_recovery_never_landing_fails',
    'BackupHealthTests.test_no_tracking_pair_is_inconclusive',
    'BackupHealthTests.test_missing_wiring_is_inconclusive',
    'BackupHealthTests.test_no_plant_endpoint_is_inconclusive',
    'BackupHealthTests.test_backup_point_unserved_is_inconclusive',
    'BackupHealthTests.test_no_active_is_failed',
})


class BackupHealthFeed:
    """A stubbed monitor pair for the backup-health scenario: a tiny
    executor over the failover-select's backup leg and the wired
    managed bool-latching alarm, against a real FakePlantPeer. Every
    `http_json` call is one completed scan: the link lands the health
    output's last image on the alarm's `in` carrier, the selector
    drives backup_unhealthy off the plant-served backup quality, and
    the latching alarm stands on `in`, holds unacknowledged on its
    edge latch, and consumes ack on its rising edge — so the carrier
    delay means the
    full annunciation lands a scan behind the injected fault.
    Declared-journaled points record point_changed; the carrier does
    not. Fault flags stage each named failure the issue calls out."""

    PRIMARY, BACKUP, SELECTED = 10, 11, 200
    BACKUP_ACTIVE = 216
    UNHEALTHY, CARRIER = 222, 223
    ACK, ALARM, UNACK = 1060, 1063, 1064
    JOURNALED = (216, 222, 1063, 1064)

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 216, 'signal': 10216, 'name': 'backup-active',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 222, 'signal': 10222, 'name': 'backup-unhealthy',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 223, 'signal': 10223, 'name': 'backup-unhealthy-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 1060, 'signal': 11060, 'name': 'backup-unhealthy-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1063, 'signal': 11063, 'name': 'backup-unhealthy-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1064, 'signal': 11064,
         'name': 'backup-unhealthy-unacknowledged',
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
        self.values = {216: False, 222: False, 223: False,
                       1063: False, 1064: False}
        self._role_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False          # ctrl-a never reports active
        self.no_tracking = False        # the peer never reports tracking
        self.bare_signals = False       # the annunciation wiring absent
        self.mute_unhealthy = False     # backup_unhealthy never asserts
        self.mute_alarm = False         # the alarm never stands
        self.mute_latch = False         # unacknowledged never latches
        self.selects_backup = False     # the selection moves anyway
        self.role_moves = False         # the fault reads as peer loss
        self.journals_role = False      # a role_changed entry lands
        self.no_journal = False         # transitions never journal
        self.ack_rejected = False       # the ack submission is refused
        self.ack_never_applies = False  # the accepted ack never settles
        self.ack_never_journals = False # the settled ack never journals
        self.ack_unattributed = False   # the settlement loses its actor
        self.never_recovers = False     # the clear never returns

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

    def _backup_sample(self):
        sample = dict(self.plant.samples.get(self.BACKUP) or
                      {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0})
        fault = self.plant.faults.get(self.BACKUP)
        if isinstance(fault, dict) and 'quality' in fault:
            sample['quality'] = fault['quality']
        if self.never_recovers and self.ever_faulted:
            sample['quality'] = {'bad': 'device_fault'}
        return sample

    # One completed scan: the boundary settles queued commands, then
    # the link, the selector, and the alarm step in order.
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
            if not self.ack_never_journals:
                body = receipt
                if self.ack_unattributed:
                    body = {'command': receipt['command'],
                            'outcome': receipt['outcome']}
                self._entry({'command_settled': {'receipt': body}})
        # The link's carrier: `in` reads the health output's last
        # image — a one-scan lag, as the wired scan order implies.
        self.values[self.CARRIER] = self.values[self.UNHEALTHY]
        quality = self._backup_sample().get('quality')
        degraded = quality != 'good'
        if degraded:
            self.ever_faulted = True
        self._drive(self.UNHEALTHY,
                    degraded and not self.mute_unhealthy)
        self._drive(self.BACKUP_ACTIVE,
                    bool(self.selects_backup and degraded))
        condition = self.values[self.CARRIER]
        fresh = condition and not self.state
        self.state = condition
        acknowledged = self.ack and not self.ack_seen
        self.ack_seen = self.ack
        self.latched = (self.latched and not acknowledged) or fresh
        self._drive(self.ALARM, condition and not self.mute_alarm)
        self._drive(self.UNACK, self.latched and not self.mute_latch)
        if degraded and self.journals_role and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})

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
                    or (self.role_moves and self.values[self.UNHEALTHY]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            if self.bare_signals:
                return 200, {'points': [
                    {'point': 10, 'signal': 10010, 'name':
                     'level-primary', 'direction': 'in',
                     'value_type': 'float', 'writable': False}]}
            return 200, {'points': list(self.SIGNALS)}
        if (method, route) == ('GET', '/snapshot'):
            backup = self._backup_sample()
            selected = dict(self.plant.served(self.PRIMARY)['value'])
            if self.values[self.BACKUP_ACTIVE]:
                selected = dict(backup['value'])
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': dict(self.plant.served(self.PRIMARY),
                                tick=self.tick)},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': dict(backup, tick=self.tick)},
                {'point': self.SELECTED, 'direction': 'out',
                 'sample': {'value': selected, 'quality': 'good',
                            'tick': self.tick}},
                {'point': self.ACK, 'direction': 'in',
                 'sample': {'value': {'bool': self.ack},
                            'quality': 'good', 'tick': self.tick}}]
            for point in (self.BACKUP_ACTIVE, self.UNHEALTHY,
                          self.CARRIER, self.ALARM, self.UNACK):
                points.append({
                    'point': point,
                    'direction': 'in' if point == self.CARRIER
                    else 'out',
                    'sample': {'value': {'bool': self.values[point]},
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points}
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
                                     'queue_full': {
                                         'point': write['point'],
                                         'capacity': 4}}}},
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


class BackupHealthTests(unittest.TestCase):
    """scenario_backup_health against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    each annunciation assertion, the no-transition invariant, the
    acknowledgment leg, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        self.plant.samples = {
            10: {'value': {'float': 1.5}, 'quality': 'good', 'tick': 0},
            11: {'value': {'float': 1.4}, 'quality': 'good', 'tick': 0}}
        self.feed = BackupHealthFeed(self.plant)

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
                patch.object(scenarios, 'BACKUP_HEALTH_DEADLINE', 2.0):
            return scenarios.scenario_backup_health(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same restored pre-switch window as the force case, ahead
        # of the tune case's a->b switch — the settled tracking pair.
        # The source-failover leg — the complementary primary-faulted
        # half — shares the window directly behind, then lag-staging.
        self.assertEqual(
            order.index(scenarios.scenario_force_carryover) + 1,
            order.index(scenarios.scenario_backup_health))
        self.assertEqual(
            order.index(scenarios.scenario_backup_health) + 1,
            order.index(scenarios.scenario_source_failover))
        self.assertIs(verify.case_function('backup-health'),
                      scenarios.scenario_backup_health)

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
        self.assertEqual(ops.count('clear_fault'), 1)
        self.assertEqual(self.plant.faults, {})
        # The ack point was written true then restored false through
        # the receipted path.
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
        feed2 = BackupHealthFeed(plant2)
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

    def test_unhealthy_never_asserting_fails(self):
        self.feed.mute_unhealthy = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('backup_unhealthy never asserted',
                      record.get('detail', ''))
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

    def test_selection_moving_to_the_backup_fails(self):
        # The no-failover invariant: backup_active asserts and the
        # served selection leaves the primary under a backup-only
        # fault.
        self.feed.selects_backup = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('backup_active asserted', detail)
        self.assertIn('selection left the primary', detail)
        report.validate_scenario(record)

    def test_role_move_under_backup_fault_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the active role', record.get('detail', ''))
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
        self.assertIn('never recorded point_changed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused', record.get('detail', ''))
        report.validate_scenario(record)
        # The refused write never stood — nothing to restore.
        self.assertFalse(self.feed.ack)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the unacknowledged latch',
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

    def test_recovery_never_landing_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never returned after the clear',
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
        self.assertIn('annunciation wiring', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_point_unserved_is_inconclusive(self):
        # The field's own census does not list the backup point — the
        # scenario may not fault what the plant does not serve.
        self.plant.samples.pop(11)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a field in-point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        # A pair mid-transition — no endpoint reports settled active —
        # is the rig's own failure, not an unreachable one.
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
