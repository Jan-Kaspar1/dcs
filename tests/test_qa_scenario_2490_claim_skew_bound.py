"""The 2490_claim_skew_bound leg's scenario unit coverage — the feed
fake and TestCase class for scenario_claim_skew_bound, split out per
the leg-module convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape: the deployed pair owns a sim-tcp
field and never moves, while the born legs' scratch field serves on
the rig bridge and the three born seats attach to it through the born
launcher's `--remote` seam — the holder declaring no pair, the in-bound
claimant declaring `--standby <holder>` at the documented cadence, and
the skewed claimant declaring `--standby <the promoted peer>` at the
per-container `scan_ms` cadence the leg drives. A seat's run tick
accrues one per scan, so the fake accrues `100 / scan_ms` ticks per
served read and the skewed seat's basis really does run past the
recorded bound on the rig's own clock. Each promotion really takes the
claim: the prior holder demotes in place through the fenced-origin
walk with one attributed `field_claim_lost`. By default the skewed
claim is refused by name — the fixed revision the leg judges — and
every fault flag stages one named defect, one nondeterministic
surface, or one rig shape the leg declines on."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ClaimSkewBoundTests.test_registered',
    'ClaimSkewBoundTests.test_clean_passes_validates_and_tears_down',
    'ClaimSkewBoundTests.test_two_passes_produce_identical_digests',
    'ClaimSkewBoundTests.test_skewed_claim_preempts_the_field_fails',
    'ClaimSkewBoundTests.test_unnamed_skew_refusal_fails',
    'ClaimSkewBoundTests.test_in_bound_claim_refused_fails',
    'ClaimSkewBoundTests.test_in_bound_claim_never_lands_fails',
    'ClaimSkewBoundTests.test_in_bound_refusal_journaled_fails',
    'ClaimSkewBoundTests.test_holder_demotion_unwalked_fails',
    'ClaimSkewBoundTests.test_holder_demotion_unattributed_fails',
    'ClaimSkewBoundTests.test_holder_lost_the_field_fails',
    'ClaimSkewBoundTests.test_holder_journaled_a_claim_loss_fails',
    'ClaimSkewBoundTests.test_holder_left_active_fails',
    'ClaimSkewBoundTests.test_holder_scan_wedged_fails',
    'ClaimSkewBoundTests.test_diverging_digests_fail',
    'ClaimSkewBoundTests.test_field_stage_failure_is_nondeterministic',
    'ClaimSkewBoundTests.test_launch_failures_are_nondeterministic',
    'ClaimSkewBoundTests.test_holder_never_claims_is_nondeterministic',
    'ClaimSkewBoundTests.test_claimants_never_converge_is_nondeterministic',
    'ClaimSkewBoundTests.test_skew_never_separates_is_nondeterministic',
    'ClaimSkewBoundTests.test_in_bound_basis_outside_is_nondeterministic',
    'ClaimSkewBoundTests.test_starved_watch_is_nondeterministic',
    'ClaimSkewBoundTests.test_unanswered_attempt_is_nondeterministic',
    'ClaimSkewBoundTests.test_gate_answered_attempt_is_nondeterministic',
    'ClaimSkewBoundTests.test_journal_vanished_is_nondeterministic',
    'ClaimSkewBoundTests.test_pair_disturbance_is_nondeterministic',
    'ClaimSkewBoundTests.test_rig_left_standing_is_nondeterministic',
    'ClaimSkewBoundTests.test_field_left_serving_is_nondeterministic',
    'ClaimSkewBoundTests.test_field_presence_unreadable_is_nondeterministic',
    'ClaimSkewBoundTests.test_unreachable_rig_is_inconclusive',
    'ClaimSkewBoundTests.test_unsettled_pair_is_inconclusive',
    'ClaimSkewBoundTests.test_missing_seams_are_inconclusive',
    'ClaimSkewBoundTests.test_unpinned_tokens_are_inconclusive',
    'ClaimSkewBoundTests.test_unchecked_self_check_fails',
    'ClaimSkewBoundTests.test_self_check_is_complete',
})


class SkewFeed:
    """A stubbed rig for the skewed-claim preemption-bound leg.

    The deployed pair owns a sim-tcp field and never moves; the born
    legs' scratch field serves the three born seats, each attaching
    through the born launcher's `--remote` seam with its own
    `--owner-token` pin and its own runner-owned journal file. Every
    transition keys off the leg's lever calls so two passes emit
    identical digests; the fault flags stage each named defect, each
    nondeterministic surface, and each rig shape the leg declines on."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-f:4': 'foreign',
             'ctrl-d:5': 'driven'}
    FIELD = 'dcs-hw-qa-1-born-plant:9003'
    FIELD_PORT = 9003
    TOKENS = {'revised': 424243, 'foreign': 424244, 'driven': 424245}
    SEATS = ('revised', 'foreign', 'driven')
    BOUNDED = ("the claim's basis is past the recorded skew bound "
               "(64 run ticks) and an untranslated comparison may not "
               "resolve the field's write-ownership claim")

    def __init__(self, journals):
        self.tick = 900            # the deployed pair's scan tick
        self.field = None          # the scratch field, while it serves
        self.claim = None          # the seat holding the field's claim
        self.seats = {}            # seat -> run state
        self.calls = []
        self.reads = {'ctrl-a:1': 0, 'ctrl-b:2': 0}
        self.journals = journals   # {seat: path} the runner owns
        self.written = {seat: [] for seat in self.SEATS}
        self.posted = False        # the skewed attempt has gone out
        self.sweeps = 0            # completed teardown sweeps
        # Doctors staging each named defect.
        self.skew_preempts = False        # the skewed claim takes the
                                          # field — the defect
        self.skew_unnamed = False         # refused with no named cause
        self.in_bound_refused = False     # the bound wedged the
                                          # switchover
        self.in_bound_unclaimed = False   # answered, never settled
        self.in_bound_refusal_journaled = False  # a journaled refusal
        self.demotion_unwalked = False    # no fenced-origin walk
        self.demotion_unattributed = False  # a foreign claim token
        self.holder_lost = False          # the claim flipped the holder
        self.holder_demoted = False       # a loss journaled on the
                                          # holder without the flip
        self.holder_left_active = False   # the holder left active
        self.holder_wedged = False        # the holder's scan stops
        # Doctors staging each nondeterministic surface.
        self.stage_fails = False          # start_born_field raises
        self.holder_launch_fails = False  # the holder's launch raises
        self.control_launch_fails = False
        self.skewed_launch_fails = False
        self.holder_never_claims = False  # no startup grant lands
        self.control_never_converges = False
        self.skewed_never_converges = False
        self.skew_never_separates = False  # both pace alike
        self.control_basis_outside = False  # the holder aged past it
        self.control_watch_starves = False   # the monitor goes quiet
        self.skewed_watch_starves = False
        self.promote_unanswered = False     # the post goes nowhere
        self.skew_gated = False             # the gate answers first
        self.journal_vanishes = False       # the durable half unreadable
        self.pair_moves = False             # the standby reports active
        self.pair_wedged = False            # the owner's tick freezes
        self.pair_moves_after_sweep = False  # the pair moves once the
        # leg's own claim is gone — the launch roles it must restore
        self.unsettled_pair = False         # the standby never tracks
        self.silent_rig = False             # every endpoint refuses
        self.teardown_keeps = frozenset()  # seats that outlive the
        # sweep — the rig's claim state the legs behind inherit
        self.field_left_serving = False     # the field survives the sweep

    # --- the durable journal the audit reads ------------------------

    def _journal(self, seat, event):
        records = self.written[seat]
        records.append({'entry': {'seq': len(records), 'tick': 10,
                                  'event': event}})

    def _write_journals(self):
        if self.journal_vanishes:
            # The seat's durable sink never lands: the runner-owned
            # journal file is absent for the whole pass, so the
            # demotion's attribution cannot be audited at all.
            for seat in self.SEATS:
                Path(self.journals[seat]).unlink(missing_ok=True)
            return
        for seat, records in self.written.items():
            path = self.journals.get(seat)
            if not path:
                continue
            Path(path).write_text(
                ''.join(json.dumps(record) + '\n'
                        for record in records))

    # --- the runner's born-field and born-seat levers, faked --------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self.field = 'dcs-hw-qa-1-born-plant'
        self.claim = None       # a fresh field's arbitration
        return {'container': self.field, 'remote': self.FIELD,
                'mode': mode}

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        if not self.field_left_serving:
            self.field = None
        self.claim = None
        self.sweeps += 1

    def field_ctl(self, *args):
        self.calls.append(('born_field_ctl', args))
        serving = self.field is not None
        return subprocess.CompletedProcess(
            args=('dcs-plant-ctl',),
            returncode=0 if serving else 1,
            stdout=json.dumps({'result': 'points', 'points': []})
            if serving else '',
            stderr='' if serving else 'no such container')

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None, scan_ms=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document, scan_ms))
        if remote != self.FIELD or document is not None:
            raise AssertionError('a born launch on the scratch field '
                                 'is --remote-addressed: remote=%r '
                                 'document=%r' % (remote, document))
        if (self.holder_launch_fails and seat == 'revised') \
                or (self.control_launch_fails and seat == 'driven') \
                or (self.skewed_launch_fails and seat == 'foreign'):
            raise RuntimeError('docker run failed: name in use')
        self.written[seat] = []
        pace = scan_ms if scan_ms is not None else runner.BORN_SCAN_MS
        state = {'launched': True, 'seat': seat, 'standby': standby,
                 'scan_ms': pace,
                 # One run tick per scan: the fake's served reads
                 # stand in for scans, so a seat paced four times
                 # faster accrues four ticks per read.
                 'step': max(1, runner.BORN_SCAN_MS // pace),
                 'owns': False, 'tracking': False, 'role': 'standby',
                 'tick': 0, 'reads': 0, 'serving': True}
        self.seats[seat] = state
        if standby is None and not self.holder_never_claims:
            # A born-active declaring no pair takes an unclaimed
            # field's write-ownership claim through its conditional
            # startup grant.
            self._seat(seat, 'write')
        else:
            state['tracking'] = True
        self._write_journals()
        return {'container': 'dcs-hw-' + seat, 'seat': seat,
                'address': 'dcs-hw-' + seat + ':'
                + str(runner.BORN_MONITOR_PORT),
                'remote': remote, 'peer': None, 'standby': standby,
                'model': '/run/qa-1/model.json', 'scan_ms': pace,
                'monitor': 'http://' + self._host(seat)}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if seat in self.teardown_keeps:
            return          # a seat the sweep could not remove
        if self.claim == seat:
            self.claim = None
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        state = self.seats.get(seat)
        if state is None:
            return {'container': 'dcs-hw-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        return {'container': 'dcs-hw-' + seat, 'running': True,
                'exit': None, 'logs': '', 'absent': False}

    def _host(self, seat):
        for host, name in self.HOSTS.items():
            if name == seat:
                return host
        return 'ctrl-' + seat + ':9'

    # --- the field's own arbitration --------------------------------

    def _seat(self, seat, action):
        """One claim action on a seat: `write` takes the claim,
        `refuse` leaves it standing."""
        state = self.seats[seat]
        if action == 'write':
            previous = self.claim
            self.claim = seat
            state['owns'] = True
            state['tracking'] = False
            if previous is not None and previous != seat:
                self._demote(previous, seat)
        else:
            self._journal(seat, {'promotion_refused': {
                'error': {'field_claim_failed': {'detail':
                                                 self.BOUNDED}},
                'misses': 0}})

    def _demote(self, seat, promoted):
        """The documented in-place demotion of a preempted field
        owner: the fenced-origin walk beside one attributed claim loss."""
        state = self.seats[seat]
        state['owns'] = False
        state['role'] = 'standby'
        state['tracking'] = False
        if not self.demotion_unwalked:
            self._journal(seat, {'role_changed': {
                'from': 'active', 'to': 'demoting',
                'origin': 'fenced'}})
            self._journal(seat, {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'fenced'}})
        self._journal(seat, {'field_claim_lost': {
            'point': 20,
            'claimant': 424244 if self.demotion_unattributed
            else self.TOKENS[promoted]}})
        self._write_journals()

    def _promote(self, seat):
        """One `POST /promote` and the field's answer: a 200
        `promoting` report when the claim lands, or the 4xx body whose
        single key names the refusal."""
        state = self.seats[seat]
        if self.promote_unanswered and seat == 'foreign':
            raise urllib.error.URLError('connection reset')
        if not state['tracking']:
            return 409, {'not_converged': {'sync': 'unsynchronized'}}
        if seat == 'foreign':
            self.posted = True
            if self.skew_preempts:
                # The defect: the untranslated comparison resolves the
                # field and the live incumbent loses it.
                self._seat(seat, 'write')
            elif self.holder_lost:
                # The claim moved under a refusal: the holder's field
                # is gone while its own surface still reads held.
                self._seat(seat, 'write')
                self.seats[seat]['owns'] = False
                self.seats[self.holder()]['owns'] = False
            elif self.holder_demoted:
                # A claim loss journaled on the holder with nothing
                # else moving — the durable half of the disturbance.
                self._journal(self.holder(), {'field_claim_lost': {
                    'point': 20, 'claimant': self.TOKENS['foreign']}})
                self._write_journals()
            elif self.skew_gated:
                return 409, {'not_converged': {'sync': 'tracking'}}
            elif self.skew_unnamed:
                return 409, {}
            else:
                return 409, {'field_claim_failed': {'detail':
                                                    self.BOUNDED}}
        if self.in_bound_refused:
            return 409, {'field_claim_failed': {'detail': self.BOUNDED}}
        self._seat(seat, 'write')
        if self.in_bound_unclaimed:
            # Answered, and the gate never lifted: the documented
            # switchover did not happen.
            self.seats[seat]['owns'] = False
            self.claim = None
        elif self.in_bound_refusal_journaled:
            self._journal(seat, {'promotion_refused': {
                'error': {'field_claim_failed': {'detail':
                                                 self.BOUNDED}},
                'misses': 0}})
            self._write_journals()
        return 200, {'role': 'promoting', 'tick': state['tick']}

    def holder(self):
        """The seat the skewed arm's claim is computed against: the
        in-bound arm's promoted peer where it landed, the first holder
        otherwise."""
        return ('driven' if self.seats['driven'].get('owns')
                else 'revised')

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _pair_role(self, host):
        self.reads[host] += 1
        reads = self.reads[host]
        if host == 'ctrl-a:1':
            if not (self.pair_wedged and reads > 1):
                self.tick += 1
            return {'role': 'active', 'tick': self.tick}
        if (self.pair_moves and reads > 1) \
                or (self.pair_moves_after_sweep and self.sweeps):
            return {'role': 'active', 'tick': self.tick}
        report = {'role': 'standby', 'tick': self.tick}
        report['sync'] = {'degraded': {'reason': 'unsettled'}} \
            if self.unsettled_pair \
            else {'tracking': {'aligned': self.tick}}
        return report

    def _seat_role(self, seat, state):
        state['reads'] += 1
        holder = (self.posted and seat != 'foreign')
        if not (self.holder_wedged and holder and state['reads'] > 2):
            state['tick'] += state['step']
        lost_role = self.holder_left_active and holder \
            and state['reads'] > 1
        if state['owns'] and not lost_role:
            return {'role': 'active', 'field_claim': 'held',
                    'tick': state['tick']}
        report = {'role': 'standby', 'tick': state['tick'],
                  'field_claim': 'held' if self.claim else None}
        converged = state['tracking'] and not (
            (self.control_never_converges and seat == 'driven')
            or (self.skewed_never_converges and seat == 'foreign'))
        report['sync'] = {'tracking': {'aligned': self.tick}} \
            if converged else 'unsynchronized'
        return report

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        if host in ('ctrl-a:1', 'ctrl-b:2'):
            if (method, route) == ('GET', '/role'):
                return 200, self._pair_role(host)
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        seat = self.HOSTS.get(host)
        state = self.seats.get(seat) if seat else None
        if state is None or not state['serving']:
            raise urllib.error.URLError('connection refused')
        if (self.control_watch_starves and seat == 'driven') \
                or (self.skewed_watch_starves and seat == 'foreign'):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._seat_role(seat, state)
        if (method, route) == ('POST', '/promote'):
            status, answer = self._promote(seat)
            if status == 200:
                return status, answer
            raise urllib.error.HTTPError(
                url, status, 'conflict', {},
                io.BytesIO(json.dumps(answer).encode()))
        raise AssertionError('unexpected request %s %s' % (method, url))


