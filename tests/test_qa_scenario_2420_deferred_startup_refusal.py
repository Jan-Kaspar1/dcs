"""The 2420_deferred_startup_refusal leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_deferred_startup_refusal, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DeferredRefusalTests.test_registered_after_born_active_before_revisions',
    'DeferredRefusalTests.test_fixed_shape_passes_validates_and_tears_down',
    'DeferredRefusalTests.test_two_runs_produce_identical_evidence',
    'DeferredRefusalTests.test_refused_seat_stranded_fails',
    'DeferredRefusalTests.test_refused_seat_never_settling_fails',
    'DeferredRefusalTests.test_refused_zero_exit_fails',
    'DeferredRefusalTests.test_unnamed_refusal_exit_fails',
    'DeferredRefusalTests.test_unnamed_remedy_exit_fails',
    'DeferredRefusalTests.test_unjournaled_refusal_fails',
    'DeferredRefusalTests.test_wrong_claimant_fails',
    'DeferredRefusalTests.test_declared_seat_exit_fails',
    'DeferredRefusalTests.test_declared_seat_never_converging_fails',
    'DeferredRefusalTests.test_declared_claim_unobserved_fails',
    'DeferredRefusalTests.test_declared_unjournaled_fails',
    'DeferredRefusalTests.test_standby_seat_exit_fails',
    'DeferredRefusalTests.test_standby_never_converging_fails',
    'DeferredRefusalTests.test_standby_journaling_refusal_fails',
    'DeferredRefusalTests.test_incumbent_demotion_fails',
    'DeferredRefusalTests.test_incumbent_stall_fails',
    'DeferredRefusalTests.test_member_disturbance_fails',
    'DeferredRefusalTests.test_pending_reporting_active_fails',
    'DeferredRefusalTests.test_pending_exit_is_inconclusive',
    'DeferredRefusalTests.test_pre_contract_strand_is_inconclusive',
    'DeferredRefusalTests.test_unsettled_pair_is_inconclusive',
    'DeferredRefusalTests.test_pause_failure_is_nondeterministic',
    'DeferredRefusalTests.test_launch_failure_is_nondeterministic',
    'DeferredRefusalTests.test_starved_watch_is_nondeterministic',
    'DeferredRefusalTests.test_second_pass_defect_fails',
    'DeferredRefusalTests.test_missing_seams_are_inconclusive',
    'DeferredRefusalTests.test_missing_journals_are_inconclusive',
    'DeferredRefusalTests.test_unchecked_self_check_fails',
    'DeferredRefusalTests.test_judge_self_check_is_complete',
})


class DeferredRefusalFeed:
    """A stubbed rig for the deferred startup-claim refusal leg. The
    deployed pair is the incumbent: 'active' owns the field's claim,
    'standby' tracks it. `pause_plant` freezes the field — launched
    born-actives stand pending (standby, unsynchronized, no observed
    claim) while the `--standby` control seat tracks the incumbent's
    live monitor; `unpause_plant` resolves every pending seat's
    re-issued conditional ask against the incumbent's standing claim:
    the pairless seat exits nonzero naming the refusal and the
    standby remedy, the declared-peer seat keeps standing as the
    pair's rejoined tracking standby, and both journal
    `startup_claim_refused` beside the `field_claim_observed`
    attribution. Every transition is staged by the leg's own lever
    calls — never wall-clock — so two passes emit identical
    evidence. Fault flags stage each named failure and each
    pre-contract shape."""
    SEATS = ('driven', 'foreign', 'revised')
    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': 'driven', 'ctrl-f:4': 'foreign',
             'ctrl-c:3': 'revised'}
    TOKENS = {'active': 424243, 'standby': 424244, 'revised': 424245,
              'foreign': 424246, 'driven': 424247}
    REMOTE = 'dcs-hw-qa-1-plant:9001'
    REFUSAL_LOGS = ("error: a live peer holds the field's "
                    "write-ownership claim — a controller restarting "
                    "into a pair cannot prove its resumed state is "
                    "current with the incumbent's and must not "
                    "preempt it; rejoin as a standby instead — no "
                    "--peer was declared, so there is no pair to "
                    "rejoin")

    def __init__(self, root):
        self.root = Path(root)
        self.journals = {
            seat: self.root / 'controllers' / seat / 'journal.jsonl'
            for seat in self.SEATS}
        self.paused = False
        self.members = {
            'active': {'role': 'active', 'tick': 0,
                       'field_claim': 'held'},
            'standby': {'role': 'standby', 'tick': 0,
                        'field_claim': 'held'}}
        self.seats = {}
        self.calls = []
        self.seq = {}
        self.thaws = 0
        # Fault injection for the named-failure and pre-contract
        # cases.
        self.pause_fails = False        # the plant-pause lever raises
        self.launch_fails = False       # the born-controller lever
        self.freeze_exits = False       # pre-contract: dies on the
                                        # frozen field
        self.watch_starves = False      # the pending monitor never
                                        # answers
        self.pending_reports_active = False
        self.refused_stranded = False   # the journaled refusal never
                                        # settles — the #1301 defect
        self.refused_stranded_pre = False  # pre-contract: claimant
                                           # observed, no
                                           # startup_claim_refused
        self.refused_unsettled = False  # no verdict ever lands
        self.refused_exit_zero = False
        self.unnamed_refusal = False
        self.unnamed_remedy = False
        self.refusal_unjournaled = False
        self.wrong_claimant = False
        self.declared_exits = False     # the refusal swallows the
                                        # declared pair
        self.declared_never_converges = False
        self.declared_claim_unobserved = False
        self.declared_unjournaled = False
        self.standby_exits = False
        self.standby_never_converges = False
        self.standby_journals_refusal = False
        self.incumbent_demoted = False  # the refusal disturbs the
                                        # deployed pair
        self.incumbent_stalls = False
        self.member_disturbed = False
        self.no_active = False          # the pair never settled
        self.second_pass_defect = False  # pass 2 diverges

    # --- the runner's levers, faked ---------------------------------

    def pause_plant(self):
        self.calls.append(('pause_plant',))
        if self.pause_fails:
            raise RuntimeError('docker pause failed')
        self.paused = True

    def unpause_plant(self):
        self.calls.append(('unpause_plant',))
        self.paused = False
        self.thaws += 1
        if self.incumbent_demoted:
            self.members['active'].update(
                {'role': 'standby', 'field_claim': None})
        if self.member_disturbed:
            self.members['standby'].update({'role': 'active'})
        self._resolve_pending()

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, peer,
                           standby))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        old = self.seats.get(seat)
        if old is not None and old['launched'] and not old['exited'] \
                and old['owns']:
            # The runner's refuse-to-replace guard.
            raise RuntimeError('start_born_controller refuses to '
                               'replace dcs-hw-qa-1-' + seat
                               + ': it reports role active')
        self._boundary(seat)
        state = {'launched': True, 'mode': 'born-active',
                 'pending': False, 'exited': False, 'exit': None,
                 'logs': '', 'owns': False, 'refused': False,
                 'peer': peer, 'track': standby, 'tick': 0}
        self.seats[seat] = state
        if standby is not None:
            state['mode'] = 'standby'
            if self.standby_exits:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = 'tracking source refused'
            elif self.standby_journals_refusal:
                self._journal(seat, {'startup_claim_refused': {
                    'error': {'field_claim_failed': {
                        'detail': 'a live peer holds the field'}}}})
            return {'container': 'dcs-hw-qa-1-' + seat}
        # The born-active launch: while the field stands frozen its
        # conditional grant goes pending; an answered field's standing
        # claim refuses it at once.
        if self.paused:
            if self.freeze_exits:
                # The pre-record disposition — the unreachable or
                # verdict-free startup died in driver assembly.
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = 'remote driver connect failed'
            else:
                state['pending'] = True
        else:
            self._refuse(seat)
        return {'container': 'dcs-hw-qa-1-' + seat}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        seat_state = self.seats.get(seat)
        if seat_state is None or not seat_state['launched']:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': not seat_state['exited'],
                'exit': seat_state['exit'],
                'logs': seat_state['logs'], 'absent': False}

    # --- the deferred-refusal settle --------------------------------

    def _refuse(self, seat):
        """The incumbent's standing claim refuses the re-issued
        conditional grant: the journaled claimant attribution plus —
        on the fixed revision — the `startup_claim_refused` settle
        record; the pairless run exits nonzero naming the refusal and
        the remedy, the declared-pair run keeps standing as the
        rejoined tracking standby."""
        state = self.seats[seat]
        state['pending'] = False
        state['refused'] = True
        claimant = (self.TOKENS['foreign'] if self.wrong_claimant
                    else self.TOKENS['active'])
        self._journal(seat, {'field_claim_observed': {
            'point': 100, 'claimant': claimant}})
        journaled = not (
            (seat == 'driven' and (self.refusal_unjournaled
                                   or self.refused_stranded_pre))
            or (seat == 'foreign' and self.declared_unjournaled))
        if journaled:
            self._journal(seat, {'startup_claim_refused': {
                'error': {'field_claim_failed': {
                    'detail': 'a live peer holds the field'}}}})
        if state['peer'] is not None:
            # The declared-pair disposition: the run stays the pair's
            # rejoined standby.
            if self.declared_exits:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = 'claim refused; exiting'
            return
        if self.refused_stranded or self.refused_stranded_pre:
            # The strand shapes: the wedge the contract exists to
            # prevent, and its pre-contract twin — the pairless run
            # keeps standing as an unpaired standby.
            return
        state['exited'] = True
        state['exit'] = 0 if self.refused_exit_zero else 1
        if self.unnamed_refusal:
            state['logs'] = ('error: the conditional startup claim '
                             'was refused — no --peer was declared, '
                             'so there is no pair to rejoin — '
                             'relaunch with --standby')
        elif self.unnamed_remedy:
            state['logs'] = ("error: a live peer holds the field's "
                             "write-ownership claim")
        else:
            state['logs'] = self.REFUSAL_LOGS

    def _resolve_pending(self):
        """The thaw's first answered contact: every pending born-active
        re-issues its conditional ask and meets the incumbent's
        standing claim."""
        if self.paused:
            return
        for seat, state in self.seats.items():
            if state.get('pending'):
                if seat == 'driven' and self.refused_unsettled:
                    continue  # the re-issued ask never lands a verdict
                self._refuse(seat)

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        """One scan for every live member and seat — the request
        boundary is the tick boundary, so served ticks are call-count
        deterministic. The incumbent's tick freezes with its plant."""
        for name, member in self.members.items():
            if name == 'active' and self.incumbent_stalls:
                continue
            if not self.paused or member['role'] != 'active':
                member['tick'] += 1
        for state in self.seats.values():
            if state['launched'] and not state['exited']:
                state['tick'] += 1

    def _sync(self, seat):
        state = self.seats[seat]
        if state['pending']:
            return 'unsynchronized'
        if seat == 'foreign' and (self.declared_never_converges
                                  or (self.second_pass_defect
                                      and self.thaws >= 2)):
            return 'unsynchronized'
        if seat == 'revised' and self.standby_never_converges:
            return 'degraded'
        if seat == 'driven':
            return 'unsynchronized'
        return {'tracking': {'aligned': self.members['active']['tick']}}

    def _role(self, seat):
        state = self.seats[seat]
        if state['exited']:
            raise urllib.error.URLError('connection refused')
        report = {'tick': state['tick'], 'role': 'standby'}
        if state['pending']:
            if self.watch_starves:
                raise urllib.error.URLError('connection refused')
            report['role'] = ('active' if self.pending_reports_active
                              else 'standby')
            report['sync'] = 'unsynchronized'
            return report
        report['sync'] = self._sync(seat)
        if state['refused'] and not (seat == 'foreign'
                                     and self.declared_claim_unobserved):
            report['field_claim'] = 'held'
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
            if self.no_active:
                report['role'] = 'standby'
            return 200, report
        state = self.seats.get(name)
        if state is None or not state['launched']:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(name)
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': state['tick'], 'points': [],
                         'io_health': {'driver': {'link': 'connected'}}}
        raise AssertionError('unexpected request %s %s' % (method, url))

    # --- the seat's runner-owned --journal-file ---------------------

    def _boundary(self, seat):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        self.seq[seat] = 0

    def _journal(self, seat, event):
        path = self.journals[seat]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.seq[seat] = self.seq.get(seat, 0) + 1
        with path.open('a') as stream:
            stream.write(json.dumps({'entry': {
                'seq': self.seq[seat],
                'tick': self.seats[seat]['tick'],
                'event': event}}) + '\n')


