"""The 2485_reclaim_convergence_gate leg's scenario unit coverage —
the feed fake and TestCase class for
scenario_reclaim_convergence_gate, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EX_OWNER = 'driven'
INCUMBENT = 'foreign'
CONTROL = 'revised'
SEATS = (EX_OWNER, INCUMBENT, CONTROL)

EXPECTED_CASES = frozenset({
    'ReclaimConvergenceTests.test_registered_after_sim_bus_before_revisions',
    'ReclaimConvergenceTests.test_fixed_shape_passes_validates_and_tears_down',
    'ReclaimConvergenceTests.test_two_runs_produce_identical_evidence',
    'ReclaimConvergenceTests.test_two_passes_produce_identical_digests',
    'ReclaimConvergenceTests.test_gating_asserted_while_reclaim_took_field_fails',
    'ReclaimConvergenceTests.test_reclaim_rearming_the_claim_fails',
    'ReclaimConvergenceTests.test_reclaim_asking_the_standing_claim_fails',
    'ReclaimConvergenceTests.test_ownership_epoch_rolling_again_fails',
    'ReclaimConvergenceTests.test_pre_contract_reclaim_is_inconclusive',
    'ReclaimConvergenceTests.test_ex_owner_promoting_on_its_monitor_fails',
    'ReclaimConvergenceTests.test_ex_owner_converging_in_the_window_fails',
    'ReclaimConvergenceTests.test_field_unclaimed_under_the_ex_owner_fails',
    'ReclaimConvergenceTests.test_ex_owner_never_fenced_fails',
    'ReclaimConvergenceTests.test_fenced_ex_owner_tracking_fails',
    'ReclaimConvergenceTests.test_fenced_ex_owner_sees_no_claim_fails',
    'ReclaimConvergenceTests.test_demotion_walk_unjournaled_fails',
    'ReclaimConvergenceTests.test_loss_unjournaled_fails',
    'ReclaimConvergenceTests.test_loss_journaled_twice_fails',
    'ReclaimConvergenceTests.test_loss_unattributed_fails',
    'ReclaimConvergenceTests.test_loss_wrong_claimant_fails',
    'ReclaimConvergenceTests.test_link_never_dropping_fails',
    'ReclaimConvergenceTests.test_incumbent_displaced_fails',
    'ReclaimConvergenceTests.test_incumbent_losing_the_claim_fails',
    'ReclaimConvergenceTests.test_incumbent_tick_stalled_fails',
    'ReclaimConvergenceTests.test_incumbent_fenced_on_reattach_fails',
    'ReclaimConvergenceTests.test_seat_process_exiting_fails',
    'ReclaimConvergenceTests.test_control_reclaim_silent_fails',
    'ReclaimConvergenceTests.test_control_walk_unjournaled_fails',
    'ReclaimConvergenceTests.test_control_operator_promote_fails',
    'ReclaimConvergenceTests.test_control_ex_owner_exiting_fails',
    'ReclaimConvergenceTests.test_control_never_converging_is_nondeterministic',
    'ReclaimConvergenceTests.test_control_never_claiming_is_nondeterministic',
    'ReclaimConvergenceTests.test_pair_owner_disturbed_fails',
    'ReclaimConvergenceTests.test_pair_owner_stalled_fails',
    'ReclaimConvergenceTests.test_pair_member_promoted_fails',
    'ReclaimConvergenceTests.test_launch_layout_unrestored_fails',
    'ReclaimConvergenceTests.test_second_pass_defect_fails',
    'ReclaimConvergenceTests.test_divergent_digests_are_nondeterministic',
    'ReclaimConvergenceTests.test_serve_failure_is_nondeterministic',
    'ReclaimConvergenceTests.test_launch_failure_is_nondeterministic',
    'ReclaimConvergenceTests.test_freeze_failure_is_nondeterministic',
    'ReclaimConvergenceTests.test_thaw_failure_is_nondeterministic',
    'ReclaimConvergenceTests.test_ex_owner_never_activating_is_nondeterministic',
    'ReclaimConvergenceTests.test_incumbent_never_claiming_is_nondeterministic',
    'ReclaimConvergenceTests.test_starved_window_is_nondeterministic',
    'ReclaimConvergenceTests.test_dropped_durable_read_is_nondeterministic',
    'ReclaimConvergenceTests.test_vanished_container_is_nondeterministic',
    'ReclaimConvergenceTests.test_unsettled_pair_is_inconclusive',
    'ReclaimConvergenceTests.test_missing_seams_are_inconclusive',
    'ReclaimConvergenceTests.test_missing_journals_are_inconclusive',
    'ReclaimConvergenceTests.test_unchecked_self_check_fails',
    'ReclaimConvergenceTests.test_judge_self_check_is_complete',
})


class ReclaimConvergenceFeed:
    """A stubbed rig for the convergence-gated reclaim leg. The
    deployed pair is the undisturbed subject — 'active' holds the
    field's claim, 'standby' tracks it, and both keep serving through
    every staged episode. The lane's scratch sim-serve field is the
    subject field: `start_born_field` serves it with an empty claim
    table and severs every live attachment's control connection, a
    born-active launch on an unclaimed field claims it and demotes
    every other field-owning seat in place with the fencing loss
    journaled, `pause_born_field` holds the field frozen until both
    seats' served links report down, and the thaw drops the claim's
    holders — a `dcs-sim-net` claim outlives them — and settles the
    race: on the fixed revision the incumbent's re-attach re-binds its
    own claim and the armed ex-owner's gated reclaim never issues;
    with `reclaim_preempts` the ungated build's ask wins the race on
    transport ordering and re-activates the stale ex-owner. A
    converged standby whose dead owner's claim stands holderless
    reclaims it by design, which is the positive control. Every
    transition is keyed off the leg's own lever calls — never
    wall-clock — so two passes emit identical evidence, and every
    fault flag stages a named failure, a pre-contract shape, or an
    instability."""
    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': EX_OWNER, 'ctrl-f:4': INCUMBENT,
             'ctrl-c:3': CONTROL}
    SEATS = SEATS
    TOKENS = {'active': 424243, 'standby': 424244, 'revised': 424245,
              'foreign': 424246, 'driven': 424247}
    REMOTE = 'dcs-hw-qa-1-born-plant:9001'
    FIELD_POINT = 100
    # The reads the frozen field answers before a seat's served link
    # reports down — the freeze is held long enough for the driver's
    # own failure to surface, not merely for the pause to land.
    LINK_STALL = 2

    def __init__(self, root):
        self.root = Path(root)
        self.journals = {seat: self.root / 'controllers' / seat
                         / 'journal.jsonl' for seat in SEATS}
        self.calls = []
        self.seq = {}
        self.serves = 0
        self.thaws = 0
        self.polls = 0
        self.raced = False
        self.restore_broken = False
        self.field = {'claim': None, 'holders': set(), 'frozen': False}
        self.members = {
            'active': {'role': 'active', 'tick': 0,
                       'field_claim': 'held'},
            'standby': {'role': 'standby', 'tick': 0,
                        'field_claim': 'held'}}
        self.seats = {}
        # Fault injection — the named failures, the pre-contract
        # shape, and the instabilities.
        self.serve_fails = False       # the scratch field never served
        self.launch_fails = False      # a born launch never ran
        self.pause_fails = False       # the freeze never landed
        self.thaw_fails = False        # the thaw never landed
        self.owner_never_claims = False   # the ex-owner never activated
        self.preemptor_never_claims = False  # nobody claimed the
                                              # re-served field
        self.ex_owner_never_fenced = False  # the fence never demoted
        self.fenced_ex_owner_tracks = False  # it adopted a source
        self.fenced_sees_unclaimed = False   # it observed no claim
        self.no_demotion_walk = False  # the demotion never journaled
        self.loss_unjournaled = False  # the fencing loss never landed
        self.loss_twice = False        # a second loss record landed
        self.loss_unattributed = False  # the loss named no claimant
        self.loss_wrong_claimant = False  # the wrong claimant
        self.link_stays_up = False     # the freeze dropped no link
        self.reclaim_preempts = False  # the pre-contract shape
        self.gating_asserted_take = False  # the doctored contradiction
        self.reclaim_rearms = False    # a re-armed claim landed
        self.reclaim_asks_anyway = False  # the refused ask journaled
        self.epoch_rolls_again = False  # the epoch rolled twice
        self.ex_owner_promoted = False  # its monitor showed the take
        self.ex_owner_converges = False  # it tracked inside the window
        self.field_unclaimed = False   # its probe answered unclaimed
        self.incumbent_displaced = False  # it lost the field
        self.incumbent_loses_claim = False  # its claim read unclaimed
        self.incumbent_stalls = False  # its tick froze
        self.incumbent_fenced = False  # it demoted on its re-attach
        self.seat_exits = False        # a seat's process stopped
        self.container_vanished = False  # its container vanished
        self.monitor_starves = False   # a monitor stopped answering
        self.journal_dropped = False   # the durable read dropped
        self.control_never_claims = False   # the control never owned
        self.control_never_converges = False  # it never tracked
        self.control_silent = False    # its reclaim never landed
        self.control_walk_unjournaled = False  # no reclaim walk
        self.control_operator_promote = False  # an operator promote
        self.control_exits = False     # it stopped on the re-take
        self.pair_owner_demoted = False  # the pair was disturbed
        self.pair_owner_stalls = False
        self.pair_member_promoted = False
        self.no_active = False         # the pair never settled
        self.pair_unsettled = False    # the pair is off its launch shape
        self.second_pass_defect = False  # pass 2's ex-owner converges
        self.never_restored = False    # the restore never converges

    # --- the runner's levers, faked ---------------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.serve_fails:
            raise RuntimeError('docker run failed: name in use')
        self.serves += 1
        # A fresh plant server: an empty claim table, and every live
        # attachment's control connection severed by the replacement.
        self.field = {'claim': None, 'holders': set(), 'frozen': False}
        for state in self.seats.values():
            if state['live']:
                state['link'] = 'connected'
                state['stall'] = self.LINK_STALL
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': self.REMOTE, 'mode': mode}

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, peer, standby))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        old = self.seats.get(seat)
        if old is not None and old['live'] \
                and old['role'] in ('active', 'promoting'):
            raise RuntimeError('start_born_controller refuses to '
                               'replace dcs-hw-qa-1-' + seat
                               + ': it reports role ' + old['role'])
        if seat == EX_OWNER:
            # The pass's first launch: the race latch and the
            # post-outage fault windows start clean for this pass.
            self.raced = False
        self._boundary(seat)
        state = {'live': True, 'role': 'standby',
                 'sync': 'unsynchronized', 'claim': None,
                 'owns': False, 'converged': False, 'armed': False,
                 'tick': 0, 'link': 'connected', 'stall': 0,
                 'peer': peer, 'track': standby, 'exit': None}
        self.seats[seat] = state
        if standby is not None:
            state['sync'] = {'tracking': {'aligned': 0}}
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'monitor': 'http://ctrl-' + seat + ':1'}
        if self.owner_never_claims and seat == EX_OWNER:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'monitor': 'http://ctrl-' + seat + ':1'}
        if self.control_never_claims and seat == CONTROL:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'monitor': 'http://ctrl-' + seat + ':1'}
        if self.preemptor_never_claims and seat == INCUMBENT:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'monitor': 'http://ctrl-' + seat + ':1'}
        self._claim(seat)
        return {'container': 'dcs-hw-qa-1-' + seat,
                'monitor': 'http://ctrl-' + seat + ':1'}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        state = self.seats.pop(seat, None)
        if state is None:
            return
        # A connection's end drops only its own hold: the claim stands
        # holderless, the dead-owner shape the reclaim preempts.
        if state['owns'] and self.field['claim'] == self.TOKENS[seat]:
            self.field['holders'] = set()
            self._release()

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        entry = self.seats.get(seat)
        if entry is None:
            return {'container': 'dcs-hw-qa-1-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        if self.container_vanished and self.raced and seat == EX_OWNER:
            # The container vanished between reads: no process verdict
            # exists to audit.
            return {'container': 'dcs-hw-qa-1-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': entry['live'], 'exit': entry['exit'],
                'logs': '', 'absent': False}

    def pause_field(self):
        self.calls.append(('pause_born_field',))
        if self.pause_fails:
            raise RuntimeError('docker pause failed')
        self.field['frozen'] = True
        if self.journal_dropped:
            self.journals[EX_OWNER].unlink(missing_ok=True)
        for state in self.seats.values():
            if state['live']:
                state['stall'] = 0 if self.link_stays_up \
                    else self.LINK_STALL

    def unpause_field(self):
        self.calls.append(('unpause_born_field',))
        if self.thaw_fails:
            raise RuntimeError('docker unpause failed')
        self.field['frozen'] = False
        self.thaws += 1
        for state in self.seats.values():
            if state['live']:
                state['link'] = 'connected'
                state['stall'] = 0
        # Both connections died under the freeze, so the incumbent's
        # claim stands with no holders left.
        self.field['holders'] = set()
        self._race()

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        self.field = {'claim': None, 'holders': set(), 'frozen': False}
        if self.never_restored:
            # The teardown left the pair off its launch roles, so the
            # restore can never converge.
            self.restore_broken = True

    # --- the field's claim arbitration, faked ----------------------

    def _claim(self, seat):
        """A born-active's conditional startup grant lands on the
        unclaimed field: it takes the claim and every other
        field-owning seat meets the fence on its next write."""
        token = self.TOKENS[seat]
        for other, state in sorted(self.seats.items()):
            if other != seat and state['live'] and state['owns']:
                self._fence(other, token)
        self.field['claim'] = token
        self.field['holders'] = {seat}
        entry = self.seats[seat]
        entry['role'] = 'active'
        entry['sync'] = None
        entry['owns'] = True
        entry['claim'] = 'held'
        entry['converged'] = False

    def _fence(self, seat, claimant):
        """The fencing demotion in place: `active → demoting →
        standby` under the fenced origin with the loss journaled, the
        arm the fencing-loss reclaim reads."""
        entry = self.seats[seat]
        self._journal(seat, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': 'fenced',
            'actor': None}})
        if seat == CONTROL:
            converged = not self.control_never_converges
            entry['sync'] = {'tracking': {'aligned': 0}} if converged \
                else 'unsynchronized'
        else:
            converged = bool(self.fenced_ex_owner_tracks)
            entry['sync'] = {'tracking': {'aligned': 0}} if converged \
                else 'unsynchronized'
        entry['converged'] = converged
        if not self.loss_unjournaled and not (
                seat == EX_OWNER and self.loss_twice):
            self._journal(seat, {'field_claim_lost': {
                'point': self.FIELD_POINT,
                'claimant': None if self.loss_unattributed
                else (424249 if self.loss_wrong_claimant
                      else claimant)}})
        if not self.no_demotion_walk:
            self._journal(seat, {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'fenced', 'actor': None}})
        if seat == EX_OWNER and self.ex_owner_never_fenced:
            # The demotion never walked to standby: the ex-owner is
            # still mid-demotion, so nothing armed the reclaim.
            entry['role'] = 'demoting'
        else:
            entry['role'] = 'standby'
        entry['owns'] = False
        entry['armed'] = True
        entry['claim'] = 'unclaimed' if self.fenced_sees_unclaimed \
            else 'held'
        entry['link'] = 'connected'
        entry['stall'] = self.LINK_STALL

    def _reclaim_walk(self, seat, origin='reclaim'):
        self._journal(seat, {'role_changed': {
            'from': 'standby', 'to': 'promoting', 'origin': origin,
            'actor': None}})
        self._journal(seat, {'role_changed': {
            'from': 'promoting', 'to': 'active', 'origin': origin,
            'actor': None}})

    def _race(self):
        """The thaw's resolution — the incumbent's re-attach against
        the armed ex-owner's fencing-loss reclaim. On the fixed
        revision the gate holds the ask back entirely."""
        if self.raced:
            return
        self.raced = True
        if self.pair_owner_demoted:
            # The deployed pair's field owner is disturbed mid-episode.
            self.members['active'].update({'role': 'standby',
                                          'field_claim': None})
        claim = self.field['claim']
        holder = self._claim_holder()
        ex = self.seats.get(EX_OWNER)
        armed = (ex is not None and ex['live'] and ex['armed']
                 and claim is not None
                 and claim != self.TOKENS[EX_OWNER])
        if armed and self.reclaim_preempts:
            # The pre-contract shape: the ungated ask preempts the
            # holderless claim on stale state, re-activating the
            # ex-owner while the incumbent is fenced on its re-attach.
            self._reclaim_walk(EX_OWNER)
            ex['role'] = 'active'
            ex['sync'] = None
            ex['owns'] = True
            ex['claim'] = 'held'
            self.field['claim'] = self.TOKENS[EX_OWNER]
            self.field['holders'] = {EX_OWNER}
            if holder is not None:
                self._fence(holder, self.TOKENS[EX_OWNER])
            return
        if armed and self.gating_asserted_take:
            # The doctored rig: the durable journal proves the reclaim
            # took the field while every served read keeps reporting
            # the convergence gating holding.
            self._reclaim_walk(EX_OWNER)
        if armed and self.reclaim_rearms:
            self._reclaim_walk(EX_OWNER)
            self._journal(EX_OWNER, {'field_claim_rearmed': {
                'point': self.FIELD_POINT}})
        if armed and self.reclaim_asks_anyway:
            self._journal(EX_OWNER, {'field_claim_observed': {
                'point': self.FIELD_POINT, 'claimant': claim}})
        if armed and self.epoch_rolls_again:
            self._journal(EX_OWNER, {'field_claim_lost': {
                'point': self.FIELD_POINT, 'claimant': claim}})
        if holder is not None and self.seats[holder]['live']:
            entry = self.seats[holder]
            if self.incumbent_fenced:
                self._fence(holder, claim)
            elif self.incumbent_displaced:
                entry['role'] = 'standby'
                entry['sync'] = 'unsynchronized'
                entry['owns'] = False
            else:
                entry['role'] = 'active'
                entry['sync'] = None
                entry['owns'] = True
                entry['claim'] = ('unclaimed'
                                  if self.incumbent_loses_claim
                                  else 'held')
                self.field['claim'] = self.TOKENS[holder]
                self.field['holders'] = {holder}
                self._journal(holder, {'role_changed': {
                    'from': 'promoting', 'to': 'active',
                    'origin': 'request', 'actor': None}})
        if self.ex_owner_promoted and ex is not None:
            ex['role'] = 'promoting'
        if self.field_unclaimed and ex is not None:
            ex['claim'] = 'unclaimed'
        if self.pair_member_promoted:
            self.members['standby']['role'] = 'active'

    def _claim_holder(self):
        claim = self.field['claim']
        for seat in sorted(self.seats):
            if self.TOKENS[seat] == claim:
                return seat
        return None

    def _release(self):
        """The claim stands holderless. A converged, loss-marked
        standby's bound reclaim preempts that shape by design — the
        released-preemption wedge the probe exists to close."""
        if self.control_silent:
            return
        for seat, entry in sorted(self.seats.items()):
            if not entry['live'] or entry['role'] != 'standby':
                continue
            if not entry['armed'] or not entry['converged']:
                continue
            if self.field['holders']:
                continue
            if self.control_operator_promote:
                self._reclaim_walk(seat, origin='request')
            elif not self.control_walk_unjournaled:
                self._reclaim_walk(seat)
            entry['role'] = 'active'
            entry['sync'] = None
            entry['owns'] = True
            entry['claim'] = 'held'
            self.field['claim'] = self.TOKENS[seat]
            self.field['holders'] = {seat}
            if self.control_exits:
                entry['live'] = False
                entry['exit'] = 1
            return

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        """One scan for every live member and seat. The request
        boundary is the tick boundary, so served ticks are
        call-count deterministic."""
        self.polls += 1
        for name, member in self.members.items():
            if name == 'active' and self.pair_owner_stalls:
                continue
            member['tick'] += 1
        for seat, entry in self.seats.items():
            if not entry['live']:
                continue
            if self.incumbent_stalls and self.raced and seat == INCUMBENT:
                continue
            entry['tick'] += 1
            if entry['stall'] > 0:
                entry['stall'] -= 1
                if entry['stall'] == 0:
                    entry['link'] = 'disconnected'

    def _seat_report(self, seat):
        entry = self.seats[seat]
        if self.monitor_starves and self.raced and seat == EX_OWNER:
            raise urllib.error.URLError('connection refused')
        if self.seat_exits and self.raced and seat == EX_OWNER:
            entry['live'] = False
            entry['exit'] = 1
            raise urllib.error.URLError('connection refused')
        sync = entry['sync']
        second = (self.second_pass_defect and self.thaws >= 2
                  and self.raced and seat == EX_OWNER)
        if second or (self.ex_owner_converges and self.raced
                      and seat == EX_OWNER):
            sync = {'tracking': {'aligned': 0}}
        report = {'tick': entry['tick'], 'role': entry['role']}
        if entry['role'] != 'active':
            report['sync'] = sync
        if entry['claim'] is not None:
            report['field_claim'] = entry['claim']
        elif self.field['claim'] is not None and entry['role'] == 'standby':
            report['field_claim'] = ('unclaimed'
                                     if (self.field_unclaimed
                                         and seat == EX_OWNER)
                                     else 'held')
        return report

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = self.HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        self._advance()
        if name in ('active', 'standby'):
            if (method, route) != ('GET', '/role'):
                raise AssertionError('unexpected request %s %s'
                                     % (method, url))
            report = dict(self.members[name])
            report['sync'] = ('unsynchronized' if name == 'active'
                              else {'tracking': {'aligned':
                                    self.members['active']['tick']}})
            if self.pair_unsettled:
                report['field_claim'] = None
            if self.no_active:
                report['role'] = 'standby'
            if self.restore_broken:
                self.members['active'].update({'role': 'standby',
                                              'field_claim': None})
            return 200, report
        entry = self.seats.get(name)
        if entry is None or not entry['live']:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._seat_report(name)
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': entry['tick'], 'points': [],
                         'io_health': {'driver': {'link': entry['link'],
                                                 'last_error': None}}}
        raise AssertionError('unexpected request %s %s' % (method, url))

    # --- each seat's runner-owned --journal-file --------------------

    def _boundary(self, seat):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'run_boundary': {'run': 1,
                                                     'tick': 0}}) + '\n')
        self.seq[seat] = 0

    def _journal(self, seat, event):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seq[seat] = self.seq.get(seat, 0) + 1
        with path.open('a') as stream:
            stream.write(json.dumps({'entry': {
                'seq': self.seq[seat],
                'tick': (self.seats.get(seat) or {}).get('tick', 0),
                'event': event}}) + '\n')


class ReclaimConvergenceTests(unittest.TestCase):
    """scenario_reclaim_convergence_gate against the stubbed rig: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the doctored record where the convergence gating is
    asserted while the unsynchronized reclaimer takes the field, the
    ownership-epoch rollback, the displaced or fenced incumbent, the
    silent positive control, the disturbed pair — plus the
    pre-contract shape that must report inconclusive and the
    instabilities that must report nondeterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ReclaimConvergenceFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'driven': 'http://ctrl-d:5',
                'plant_owner': dict(feed.TOKENS),
                'evidence_dir': str(self.evidence),
                'journal_files': {seat: str(feed.journals[seat])
                                  for seat in feed.SEATS},
                'start_born_field': feed.start_field,
                'pause_born_field': feed.pause_field,
                'unpause_born_field': feed.unpause_field,
                'stop_born_field': feed.stop_field,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RECLAIM_SERVE', 1.5), \
                patch.object(scenarios, 'RECLAIM_SETTLE', 1.5), \
                patch.object(scenarios, 'RECLAIM_POLL', 0.001), \
                patch.object(scenarios, 'RECLAIM_LINK', 0.001), \
                patch.object(scenarios, 'RECLAIM_FREEZE', 1.5), \
                patch.object(scenarios, 'RECLAIM_WINDOW', 1.5), \
                patch.object(scenarios, 'RECLAIM_ROUNDS', 2), \
                patch.object(scenarios, 'RECLAIM_RESTORE', 1.5):
            return scenarios.scenario_reclaim_convergence_gate(
                ctx or self._ctx(feed))

    def test_registered_after_sim_bus_before_revisions(self):
        self.assertIn(scenarios.scenario_reclaim_convergence_gate,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('reclaim-convergence-gate'),
            scenarios.scenario_reclaim_convergence_gate)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_reclaim_convergence_gate)
        self.assertLess(
            order.index(
                scenarios.scenario_sim_bus_startup_claim_refusal), mine)
        self.assertLess(order.index(
            scenarios.scenario_ownerless_remote_backoff), mine)
        self.assertLess(mine, order.index(
            scenarios.scenario_incompatible_revision))
        self.assertLess(mine, order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = sorted(entry['ref'] for entry in record['evidence'])
        self.assertEqual(
            refs,
            ['evidence/reclaim-convergence-gate-pass-1.json',
             'evidence/reclaim-convergence-gate-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        # Each pass serves the field four times — the ex-owner's field,
        # the re-serve that lets the second born-active claim first,
        # and the positive control's own pair of serves — freezes and
        # thaws it once, and sweeps the born seats six times: the two
        # the race staged, the control's incumbent, and the restore's
        # own sweep of all three.
        self.assertEqual(kinds.count('start_born_field'), 8)
        self.assertEqual(kinds.count('pause_born_field'), 2)
        self.assertEqual(kinds.count('unpause_born_field'), 2)
        self.assertEqual(kinds.count('stop_born_controller'), 12)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        self.assertFalse(self.feed.seats)
        self.assertFalse(self.feed.field['claim'])

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = ReclaimConvergenceFeed(second_tmp.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_two_passes_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        observed = [line for line in record['observations']
                    if 'identical digests' in line]
        self.assertEqual(len(observed), 1, record['observations'])
        digest = json.loads(observed[0].split('digests: ', 1)[1])
        self.assertEqual(digest['reclaim'], 'gated', digest)
        self.assertEqual(digest['control'], 'reclaimed', digest)
        self.assertEqual(digest['pair'], 'undisturbed', digest)
        self.assertEqual(digest['evidence'], 'none', digest)

    # The doctored negatives — each wrong disposition must fail the
    # run by the named diagnostic.

    def test_gating_asserted_while_reclaim_took_field_fails(self):
        # The issue's doctored negative: every served read reports the
        # ex-owner unsynchronized and the incumbent holding the field,
        # while its durable journal proves the reclaim took it back on
        # stale state. The judge names it — the leg must not read the
        # contradiction as a revision predating the contract.
        self.feed.gating_asserted_take = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        self.assertIn('stale state', record.get('detail', ''))
        report.validate_scenario(record)

    def test_reclaim_rearming_the_claim_fails(self):
        self.feed.reclaim_rearms = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_reclaim_asking_the_standing_claim_fails(self):
        # The ask issued at all against a standing claim with no
        # convergence evidence behind it — the gated probe never
        # issues there.
        self.feed.reclaim_asks_anyway = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field_claim_observed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ownership_epoch_rolling_again_fails(self):
        self.feed.epoch_rolls_again = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pre_contract_reclaim_is_inconclusive(self):
        # The staged revision predates the contract: the unsynchronized
        # reclaim issued and its own serving monitor showed it taking
        # the field — a build with no convergence gate at all.
        self.feed.reclaim_preempts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ex_owner_promoting_on_its_monitor_fails(self):
        self.feed.ex_owner_promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ex_owner_converging_in_the_window_fails(self):
        self.feed.ex_owner_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_unclaimed_under_the_ex_owner_fails(self):
        self.feed.field_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unclaimed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ex_owner_never_fenced_fails(self):
        self.feed.ex_owner_never_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fenced_ex_owner_tracking_fails(self):
        self.feed.fenced_ex_owner_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('convergence evidence', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fenced_ex_owner_sees_no_claim_fails(self):
        self.feed.fenced_sees_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('standing claim', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demotion_walk_unjournaled_fails(self):
        self.feed.no_demotion_walk = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demoting', record.get('detail', ''))
        report.validate_scenario(record)

    def test_loss_unjournaled_fails(self):
        self.feed.loss_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field_claim_lost', record.get('detail', ''))
        report.validate_scenario(record)

    def test_loss_journaled_twice_fails(self):
        self.feed.loss_twice = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field_claim_lost', record.get('detail', ''))
        report.validate_scenario(record)

    def test_loss_unattributed_fails(self):
        self.feed.loss_unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claimant', record.get('detail', ''))
        report.validate_scenario(record)

    def test_loss_wrong_claimant_fails(self):
        self.feed.loss_wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('424249', record.get('detail', ''))
        report.validate_scenario(record)

    def test_link_never_dropping_fails(self):
        # The freeze landed but no control connection reported down,
        # so the post-outage race was never staged.
        self.feed.link_stays_up = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('io_health', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_displaced_fails(self):
        self.feed.incumbent_displaced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('live line', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_losing_the_claim_fails(self):
        self.feed.incumbent_loses_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('live line', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_tick_stalled_fails(self):
        self.feed.incumbent_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_fenced_on_reattach_fails(self):
        self.feed.incumbent_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fenced and demoted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_seat_process_exiting_fails(self):
        self.feed.seat_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_reclaim_silent_fails(self):
        # The positive control: a converged fenced ex-owner's bound
        # reclaim of its own released claim must still take the field.
        self.feed.control_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('released-preemption wedge',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_walk_unjournaled_fails(self):
        self.feed.control_walk_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim', record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_operator_promote_fails(self):
        self.feed.control_operator_promote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('operator promote', record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_ex_owner_exiting_fails(self):
        self.feed.control_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_never_converging_is_nondeterministic(self):
        self.feed.control_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_never_claiming_is_nondeterministic(self):
        self.feed.control_never_claims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_owner_disturbed_fails(self):
        self.feed.pair_owner_demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('disturbed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_owner_stalled_fails(self):
        self.feed.pair_owner_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_member_promoted_fails(self):
        self.feed.pair_member_promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking member', record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_layout_unrestored_fails(self):
        self.feed.never_restored = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch configuration', record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_defect_fails(self):
        # A defect on either pass is the contract's failure, and the
        # pass's own judge names it before the digests compare.
        self.feed.second_pass_defect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        calls = []
        real = scenarios._reclaim_digest

        def diverging(record, violations):
            calls.append(1)
            digest = dict(real(record, violations))
            if len(calls) > 1:
                digest['reclaim'] = 'ungated'
            return digest

        with patch.object(scenarios, '_reclaim_digest', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_serve_failure_is_nondeterministic(self):
        self.feed.serve_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_freeze_failure_is_nondeterministic(self):
        self.feed.pause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_thaw_failure_is_nondeterministic(self):
        self.feed.thaw_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ex_owner_never_activating_is_nondeterministic(self):
        self.feed.owner_never_claims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_never_claiming_is_nondeterministic(self):
        self.feed.preemptor_never_claims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_window_is_nondeterministic(self):
        self.feed.monitor_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dropped_durable_read_is_nondeterministic(self):
        self.feed.journal_dropped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_vanished_container_is_nondeterministic(self):
        # A seat's container vanished between reads: the process
        # verdict the audit needs cannot exist.
        self.feed.container_vanished = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('vanished', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('field-owning member', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_born_field', 'pause_born_field',
                    'unpause_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('staging levers', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journals_are_inconclusive(self):
        ctx = self._ctx()
        ctx['journal_files'] = {}
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unchecked_self_check_fails(self):
        # A judge that names nothing slips every planted negative —
        # the leg reports itself unchecked rather than passing.
        with patch.object(scenarios, '_judge_reclaim',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reclaim-convergence-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_judge_self_check_is_complete(self):
        # Every planted negative the leg can stage names the
        # diagnostic it must — the self-check slips nothing.
        self.assertEqual(scenarios._reclaim_self_check(), [])