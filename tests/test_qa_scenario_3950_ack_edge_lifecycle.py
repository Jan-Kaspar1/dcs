"""The 3950_ack_edge_lifecycle leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_ack_edge_lifecycle. The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'AckEdgeTests.test_registered_in_scenarios',
    'AckEdgeTests.test_clean_feed_passes_and_validates',
    'AckEdgeTests.test_two_runs_produce_identical_evidence',
    'AckEdgeTests.test_no_active_fails',
    'AckEdgeTests.test_missing_wiring_is_inconclusive',
    'AckEdgeTests.test_unwritable_ack_is_inconclusive',
    'AckEdgeTests.test_no_descriptor_is_inconclusive',
    'AckEdgeTests.test_drifted_binding_is_inconclusive',
    'AckEdgeTests.test_no_in_binding_is_inconclusive',
    'AckEdgeTests.test_refused_claim_is_inconclusive',
    'AckEdgeTests.test_standing_contact_is_inconclusive',
    'AckEdgeTests.test_latched_baseline_is_inconclusive',
    'AckEdgeTests.test_unack_never_reports_is_inconclusive',
    'AckEdgeTests.test_alarm_never_asserts_fails',
    'AckEdgeTests.test_latch_never_latches_fails',
    'AckEdgeTests.test_press_refused_fails',
    'AckEdgeTests.test_press_never_applies_fails',
    'AckEdgeTests.test_latch_never_clears_fails',
    'AckEdgeTests.test_ack_clears_the_standing_alarm_fails',
    'AckEdgeTests.test_held_level_suppresses_fresh_trip_fails',
    'AckEdgeTests.test_held_press_clears_the_latch_fails',
    'AckEdgeTests.test_release_refused_fails',
    'AckEdgeTests.test_release_never_lands_fails',
    'AckEdgeTests.test_dropped_release_wedges_later_acks_fails',
    'AckEdgeTests.test_journaled_role_change_is_nondeterministic',
    'AckEdgeTests.test_unjournaled_point_recording_is_nondeterministic',
    'AckEdgeTests.test_journal_out_of_order_is_nondeterministic',
    'AckEdgeTests.test_missing_journal_fails',
    'AckEdgeTests.test_missing_settle_journal_fails',
    'AckEdgeTests.test_role_moving_under_the_lifecycle_fails',
    'AckEdgeTests.test_peer_role_moved_fails',
    'AckEdgeTests.test_restore_read_lying_fails',
})


class AckEdgePlantPeer(StagingPlantPeer):
    """The ack-edge rig's plant half: StagingPlantPeer's shared-claim
    write path with the rig seeded down to the journaled
    `p101-moisture` field contact the scenario drives."""

    MOISTURE = 80

    def __init__(self, owner, wet=False):
        super().__init__(owner)
        self.samples = {
            self.MOISTURE: {'value': {'bool': wet}, 'quality': 'good',
                            'tick': 0}}
        # Fault injection: the restore write — the leg's fourth field
        # write — answers done but stores a type-confused zero, so
        # the read-back audit sees the contact never restored.
        self.lying_final_restore = False

    def dispatch(self, request):
        if request.get('op') == 'write' and self.lying_final_restore \
                and self.write_count == 3:
            self.requests.append(request)
            self.write_count += 1
            self.samples[request['point']]['value'] = {'int': 0}
            return {'result': 'done'}
        return super().dispatch(request)


class AckEdgeFeed:
    """A stubbed monitor pair for the ack-edge-lifecycle scenario: a
    tiny executor over the station's moisture-guard alarm chain — the
    journaled `p101-moisture` field contact read same-scan, the guard
    interlock's synthesized carrier delivering last scan's trip into
    the managed alarm's `in`, the contact's inverted serving, and the
    managed-bool-latching-alarm's consumed-edge ack contract — against
    a real plant-protocol peer whose `p101-moisture` point the
    scenario writes under the shared claim. Every `http_json` call is
    one completed scan: the carrier delivers one scan later, the
    receipted ack writes settle at the boundary, and the latch obeys
    `latched = (latched and not edge) or fresh` with the edge tracked
    on the input's served level — a held level acknowledges once and
    cannot pre-acknowledge a later trip. Declared-journaled points
    record point_changed — the contact and the alarm's flag set — the
    carrier, the ack input, and the inverted serving do not. Fault
    flags stage each named failure the issue calls out."""

    MOISTURE, MOISTURE_OK = 80, 326
    ALARM_IN, ACK = 700, 1090
    ALARM, UNACK = 1093, 1094
    SHELVED, SUPPRESSED, ALOOS = 1095, 1096, 1097
    JOURNALED = (80, 1093, 1094, 1095, 1096, 1097)
    INS = (80, 700, 1090)

    SIGNALS = [
        {'point': 80, 'signal': 10080, 'name': 'p101-moisture',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 326, 'signal': 10326, 'name': 'p101-moisture-ok',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1090, 'signal': 11090, 'name': 'p101-moisture-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1093, 'signal': 11093, 'name': 'p101-moisture-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1094, 'signal': 11094,
         'name': 'p101-moisture-unacknowledged', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1095, 'signal': 11095,
         'name': 'p101-moisture-shelved', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1096, 'signal': 11096,
         'name': 'p101-moisture-suppressed', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1097, 'signal': 11097,
         'name': 'p101-moisture-out-of-service', 'direction': 'out',
         'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []           # accepted commands awaiting boundary
        self.delivered_in = False   # the guard carrier's delivery
        self.state = False          # the alarm's tracked `in` level
        self.latched = False
        self.ack_seen = False       # the consumed edge's baseline
        self.values = {
            self.MOISTURE: False, self.MOISTURE_OK: True,
            self.ALARM_IN: False, self.ACK: False,
            self.ALARM: False, self.UNACK: False,
            self.SHELVED: False, self.SUPPRESSED: False,
            self.ALOOS: False}
        self.jseen = {}             # last journaled value per point
        self._deferred = []         # reorder fault's held records
        self._contact_ups = 0       # contact->true records emitted
        self._role_journaled = False
        self._ack_journaled = False
        self._peer_moved = False    # the peer's claimed role flipped
        # Fault injection for the named-failure cases.
        self.no_active = False          # ctrl-a never reports active
        self.peer_moves = False         # the tracking peer claims active
        self.role_moves = False         # the trip reads as peer loss
        self.bare_signals = False       # the alarm wiring absent
        self.bad_ack_signal = False     # moisture-ack non-writable
        self.no_descriptor = False      # the snapshot binds no alarms
        self.bare_descriptor = False    # the descriptor binds no `in`
        self.drifted_binding = False    # unacknowledged bound off 1094
        self.missing_unack = False      # 1094 absent from the snapshot
        self.baseline_latched = False   # the latch already stands
        self.mute_alarm = False         # the alarm never stands
        self.mute_unack = False         # the latch never latches
        self.ack_rejected = False       # the press submission refused
        self.release_rejected = False   # the release submission refused
        self.ack_never_applies = False  # the accepted press never applies
        self.unack_stuck = False        # the latch never clears
        self.ack_clears_alarm = False   # the press drops the alarm too
        self.level_suppresses = False   # the #781 regression: the held
                                        # level pre-acknowledges
        self.level_clears = False       # the held press lands a clear
        self.release_stuck = False      # the applied release leaves the
                                        # input serving held true
        self.wedge_ack = False          # the #961 regression: once an
                                        # edge lands the tracking never
                                        # re-arms — later presses dead
        self.journals_role = False      # a role_changed entry lands
        self.journals_unjournaled = False  # the ack input journals
        self.reorders_journal = False   # the second contact+ records
                                        # past the alarm's batch
        self.no_journal = False         # transitions never journal
        self.no_settle_journal = False  # settlements never journal

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
        """The recorder's per-scan diff over declared-journaled
        points. `reorders_journal` holds the second contact
        assertion's record for one scan — the durable order then
        carries the fresh trip's latch ahead of its own cause."""
        emit, deferred = [], []
        for point in self.JOURNALED:
            value = self.values[point]
            previous = self.jseen.get(point)
            if point in self.jseen and previous == value:
                continue
            self.jseen[point] = value
            event = {'point_changed': {
                'point': point,
                'from': (self._wrap(previous)
                         if previous is not None else None),
                'to': self._wrap(value)}}
            if self.reorders_journal and point == self.MOISTURE \
                    and value is True:
                self._contact_ups += 1
                if self._contact_ups == 2:
                    deferred.append(event)
                    continue
            emit.append(event)
        emit.extend(self._deferred)
        self._deferred = deferred
        if self.no_journal:
            return
        for event in emit:
            self._entry(event)

    # One completed scan: boundary-settled commands first — the
    # applied write lands on the input's served level the same scan —
    # then the field read, the guard carrier's one-scan delivery, the
    # consumed-edge alarm evaluation, and the journaled diffs.
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
                if self.release_stuck \
                        and write['value']['bool'] is False:
                    pass        # the release's apply reports but
                                # never lands — the wedged input
                else:
                    self.values[self.ACK] = write['value']['bool']
                    if self.journals_unjournaled \
                            and not self._ack_journaled:
                        self._ack_journaled = True
                        self._entry({'point_changed': {
                            'point': self.ACK,
                            'from': {'bool': False},
                            'to': self._wrap(self.values[self.ACK])}})
            if not self.no_settle_journal:
                self._entry({'command_settled': {'receipt':
                                                 dict(receipt)}})
        # The field read: the driven contact, served same-scan — plus
        # its inverted `moisture-ok` serving.
        wet = bool(self.plant.samples[self.MOISTURE]['value']
                   .get('bool', False))
        self.values[self.MOISTURE] = wet
        self.values[self.MOISTURE_OK] = not wet
        # The guard interlock trips this scan; the synthesized
        # port-pair delivers last scan's trip into the alarm's `in`.
        alarm_in = self.delivered_in
        self.delivered_in = wet
        self.values[self.ALARM_IN] = alarm_in
        # The managed bool-latching alarm's consumed-edge evaluation —
        # the contract the leg exists to pin: the input's own rising
        # edge clears the latch exactly once, a held level is not an
        # acknowledgment, and a fresh trip re-latches under it.
        fresh = alarm_in and not self.state
        self.state = bool(alarm_in)
        ack = bool(self.values[self.ACK])
        if self.level_suppresses:
            self.latched = (self.latched or fresh) and not ack
            self.ack_seen = ack
        elif self.level_clears:
            self.latched = (self.latched and not ack) or fresh
            self.ack_seen = ack
        elif self.wedge_ack:
            edge = ack and not self.ack_seen
            self.ack_seen = self.ack_seen or edge
            self.latched = (self.latched and not edge) or fresh
        elif self.unack_stuck:
            self.latched = self.latched or fresh
            self.ack_seen = ack
        else:
            edge = ack and not self.ack_seen
            self.ack_seen = ack
            self.latched = (self.latched and not edge) or fresh
        alarm = bool(alarm_in)
        if self.ack_clears_alarm:
            alarm = alarm and not ack
        self.values[self.ALARM] = alarm
        self.values[self.UNACK] = self.latched
        if self.mute_alarm:
            self.values[self.ALARM] = False
        if self.mute_unack:
            self.values[self.UNACK] = False
        self.values[self.SHELVED] = False
        self.values[self.SUPPRESSED] = False
        self.values[self.ALOOS] = False
        if self.baseline_latched:
            self.values[self.UNACK] = True
        self._journal_values()
        if self.journals_role and self.values[self.ALARM] \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})
        if self.peer_moves and self.values[self.ALARM]:
            self._peer_moved = True

    def _descriptors(self):
        """The served bound-point-annotated descriptor for the
        moisture alarm instance — the leg resolves the lifecycle's
        wiring from this, not the name convention."""
        if self.no_descriptor:
            return []
        ports = [{'name': 'in', 'direction': 'in', 'kind': 'bool',
                  'role': 'process', 'point': self.ALARM_IN},
                 {'name': 'ack', 'direction': 'in', 'kind': 'bool',
                  'role': 'status', 'point': self.ACK},
                 {'name': 'alarm', 'direction': 'out', 'kind': 'bool',
                  'role': 'status', 'point': self.ALARM},
                 {'name': 'unacknowledged', 'direction': 'out',
                  'kind': 'bool', 'role': 'status',
                  'point': 9999 if self.drifted_binding
                  else self.UNACK},
                 {'name': 'shelved', 'direction': 'out', 'kind': 'bool',
                  'role': 'status', 'point': self.SHELVED},
                 {'name': 'suppressed', 'direction': 'out',
                  'kind': 'bool', 'role': 'status',
                  'point': self.SUPPRESSED},
                 {'name': 'out_of_service', 'direction': 'out',
                  'kind': 'bool', 'role': 'status',
                  'point': self.ALOOS}]
        if self.bare_descriptor:
            ports = [port for port in ports if port['name'] != 'in']
        return [{'name': 'p101-moisture-alarm',
                 'kind': 'managed-bool-latching-alarm',
                 'label': 'p101 moisture',
                 'ports': ports, 'parameters': []}]

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                role = 'active' if self._peer_moved else 'standby'
                return 200, {'role': role, 'tick': self.tick,
                             'sync': {'tracking': {'aligned':
                                                   self.tick}}}
            if self.no_active or (self.role_moves
                                  and self.values[self.ALARM]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'p101-moisture-ack'
                          else entry for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/snapshot'):
            points = []
            for point, value in sorted(self.values.items()):
                if self.missing_unack and point == self.UNACK:
                    continue
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': self._wrap(value),
                               'quality': 'good',
                               'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'descriptors': self._descriptors()}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            refuse = self.ack_rejected \
                or (self.release_rejected
                    and (write or {}).get('value', {})
                    .get('bool') is False)
            if write and write.get('point') == self.ACK \
                    and not refuse:
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {
                                 'point': (write or {})
                                 .get('point')}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class AckEdgeTests(unittest.TestCase):
    """scenario_ack_edge_lifecycle against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named
    acceptance leg — the edge acknowledgment while the condition
    stands, the held level's non-acknowledgment of a fresh trip, the
    dropped-release wedge window and its re-armed recovery, the
    journaled order, the restored inputs and roles — plus the
    inconclusive paths."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = AckEdgePlantPeer(self.OWNER)
        self.feed = AckEdgeFeed(self.plant)

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
                patch.object(scenarios, 'ACK_EDGE_POLL', 0.001), \
                patch.object(scenarios, 'ACK_EDGE_DEADLINE', 3.0):
            return scenarios.scenario_ack_edge_lifecycle(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The lifecycle leg sits with the alarm contract's cluster,
        # ahead of the schedule's closing observation case.
        self.assertEqual(
            order.index(scenarios.scenario_power_fail_trip) + 1,
            order.index(scenarios.scenario_ack_edge_lifecycle))
        self.assertEqual(
            order.index(scenarios.scenario_ack_edge_lifecycle) + 1,
            order.index(scenarios.scenario_dcs_ctl))
        self.assertIs(verify.case_function('ack-edge-lifecycle'),
                      scenarios.scenario_ack_edge_lifecycle)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim
        # attachment, the baseline and restore reads, and the
        # drive/clear/drive/restore write four.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertEqual(ops.count('write'), 4)
        # The driven contact restored, the latch and the ack input
        # re-armed, and the lifecycle's evidence lines all filed.
        self.assertEqual(
            self.plant.samples[AckEdgePlantPeer.MOISTURE]['value'],
            {'bool': False})
        self.assertFalse(self.feed.latched)
        self.assertFalse(self.feed.values[self.feed.ACK])
        names = {entry['ref'] for entry in record['evidence']}
        for name in ('ack-edge-signals.json',
                     'ack-edge-wiring.json',
                     'ack-edge-baseline.json',
                     'ack-edge-tripped.json',
                     'ack-edge-acknowledged.json',
                     'ack-edge-cleared.json',
                     'ack-edge-retripped.json',
                     'ack-edge-held-press.json',
                     'ack-edge-released.json',
                     'ack-edge-re-acknowledged.json',
                     'ack-edge-restored-alarm.json',
                     'ack-edge-restored.json',
                     'ack-edge-journal.json',
                     'ack-edge-journal-order.json',
                     'ack-edge-press-1-receipt.json',
                     'ack-edge-press-2-receipt.json',
                     'ack-edge-press-3-receipt.json',
                     'ack-edge-release-receipt.json',
                     'ack-edge-rearm-receipt.json'):
            self.assertIn('evidence/' + name, names)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = AckEdgePlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = AckEdgeFeed(plant2)
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

    def test_no_active_fails(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('moisture-contact alarm wiring',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('p101-moisture-ack', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_descriptor_is_inconclusive(self):
        # The rig predates the bound-point-annotated descriptors the
        # wiring audit resolves through.
        self.feed.no_descriptor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no served managed-alarm descriptor binds',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_drifted_binding_is_inconclusive(self):
        self.feed.drifted_binding = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('off the declared points',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_in_binding_is_inconclusive(self):
        self.feed.bare_descriptor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('binds no in port', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer claim refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standing_contact_is_inconclusive(self):
        # The contact already stands: the drive's baseline read fails
        # its precondition instead of touching the field.
        self.plant.samples[AckEdgePlantPeer.MOISTURE]['value'] = \
            {'bool': True}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('does not read false', record.get('detail', ''))
        report.validate_scenario(record)

    def test_latched_baseline_is_inconclusive(self):
        # The latch already stands: the rig is not in the posture the
        # lifecycle runs from.
        self.feed.baseline_latched = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('idle baseline never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unack_never_reports_is_inconclusive(self):
        # The named inconclusive leg: the served snapshot never
        # carries the latch point, so the baseline window never lands.
        self.feed.missing_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unack', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_asserts_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('tripped leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_never_latches_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_press_refused_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('press-1 write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_press_never_applies_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_never_clears_fails(self):
        self.feed.unack_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('acknowledged leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_clears_the_standing_alarm_fails(self):
        # The press drops the standing alarm with the latch — the
        # alarm owes process truth until the condition clears.
        self.feed.ack_clears_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_held_level_suppresses_fresh_trip_fails(self):
        # The #781 regression: the held ack level clears the fresh
        # trip's latch instead of latching it.
        self.feed.level_suppresses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('retripped leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_held_press_clears_the_latch_fails(self):
        # The wedge the consumed edge owes never to land: the press
        # on the held level acknowledges anyway.
        self.feed.level_clears = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('held-level press acknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_refused_fails(self):
        self.feed.release_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('release write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_never_lands_fails(self):
        # The release settles applied but the input keeps serving the
        # held level — the receipted write's landing never happened.
        self.feed.release_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('released leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dropped_release_wedges_later_acks_fails(self):
        # The #961 regression: the once-consumed edge never re-arms,
        # so the post-release press acknowledges nothing.
        self.feed.wedge_ack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('re-acknowledged leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_is_nondeterministic(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_point_recording_is_nondeterministic(self):
        self.feed.journals_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_out_of_order_is_nondeterministic(self):
        # The fresh trip's latch records ahead of the contact's own
        # assertion — a journaled order that inverts the causality.
        self.feed.reorders_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_settle_journal_fails(self):
        self.feed.no_settle_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_moving_under_the_lifecycle_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('active role moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_peer_role_moved_fails(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('roles moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restore_read_lying_fails(self):
        self.plant.lying_final_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack-edge-failed', record.get('detail', ''))
        self.assertIn('did not restore', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
