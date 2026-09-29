"""The 2410_born_active_failure leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_born_active_failure, split out
per the leg-module convention (#940). The shared fakes and helpers live
in tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'BornActiveFailureTests.test_registered_after_doomed_before_revisions',
    'BornActiveFailureTests.test_fixed_shape_passes_validates_and_tears_down',
    'BornActiveFailureTests.test_two_runs_produce_identical_evidence',
    'BornActiveFailureTests.test_pending_reporting_active_fails',
    'BornActiveFailureTests.test_pending_reporting_held_claim_fails',
    'BornActiveFailureTests.test_pending_admitting_command_fails',
    'BornActiveFailureTests.test_pending_healthy_link_fails',
    'BornActiveFailureTests.test_pending_stalled_tick_fails',
    'BornActiveFailureTests.test_pending_exit_is_inconclusive',
    'BornActiveFailureTests.test_unjournaled_stand_down_fails',
    'BornActiveFailureTests.test_grant_never_landing_fails',
    'BornActiveFailureTests.test_unjournaled_grant_fails',
    'BornActiveFailureTests.test_pair_not_reconverging_fails',
    'BornActiveFailureTests.test_declared_refusal_exit_is_inconclusive',
    'BornActiveFailureTests.test_rejoin_never_converging_fails',
    'BornActiveFailureTests.test_wrong_claimant_fails',
    'BornActiveFailureTests.test_undeclared_survival_fails',
    'BornActiveFailureTests.test_unnamed_undeclared_exit_fails',
    'BornActiveFailureTests.test_incumbent_demotion_fails',
    'BornActiveFailureTests.test_dead_peer_converging_fails',
    'BornActiveFailureTests.test_dead_peer_exit_fails',
    'BornActiveFailureTests.test_inconclusive_exit_is_inconclusive',
    'BornActiveFailureTests.test_frozen_claim_never_resolving_fails',
    'BornActiveFailureTests.test_field_stage_failure_is_nondeterministic',
    'BornActiveFailureTests.test_launch_failure_is_nondeterministic',
    'BornActiveFailureTests.test_starved_watch_is_nondeterministic',
    'BornActiveFailureTests.test_second_pass_instability_is_nondeterministic',
    'BornActiveFailureTests.test_missing_seams_are_inconclusive',
    'BornActiveFailureTests.test_missing_journals_are_inconclusive',
    'BornActiveFailureTests.test_unchecked_self_check_fails',
    'BornActiveFailureTests.test_judge_self_check_is_complete',
})


class BornActiveFeed:
    """A stubbed born-active rig for the startup-failure leg. The
    scratch field is one of absent / 'silent' (resolvable, nothing
    listening) / 'serving' / 'paused' (attaching, never answering);
    the seats are 'revised'/'foreign'/'driven', each launched cold —
    journal reset, tick from zero. A born-active launch meets the
    field state at activation: silent or paused holds it pending
    (standby, unsynchronized, no observed claim, the fenced
    stand-down journaled), a serving field's standing claim refuses
    it (the declared-pair launch rejoins tracking, the undeclared
    exits nonzero naming the refusal), and an unclaimed serving
    field grants it. Serving the field or unpausing it resolves
    every pending seat through the same conditional grant. Every
    transition is staged by the leg's own lever calls — never
    wall-clock — so two passes emit identical evidence. Fault flags
    stage each named failure and each pre-contract shape."""
    SEATS = ('revised', 'foreign', 'driven')
    HOSTS = {'ctrl-c:3': 'revised', 'ctrl-f:4': 'foreign',
             'ctrl-d:5': 'driven'}
    TOKENS = {'revised': 424245, 'foreign': 424246, 'driven': 424247}
    REMOTE = 'dcs-hw-qa-1-born-plant:9003'

    def __init__(self, root):
        self.root = Path(root)
        self.journals = {
            seat: self.root / 'controllers' / seat / 'journal.jsonl'
            for seat in self.SEATS}
        self.field = None      # None / 'silent' / 'serving' / 'paused'
        self.claim = None      # the seat the field's claim stands for
        self.seats = {}
        self.calls = []
        self.seq = {}
        self.passes = 0        # silent-field stagings — pass counter
        # Fault injection for the named-failure and pre-contract cases.
        self.stage_fails = False        # the born-field lever raises
        self.launch_fails = False       # the born-controller lever
        self.pending_exits = False      # pre-contract: dies on the
                                        # unreachable/frozen field
        self.pending_reports_active = False
        self.pending_claims_held = False
        self.pending_admits = False     # the closed gate accepts
        self.pending_link_healthy = False
        self.pending_tick_stall = False
        self.second_pass_stage_fails = False  # pass 2's staging raises
        self.no_stand_down = False      # the stand-down never journals
        self.grant_never_lands = False  # pending never resolves
        self.grant_unjournaled = False  # the landing skips its records
        self.never_converge_foreign = False
        self.refused_declared_exits = False  # pre-contract: exit on a
                                             # declared-pair refusal
        self.rejoin_never_converges = False
        self.wrong_claimant = False
        self.undeclared_survives = False
        self.unnamed_exit = False
        self.refusal_demotes = False    # the refusal demotes the holder
        self.dead_peer_converges = False
        self.dead_peer_exits = False
        self.inconclusive_exits = False    # the frozen-field pending
                                           # exits — pre-contract
        self.frozen_grant_never_lands = False
        self.watch_starves = False      # the pending monitor never
                                        # answers

    # --- the runner's born levers, faked --------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if mode == 'silent':
            self.passes += 1
        if self.stage_fails \
                or (self.second_pass_stage_fails and self.passes > 1):
            raise RuntimeError('docker run failed: name in use')
        self.field = mode
        self.claim = None  # the relaunched field's arbitration is
        # fresh — every claim the replaced container held dies with it
        self._resolve_pending('serving')
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': self.REMOTE, 'mode': mode}

    def pause_field(self):
        self.calls.append(('pause_born_field',))
        self.field = 'paused'

    def unpause_field(self):
        self.calls.append(('unpause_born_field',))
        self.field = 'serving'
        self._resolve_pending('unpause')

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        self.field = None
        self.claim = None

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, peer,
                           standby))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        old = self.seats.get(seat)
        if old is not None and old['launched'] and not old['exited'] \
                and old['owns']:
            # The runner's refuse-to-replace guard: an owning seat is
            # never silently removed.
            raise RuntimeError('start_born_controller refuses to '
                               'replace dcs-hw-qa-1-' + seat
                               + ': it reports role active')
        self._boundary(seat)
        state = {'launched': True, 'pending': False, 'exited': False,
                 'exit': None, 'logs': '', 'owns': False,
                 'refused': False, 'peer': peer,
                 'track': standby if standby is not None else peer,
                 'tick': 0}
        self.seats[seat] = state
        if standby is not None:
            state['mode'] = 'standby'
            return {'container': 'dcs-hw-qa-1-' + seat}
        # The born-active launch: the startup claim's verdict by the
        # field's staged state.
        if self.field != 'serving':
            if self.pending_exits \
                    or (self.field == 'paused'
                        and self.inconclusive_exits):
                # The pre-record disposition — the unreachable or
                # verdict-free startup died in driver assembly.
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = 'remote driver connect failed'
            else:
                state['pending'] = True
                self._stand_down(seat)
        elif self.claim is not None:
            self._refuse(seat)
        else:
            self.claim = seat
            state['owns'] = True
            state['mode'] = 'active'
        return {'container': 'dcs-hw-qa-1-' + seat}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if self.claim == seat:
            self.claim = None
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

    # --- the feed's claim arbitration ------------------------------

    def _stand_down(self, seat):
        if not self.no_stand_down:
            self._journal(seat, {'role_changed': {
                'from': 'active', 'to': 'standby',
                'origin': 'fenced'}})

    def _refuse(self, seat):
        """The incumbent's standing claim refuses the launch's
        conditional grant — the recorded response is the journaled
        stand-down plus the observed claimant; the declared-pair
        launch rejoins tracking, the undeclared exits naming the
        refusal."""
        state = self.seats[seat]
        state['refused'] = True
        self._stand_down(seat)
        claimant = (self.TOKENS['foreign'] if self.wrong_claimant
                    else self.TOKENS[self.claim])
        self._journal(seat, {'field_claim_observed': {
            'point': 100, 'claimant': claimant}})
        if self.refusal_demotes:
            holder = self.seats[self.claim]
            holder['owns'] = False
            holder['demoted'] = True
        if seat == 'driven' and self.dead_peer_exits \
                and state['track'] is not None \
                and state['track'] not in self.SEATS:
            state['exited'] = True
            state['exit'] = 1
            state['logs'] = 'peer refused the rejoin'
        elif seat == 'driven' and self.refused_declared_exits \
                and state['peer'] == 'revised':
            state['exited'] = True
            state['exit'] = 1
            state['logs'] = 'claim refused; exiting'
        elif state['peer'] is None and not self.undeclared_survives:
            state['exited'] = True
            state['exit'] = 1
            state['logs'] = (
                'claim refused' if self.unnamed_exit else
                'error: the conditional startup claim was refused '
                'and no --peer was declared, so there is no pair to '
                'rejoin — relaunch with --standby to join as the '
                'pair\'s tracking member')

    def _resolve_pending(self, source):
        """The deferred grant's landing: each pending seat re-issues
        its conditional ask on the field's first answered contact —
        granted where the claim stands unclaimed, refused where the
        incumbent still holds it."""
        if self.field != 'serving':
            return
        if source == 'unpause' and self.frozen_grant_never_lands:
            return
        for seat in self.SEATS:
            state = self.seats.get(seat)
            if state is None or not state.get('pending'):
                continue
            if self.grant_never_lands:
                continue
            state['pending'] = False
            if self.claim is None:
                self.claim = seat
                state['owns'] = True
                state['mode'] = 'active'
                if not self.grant_unjournaled:
                    self._journal(seat, {'role_changed': {
                        'from': 'standby', 'to': 'promoting',
                        'origin': 'reclaim'}})
                    self._journal(seat, {'role_changed': {
                        'from': 'promoting', 'to': 'active',
                        'origin': 'reclaim'}})
            else:
                self._refuse(seat)

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        """One scan for every live seat — the request boundary is the
        tick boundary, so served ticks are call-count deterministic."""
        for state in self.seats.values():
            if state['launched'] and not state['exited'] \
                    and not (self.pending_tick_stall
                             and state['pending']):
                state['tick'] += 1

    def _sync(self, seat):
        state = self.seats[seat]
        if state['pending']:
            return 'unsynchronized'
        if seat == 'foreign' and self.never_converge_foreign:
            return 'orphaned'
        if seat == 'driven':
            if self.rejoin_never_converges:
                return 'degraded'
            if self.dead_peer_converges:
                return {'tracking': {'aligned': 5}}
        target = state.get('track')
        if target is None:
            return 'unsynchronized'
        if target not in self.SEATS:
            return 'degraded'
        source = self.seats.get(target)
        if source is None or not source['launched'] \
                or source['exited']:
            return 'degraded'
        if source['owns'] and self.claim == target:
            return {'tracking': {'aligned': source['tick']}}
        return 'orphaned'

    def _role(self, seat):
        state = self.seats[seat]
        if state['exited']:
            raise urllib.error.URLError('connection refused')
        report = {'tick': state['tick']}
        if state['pending']:
            if self.watch_starves:
                raise urllib.error.URLError('connection refused')
            report['role'] = ('active' if self.pending_reports_active
                              else 'standby')
            report['sync'] = 'unsynchronized'
            if self.pending_claims_held:
                report['field_claim'] = 'held'
            return report
        if state['owns']:
            report['role'] = 'active'
            report['field_claim'] = 'held'
            return report
        report['role'] = 'standby'
        report['sync'] = self._sync(seat)
        if state.get('demoted'):
            return report
        if state['refused'] or (self.field == 'serving'
                                and self.claim is not None):
            report['field_claim'] = 'held'
        return report

    def _snapshot(self, seat):
        state = self.seats[seat]
        if state['exited']:
            raise urllib.error.URLError('connection refused')
        link = ('connected' if self.field in ('serving', 'paused')
                else 'disconnected')
        if self.pending_link_healthy:
            link = 'connected'
        last_error = (None if self.field == 'serving'
                      else 'connection timed out'
                      if self.field == 'paused'
                      else 'connection refused')
        return {'tick': state['tick'],
                'points': [{'point': 100,
                            'sample': {'value': {'float': 1.0},
                                       'quality': 'good'
                                       if link == 'connected'
                                       else 'bad'}}],
                'io_health': {'driver': {'link': link,
                                         'last_error': last_error}}}

    def _command(self, seat, body):
        state = self.seats[seat]
        if state['pending'] and self.pending_admits:
            return 200, {'command': body.get('command'),
                         'outcome': {'accepted': {'apply_tick': 5}},
                         'actor': body.get('actor')}
        if not state['owns']:
            return 200, {'command': body.get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_active': {'point': 1000,
                                            'role': 'standby'}}}},
                         'actor': body.get('actor')}
        return 200, {'command': body.get('command'),
                     'outcome': {'accepted': {
                         'apply_tick': state['tick'] + 1}},
                     'actor': body.get('actor')}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        seat = self.HOSTS.get(host)
        if seat is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        state = self.seats.get(seat)
        if state is None or not state['launched']:
            raise urllib.error.URLError('connection refused')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, self._role(seat)
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(seat)
        if (method, route) == ('POST', '/command'):
            return self._command(seat, body)
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


class BornActiveFailureTests(unittest.TestCase):
    """scenario_born_active_failure against the stubbed rig: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named
    acceptance failure — each recorded failure class's wrong
    disposition, the wedge shapes (a peer reporting a role it
    cannot hold, a refused pair left unpaired), the incumbent
    disturbances, the pre-contract exit shapes that must report
    inconclusive, and the instability that must report
    nondeterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = BornActiveFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'revised': 'http://ctrl-c:3',
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
                patch.object(scenarios, 'BORN_SETTLE', 1.5), \
                patch.object(scenarios, 'BORN_POLL', 0.001):
            return scenarios.scenario_born_active_failure(
                ctx or self._ctx(feed))

    def test_registered_after_doomed_before_revisions(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_doomed_startup_claim),
            order.index(scenarios.scenario_born_active_failure))
        self.assertLess(
            order.index(scenarios.scenario_born_active_failure),
            order.index(scenarios.scenario_incompatible_revision))
        self.assertLess(
            order.index(scenarios.scenario_born_active_failure),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/born-active-failure-pass-1.json',
             'evidence/born-active-failure-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Every pass ends torn down: all three seats and the scratch
        # field removed per pass.
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_born_field'), 4)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        # Per pass: the inconclusive-claim class frees the incumbent's
        # seat once, and the teardown sweeps all three seats.
        self.assertEqual(kinds.count('stop_born_controller'), 8)
        self.assertFalse(self.feed.seats)
        self.assertIsNone(self.feed.field)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = BornActiveFeed(second_tmp.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each class's wrong recorded response
    # must fail the run by the named diagnostic.

    def test_pending_reporting_active_fails(self):
        # The wedge shape: the pending run reports a role it cannot
        # hold — active with no granted claim.
        self.feed.pending_reports_active = True
        self.feed.pending_claims_held = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        self.assertIn('pending', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_reporting_held_claim_fails(self):
        self.feed.pending_claims_held = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_admitting_command_fails(self):
        self.feed.pending_admits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_healthy_link_fails(self):
        self.feed.pending_link_healthy = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_stalled_tick_fails(self):
        self.feed.pending_tick_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_exit_is_inconclusive(self):
        # The pre-record disposition: the born-active died inside
        # driver assembly instead of standing pending.
        self.feed.pending_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the born-active startup-failure '
                      'contract', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_stand_down_fails(self):
        self.feed.no_stand_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stand-down', record.get('detail', ''))
        report.validate_scenario(record)

    def test_grant_never_landing_fails(self):
        self.feed.grant_never_lands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_grant_fails(self):
        self.feed.grant_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('grant', record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_not_reconverging_fails(self):
        self.feed.never_converge_foreign = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reconverge', record.get('detail', ''))
        report.validate_scenario(record)

    def test_declared_refusal_exit_is_inconclusive(self):
        # The pre-record disposition: the refused launch exits even
        # though its declared pair offered the rejoin.
        self.feed.refused_declared_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the born-active startup-failure '
                      'contract', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rejoin_never_converging_fails(self):
        self.feed.rejoin_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_claimant_fails(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claimant', record.get('detail', ''))
        report.validate_scenario(record)

    def test_undeclared_survival_fails(self):
        # The unpaired-active wedge the contract exists to prevent:
        # a refused launch with no declared pair that keeps running.
        self.feed.undeclared_survives = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('undeclared', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_undeclared_exit_fails(self):
        self.feed.unnamed_exit = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('undeclared', record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_demotion_fails(self):
        self.feed.refusal_demotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('incumbent', record.get('detail', ''))
        report.validate_scenario(record)

    def test_dead_peer_converging_fails(self):
        # The wedge shape: a rejoin reporting tracking behind a peer
        # that does not exist.
        self.feed.dead_peer_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dead_peer_exit_fails(self):
        self.feed.dead_peer_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_inconclusive_exit_is_inconclusive(self):
        # The pre-record disposition for class (c): the verdict-free
        # claim exits rather than holding pending.
        self.feed.inconclusive_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the born-active startup-failure '
                      'contract', record.get('detail', ''))
        report.validate_scenario(record)

    def test_frozen_claim_never_resolving_fails(self):
        self.feed.frozen_grant_never_lands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_watch_is_nondeterministic(self):
        self.feed.watch_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_instability_is_nondeterministic(self):
        # Pass 2's staging raises — the second pass reports the
        # instability its own record carries.
        self.feed.second_pass_stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-nondeterministic',
                      record.get('detail', ''))
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
        self.assertIn('born-active staging', record.get('detail', ''))
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
        with patch.object(scenarios, '_judge_born',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('born-active-failure-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_judge_self_check_is_complete(self):
        # Every planted negative the leg can stage names the
        # diagnostic it must — the self-check slips nothing.
        self.assertEqual(scenarios._self_check(), [])
