"""The 3900_power_fail_trip leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_power_fail_trip, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PowerTripTests.test_registered_in_scenarios',
    'PowerTripTests.test_clean_feed_passes_and_validates',
    'PowerTripTests.test_two_runs_produce_identical_evidence',
    'PowerTripTests.test_no_active_fails',
    'PowerTripTests.test_missing_wiring_is_inconclusive',
    'PowerTripTests.test_unwritable_ack_is_inconclusive',
    'PowerTripTests.test_bare_schema_is_inconclusive',
    'PowerTripTests.test_refused_claim_is_inconclusive',
    'PowerTripTests.test_standing_contact_is_inconclusive',
    'PowerTripTests.test_power_ok_never_reports_is_inconclusive',
    'PowerTripTests.test_power_ok_never_drops_fails',
    'PowerTripTests.test_availability_never_drops_fails',
    'PowerTripTests.test_commands_never_release_fails',
    'PowerTripTests.test_none_available_never_asserts_fails',
    'PowerTripTests.test_demand_drops_under_outage_fails',
    'PowerTripTests.test_duty_never_releases_fails',
    'PowerTripTests.test_mute_alarm_fails',
    'PowerTripTests.test_mute_unack_fails',
    'PowerTripTests.test_ack_rejected_fails',
    'PowerTripTests.test_ack_never_applies_fails',
    'PowerTripTests.test_unack_stuck_fails',
    'PowerTripTests.test_never_recovers_fails',
    'PowerTripTests.test_slow_restage_is_nondeterministic',
    'PowerTripTests.test_early_restage_is_nondeterministic',
    'PowerTripTests.test_journaled_role_change_is_nondeterministic',
    'PowerTripTests.test_unjournaled_point_recording_is_nondeterministic',
    'PowerTripTests.test_missing_journal_fails',
    'PowerTripTests.test_role_moving_under_the_drive_fails',
    'PowerTripTests.test_moved_pump_operator_state_fails',
    'PowerTripTests.test_restore_write_lying_fails',
})


class PowerTripPlantPeer(StagingPlantPeer):
    """The power-fail rig's plant half: StagingPlantPeer's shared-claim
    write path with the rig seeded down to the journaled `power-fail`
    field contact the scenario drives."""

    def __init__(self, owner, tripped=False):
        super().__init__(owner)
        self.samples = {
            120: {'value': {'bool': tripped}, 'quality': 'good',
                  'tick': 0}}


class PowerTripFeed:
    """A stubbed monitor pair for the power-fail-trip scenario: a tiny
    executor over the station's power-fail interlock chain — the
    inverted power-ok feeding each pump's availability aggregation one
    carrier hop per scan, the pump group's release-and-restage contract
    with its declared holdouts, and the managed bool-latching alarm set
    — against a real plant-protocol peer whose `power-fail` point the
    scenario writes under the shared claim. Every `http_json` call is
    one completed scan: the carriers deliver the last image one scan
    later, the demand chain holds between its hysteresis bounds, the
    group re-stages eligible pumps inside start_delay_ticks with the
    min_off_ticks holdout banked on every stop, the bool alarms latch
    on their fresh edge and clear under the receipted ack, and the run
    contacts loop the field commands back one scan later.
    Declared-journaled points record point_changed — the first observed
    sample included — the carrier and staging points do not. Fault
    flags stage each named failure the issue calls out."""

    POWER_FAIL = 120
    POWER_OK = 206
    POK_IN1, POK_IN2 = 311, 343
    AVAIL1, AVAIL2 = 328, 360
    AVAIL_IN1, AVAIL_IN2 = 329, 361
    CMD1, CMD2 = 100, 101
    RUN1, RUN2 = 40, 41
    MODE1, OOS1, MODE2, OOS2 = 300, 302, 332, 334
    LEVEL, CHAIN_IN, LAH_IN = 200, 201, 202
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED = 210, 211
    HIGH_LEVEL = 215
    NONE_AVAIL, NONE_IN = 217, 220
    NACK, NALARM, NUNACK = 1030, 1033, 1034
    LACK, LALARM, LUNACK = 1000, 1003, 1004
    ACK, ALARM, UNACK = 1050, 1053, 1054
    SHELVED, SUPPRESSED, ALARM_OOS = 1055, 1056, 1057
    JOURNALED = (40, 41, 120, 215, 217, 300, 302, 328, 332, 334, 360,
                 1003, 1004, 1033, 1034, 1053, 1054, 1055, 1056, 1057)
    INS = (10, 11, 12, 40, 41, 120, 201, 202, 205, 220, 300, 302, 311,
           329, 332, 334, 343, 361, 1000, 1030, 1050)
    # The declared setpoint chain, the staging bounds, and the well's
    # integration — the same table the deployed model serves.
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    START_DELAY, MIN_OFF, RESTAGE_DELAY = 1, 2, 0
    BIAS, DRAW, DT = 0.2, -1.0, 0.1

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 12, 'signal': 10012, 'name': 'inflow',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 40, 'signal': 10040, 'name': 'p101-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 41, 'signal': 10041, 'name': 'p102-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 100, 'signal': 10100, 'name': 'p101-cmd',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 101, 'signal': 10101, 'name': 'p102-cmd',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 120, 'signal': 10120, 'name': 'power-fail',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
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
        {'point': 206, 'signal': 10206, 'name': 'power-ok',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 210, 'signal': 10210, 'name': 'duty',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 211, 'signal': 10211, 'name': 'staged',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 215, 'signal': 10215, 'name': 'high-level',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 217, 'signal': 10217, 'name': 'none-available',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 220, 'signal': 10220, 'name': 'none-available-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 300, 'signal': 10300, 'name': 'p101-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 302, 'signal': 10302, 'name': 'p101-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 311, 'signal': 10311, 'name': 'p101-power-ok-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 328, 'signal': 10328, 'name': 'p101-avail',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 329, 'signal': 10329, 'name': 'p101-avail-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 332, 'signal': 10332, 'name': 'p102-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 334, 'signal': 10334, 'name': 'p102-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 343, 'signal': 10343, 'name': 'p102-power-ok-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 360, 'signal': 10360, 'name': 'p102-avail',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 361, 'signal': 10361, 'name': 'p102-avail-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 1000, 'signal': 11000, 'name': 'lah-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1003, 'signal': 11003, 'name': 'lah-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1004, 'signal': 11004, 'name': 'lah-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1030, 'signal': 11030, 'name': 'none-available-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1033, 'signal': 11033, 'name': 'none-available-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1034, 'signal': 11034,
         'name': 'none-available-unacknowledged', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1050, 'signal': 11050, 'name': 'power-fail-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1053, 'signal': 11053, 'name': 'power-fail-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1054, 'signal': 11054,
         'name': 'power-fail-unacknowledged', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1055, 'signal': 11055, 'name': 'power-fail-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1056, 'signal': 11056,
         'name': 'power-fail-suppressed', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 1057, 'signal': 11057,
         'name': 'power-fail-out-of-service', 'direction': 'out',
         'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []           # accepted commands awaiting boundary
        self.level = 0.8
        self.demand_held = 0        # the chain's held stage count
        self.last_demand = 0        # the group's last Good demand
        self.duty_index = None      # 0 | 1 | None
        self.cursor = 0             # rotation cursor, 0-based
        self.cmd = [False, False]   # commanded pumps, 0-based
        self.held_until = [0, 0]    # banked stop holdouts
        self.last_start = None      # tick the last start issued
        self.pending_start = {}     # pump -> first target tick
        self.alarm_state = False    # the power-fail alarm's tracked in
        self.latched = False
        self.none_state = False     # the none-available alarm's in
        self.none_latched = False
        self.lah_state = False
        self.lah_latched = False
        self._ok_dropped = False    # power-ok has dropped once
        self.values = {
            10: 0.8, 11: 0.8, 12: 0.0,
            40: False, 41: False, 100: False, 101: False, 120: False,
            200: 0.8, 201: 0.8, 202: 0.8,
            204: 0, 205: 0, 206: True, 210: 0, 211: 0,
            215: False, 217: False, 220: False,
            300: False, 302: False, 311: True, 328: True, 329: True,
            332: False, 334: False, 343: True, 360: True, 361: True,
            1000: False, 1003: False, 1004: False,
            1030: False, 1033: False, 1034: False,
            1050: False, 1053: False, 1054: False,
            1055: False, 1056: False, 1057: False}
        self.jseen = {}             # last journaled value per point
        self.history = {}           # point -> [{'seq','sample'}]
        self.hseq = {}
        self._role_journaled = False
        self._demand_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False         # ctrl-a never reports active
        self.bare_signals = False      # the interlock wiring absent
        self.bad_ack_signal = False    # power-fail-ack non-writable
        self.bare_schema = False       # schema lacks the components
        self.missing_power_ok = False  # 206 absent from the snapshot
        self.ok_holds = False          # power-ok never drops
        self.avail_holds = False       # availability never drops
        self.cmd_holds = False         # the commands never release
        self.never_none = False        # none-available never asserts
        self.duty_holds = False        # duty never releases
        self.demand_drops = False      # the chain stops calling
        self.mute_alarm = False        # the alarm never stands
        self.mute_unack = False        # the latch never latches
        self.unack_stuck = False       # the latch never clears
        self.ack_rejected = False      # the ack submission is refused
        self.ack_never_applies = False # the accepted ack never settles
        self.never_recovers = False    # power-ok never returns
        self.slow_restage = False      # the re-stage lands beyond bound
        self.early_restage = False     # a start lands in the outage
        self.role_moves = False        # the trip reads as peer loss
        self.journals_role = False     # a role_changed entry lands
        self.journals_demand = False   # a non-journaled point journals
        self.no_journal = False        # transitions never journal
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

    def _assign(self, avail):
        """The rotation policy's pick — the next available pump in
        rotation order after the last holder; none when nothing is."""
        self.duty_index = None
        for offset in range(2):
            idx = (self.cursor + offset) % 2
            if avail[idx]:
                self.duty_index = idx
                break
        if self.duty_index is not None:
            self.cursor = (self.duty_index + 1) % 2

    # The pump group's step on the delivered inputs, mirroring the
    # block: fail-safe availability reads, exclusion handing duty over
    # the same scan, duty-first staging gated on the banked holdouts,
    # each fresh start gated on start_delay_ticks since the last one.
    def _step_group(self):
        demand_in = self.values[self.DEMAND_IN]
        demand_eff = self.last_demand
        if isinstance(demand_in, int) and not isinstance(demand_in,
                                                         bool):
            demand_eff = max(0, min(2, demand_in))
        avail = [bool(self.values[self.AVAIL_IN1]),
                 bool(self.values[self.AVAIL_IN2])]
        if self.duty_index is not None \
                and not avail[self.duty_index]:
            self._assign(avail)
        elif self.last_demand >= 1 and demand_eff == 0:
            self._assign(avail)
        if self.duty_index is None:
            self._assign(avail)
        lead = self.duty_index if self.duty_index is not None \
            else self.cursor
        if self.cmd_holds and self.values[self.POWER_FAIL]:
            # The interlock never releases the commands it holds.
            targets = [idx for idx in range(2) if self.cmd[idx]]
        else:
            targets = []
            for offset in range(2):
                if len(targets) >= demand_eff:
                    break
                idx = (lead + offset) % 2
                if (avail[idx] or self.early_restage) \
                        and (self.cmd[idx] or self.early_restage
                             or self.tick >= self.held_until[idx]):
                    targets.append(idx)
        new = [False, False]
        for idx in targets:
            if self.cmd[idx]:
                new[idx] = True
                continue
            self.pending_start.setdefault(idx, self.tick)
            if self.slow_restage \
                    and self.tick < self.pending_start[idx] \
                    + self.START_DELAY + 3:
                continue
            if self.last_start is None \
                    or self.tick >= self.last_start + self.START_DELAY:
                new[idx] = True
                self.last_start = self.tick
                self.pending_start.pop(idx, None)
        for idx in range(2):
            if self.cmd[idx] and not new[idx]:
                hold = self.MIN_OFF
                if avail[idx] and demand_eff > 0:
                    hold = max(hold, self.RESTAGE_DELAY)
                self.held_until[idx] = self.tick + hold
            self.cmd[idx] = new[idx]
            if idx not in targets:
                self.pending_start.pop(idx, None)
        self.last_demand = demand_eff
        computed_duty = 0 if self.duty_index is None \
            else self.duty_index + 1
        # duty_holds keeps the last designation published — the output
        # never drops its holder under the trip.
        if self.duty_holds and computed_duty == 0 \
                and self.values[self.DUTY] > 0:
            computed_duty = self.values[self.DUTY]
        self.values[self.DUTY] = computed_duty
        self.values[self.STAGED] = int(sum(new))
        self.values[self.CMD1], self.values[self.CMD2] = new
        nav = not any(avail)
        self.values[self.NONE_AVAIL] = False if self.never_none \
            else nav

    # One completed scan: boundary-settled commands first, then the
    # carriers' one-scan delivery, the components in declared order —
    # the field read, the availability aggregation, the threshold
    # chain, the group, the alarm set — the journaled diffs, the
    # per-point history append, and the dynamics step.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            if self.ack_never_applies:
                self.pending.append(receipt)
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if write['point'] in (self.ACK, self.NACK, self.LACK,
                                  self.MODE1, self.OOS1,
                                  self.MODE2, self.OOS2):
                self.values[write['point']] = \
                    write['value']['bool']
            if not self.no_journal:
                self._entry({'command_settled': {'receipt':
                                                 dict(receipt)}})
        # The carriers deliver last tick's image.
        prev_power_ok = self.values[self.POWER_OK]
        prev_avail = (self.values[self.AVAIL1],
                      self.values[self.AVAIL2])
        prev_none = self.values[self.NONE_AVAIL]
        prev_demand = self.values[self.DEMAND]
        prev_selected = self.values[self.LEVEL]
        prev_cmd = list(self.cmd)
        # The field read: the driven contact, served same-scan.
        pf = self.plant.samples[self.POWER_FAIL]['value'] \
            .get('bool', False)
        self.values[self.POWER_FAIL] = pf
        if pf:
            self._ok_dropped = True
        ok = not pf
        if self.ok_holds:
            ok = True
        if self.never_recovers and self._ok_dropped:
            ok = False
        self.values[self.POWER_OK] = ok
        self.values[self.POK_IN1] = prev_power_ok
        self.values[self.POK_IN2] = prev_power_ok
        # The availability aggregation — in the fake the auto, oos,
        # thermal, and moisture legs stand; power-ok-in is the only
        # leg that can drop.
        for avail_point, pok_in in ((self.AVAIL1, self.POK_IN1),
                                    (self.AVAIL2, self.POK_IN2)):
            gate = bool(self.values[pok_in])
            self.values[avail_point] = True if self.avail_holds \
                else gate
        self.values[self.AVAIL_IN1] = prev_avail[0]
        self.values[self.AVAIL_IN2] = prev_avail[1]
        self.values[self.NONE_IN] = prev_none
        self.values[self.DEMAND_IN] = prev_demand
        self.values[self.CHAIN_IN] = prev_selected
        self.values[self.LAH_IN] = prev_selected
        self.values[self.RUN1] = prev_cmd[0]
        self.values[self.RUN2] = prev_cmd[1]
        self.values[self.LEVEL] = self.level
        self.values[10] = self.level
        self.values[11] = self.level
        # The threshold chain holds its demand between the hysteresis
        # bounds; a rig that stops calling under the outage is the
        # named failure.
        level_in = self.values[self.CHAIN_IN]
        if self.demand_drops and pf:
            self.demand_held = 0
        elif level_in <= self.STOP:
            self.demand_held = 0
        elif self.demand_held == 2 and level_in <= self.START:
            self.demand_held = 1
        elif level_in >= self.LAG_START:
            self.demand_held = 2
        elif self.demand_held == 0 and level_in >= self.START:
            self.demand_held = 1
        self.values[self.DEMAND] = self.demand_held
        self.values[self.HIGH_LEVEL] = level_in >= self.HIGH
        self._step_group()
        # The power-fail alarm reads the field contact directly — its
        # two flags land the same scan the contact does — while the
        # none-available alarm's input rides its own carrier.
        for state_key, in_, ack_point, alarm_point, unack_point in (
                ('alarm_state', pf, self.ACK, self.ALARM, self.UNACK),
                ('none_state', self.values[self.NONE_IN], self.NACK,
                 self.NALARM, self.NUNACK),
                ('lah_state', self.values[self.LAH_IN] >= self.HIGH,
                 self.LACK, self.LALARM, self.LUNACK)):
            previous = getattr(self, state_key)
            fresh = in_ and not previous
            setattr(self, state_key, bool(in_))
            latch_key = {'alarm_state': 'latched',
                         'none_state': 'none_latched',
                         'lah_state': 'lah_latched'}[state_key]
            ack = self.values[ack_point]
            if self.unack_stuck and latch_key == 'latched':
                ack = False
            setattr(self, latch_key,
                    (getattr(self, latch_key) or fresh) and not ack)
            self.values[alarm_point] = bool(in_)
            self.values[unack_point] = getattr(self, latch_key)
        if self.mute_alarm:
            self.values[self.ALARM] = False
        if self.mute_unack:
            self.values[self.UNACK] = False
        self._journal_values()
        # Injected journal-contract violations.
        if self.journals_role and self.values[self.ALARM] \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})
        if self.journals_demand and self.values[self.ALARM] \
                and not self._demand_journaled:
            self._demand_journaled = True
            self._entry({'point_changed': {
                'point': self.DEMAND,
                'from': {'int': 0},
                'to': {'int': self.values[self.DEMAND]}}})
        # Operator state the leg never drove must not move.
        if self.moves_pump_state and self.values[self.ALARM]:
            self.values[self.OOS1] = True
        for point, value in self.values.items():
            self.hseq[point] = self.hseq.get(point, 0) + 1
            self.history.setdefault(point, []).append({
                'seq': self.hseq[point],
                'sample': {'value': self._wrap(value),
                           'quality': 'good', 'tick': self.tick}})
        # The plant's dynamics: the declared bias inflow plus each
        # commanded pump's draw integrates into the well.
        self.level += (self.BIAS + self.DRAW * sum(self.cmd)) * self.DT

    def _parameters(self):
        return [
            {'name': 'select', 'values': {}},
            {'name': 'chain', 'values': dict(
                {key: {'float': value} for key, value in {
                    'cutoff': self.CUTOFF, 'stop': self.STOP,
                    'start': self.START, 'lag_start': self.LAG_START,
                    'high': self.HIGH}.items()},
                on_bad_demand={'int': 0})},
            {'name': 'group', 'values': {
                'rotation': {'int': 0},
                'start_delay_ticks': {'int': self.START_DELAY},
                'restage_delay_ticks': {'int': self.RESTAGE_DELAY},
                'min_off_ticks': {'int': self.MIN_OFF}}},
            {'name': 'power-alarm', 'values': {
                'priority': {'int': 1}, 'class': {'int': 1},
                'response_ticks': {'int': 2}}}]

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
            {'name': 'chain', 'interface': self._interface(
                'threshold-chain', [('level', 201), ('demand', 204)])},
            {'name': 'group', 'interface': self._interface(
                'pump-group', [('demand', 205), ('staged', 211)],
                [('duty', 210)])},
            {'name': 'power-alarm', 'interface': self._interface(
                'managed-bool-latching-alarm', [('in', 120)],
                [('alarm', 1053), ('unacknowledged', 1054)])},
            {'name': 'none-alarm', 'interface': self._interface(
                'managed-bool-latching-alarm', [('in', 220)],
                [('alarm', 1033), ('unacknowledged', 1034)])},
            {'name': 'lah', 'interface': self._interface(
                'managed-latching-alarm', [('in', 202)],
                [('alarm', 1003), ('unacknowledged', 1004)])}]}

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
            if self.no_active or (self.role_moves
                                  and self.values[self.ALARM]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'power-fail-ack'
                          else entry for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/schema'):
            return 200, self._schema()
        if (method, route) == ('GET', '/snapshot'):
            points = []
            for point, value in sorted(self.values.items()):
                if self.missing_power_ok and point == self.POWER_OK:
                    continue
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': self._wrap(value),
                               'quality': 'good',
                               'tick': self.tick}})
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
            if write and write.get('point') in (
                    self.ACK, self.NACK, self.LACK, self.MODE1,
                    self.OOS1, self.MODE2, self.OOS2) \
                    and not self.ack_rejected:
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


class PowerTripTests(unittest.TestCase):
    """scenario_power_fail_trip against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance leg —
    power-ok drop, availability loss, command release, the
    none-available annunciation, the alarm lifecycle, the ack while
    the condition stands, the bounded recovery, role stability — plus
    the inconclusive paths."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = PowerTripPlantPeer(self.OWNER)
        self.feed = PowerTripFeed(self.plant)

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
                patch.object(scenarios, 'POWER_TRIP_POLL', 0.001), \
                patch.object(scenarios, 'POWER_TRIP_DEADLINE', 3.0):
            return scenarios.scenario_power_fail_trip(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The self-contained cluster ahead of the schedule's closing
        # observation case.
        self.assertEqual(
            order.index(scenarios.scenario_unavailable_fallback) + 1,
            order.index(scenarios.scenario_power_fail_trip))
        self.assertEqual(
            order.index(scenarios.scenario_power_fail_trip) + 1,
            order.index(scenarios.scenario_dcs_ctl))
        self.assertIs(verify.case_function('power-fail-trip'),
                      scenarios.scenario_power_fail_trip)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim attachment,
        # a baseline read and a restore read, and the drive/release
        # write pair.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertEqual(ops.count('write'), 2)
        # The driven contact restored, every latch re-armed, roles
        # unmoved, and the group cycling on its own demand again.
        self.assertEqual(
            self.plant.samples[120]['value'], {'bool': False})
        self.assertFalse(self.feed.latched)
        self.assertFalse(self.feed.none_latched)
        self.assertFalse(self.feed.values[self.feed.ACK])
        self.assertFalse(self.feed.values[self.feed.NACK])
        self.assertTrue(self.feed.values[self.feed.POWER_OK])

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = PowerTripPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = PowerTripFeed(plant2)
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
        self.assertIn('lacks the power-fail interlock wiring',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('power-fail-ack', record.get('detail', ''))
        report.validate_scenario(record)

    def test_bare_schema_is_inconclusive(self):
        self.feed.bare_schema = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('served schema lacks', record.get('detail', ''))
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
        self.plant.samples[120]['value'] = {'bool': True}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('does not read false', record.get('detail', ''))
        report.validate_scenario(record)

    def test_power_ok_never_reports_is_inconclusive(self):
        # The named inconclusive leg: the served snapshot never
        # carries power-ok, so the baseline window never lands.
        self.feed.missing_power_ok = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('power_ok', record.get('detail', ''))
        report.validate_scenario(record)

    def test_power_ok_never_drops_fails(self):
        self.feed.ok_holds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        self.assertIn('tripped leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_availability_never_drops_fails(self):
        self.feed.avail_holds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_commands_never_release_fails(self):
        self.feed.cmd_holds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_none_available_never_asserts_fails(self):
        self.feed.never_none = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demand_drops_under_outage_fails(self):
        self.feed.demand_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_never_releases_fails(self):
        self.feed.duty_holds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_mute_alarm_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_mute_unack_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_rejected_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_never_applies_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unack_stuck_fails(self):
        self.feed.unack_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_recovers_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        self.assertIn('recovered leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_slow_restage_is_nondeterministic(self):
        # The re-stage lands beyond the declared start_delay bound —
        # the named nondeterminism the bound exists to catch.
        self.feed.slow_restage = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_early_restage_is_nondeterministic(self):
        # A start lands while the outage still stands — an output step
        # outside the deterministic scan sequence.
        self.feed.early_restage = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_is_nondeterministic(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_point_recording_is_nondeterministic(self):
        self.feed.journals_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_moving_under_the_drive_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        self.assertIn('active role moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_pump_operator_state_fails(self):
        self.feed.moves_pump_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved pump operator state',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_restore_write_lying_fails(self):
        self.plant.lying_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('power-trip-failed', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
