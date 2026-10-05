"""The 3960_cause_alarm_quality leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_cause_alarm_quality. The
shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CauseAlarmTests.test_registered_in_scenarios',
    'CauseAlarmTests.test_clean_feed_passes_and_validates',
    'CauseAlarmTests.test_two_runs_produce_identical_evidence',
    'CauseAlarmTests.test_no_active_fails',
    'CauseAlarmTests.test_missing_surface_is_inconclusive',
    'CauseAlarmTests.test_unwritable_ack_is_inconclusive',
    'CauseAlarmTests.test_no_descriptor_is_inconclusive',
    'CauseAlarmTests.test_drifted_binding_is_inconclusive',
    'CauseAlarmTests.test_no_in_binding_is_inconclusive',
    'CauseAlarmTests.test_raw_contact_wiring_fails',
    'CauseAlarmTests.test_refused_claim_is_inconclusive',
    'CauseAlarmTests.test_no_plant_endpoint_is_inconclusive',
    'CauseAlarmTests.test_no_owner_token_is_inconclusive',
    'CauseAlarmTests.test_standing_contact_is_inconclusive',
    'CauseAlarmTests.test_lying_field_baseline_is_inconclusive',
    'CauseAlarmTests.test_degraded_contact_is_inconclusive',
    'CauseAlarmTests.test_latched_baseline_is_inconclusive',
    'CauseAlarmTests.test_refused_write_fails',
    'CauseAlarmTests.test_silent_cause_alarm_fails',
    'CauseAlarmTests.test_latch_never_latches_fails',
    'CauseAlarmTests.test_protection_never_trips_fails',
    'CauseAlarmTests.test_command_ignores_the_trip_fails',
    'CauseAlarmTests.test_degraded_sample_replaces_the_value_fails',
    'CauseAlarmTests.test_fault_never_clears_fails',
    'CauseAlarmTests.test_stuck_latch_fails',
    'CauseAlarmTests.test_cross_annunciation_fails',
    'CauseAlarmTests.test_annunciation_beyond_the_stopped_pump_fails',
    'CauseAlarmTests.test_spurious_start_under_degradation_fails',
    'CauseAlarmTests.test_late_annunciation_is_nondeterministic',
    'CauseAlarmTests.test_duplicate_trip_is_nondeterministic',
    'CauseAlarmTests.test_journaled_role_change_is_nondeterministic',
    'CauseAlarmTests.test_unjournaled_point_recording_is_nondeterministic',
    'CauseAlarmTests.test_quiet_point_recording_is_nondeterministic',
    'CauseAlarmTests.test_missing_journal_fails',
    'CauseAlarmTests.test_missing_settle_journal_fails',
    'CauseAlarmTests.test_role_moving_under_the_drive_fails',
    'CauseAlarmTests.test_peer_role_moved_fails',
    'CauseAlarmTests.test_restore_read_lying_fails',
})

# The pump-group's declared hand-leg holdout, from the emitted model's
# `min_off_ticks` — the off-delay timer that keeps the hand leg out for
# this many scans after the protections clear.
MIN_OFF_TICKS = 2

# The contact read each the leg's own field-account gate reads: the
# baseline's contact census, the baseline posture gate, each drive's
# degraded-contact check, and the final restore gate. A restore-lying
# plant reports a changed stored value from the leg's last read on.
RESTORE_LIE_READ = {60: 4, 80: 5}


def _quality(sample):
    """A stored field sample's comparable quality: 'good' or
    'bad:<reason>' for a substituted stamp."""
    quality = (sample or {}).get('quality')
    if quality == 'good':
        return 'good'
    if isinstance(quality, dict) and quality:
        kind, reason = next(iter(quality.items()))
        return str(kind) + ':' + str(reason)
    return 'unknown'


def _reading(sample):
    """(value, quality) out of a stored field sample."""
    raw = (sample or {}).get('value')
    if isinstance(raw, dict):
        raw = next(iter(raw.values()), None)
    return bool(raw), _quality(sample)


def _served(key):
    """A flattened quality key in the wire shape the served snapshot
    carries: the bare string for Good, the one-key object for a
    substituted stamp."""
    if key == 'good':
        return 'good'
    kind, _, reason = key.partition(':')
    return {kind: reason}


def _worst(*qualities):
    """The worst of a set of quality keys — the AND gate's merged
    stamp."""
    rank = {'good': 0, 'uncertain': 1, 'bad': 2}
    worst = 'good'
    for quality in qualities:
        if rank.get(quality.split(':')[0], 0) > rank.get(
                worst.split(':')[0], 0):
            worst = quality
    return worst


class CauseAlarmPlantPeer(StagingPlantPeer):
    """The cause-alarm rig's plant half: StagingPlantPeer's shared-claim
    write path and fault surface with the rig seeded down to the
    journaled `p101-thermal` / `p101-moisture` field contacts and the
    `p101-cmd` field output the leg reads back."""

    THERMAL, MOISTURE, CMD = 60, 80, 100

    def __init__(self, owner):
        super().__init__(owner)
        self.samples = {
            point: {'value': {'bool': False}, 'quality': 'good',
                    'tick': 0}
            for point in (self.THERMAL, self.MOISTURE, self.CMD)}
        # Fault injection: the degraded contact's stored field value is
        # reported changed under the injected quality — the silent
        # substitution the honest-degradation clause forbids.
        self.lying_degraded_value = False
        # Fault injection: the field's own account lies — the first
        # BASELINE_READS contact reads report a standing value, and the
        # reads after the leg's drive report one again, so the leg's
        # two field-account gates are the ones that catch it.
        self.lying_baseline = False
        self.lying_restore = False
        self.reads = {}

    def _read_count(self, point):
        self.reads[point] = self.reads.get(point, 0) + 1
        return self.reads[point]

    def dispatch(self, request):
        op = request.get('op')
        point = request.get('point')
        if op == 'read' and self.lying_degraded_value \
                and isinstance(self.faults.get(point), dict) \
                and 'quality' in self.faults[point]:
            self.requests.append(request)
            served = self.served(point)
            served['value'] = {'bool': True}
            return {'result': 'sample', 'sample': served}
        if op == 'read' and point in (self.THERMAL, self.MOISTURE) \
                and (self.lying_baseline
                     or (self.lying_restore
                         and self._read_count(point)
                         >= RESTORE_LIE_READ[self.THERMAL if point
                                             == self.THERMAL
                                             else self.MOISTURE])):
            if not self.lying_baseline:
                self._read_count(point)
            self.requests.append(request)
            served = self.served(point)
            served['value'] = {'bool': True}
            return {'result': 'sample', 'sample': served}
        return super().dispatch(request)


class CauseAlarmFeed:
    """A stubbed monitor pair for the cause-alarm-quality scenario: a
    tiny executor over the pump-station protection chain — the
    journaled `p101-thermal` / `p101-moisture` field contacts read
    through the plant's own fault surface, each contact's inverted
    serving and the aggregated `avail` carrying the contact's quality,
    the protection aggregator tripping on an asserted *or* untrusted
    contact, its two declared carrier hops to `protections-ok`, the
    manual hand leg under the model's `min_off_ticks` holdout, one
    single-trip cause guard per contact whose `tripped` rides a
    synthesized port-to-port carrier into the managed cause alarm's
    `in`, and that alarm's consumed-edge ack contract — against a real
    plant-protocol peer whose contact points and `p101-cmd` field
    output the leg drives and reads. Every `http_json` call is one
    completed scan: each carrier crosses one hop per scan, receipted
    writes settle at the boundary, and the latch obeys
    `latched = (latched and not edge) or fresh` with the edge tracked
    on the input's served level. Declared-journaled points record
    `point_changed`, and a contact's substituted quality records
    `quality_changed` — the carriers, the ack inputs, the inverted
    servings, and the field output do not. Doctor flags stage each
    named failure the issue calls out."""

    MODE, HAND, OOS, CMD, AVAIL = 300, 301, 302, 100, 328
    PROTECT, PROTECT_OK = 316, 318
    THERMAL, MOISTURE = 60, 80
    THERMAL_OK, MOISTURE_OK = 324, 326
    FAULT = 312
    THERMAL_IN, MOISTURE_IN = 700, 701
    FAULT_ALARM, FAULT_UNACK = 1073, 1074
    FAULT_SHELVED, FAULT_SUPPRESSED, FAULT_OOS = 1075, 1076, 1077
    THERMAL_ACK, MOISTURE_ACK = 1080, 1090
    THERMAL_ALARM, THERMAL_UNACK = 1083, 1084
    THERMAL_SHELVED, THERMAL_SUPPRESSED, THERMAL_OOS = 1085, 1086, 1087
    MOISTURE_ALARM, MOISTURE_UNACK = 1093, 1094
    MOISTURE_SHELVED, MOISTURE_SUPPRESSED, MOISTURE_OOS = 1095, 1096, 1097

    JOURNALED = (60, 80, 300, 312, 316, 318, 1073, 1074, 1075, 1076,
                 1077, 1083, 1084, 1085, 1086, 1087, 1093, 1094,
                 1095, 1096, 1097)
    INS = (300, 301, 302, 1080, 1090, 700, 701)

    CONTACTS = {'thermal': (60, 324, 700, 1080, 1083, 1084, 1085, 1086,
                            1087),
                'moisture': (80, 326, 701, 1090, 1093, 1094, 1095, 1096,
                             1097)}

    SIGNALS = [
        {'point': 300, 'signal': 10300, 'name': 'p101-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 301, 'signal': 10301, 'name': 'p101-hand',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 302, 'signal': 10302, 'name': 'p101-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 100, 'signal': 10100, 'name': 'p101-cmd',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 328, 'signal': 10328, 'name': 'p101-avail',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 316, 'signal': 10316, 'name': 'p101-protect-tripped',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 318, 'signal': 10318, 'name': 'p101-protections-ok',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 312, 'signal': 10312, 'name': 'p101-fault',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 60, 'signal': 10060, 'name': 'p101-thermal',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 324, 'signal': 10324, 'name': 'p101-thermal-ok',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1080, 'signal': 11080, 'name': 'p101-thermal-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1083, 'signal': 11083, 'name': 'p101-thermal-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1084, 'signal': 11084,
         'name': 'p101-thermal-unacknowledged', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1085, 'signal': 11085, 'name': 'p101-thermal-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1086, 'signal': 11086, 'name': 'p101-thermal-suppressed',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1087, 'signal': 11087,
         'name': 'p101-thermal-out-of-service', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
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
        {'point': 1095, 'signal': 11095, 'name': 'p101-moisture-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1096, 'signal': 11096, 'name': 'p101-moisture-suppressed',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1097, 'signal': 11097,
         'name': 'p101-moisture-out-of-service', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1073, 'signal': 11073, 'name': 'p101-fault-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1074, 'signal': 11074,
         'name': 'p101-fault-unacknowledged', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1075, 'signal': 11075, 'name': 'p101-fault-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1076, 'signal': 11076, 'name': 'p101-fault-suppressed',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1077, 'signal': 11077,
         'name': 'p101-fault-out-of-service', 'direction': 'out',
         'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []          # accepted commands awaiting the boundary
        self.qualities = {}        # served quality per point
        self.values = {point: False for point in self.INS}
        self.values.update({self.CMD: False, self.AVAIL: True,
                            self.PROTECT: False, self.PROTECT_OK: True,
                            self.THERMAL_OK: True, self.MOISTURE_OK: True,
                            self.FAULT: False})
        for tag, points in self.CONTACTS.items():
            (_contact, ok, carrier, ack, alarm, unack, shelved,
             suppressed, oos) = points
            self.values.update({ok: True, carrier: False, ack: False,
                                alarm: False, unack: False,
                                shelved: False, suppressed: False,
                                oos: False})
            self.qualities[carrier] = 'good'
        self.values.update({self.FAULT_ALARM: False, self.FAULT_UNACK: False,
                            self.FAULT_SHELVED: False,
                            self.FAULT_SUPPRESSED: False,
                            self.FAULT_OOS: False})
        self.jseen = {}            # last journaled value per point
        self.qseen = {}            # last journaled quality per point
        self.states = {}           # each alarm's tracked `in` level
        self.latched = {}          # each alarm's acknowledgment latch
        self.ack_seen = {}         # each alarm's consumed-edge baseline
        self.delivered = {}        # each guard carrier's last trip
        self.protect_delivered = False
        self.protect_ok_delivered = True
        self.holdout = True
        self.holdout_elapsed = 0
        self.late = 0              # the guard carrier's extra delay
        self._sticky_fault = set()  # contacts whose fault never clears
        self._role_journaled = False
        self._role_moved = False
        self._peer_moved = False
        self._quiet_journaled = False
        self._cmd_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False           # ctrl-a never reports active
        self.peer_moves = False          # the tracking peer claims active
        self.role_moves = False          # the trip reads as peer loss
        self.bare_signals = False        # the declared surface absent
        self.unwritable_ack = False      # the ack point is not writable
        self.no_descriptor = False       # the snapshot binds no alarms
        self.bare_descriptor = False     # a descriptor binds no `in`
        self.drifted_binding = False     # unacknowledged bound off-point
        self.raw_contact_wiring = False  # `in` binds the raw contact
        self.refuse_write = False        # every submission refused
        self.silent_alarm = False        # the cause alarm never asserts
        self.mute_unack = False          # the latch never latches
        self.never_trips = False         # the protection is not fail-safe
        self.cmd_holds = False           # the command ignores the trip
        self.never_clears = False        # the fault never releases
        self.stuck_latch = False         # the ack never clears the latch
        self.cross_fires = False         # the sibling alarm annunciates
        self.fault_alarm_stopped = False  # an alarm beyond the contract
        self.start_under_fault = False   # the stopped pump starts
        self.late_annunciation = False  # the alarm lands many scans late
        self.duplicate_trip = False     # the trip records twice
        self.journals_role = False       # a role_changed entry lands
        self.journals_unjournaled = False   # the field output journals
        self.journals_quiet = False      # a managed flag journals
        self.no_journal = False          # transitions never journal
        self.no_settle_journal = False   # settlements never journal

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

    def _journal_diff(self):
        """The recorder's per-scan diff over the declared-journaled
        points: a value transition records `point_changed`, a quality
        transition `quality_changed`."""
        if self.no_journal:
            return
        for point in self.JOURNALED:
            value = self.values[point]
            if point in self.jseen and self.jseen[point] == value:
                continue
            previous = self.jseen.get(point)
            self.jseen[point] = value
            self._entry({'point_changed': {
                'point': point,
                'from': self._wrap(previous)
                if previous is not None else None,
                'to': self._wrap(value)}})
        for point, quality in sorted(self.qualities.items()):
            if point not in self.JOURNALED:
                continue
            previous = self.qseen.get(point)
            if previous == quality:
                continue
            self.qseen[point] = quality
            self._entry({'quality_changed': {
                'point': point, 'from': _served(previous)
                if previous is not None else None,
                'to': _served(quality)}})

    def _alarm(self, tag):
        """The managed bool-latching alarm's consumed-edge evaluation on
        one contact's guard carrier: a fresh assertion latches, the
        consumed ack edge clears it exactly once, and the managed
        outputs never move."""
        (_contact, _ok, carrier, ack, alarm, unack, shelved, suppressed,
         oos) = self.CONTACTS[tag]
        asserting = bool(self.values[carrier])
        if self.silent_alarm:
            self.values[alarm] = False
            self.values[unack] = False
            self.states[tag] = asserting
            return
        fresh = asserting and not self.states.get(tag, False)
        self.states[tag] = asserting
        ack_level = bool(self.values[ack])
        if self.stuck_latch:
            self.latched[tag] = self.latched.get(tag, False) or fresh
        else:
            edge = ack_level and not self.ack_seen.get(tag, False)
            self.ack_seen[tag] = ack_level
            self.latched[tag] = (self.latched.get(tag, False)
                                 and not edge) or fresh
        self.values[alarm] = asserting
        self.values[unack] = False if self.mute_unack \
            else self.latched[tag]
        self.values[shelved] = False
        self.values[suppressed] = False
        self.values[oos] = False

    # One completed scan: boundary-settled writes land on their served
    # level, the field reads the plant's stored contacts and faults,
    # the protection chain and both cause guards evaluate, the carriers
    # cross one hop, the alarms evaluate, and the journaled diffs land.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            write = receipt['command']['write_value']
            self.values[write['point']] = write['value']['bool']
            if not self.no_settle_journal:
                self._entry({'command_settled': {'receipt':
                                                 dict(receipt)}})
        mode = bool(self.values[self.MODE])
        hand = bool(self.values[self.HAND])
        oos = bool(self.values[self.OOS])
        # The field contacts, read through the plant's own fault surface:
        # the substituted quality rides the stored value.
        degraded = {}
        for tag, points in self.CONTACTS.items():
            contact = points[0]
            value, quality = _reading(self.plant.served(contact))
            if self.plant.faults.get(contact):
                self._sticky_fault.add(contact)
            if self.never_clears and contact in self._sticky_fault:
                quality = 'bad:device_fault'
            self.values[contact] = value
            self.qualities[contact] = quality
            degraded[tag] = (value, quality)
            self.values[points[1]] = not value       # the inverted serving
            self.qualities[points[1]] = quality
        # The aggregated availability: in-auto and in-service and
        # power-ok and thermal-ok and moisture-ok, carrying the worst of
        # its inputs' qualities.
        avail = (not mode) and (not oos) \
            and self.values[self.THERMAL_OK] \
            and self.values[self.MOISTURE_OK]
        self.values[self.AVAIL] = avail
        self.qualities[self.AVAIL] = _worst(
            self.qualities[self.THERMAL_OK], self.qualities[self.MOISTURE_OK])
        # The protection aggregator: an asserted or untrusted contact —
        # or the maintenance inhibit — trips it; `tripped` is always
        # Good.
        tripped = oos or any(
            value or quality != 'good' for value, quality
            in degraded.values())
        if self.never_trips:
            tripped = False
        self.values[self.PROTECT] = tripped
        self.qualities[self.PROTECT] = 'good'
        if self.duplicate_trip and tripped:
            self.jseen.pop(self.PROTECT, None)
        # The declared carrier hops: the tripped flag reaches the
        # inverted serving a scan later, and the command guard's copy a
        # further scan later.
        self.values[self.PROTECT_OK] = not self.protect_delivered
        self.qualities[self.PROTECT_OK] = 'good'
        protect_ok_in = self.protect_ok_delivered
        self.protect_delivered = tripped
        self.protect_ok_delivered = self.values[self.PROTECT_OK]
        # The hand leg's off-delay holdout on the protections clearing.
        if self.values[self.PROTECT_OK]:
            self.holdout = True
            self.holdout_elapsed = 0
        else:
            self.holdout_elapsed = min(self.holdout_elapsed + 1,
                                       MIN_OFF_TICKS)
            self.holdout = self.holdout_elapsed < MIN_OFF_TICKS
        stopped = not (hand and mode)
        cmd = bool(hand and mode and self.holdout) and protect_ok_in
        if self.start_under_fault and stopped and any(
                quality != 'good' for _, quality in degraded.values()):
            cmd = True
        if self.cmd_holds:
            cmd = True
        self.values[self.CMD] = cmd
        # The per-contact cause guards: held anchor and held permissive,
        # so `tripped` reports the contact alone — asserted or
        # untrusted alike — and delivers into the alarm's condition one
        # hop later.
        for tag, points in self.CONTACTS.items():
            value, quality = degraded[tag]
            guard = value or quality != 'good'
            if self.late_annunciation and guard and not self.values[
                    points[2]]:
                self.values[points[2]] = guard
                continue
            self.values[points[2]] = self.delivered.get(tag, False)
            self.qualities[points[2]] = 'good'
            self.delivered[tag] = guard
            self._alarm(tag)
        if self.cross_fires:
            # The sibling cause alarm wired to the same contact: it
            # annunciates exactly as the driven one does — the
            # double-firing the leg must reject.
            for tag, points in self.CONTACTS.items():
                if not self.delivered.get(tag, False):
                    continue
                twin = self.CONTACTS[
                    [other for other in self.CONTACTS
                     if other != tag][0]]
                self.values[twin[2]] = True
                self.values[twin[4]] = True
                self.values[twin[5]] = True
        if self.journals_quiet and not self._quiet_journaled \
                and any(self.values[points[4]]
                        for points in self.CONTACTS.values()):
            # a declared-quiet managed flag records a transition it
            # never made — a stale durable record the audit must name.
            self._quiet_journaled = True
            self._entry({'point_changed': {
                'point': self.CONTACTS['thermal'][6],
                'from': {'bool': False}, 'to': {'bool': True}}})
        if self.journals_unjournaled and not self._cmd_journaled \
                and self.values[self.CMD] is True:
            # the field output — which the model leaves unjournaled —
            # records a transition anyway.
            self._cmd_journaled = True
            self._entry({'point_changed': {
                'point': self.CMD, 'from': {'bool': False},
                'to': {'bool': True}}})
        # The motor's fault flag and its alarm: the model wires them off
        # the motor's own feedback, so a protection contact never moves
        # them — the doctor that asserts otherwise proves the
        # honest-absence half.
        beyond = self.fault_alarm_stopped and any(
            quality != 'good' for _, quality in degraded.values()) \
            and stopped
        self.values[self.FAULT] = bool(beyond)
        self.values[self.FAULT_ALARM] = bool(beyond)
        self.values[self.FAULT_UNACK] = bool(beyond)
        self._journal_diff()
        if self.cross_fires:
            for tag in self.CONTACTS:
                if self.values[self.CONTACTS[tag][0]] is False \
                        and self.qualities[self.CONTACTS[tag][0]] == 'good' \
                        and self.values[self.CONTACTS[tag][4]]:
                    # the sibling's alarm standing while its contact is
                    # healthy — a cross-annunciation
                    self.values[self.CONTACTS[tag][4]] = False
        if self.role_moves and any(
                self.values[self.CONTACTS[tag][4]]
                for tag in self.CONTACTS):
            self._role_moved = True
        if self.journals_role and any(
                self.values[self.CONTACTS[tag][4]]
                for tag in self.CONTACTS) and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})
        if self.peer_moves and any(
                self.values[self.CONTACTS[tag][4]]
                for tag in self.CONTACTS):
            self._peer_moved = True
        # The field output: the controller's delivered command lands on
        # the plant's stored sample the scan it was written.
        self.plant.samples[self.CMD] = {
            'value': {'bool': cmd}, 'quality': 'good', 'tick': self.tick}

    def _descriptors(self):
        """The served bound-point-annotated descriptors for the three
        managed bool-latching alarm instances — the leg resolves the
        cause-guard wiring from these, not the name convention."""
        if self.no_descriptor:
            return []
        found = []
        for name, tag, fault in (('p101-thermal-alarm', 'thermal', False),
                                 ('p101-moisture-alarm', 'moisture', False)):
            (_contact, _ok, carrier, ack, alarm, unack, shelved,
             suppressed, oos) = self.CONTACTS[tag]
            in_point = self.CONTACTS[tag][0] if self.raw_contact_wiring \
                else carrier
            ports = [{'name': 'in', 'direction': 'in', 'kind': 'bool',
                      'role': 'process', 'point': in_point},
                     {'name': 'ack', 'direction': 'in', 'kind': 'bool',
                      'role': 'status', 'point': ack},
                     {'name': 'alarm', 'direction': 'out', 'kind': 'bool',
                      'role': 'status', 'point': alarm},
                     {'name': 'unacknowledged', 'direction': 'out',
                      'kind': 'bool', 'role': 'status',
                      'point': 9999 if self.drifted_binding else unack},
                     {'name': 'shelved', 'direction': 'out', 'kind': 'bool',
                      'role': 'status', 'point': shelved},
                     {'name': 'suppressed', 'direction': 'out',
                      'kind': 'bool', 'role': 'status',
                      'point': suppressed},
                     {'name': 'out_of_service', 'direction': 'out',
                      'kind': 'bool', 'role': 'status', 'point': oos}]
            if self.bare_descriptor:
                ports = [port for port in ports if port['name'] != 'in']
            found.append({'name': name,
                          'kind': 'managed-bool-latching-alarm',
                          'label': name, 'ports': ports,
                          'parameters': []})
        # The pump's motor-fault alarm: wired off the motor's own
        # feedback, never through a cause guard — the leg reads its
        # surfaces only to prove the honest absence.
        found.append({
            'name': 'p101-fault-alarm',
            'kind': 'managed-bool-latching-alarm',
            'label': 'p101-fault-alarm',
            'ports': [{'name': 'in', 'direction': 'in', 'kind': 'bool',
                       'role': 'process', 'point': self.FAULT},
                      {'name': 'ack', 'direction': 'in', 'kind': 'bool',
                       'role': 'status', 'point': 1070},
                      {'name': 'alarm', 'direction': 'out', 'kind': 'bool',
                       'role': 'status', 'point': self.FAULT_ALARM},
                      {'name': 'unacknowledged', 'direction': 'out',
                       'kind': 'bool', 'role': 'status',
                       'point': self.FAULT_UNACK},
                      {'name': 'shelved', 'direction': 'out', 'kind': 'bool',
                       'role': 'status', 'point': self.FAULT_SHELVED},
                      {'name': 'suppressed', 'direction': 'out',
                       'kind': 'bool', 'role': 'status',
                       'point': self.FAULT_SUPPRESSED},
                      {'name': 'out_of_service', 'direction': 'out',
                       'kind': 'bool', 'role': 'status',
                       'point': self.FAULT_OOS}],
            'parameters': []})
        return found

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
            if self.no_active or self._role_moved:
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.unwritable_ack:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'p101-thermal-ack'
                          else entry for entry in points]
            if self.bare_signals:
                points = [entry for entry in points
                          if entry['name'] != 'p101-moisture-alarm']
            return 200, {'points': points, 'components': []}
        if (method, route) == ('GET', '/snapshot'):
            points = []
            for point in sorted(self.values):
                quality = _served(self.qualities.get(point, 'good'))
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': self._wrap(self.values[point]),
                               'quality': quality,
                               'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'descriptors': self._descriptors()}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value') or {}
            if self.refuse_write:
                return 200, {'command': (body or {}).get('command'),
                             'outcome': {'rejected': {'reason': {
                                 'not_writable': {
                                     'point': write.get('point')}}}},
                             'actor': body.get('actor')}
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.pending.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class CauseAlarmTests(unittest.TestCase):
    """scenario_cause_alarm_quality against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    leg — the degraded contact's fail-safe trip and named annunciation
    on both contacts, the value trip's identical annunciation, the
    stopped pump's honest absence, the receipted acknowledgment, the
    journaled record and ordering, the restored field and roles — plus
    every inconclusive path."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = CauseAlarmPlantPeer(self.OWNER)
        self.feed = CauseAlarmFeed(self.plant)

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
                patch.object(scenarios, 'CAUSE_ALARM_POLL', 0.001), \
                patch.object(scenarios, 'CAUSE_ALARM_DEADLINE', 1.0):
            return scenarios.scenario_cause_alarm_quality(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The cause-alarm leg sits with the alarm contract's cluster,
        # after the consumed-edge lifecycle and before the
        # graceful-shutdown case ahead of the schedule's closing
        # observation case.
        self.assertEqual(
            order.index(scenarios.scenario_ack_edge_lifecycle) + 1,
            order.index(scenarios.scenario_cause_alarm_quality))
        self.assertEqual(
            order.index(scenarios.scenario_cause_alarm_quality) + 1,
            order.index(scenarios.scenario_graceful_shutdown))
        self.assertEqual(
            order.index(scenarios.scenario_graceful_shutdown) + 1,
            order.index(scenarios.scenario_dcs_ctl))
        self.assertIs(verify.case_function('cause-alarm-quality'),
                      scenarios.scenario_cause_alarm_quality)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim attachment,
        # the field census and reads, the shipped tool's fault commands,
        # and the two value writes under the claim.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertIn('inject_fault', ops)
        self.assertIn('clear_fault', ops)
        self.assertEqual(ops.count('write'), 2)
        # Every driven input restored: both contacts healthy and clear,
        # the pump out of manual, no latch standing, and the field
        # command de-energized.
        for point in (CauseAlarmPlantPeer.THERMAL,
                      CauseAlarmPlantPeer.MOISTURE):
            self.assertEqual(self.plant.samples[point]['value'],
                             {'bool': False})
            self.assertEqual(self.plant.samples[point]['quality'], 'good')
            self.assertNotIn(point, self.plant.faults)
        self.assertEqual(
            self.plant.samples[CauseAlarmPlantPeer.CMD]['value'],
            {'bool': False})
        self.assertFalse(self.feed.values[self.feed.MODE])
        self.assertFalse(self.feed.values[self.feed.HAND])
        for tag in CauseAlarmFeed.CONTACTS:
            self.assertFalse(self.feed.values[
                CauseAlarmFeed.CONTACTS[tag][5]])
            self.assertFalse(self.feed.values[
                CauseAlarmFeed.CONTACTS[tag][3]])
        # One evidence file per phase, each recorded in the case once:
        # the report schema bounds a scenario's evidence list, so the
        # drives, the submissions, and the journaled record accumulate
        # into their own files.
        names = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(
            names, {'evidence/cause-alarm-' + stem + '.json'
                    for stem in ('signals', 'wiring', 'baseline',
                                 'receipts', 'thermal', 'moisture',
                                 'value-trip', 'stopped',
                                 'stopped-moisture', 'journal')})
        # Each drive's record carries its own legs beside the field's
        # own account of the contact and the command.
        for stem, legs in (
                ('thermal', ('thermal-degraded', 'contact',
                             'field-thermal-cut', 'thermal-cleared',
                             'field-thermal-restored',
                             'thermal-acknowledged',
                             'thermal-ack-released')),
                ('moisture', ('moisture-degraded', 'contact',
                              'field-moisture-cut', 'moisture-cleared',
                              'field-moisture-restored',
                              'moisture-acknowledged',
                              'moisture-ack-released')),
                ('stopped-moisture',
                 ('stopped-moisture-degraded', 'contact',
                  'field-stopped-moisture-cut',
                  'stopped-moisture-cleared',
                  'field-stopped-moisture-stayed-stopped',
                  'stopped-moisture-acknowledged',
                  'stopped-moisture-ack-released')),
                ('value-trip', ('value-trip', 'field-value-cut',
                                'value-released', 'value-acknowledged',
                                'value-ack-released',
                                'field-value-restored'))):
            record_file = json.loads(
                (self.evidence / ('cause-alarm-' + stem + '.json')
                 ).read_text())
            for leg_name in legs:
                self.assertIn(leg_name, record_file, stem)
        # The receipted submissions and the journaled record.
        receipts = json.loads(
            (self.evidence / 'cause-alarm-receipts.json').read_text())
        self.assertEqual(
            [entry['tag'] for entry in receipts['submissions']],
            ['hand-start', 'mode-start', 'hand-start',
             'thermal-ack-press', 'thermal-ack-release',
             'moisture-ack-press', 'moisture-ack-release',
             'value-ack-press', 'value-ack-release', 'hand-release',
             'stopped-moisture-ack-press',
             'stopped-moisture-ack-release', 'mode-restore'])
        self.assertTrue(all(entry['status'] == 200
                            for entry in receipts['submissions']))
        journal = json.loads(
            (self.evidence / 'cause-alarm-journal.json').read_text())
        self.assertEqual(journal['journal']['violations'], [])
        self.assertEqual(
            journal['journal']['transitions']['thermal:quality'],
            [{'bad': 'device_fault'}, 'good'])

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = CauseAlarmPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = CauseAlarmFeed(plant2)
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

    def test_missing_surface_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('declared quality-gated protection surface',
                      record.get('detail', ''))
        self.assertIn('p101-moisture-alarm', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.unwritable_ack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('p101-thermal-ack', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_descriptor_is_inconclusive(self):
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
        self.assertIn('off the declared points', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_in_binding_is_inconclusive(self):
        self.feed.bare_descriptor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('binds no in port', record.get('detail', ''))
        report.validate_scenario(record)

    def test_raw_contact_wiring_fails(self):
        # The pre-#827 shape: the cause alarm's condition binds the raw
        # contact, so a degraded reading can never annunciate through
        # it. The surface is declared, so the leg fails by name.
        self.feed.raw_contact_wiring = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('binds the raw p101-thermal contact',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer claim refused', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        ctx['plant'] = None
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('publishes no plant endpoint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_owner_token_is_inconclusive(self):
        ctx = self._ctx()
        ctx['plant_owner'] = {}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant-writer owner token', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standing_contact_is_inconclusive(self):
        # A contact already standing: the auto baseline the leg runs
        # from never presents.
        self.plant.samples[CauseAlarmPlantPeer.THERMAL]['value'] = \
            {'bool': True}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('auto baseline never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_lying_field_baseline_is_inconclusive(self):
        # The field's own account of the contact contradicts the served
        # surface: the leg reads the field, not just the monitor.
        self.plant.lying_baseline = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('does not read false at Good',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_degraded_contact_is_inconclusive(self):
        # The named posture gate: a contact already degraded ahead of
        # the drive — the auto baseline never presents.
        self.plant.faults[CauseAlarmPlantPeer.THERMAL] = \
            {'quality': {'bad': 'device_fault'}}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('auto baseline never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latched_baseline_is_inconclusive(self):
        self.feed.latched['thermal'] = True
        self.feed.states['thermal'] = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('auto baseline never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_write_fails(self):
        self.feed.refuse_write = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('was refused', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_cause_alarm_fails(self):
        # The #827 defect's live shape: the degraded contact trips the
        # protection and the cause alarm stays clean-false.
        self.feed.silent_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('thermal-degraded leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_never_latches_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('thermal-degraded leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_protection_never_trips_fails(self):
        # The protection is not fail-safe on quality: the command holds
        # and the trip never stands.
        self.feed.never_trips = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_command_ignores_the_trip_fails(self):
        self.feed.cmd_holds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_degraded_sample_replaces_the_value_fails(self):
        # The degraded reading silently replaces the stored field
        # value instead of substituting only its quality.
        self.plant.lying_degraded_value = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('replaced the stored field value',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fault_never_clears_fails(self):
        self.feed.never_clears = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('thermal-cleared leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stuck_latch_fails(self):
        self.feed.stuck_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('thermal-acknowledged leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_cross_annunciation_fails(self):
        # The sibling cause alarm annunciates under the other
        # contact's degradation — a double-firing.
        self.feed.cross_fires = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('thermal-degraded leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_annunciation_beyond_the_stopped_pump_fails(self):
        # An alarm beyond the declared contract plants only once the
        # pump stands stopped — the honest-absence half's negative.
        self.feed.fault_alarm_stopped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped-moisture-degraded leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_spurious_start_under_degradation_fails(self):
        # The stopped pump starts under the degraded contact.
        self.feed.start_under_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_late_annunciation_is_nondeterministic(self):
        # The cause alarm annunciates far outside the guard's declared
        # one-hop carrier crossing: the annunciation is no longer the
        # trip's own reading.
        self.feed.late_annunciation = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_duplicate_trip_is_nondeterministic(self):
        # The trip records twice: a duplicated protective event.
        self.feed.duplicate_trip = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_is_nondeterministic(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('role_changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_point_recording_is_nondeterministic(self):
        self.feed.journals_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('unjournaled point', record.get('detail', ''))
        report.validate_scenario(record)

    def test_quiet_point_recording_is_nondeterministic(self):
        self.feed.journals_quiet = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('declared-quiet point', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_fails(self):
        # No transition ever reaches the durable record: the planted
        # evidence the audit reads is absent, and the annunciation-order
        # pairs it must match are missing with it.
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('carries no trip and annunciation pair',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_settle_journal_fails(self):
        self.feed.no_settle_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('no settled receipt journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_moving_under_the_drive_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('the active role moved',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_peer_role_moved_fails(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('roles moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restore_read_lying_fails(self):
        self.plant.lying_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cause-alarm-quality-failed', record.get('detail', ''))
        self.assertIn('did not restore', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()