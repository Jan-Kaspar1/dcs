"""The 3800_unavailable_fallback leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_unavailable_fallback, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'UnavailableFallbackTests.test_registered_in_scenarios',
    'UnavailableFallbackTests.test_clean_feed_passes_and_validates',
    'UnavailableFallbackTests.test_two_runs_produce_identical_evidence',
    'UnavailableFallbackTests.test_silent_annunciation_fails',
    'UnavailableFallbackTests.test_backup_only_fault_moving_the_selection_fails',
    'UnavailableFallbackTests.test_backup_only_fault_journaling_a_failover_fails',
    'UnavailableFallbackTests.test_annunciation_never_journaled_fails',
    'UnavailableFallbackTests.test_all_bad_never_engaging_the_fallback_fails',
    'UnavailableFallbackTests.test_held_output_under_all_bad_fails',
    'UnavailableFallbackTests.test_controlling_on_untrusted_level_fails',
    'UnavailableFallbackTests.test_recovered_primary_never_reselected_fails',
    'UnavailableFallbackTests.test_annunciation_never_clearing_fails',
    'UnavailableFallbackTests.test_no_plant_endpoint_is_inconclusive',
    'UnavailableFallbackTests.test_unwired_annunciation_port_is_inconclusive',
    'UnavailableFallbackTests.test_no_healthy_baseline_is_inconclusive',
})


class FallbackFeed:
    """A stubbed monitor pair for the unavailable-fallback scenario —
    the pump-station level path the fixture wires: the failover-select
    over the plant's level-primary (10) and level-backup (11) field
    inputs serving `out` on 200, fanned out to the threshold-chain's
    `level` on 201 and driving `demand` on 204, the journaled
    backup_active (216) and backup_unhealthy (222) reports, and the
    bool-latching alarm the engagement fans out to through 219. Every
    `http_json` call is one completed scan: the derived points are
    recomputed from the plant's served samples — `out` carries the
    primary while it reads Good and finite, else the backup verbatim —
    and the journaled carriers' point_changed records land at the scan
    boundary their value transitions. Fault flags stage each named
    failure the issue calls out."""

    PRIMARY, BACKUP = 10, 11
    OUT, LEVEL, DEMAND = 200, 201, 204
    ACTIVE, ALARM_IN, UNHEALTHY = 216, 219, 222
    ALARM, UNACK = 1003, 1004
    ON_BAD = 0      # the declared on_bad_demand the parameters serve
    COMPUTED = 2    # a computed stage count distinct from ON_BAD

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.journal = []
        self.next_seq = 1
        self.prev_active = False
        self.prev_unhealthy = False
        self.phantom_journaled = False
        self.held_out = None
        self._out = {'value': {'float': 0.0}, 'quality': 'good'}
        self._demand = self.COMPUTED
        self._active = False
        self._unhealthy = False
        # Fault injection for the named-failure cases.
        self.silent_annunciation = False    # backup_unhealthy never asserts
        self.selects_on_backup_fault = False  # a backup-only fault moves the mode
        self.phantom_failover_journal = False  # a failover journals unserved
        self.silent_failover = False        # backup_active never asserts
        self.holds_last_value = False       # all-bad: out keeps its last Good
        self.controls_on_untrusted = False  # demand keeps computing on Bad level
        self.never_resumes = False          # the mode flag stays latched
        self.annunciation_sticks = False    # backup_unhealthy stays latched
        self.skips_journal = False          # transitions never reach the journal
        self.drop_unhealthy_port = False    # the descriptor drops the port
        self.backup_owns_field = False      # no healthy baseline presents

    @staticmethod
    def _float(sample):
        value = (sample or {}).get('value')
        if isinstance(value, dict):
            value = next(iter(value.values()), None)
        return value

    @classmethod
    def _ok(cls, sample):
        """The selector's trustworthy-measurement predicate: Good with
        a finite numeric value."""
        value = cls._float(sample)
        return (sample or {}).get('quality') == 'good' \
            and isinstance(value, (int, float)) and math.isfinite(value)

    def _changed(self, point, old, new):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'point_changed': {
                                 'point': point,
                                 'from': {'bool': old},
                                 'to': {'bool': new}}}})
        self.next_seq += 1

    # The scan: derived points recomputed from the plant's served
    # samples, each fault flag bending the one behavior it names.
    def _advance(self):
        self.tick += 1
        primary = self.plant.served(self.PRIMARY)
        backup = self.plant.served(self.BACKUP)
        primary_ok = self._ok(primary)
        backup_ok = self._ok(backup)
        # `out` follows the declared selection — the primary while it
        # reads Good and finite, else the backup verbatim — unless a
        # flag substitutes the served sample.
        declared = primary if primary_ok else backup
        out = declared
        if self.holds_last_value and not (primary_ok or backup_ok):
            out = self.held_out or declared
        elif self._ok(declared):
            self.held_out = dict(declared)
        active = not primary_ok
        if self.backup_owns_field \
                or (self.selects_on_backup_fault and not backup_ok) \
                or (self.never_resumes and self.prev_active):
            active = True
        if self.silent_failover:
            active = False
        unhealthy = not backup_ok
        if self.silent_annunciation:
            unhealthy = False
        if self.annunciation_sticks and self.prev_unhealthy:
            unhealthy = True
        # The point-to-point fanout delivers `out` to the chain's
        # `level` verbatim; the chain emits its declared on_bad_demand
        # while the delivered level is untrusted.
        level_ok = self._ok(out)
        self._demand = self.COMPUTED if level_ok or \
            self.controls_on_untrusted else self.ON_BAD
        self._out = {'value': out.get('value'),
                     'quality': out.get('quality')}
        self._active = active
        self._unhealthy = unhealthy
        # The journaled carriers' point_changed records land at the
        # scan boundary their served value transitions.
        if not self.skips_journal:
            if active != self.prev_active:
                self._changed(self.ACTIVE, self.prev_active, active)
            if unhealthy != self.prev_unhealthy:
                self._changed(self.UNHEALTHY, self.prev_unhealthy,
                              unhealthy)
        if self.phantom_failover_journal and primary_ok \
                and not backup_ok and not self.phantom_journaled:
            # A failover transition the telemetry never served still
            # reaches the durable record — the journaled-without-served
            # form of the backup-only leg's named failure.
            self.phantom_journaled = True
            self._changed(self.ACTIVE, False, True)
        self.prev_active = active
        self.prev_unhealthy = unhealthy

    def _port(self, name, direction, kind, point):
        return {'name': name, 'direction': direction, 'kind': kind,
                'point': point}

    def _descriptors(self):
        select_ports = [
            self._port('primary', 'in', 'float', self.PRIMARY),
            self._port('backup', 'in', 'float', self.BACKUP),
            self._port('out', 'out', 'float', self.OUT),
            self._port('backup_active', 'out', 'bool', self.ACTIVE)]
        if not self.drop_unhealthy_port:
            select_ports.append(
                self._port('backup_unhealthy', 'out', 'bool',
                           self.UNHEALTHY))
        return [
            {'name': 'level-select', 'kind': 'failover-select',
             'label': 'level-select', 'ports': select_ports,
             'parameters': []},
            {'name': 'level-chain', 'kind': 'threshold-chain',
             'label': 'level-chain',
             'ports': [self._port('level', 'in', 'float', self.LEVEL),
                       self._port('demand', 'out', 'int', self.DEMAND)],
             'parameters': [{'name': 'on_bad_demand', 'kind': 'int'}]},
            {'name': 'backup-active-alarm', 'kind': 'bool-latching-alarm',
             'label': 'backup-active-alarm',
             'ports': [self._port('in', 'in', 'bool', self.ALARM_IN),
                       self._port('alarm', 'out', 'bool', self.ALARM),
                       self._port('unacknowledged', 'out', 'bool',
                                  self.UNACK)],
             'parameters': []}]

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            def served(point):
                return dict(self.plant.served(point), tick=self.tick)

            def computed(value):
                return {'value': value, 'quality': 'good',
                        'tick': self.tick}
            out = dict(self._out, tick=self.tick)
            return 200, {
                'tick': self.tick,
                'points': [
                    {'point': self.PRIMARY, 'direction': 'in',
                     'sample': served(self.PRIMARY)},
                    {'point': self.BACKUP, 'direction': 'in',
                     'sample': served(self.BACKUP)},
                    {'point': self.OUT, 'direction': 'out',
                     'sample': out},
                    {'point': self.LEVEL, 'direction': 'in',
                     'sample': dict(out)},
                    {'point': self.DEMAND, 'direction': 'out',
                     'sample': computed({'int': self._demand})},
                    {'point': self.ACTIVE, 'direction': 'out',
                     'sample': computed({'bool': self._active})},
                    {'point': self.ALARM_IN, 'direction': 'in',
                     'sample': computed({'bool': self._active})},
                    {'point': self.UNHEALTHY, 'direction': 'out',
                     'sample': computed({'bool': self._unhealthy})},
                    {'point': self.ALARM, 'direction': 'out',
                     'sample': computed({'bool': self._active})},
                    {'point': self.UNACK, 'direction': 'out',
                     'sample': computed({'bool': self._active})}],
                'descriptors': self._descriptors(),
                'parameters': [{'name': 'level-chain', 'values': {
                    'on_bad_demand': {'int': self.ON_BAD}}}]}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s' % (method, url))


class UnavailableFallbackTests(unittest.TestCase):
    """scenario_unavailable_fallback against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the backup-degraded no-transition leg, the all-bad
    declared-demand leg, both ordered-recovery legs, the
    no-unintended-step assertion, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        # The level sources the served failover-select is wired to.
        self.plant.samples = {
            FallbackFeed.PRIMARY: {'value': {'float': 1.5},
                                   'quality': 'good', 'tick': 0},
            FallbackFeed.BACKUP: {'value': {'float': 1.4},
                                  'quality': 'good', 'tick': 0}}
        self.feed = FallbackFeed(self.plant)

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
                patch.object(scenarios, 'FALLBACK_DEADLINE', 2.0):
            return scenarios.scenario_unavailable_fallback(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_unclaimed_rearm) + 1,
            order.index(scenarios.scenario_unavailable_fallback))
        self.assertIs(verify.case_function('unavailable-fallback'),
                      scenarios.scenario_unavailable_fallback)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The legs drove the documented per-point fault surface — one
        # injection and one clear per level source — and the run left
        # no fault behind on the shared field.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertEqual(ops.count('inject_fault'), 2)
        self.assertEqual(ops.count('clear_fault'), 2)
        self.assertEqual(self.plant.faults, {})

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
        feed2 = FallbackFeed(plant2)
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

    def test_silent_annunciation_fails(self):
        # The backup-degraded leg: the standby-health report never
        # asserts though the backup is serving Bad.
        self.feed.silent_annunciation = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never asserted the backup_unhealthy',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_only_fault_moving_the_selection_fails(self):
        # The no-transition clause: a backup-only fault may not move
        # the selection off the healthy primary.
        self.feed.selects_on_backup_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the selection', record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_only_fault_journaling_a_failover_fails(self):
        # The durable-record half of the no-transition clause.
        self.feed.phantom_failover_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled a failover transition',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_annunciation_never_journaled_fails(self):
        self.feed.skips_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciation never journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_all_bad_never_engaging_the_fallback_fails(self):
        # The all-bad leg: the mode flag never asserts, so the declared
        # fallback engagement never presents.
        self.feed.silent_failover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never engaged the declared fallback',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_held_output_under_all_bad_fails(self):
        # The no-unintended-step clause: with every source bad the
        # select holds its last Good stamp rather than serving Bad.
        self.feed.holds_last_value = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('an unintended output step',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_controlling_on_untrusted_level_fails(self):
        # The chain keeps emitting a computed stage count while its
        # delivered level is untrusted — the never-silently-controls
        # clause.
        self.feed.controls_on_untrusted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('controls on an untrusted level',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovered_primary_never_reselected_fails(self):
        # The first ordered-recovery leg: clearing the primary must
        # re-select it through the named transition.
        self.feed.never_resumes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never resumed control', record.get('detail', ''))
        report.validate_scenario(record)

    def test_annunciation_never_clearing_fails(self):
        # The second ordered-recovery leg: clearing the backup must
        # drop the annunciation through the named transition.
        self.feed.annunciation_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the annunciation',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwired_annunciation_port_is_inconclusive(self):
        # A model without the optional standby-health port declares no
        # annunciation for the leg to check.
        self.feed.drop_unhealthy_port = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('does not serve the fallback leg',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_healthy_baseline_is_inconclusive(self):
        self.feed.backup_owns_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no healthy settled baseline',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