class ClaimSkewBoundTests(unittest.TestCase):
    """scenario_claim_skew_bound against the stubbed rig: an in-bound
    claim takes the field through the documented switchover, a claim
    whose basis the rig's own cadence lever drives past the recorded
    bound takes nothing and is refused by name, the promoted holder
    keeps writing with no fenced demotion journaled, and the deployed
    pair never moves — two passes, identical digests. Each fault flag
    stages a named failure, a nondeterministic surface, or a rig shape
    the leg declines on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.journals = {seat: str(self.root / (seat + '.jsonl'))
                         for seat in SkewFeed.SEATS}
        self.feed = SkewFeed(self.journals)

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'revised': 'http://ctrl-c:3',
               'foreign': 'http://ctrl-f:4',
               'driven': 'http://ctrl-d:5',
               'evidence_dir': str(self.evidence),
               'journal_files': dict(self.journals),
               'plant_owner': {'active': 424240, 'standby': 424241,
                               'revised': 424243, 'foreign': 424244,
                               'driven': 424245},
               'start_born_field': feed.start_field,
               'stop_born_field': feed.stop_field,
               'born_field_ctl': feed.field_ctl,
               'start_born_controller': feed.start_controller,
               'stop_born_controller': feed.stop_controller,
               'born_controller_state': feed.state}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'SKEW_POLL', 0.001), \
                patch.object(scenarios, 'SKEW_SETTLE', 1.5):
            return scenarios.scenario_claim_skew_bound(ctx
                                                       or self._ctx(feed))

    def _pass(self, number):
        return json.loads((self.evidence
                           / ('claim-skew-bound-pass-' + str(number)
                              + '.json')).read_text())

    def _assert_failed(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-failed',
                      record.get('detail', ''))
        self.assertIn(key, str(self._pass(1)['violations']), record)
        report.validate_scenario(record)

    def _assert_nondeterministic(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-nondeterministic',
                      record.get('detail', ''))
        self.assertIn(key, str(self._pass(1)['violations']), record)
        report.validate_scenario(record)

    def test_registered(self):
        self.assertIn(scenarios.scenario_claim_skew_bound,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('claim-skew-bound'),
                      scenarios.scenario_claim_skew_bound)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_sim_bus_startup_claim_refusal),
            order.index(scenarios.scenario_claim_skew_bound))
        self.assertLess(
            order.index(scenarios.scenario_claim_skew_bound),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/claim-skew-bound-pass-1.json',
             'evidence/claim-skew-bound-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        first = self._pass(1)
        self.assertEqual(first['field'],
                         {'remote': SkewFeed.FIELD, 'mode': 'serving'})
        self.assertEqual(first['holder'], 'driven')
        # The in-bound arm: the holder's conditional startup grant
        # landed, and the in-bound claimant's own basis read inside the
        # recorded bound before it claimed.
        self.assertEqual(first['incumbent']['granted']['role'], 'active')
        self.assertEqual(first['incumbent']['granted']['field_claim'],
                         'held')
        self.assertEqual(first['control']['scan_ms'],
                         runner.BORN_SCAN_MS)
        self.assertLessEqual(abs(first['control']['basis']['separation']),
                             scenarios.SKEW_BOUND)
        self.assertEqual(first['control']['promote']['status'], 200)
        self.assertEqual(first['control']['claimed']['field_claim'],
                         'held')
        self.assertEqual(first['control']['causes'], [])
        # The documented switchover it produced on the first holder.
        self.assertEqual(first['incumbent']['walk'],
                         [{'from': 'active', 'to': 'demoting',
                           'origin': 'fenced'},
                          {'from': 'demoting', 'to': 'standby',
                           'origin': 'fenced'}])
        self.assertEqual(first['incumbent']['claims'],
                         [{'point': 20, 'claimant': 424245}])
        # The skewed arm: the per-container cadence lever drove this
        # seat's basis past the recorded bound, and the claim it
        # computed on that basis was refused by name.
        self.assertEqual(first['skewed']['scan_ms'],
                         scenarios.SKEWED_SCAN_MS)
        self.assertGreater(first['skewed']['basis']['separation'],
                           scenarios.SKEW_BOUND)
        self.assertEqual(first['skewed']['promote']['status'], 409)
        self.assertEqual(first['skewed']['promote']['cause'],
                         'field_claim_failed')
        self.assertEqual(first['skewed']['disposition'], 'refused')
        self.assertFalse(first['skewed']['holder_fenced'])
        self.assertEqual(
            first['digest'],
            {'holder': 'holds-and-scans',
             'in-bound': 'claims-normally',
             'skewed': 'refused-and-named',
             'staging': 'past-the-bound',
             'pair': 'held',
             'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The sweep is audited back over the rig: every born seat
        # proven gone, the scratch field's own tool refusing, and the
        # deployed pair's launch roles undisturbed in the framing the
        # leg reads once its own claim is gone.
        self.assertEqual(first['rig'],
                         {'seats': {'revised': True, 'driven': True,
                                    'foreign': True},
                          'field_error': None,
                          'field_serving': False})
        self.assertEqual(first['roles']['final']['active']['role'],
                         'active')
        self.assertIs(first['roles']['final']['standby']['tracking'],
                      True)
        self.assertEqual(first['violations'], {})
        # Every pass ends torn down — the three seats and the scratch
        # field removed, so the rig's claim state is free and the field
        # the next pass finds is a fresh one.
        self.assertFalse(self.feed.seats)
        self.assertIsNone(self.feed.field)
        self.assertIsNone(self.feed.claim)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_born_field'), 2)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        self.assertEqual(kinds.count('start_born_controller'), 6)
        for seat in SkewFeed.SEATS:
            self.assertIn(('stop_born_controller', seat),
                          self.feed.calls)
        # The skew lever rode the one skewed launch and no other.
        paced = [call for call in self.feed.calls
                 if call[0] == 'start_born_controller'
                 and call[6] is not None]
        self.assertEqual([(call[1], call[6]) for call in paced],
                         [('foreign', scenarios.SKEWED_SCAN_MS)] * 2)
        # The skewed claimant declared the promoted peer its tracking
        # source, and the in-bound claimant the first holder.
        wiring = {call[1]: call[4] for call in self.feed.calls
                  if call[0] == 'start_born_controller'}
        self.assertEqual(wiring['driven'], 'revised')
        self.assertEqual(wiring['foreign'], 'driven')

    def test_two_passes_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])
        self.assertIn('identical digests', ' '.join(record['observations']))

    def test_skewed_claim_preempts_the_field_fails(self):
        # The issue's doctored negative, staged on the rig: the skewed
        # claim resolves the field and boots the live incumbent.
        self.feed.skew_preempts = True
        record = self.run_scenario()
        self._assert_failed('skewed-preempted', record)
        first = self._pass(1)
        self.assertEqual(first['skewed']['disposition'], 'preempted')
        self.assertEqual(first['digest']['skewed'], 'preempted')
        self.assertEqual(first['digest']['holder'], 'defect')

    def test_unnamed_skew_refusal_fails(self):
        self.feed.skew_unnamed = True
        record = self.run_scenario()
        self._assert_failed('skewed-unnamed', record)
        self.assertEqual(self._pass(1)['digest']['skewed'], 'unnamed')

    def test_in_bound_claim_refused_fails(self):
        self.feed.in_bound_refused = True
        record = self.run_scenario()
        self._assert_failed('control-refused', record)
        self.assertEqual(self._pass(1)['digest']['in-bound'], 'defect')

    def test_in_bound_claim_never_lands_fails(self):
        self.feed.in_bound_unclaimed = True
        record = self.run_scenario()
        self._assert_failed('control-refused', record)
        self.assertIsNone(self._pass(1)['control']['claimed'])

    def test_in_bound_refusal_journaled_fails(self):
        self.feed.in_bound_refusal_journaled = True
        record = self.run_scenario()
        self._assert_failed('control-refused-journal', record)
        self.assertEqual(self._pass(1)['control']['causes'],
                         ['field_claim_failed'])

    def test_holder_demotion_unwalked_fails(self):
        self.feed.demotion_unwalked = True
        record = self.run_scenario()
        self._assert_failed('demotion-unwalked', record)
        self.assertEqual(self._pass(1)['incumbent']['walk'], [])

    def test_holder_demotion_unattributed_fails(self):
        self.feed.demotion_unattributed = True
        record = self.run_scenario()
        self._assert_failed('demotion-unattributed', record)
        self.assertEqual(self._pass(1)['incumbent']['claims'],
                         [{'point': 20, 'claimant': 424244}])

    def test_holder_lost_the_field_fails(self):
        self.feed.holder_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-failed',
                      record.get('detail', ''))
        self.assertIn('holder-disturbed',
                      str(self._pass(1)['violations']), record)

    def test_holder_journaled_a_claim_loss_fails(self):
        # The claim moved with nothing else moving: the durable half
        # of the holder's disturbance, which the served surface alone
        # would not show.
        self.feed.holder_demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-failed',
                      record.get('detail', ''))
        self.assertIn('holder-disturbed',
                      str(self._pass(1)['violations']), record)
        self.assertIs(self._pass(1)['skewed']['holder_fenced'], True)

    def test_holder_left_active_fails(self):
        self.feed.holder_left_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-failed',
                      record.get('detail', ''))
        self.assertIn('holder-disturbed',
                      str(self._pass(1)['violations']), record)

    def test_holder_scan_wedged_fails(self):
        self.feed.holder_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-failed',
                      record.get('detail', ''))
        self.assertIn('holder-disturbed',
                      str(self._pass(1)['violations']), record)

    def test_diverging_digests_fail(self):
        # Both passes audit clean, yet their normalized digests differ —
        # the determinism contract's own failure, staged at the digest
        # seam the leg compares.
        digests = iter([
            {'holder': 'holds-and-scans',
             'in-bound': 'claims-normally',
             'skewed': 'refused-and-named',
             'staging': 'past-the-bound',
             'pair': 'held',
             'rig': 'restored'},
            {'holder': 'holds-and-scans',
             'in-bound': 'claims-normally',
             'skewed': 'unnamed',
             'staging': 'past-the-bound',
             'pair': 'held',
             'rig': 'restored'}])
        with patch.object(scenarios, '_skew_digest',
                          lambda record, violations: next(digests)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self._assert_nondeterministic('stage', record)

    def test_launch_failures_are_nondeterministic(self):
        for flag, key in (('holder_launch_fails', 'incumbent-stage'),
                          ('control_launch_fails', 'control-stage'),
                          ('skewed_launch_fails', 'skewed-stage')):
            with self.subTest(flag=flag):
                feed = SkewFeed(self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('claim-skew-bound-nondeterministic',
                              record.get('detail', ''))

    def test_holder_never_claims_is_nondeterministic(self):
        self.feed.holder_never_claims = True
        record = self.run_scenario()
        self._assert_nondeterministic('incumbent-claim', record)

    def test_claimants_never_converge_is_nondeterministic(self):
        for flag, key in (('control_never_converges', 'control-converge'),
                          ('skewed_never_converges', 'skewed-converge')):
            with self.subTest(flag=flag):
                feed = SkewFeed(self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('claim-skew-bound-nondeterministic',
                              record.get('detail', ''))
                self.assertIn(key, str(
                    json.loads((self.evidence
                                / 'claim-skew-bound-pass-1.json')
                               .read_text())['violations']), record)

    def test_skew_never_separates_is_nondeterministic(self):
        # The skew lever off: both seats pace alike, so the claimant's
        # basis never separates past the recorded bound and the attempt
        # says nothing about a claim computed past it.
        feed = SkewFeed(self.journals)
        original = feed.start_controller

        def start_controller(seat, remote, peer=None, standby=None,
                             document=None, scan_ms=None):
            return original(seat, remote, peer=peer, standby=standby,
                            document=document,
                            scan_ms=None if scan_ms else scan_ms)

        feed.start_controller = start_controller
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('skewed-basis', record)
        self.assertEqual(self._pass(1)['digest']['staging'], 'unstaged')

    def test_in_bound_basis_outside_is_nondeterministic(self):
        # The holder aged past the recorded bound before the in-bound
        # claimant converged: its claim proves nothing about a basis
        # inside one.
        self.feed.control_basis_outside = True
        original = self.feed.start_controller

        def start_controller(seat, remote, peer=None, standby=None,
                             document=None, scan_ms=None):
            launched = original(seat, remote, peer=peer,
                                standby=standby, document=document,
                                scan_ms=scan_ms)
            if self.feed.control_basis_outside and seat == 'driven':
                self.feed.seats[seat]['tick'] = 400
            return launched

        self.feed.start_controller = start_controller
        record = self.run_scenario()
        self._assert_nondeterministic('control-basis', record)

    def test_starved_watch_is_nondeterministic(self):
        for flag, key in (('control_watch_starves', 'control-watch'),
                          ('skewed_watch_starves', 'skewed-watch')):
            with self.subTest(flag=flag):
                feed = SkewFeed(self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('claim-skew-bound-nondeterministic',
                              record.get('detail', ''))

    def test_unanswered_attempt_is_nondeterministic(self):
        self.feed.promote_unanswered = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-bound-nondeterministic',
                      record.get('detail', ''))

    def test_gate_answered_attempt_is_nondeterministic(self):
        self.feed.skew_gated = True
        record = self.run_scenario()
        self._assert_nondeterministic('skewed-gated', record)
        self.assertEqual(self._pass(1)['skewed']['disposition'], 'gated')

    def test_journal_vanished_is_nondeterministic(self):
        self.feed.journal_vanishes = True
        record = self.run_scenario()
        self._assert_nondeterministic('incumbent-journal', record)
        # The durable evidence is unread rather than violated: the
        # holder's own demotion is the clause that cannot be audited.
        self.assertEqual(self._pass(1)['incumbent']['walk'], [])
        self.assertIs(self._pass(1)['incumbent']['read'], False)

    def test_pair_disturbance_is_nondeterministic(self):
        for flag in ('pair_moves', 'pair_wedged', 'pair_moves_after_sweep'):
            with self.subTest(flag=flag):
                feed = SkewFeed(self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('claim-skew-bound-nondeterministic',
                              record.get('detail', ''))

    def test_rig_left_standing_is_nondeterministic(self):
        self.feed.teardown_keeps = frozenset({'driven'})
        record = self.run_scenario()
        self._assert_nondeterministic('rig-not-restored', record)
        self.assertFalse(self._pass(1)['rig']['seats']['driven'])

    def test_field_left_serving_is_nondeterministic(self):
        self.feed.field_left_serving = True
        record = self.run_scenario()
        self._assert_nondeterministic('rig-not-restored', record)
        self.assertIs(self._pass(1)['rig']['field_serving'], True)

    def test_field_presence_unreadable_is_nondeterministic(self):
        # No shipped-tool seam at all: the scratch field's own removal
        # cannot be audited, which is the rig's staging surface rather
        # than a leg that left something standing.
        ctx = self._ctx()
        ctx['born_field_ctl'] = None
        record = self.run_scenario(ctx=ctx)
        self._assert_nondeterministic('field-unread', record)
        self.assertIsNone(self._pass(1)['rig']['field_serving'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled_pair = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no tracking standby', record.get('detail', ''))

    def test_missing_seams_are_inconclusive(self):
        for key in ('start_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            with self.subTest(key=key):
                ctx = self._ctx()
                ctx[key] = None
                record = self.run_scenario(ctx=ctx)
                self.assertEqual(record['outcome'], 'inconclusive',
                                 record)
                self.assertIn(key, record.get('detail', ''))
        # A seat with no published monitor, and a seat with no
        # durable journal to audit, are equally unstageable.
        ctx = self._ctx()
        ctx['driven'] = None
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('published monitor', record.get('detail', ''))
        ctx = self._ctx()
        ctx['journal_files'] = dict(self.journals)
        ctx['journal_files'].pop('foreign')
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record.get('detail', ''))

    def test_unpinned_tokens_are_inconclusive(self):
        ctx = self._ctx()
        ctx['plant_owner'] = dict(ctx['plant_owner'])
        ctx['plant_owner'].pop('driven')
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('owner-token', record.get('detail', ''))

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_skew_self_check',
                          lambda: ['refusal-unnamed']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-skew-unchecked', record.get('detail', ''))
        self.assertIn('refusal-unnamed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        # The self-check plants a negative for every clause class the
        # judge's own naming reaches, and the audit catches each one —
        # so the two lists cannot drift apart, and a judge that let any
        # of them slip fails the leg's self-check.
        clause_classes = {
            'refusal-asserted-but-preempted': 'skewed-preempted',
            'refusal-unnamed': 'skewed-unnamed',
            'in-bound-claim-refused': 'control-refused',
            'holder-lost-its-claim': 'holder-disturbed',
            'holder-demotion-unwalked': 'demotion-unwalked',
            'skewed-basis-inside-the-bound': 'skewed-basis',
            'skewed-answered-by-the-promote-gate': 'skewed-gated'}
        source = (Path(scenarios.__file__).parent
                  / '2490_claim_skew_bound.py').read_text()
        for negative, clause in clause_classes.items():
            with self.subTest(negative=negative):
                self.assertIn("expect('" + negative + "'", source)
                self.assertIn("'" + clause + "'", source)
        self.assertEqual(scenarios._skew_self_check(), [])