class DeferredRefusalTests(unittest.TestCase):
    """scenario_deferred_startup_refusal against the stubbed rig: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named
    acceptance failure — the stranded or unnamed pairless refusal,
    the swallowed declared-pair rejoin, the failed --standby control,
    the deployed pair's disturbance, the pre-contract shapes that must
    report inconclusive, and the instability that must report
    nondeterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = DeferredRefusalFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'driven': 'http://ctrl-d:5',
                'plant_remote': feed.REMOTE,
                'plant_owner': dict(feed.TOKENS),
                'evidence_dir': str(self.evidence),
                'journal_files': {seat: str(feed.journals[seat])
                                  for seat in feed.SEATS},
                'pause_plant': feed.pause_plant,
                'unpause_plant': feed.unpause_plant,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DEFER_PENDING', 1.5), \
                patch.object(scenarios, 'DEFER_SETTLE', 1.5), \
                patch.object(scenarios, 'DEFER_POLL', 0.001):
            return scenarios.scenario_deferred_startup_refusal(
                ctx or self._ctx(feed))

    def test_registered_after_born_active_before_revisions(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_born_active_failure),
            order.index(scenarios.scenario_deferred_startup_refusal))
        self.assertLess(
            order.index(scenarios.scenario_deferred_startup_refusal),
            order.index(scenarios.scenario_incompatible_revision))
        self.assertLess(
            order.index(scenarios.scenario_deferred_startup_refusal),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/deferred-startup-refusal-pass-1.json',
             'evidence/deferred-startup-refusal-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        # Each pass freezes and thaws the plant once and sweeps all
        # three seats.
        self.assertEqual(kinds.count('pause_plant'), 2)
        self.assertEqual(kinds.count('unpause_plant'), 2)
        self.assertEqual(kinds.count('stop_born_controller'), 6)
        self.assertFalse(self.feed.seats)
        self.assertFalse(self.feed.paused)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = DeferredRefusalFeed(second_tmp.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each wrong disposition must fail the
    # run by the named diagnostic.

    def test_refused_seat_stranded_fails(self):
        # The issue's doctored negative: the refused seat stays a
        # standby forever — the #1301 wedge — with its settle
        # journaled.
        self.feed.refused_stranded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        self.assertIn('stranded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_seat_never_settling_fails(self):
        # The pending run stays pending past the thaw — no verdict
        # ever lands.
        self.feed.refused_unsettled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_zero_exit_fails(self):
        self.feed.refused_exit_zero = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_refusal_exit_fails(self):
        self.feed.unnamed_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_remedy_exit_fails(self):
        self.feed.unnamed_remedy = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_refusal_fails(self):
        self.feed.refusal_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_claimant_fails(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claimant', record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_seat_exit_fails(self):
        # The refusal path swallowed the declared pair — the rejoin
        # disposition lost.
        self.feed.declared_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_seat_never_converging_fails(self):
        self.feed.declared_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_claim_unobserved_fails(self):
        self.feed.declared_claim_unobserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_unjournaled_fails(self):
        self.feed.declared_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_seat_exit_fails(self):
        self.feed.standby_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_never_converging_fails(self):
        self.feed.standby_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_journaling_refusal_fails(self):
        # A --standby seat never issues a startup claim — a journaled
        # refusal on it is the refusal path misattributing.
        self.feed.standby_journals_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_demotion_fails(self):
        self.feed.incumbent_demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('incumbent', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_stall_fails(self):
        # The incumbent's tick never advanced across the freeze — the
        # pair's run was disturbed.
        self.feed.incumbent_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_member_disturbance_fails(self):
        self.feed.member_disturbed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_reporting_active_fails(self):
        # The wedge shape: the deferred grant's run reports a role it
        # cannot hold while the field is frozen.
        self.feed.pending_reports_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_exit_is_inconclusive(self):
        # The pre-record disposition: the born-active died on the
        # frozen field rather than standing pending.
        self.feed.freeze_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pre_contract_strand_is_inconclusive(self):
        # The pre-#1301 shape: the deferred refusal lands — the
        # claimant is journaled — but no startup_claim_refused settle
        # record exists and the pairless run keeps standing.
        self.feed.refused_stranded_pre = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_pause_failure_is_nondeterministic(self):
        self.feed.pause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_watch_is_nondeterministic(self):
        self.feed.watch_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_defect_fails(self):
        # Pass 2's declared seat never converges — a defect on either
        # pass is the contract's failure, and the pass's own judge
        # names it before the digests ever compare.
        self.feed.second_pass_defect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('pause_plant', 'unpause_plant', 'plant_remote',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('staging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journals_are_inconclusive(self):
        ctx = self._ctx()
        ctx['journal_files'] = {}
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unchecked_self_check_fails(self):
        # A judge that names nothing slips every planted negative —
        # the leg reports itself unchecked rather than passing.
        with patch.object(scenarios, '_judge_deferred',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('deferred-refusal-strand-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_judge_self_check_is_complete(self):
        # Every planted negative the leg can stage names the
        # diagnostic it must — the self-check slips nothing.
        self.assertEqual(scenarios._deferred_self_check(), [])
