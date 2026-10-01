"""The 2420_remote_foreign_model leg's scenario unit coverage — the
feed fake and TestCase class for scenario_remote_foreign_model, split
out per the leg-module convention (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py; EXPECTED_CASES pins this
module's contribution to the suite's case coverage so a dropped case
fails the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'RemoteForeignModelTests.test_registered',
    'RemoteForeignModelTests.test_clean_passes_validates_and_tears_down',
    'RemoteForeignModelTests.test_pending_verdict_passes',
    'RemoteForeignModelTests.test_two_runs_produce_identical_evidence',
    'RemoteForeignModelTests.test_claim_despite_refusal_fails',
    'RemoteForeignModelTests.test_field_claimed_despite_refusal_fails',
    'RemoteForeignModelTests.test_field_driven_despite_refusal_fails',
    'RemoteForeignModelTests.test_control_met_named_refusal_fails',
    'RemoteForeignModelTests.test_control_unclaimed_fails',
    'RemoteForeignModelTests.test_control_scan_stalled_fails',
    'RemoteForeignModelTests.test_control_field_stalled_fails',
    'RemoteForeignModelTests.test_control_drops_fails',
    'RemoteForeignModelTests.test_control_pending_is_nondeterministic',
    'RemoteForeignModelTests.test_control_launch_fails_is_nondeterministic',
    'RemoteForeignModelTests.test_census_down_is_nondeterministic',
    'RemoteForeignModelTests.test_not_foreign_field_is_inconclusive',
    'RemoteForeignModelTests.test_claim_after_unread_is_nondeterministic',
    'RemoteForeignModelTests.test_field_stage_failure_is_nondeterministic',
    'RemoteForeignModelTests.test_launch_failure_is_nondeterministic',
    'RemoteForeignModelTests.test_pair_disturbance_is_nondeterministic',
    'RemoteForeignModelTests.test_pair_wedge_is_nondeterministic',
    'RemoteForeignModelTests.test_second_pass_pending_diverges_nondeterministic',
    'RemoteForeignModelTests.test_unnamed_refusal_fails',
    'RemoteForeignModelTests.test_predates_contract_is_inconclusive',
    'RemoteForeignModelTests.test_watch_starves_is_nondeterministic',
    'RemoteForeignModelTests.test_state_fails_is_nondeterministic',
    'RemoteForeignModelTests.test_missing_seams_are_inconclusive',
    'RemoteForeignModelTests.test_unreachable_rig_is_inconclusive',
    'RemoteForeignModelTests.test_unsettled_pair_is_inconclusive',
    'RemoteForeignModelTests.test_unchecked_self_check_fails',
    'RemoteForeignModelTests.test_self_check_is_complete',
})


class ForeignFieldFeed:
    """A stubbed rig for the foreign-model correspondence leg. ctrl-a
    owns the deployed pair's field while ctrl-b tracks; the born
    levers stage the leg's scratch field — 'foreign' serves the
    dosing-skid point set (nothing at 120, bool at 20) and 'serving'
    serves the rig model — and the labeled seat launches: the
    mismatched seat on the foreign field meets the named
    correspondence refusal (a nonzero exit carrying the verdict), and
    the control seat on the same-model field claims and scans. The
    born_field_ctl seam answers the shipped tool's argv inside the
    scratch field's container — a stepped probe means unclaimed, a
    fenced one means a claim stands, and the ping tick moves only
    while a claiming seat scans. Every transition keys off the leg's
    lever calls so two passes emit identical evidence; the fault
    flags stage each named defect, each inconclusive rig state, and
    the pre-contract shape."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-f:4': 'foreign'}
    REMOTE = 'dcs-hw-qa-1-born-plant:9003'
    SKID_POINTS = [10, 11, 12, 13, 20, 21, 22, 40, 41, 50, 51,
                   60, 61, 70, 71, 100, 101, 110, 111]
    RIG_POINTS = [10, 11, 12, 13, 20, 21, 40, 41, 60, 61, 80, 81,
                  100, 101, 120]
    REFUSAL = ('error: plant server at ' + REMOTE + ' does not serve '
               'io point 120: unknown I/O point PointId(120)')
    PENDING = ('startup: field write-ownership claim produced no '
               'verdict (plant write-ownership claim failed: deferred '
               'assembly probe: the plant does not serve io point '
               'PointId(120): unknown I/O point PointId(120))')

    def __init__(self):
        self.tick = 900           # the deployed pair's scan tick
        self.ftick = 0            # the scratch field's plant tick
        self.field = None         # None | 'foreign' | 'serving'
        self.claim = None         # the seat the field's claim serves
        self.seats = {}
        self.calls = []
        self.foreign_stagings = 0  # the pass counter
        self.reads = {'ctrl-a:1': 0, 'ctrl-b:2': 0}
        # Doctors staging each named defect, each unread surface, and
        # each pre-contract or inconclusive rig shape.
        self.predates = False             # the claim lands — no probe
        self.unnamed_refusal = False      # exits without the verdict
        self.pending_mismatch = False     # stands pending naming it
        self.claim_despite_refusal = False  # named logs, claim+active
        self.claim_lingers = False        # named exit, claim still stands
        self.drives_despite_refusal = False  # named verdict, tick moves
        self.claim_probe_unread = False   # the claim probe never answers
        self.not_foreign = False          # the field answers like the rig
        self.census_down = False          # the field never answers the tool
        self.state_fails = False          # the state lever raises
        self.watch_starves = False        # the seat monitor never answers
        self.control_pending = False      # the control never claims
        self.control_refused = False      # the control exits named
        self.control_launch_fails = False  # the control's launch raises
        self.control_unclaimed = False    # active but field unclaimed
        self.control_drops = False        # the held surface drops
        self.control_stalled = False      # the control's scan tick stalls
        self.control_field_stalled = False  # the field tick never moves
        self.pair_moves = False           # the standby reports active
        self.pair_wedged = False          # the owner's tick freezes
        self.unsettled_pair = False       # the standby never tracks
        self.silent_rig = False           # every endpoint refuses
        self.stage_fails = False          # start_born_field raises
        self.launch_fails = False         # start_born_controller raises
        self.ctl_fails = False            # born_field_ctl raises
        self.second_pass_pending = False  # pass 2's verdict differs

    # --- the runner's born levers, faked ---------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        if mode == 'foreign':
            self.foreign_stagings += 1
        self.field = mode
        self.claim = None   # the relaunched field's arbitration is fresh
        self.ftick = 0
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': self.REMOTE, 'mode': mode}

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        self.field = None
        self.claim = None

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, remote))
        if self.launch_fails or (seat == 'foreign'
                                 and self.control_launch_fails):
            raise RuntimeError('docker run failed: name in use')
        state = {'launched': True, 'exited': False, 'exit': None,
                 'logs': '', 'owns': False, 'tick': 0, 'reads': 0}
        self.seats[seat] = state
        if seat == 'revised':
            # The mismatched launch — the rig model declared against
            # the foreign field.
            if self.predates or self.not_foreign:
                # No correspondence probe — the claim lands — or the
                # staged field is not foreign and the claim is
                # legitimate; either way the run goes active.
                self.claim = seat
                state['owns'] = True
            elif self.claim_despite_refusal:
                self.claim = seat
                state['owns'] = True
                state['logs'] = self.REFUSAL
            elif self.claim_lingers:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = self.REFUSAL
                self.claim = seat   # a holderless claim still stands
            elif self.watch_starves:
                pass  # stays running standby — the monitor is dead
            elif self.pending_mismatch \
                    or (self.second_pass_pending
                        and self.foreign_stagings >= 2):
                state['logs'] = self.PENDING
            elif self.census_down:
                state['logs'] = ('startup: field write-ownership '
                                 'claim produced no verdict')
            elif self.unnamed_refusal:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = 'error: startup failed'
            else:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = self.REFUSAL
        else:
            # The control launch — the same shape against the
            # same-model field.
            if self.control_refused:
                state['exited'] = True
                state['exit'] = 1
                state['logs'] = self.REFUSAL
            elif self.control_pending:
                pass  # stands standby-unnamed — the claim never lands
            else:
                self.claim = seat
                state['owns'] = True
        return {'container': 'dcs-hw-qa-1-' + seat}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if self.claim == seat:
            self.claim = None
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        if self.state_fails:
            raise RuntimeError('docker inspect failed')
        seat_state = self.seats.get(seat)
        if seat_state is None or not seat_state['launched']:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        if self.drives_despite_refusal and seat == 'revised' \
                and self.field == 'foreign':
            self.ftick += 1   # the defect: the refused run still steps
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': not seat_state['exited'],
                'exit': seat_state['exit'], 'logs': seat_state['logs'],
                'absent': False}

    # --- the born field's dcs-plant-ctl seam ------------------------

    def _field_sample(self, point):
        if self.field == 'foreign' and not self.not_foreign:
            if point == 120:
                return None
            if point == 20:
                return {'bool': True}
            return {'float': 0.0}
        if point == 120:
            return {'bool': False}
        if point == 20:
            return {'float': 1.5}
        return {'float': 0.0}

    def field_ctl(self, *args):
        self.calls.append(('born_field_ctl',) + args)
        if self.ctl_fails:
            raise RuntimeError('docker exec failed: no such container')
        if self.field is None or self.census_down:
            return _ctl_process(
                stderr='dcs-plant-ctl: cannot reach the plant server '
                       'at 127.0.0.1:9003: no live connection to the '
                       'plant server', returncode=1)
        op = args[0]
        if op == 'list':
            points = (self.SKID_POINTS
                      if self.field == 'foreign' and not self.not_foreign
                      else self.RIG_POINTS)
            return _ctl_process({'result': 'points', 'points': [
                {'point': point, 'direction': 'in'}
                for point in points]})
        if op == 'read':
            sample = self._field_sample(int(args[1]))
            if sample is None:
                return _ctl_process(
                    stderr='dcs-plant-ctl: 127.0.0.1:9003: unknown I/O '
                           'point PointId(%s)' % args[1],
                    returncode=1)
            return _ctl_process({'result': 'sample',
                                 'sample': {'value': sample,
                                            'quality': 'good',
                                            'tick': self.ftick}})
        if op == 'ping':
            return _ctl_process({'result': 'alive',
                                 'tick': self.ftick})
        if op == 'step':
            if self.claim_probe_unread and self.field == 'foreign' \
                    and 'revised' in self.seats:
                return _ctl_process(
                    stderr='dcs-plant-ctl: cannot reach the plant '
                           'server at 127.0.0.1:9003: no live '
                           'connection to the plant server',
                    returncode=1)
            if self.claim is not None and not (
                    self.control_unclaimed
                    and self.claim == 'foreign'
                    and self.field == 'serving'):
                return _ctl_process(
                    stderr='dcs-plant-ctl: 127.0.0.1:9003: field '
                           'mutation refused: another attachment owns '
                           'field writes', returncode=1)
            self.ftick += 1
            return _ctl_process({'result': 'stepped',
                                 'tick': self.ftick})
        raise AssertionError('unexpected born_field_ctl argv %s'
                             % (args,))

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        for seat, state in self.seats.items():
            if not state['launched'] or state['exited']:
                continue
            if not (self.control_stalled and seat == 'foreign'):
                state['tick'] += 1
            if self.claim == seat and not (
                    self.control_field_stalled and seat == 'foreign'):
                self.ftick += 1

    def _pair_role(self, host):
        self.reads[host] += 1
        reads = self.reads[host]
        if host == 'ctrl-a:1':
            if not (self.pair_wedged and reads > 1):
                self.tick += 1
            return {'role': 'active', 'tick': self.tick,
                    'field_claim': 'held'}
        if self.pair_moves and reads > 1:
            return {'role': 'active', 'tick': self.tick,
                    'field_claim': 'held'}
        report = {'role': 'standby', 'tick': self.tick}
        report['sync'] = {'degraded': {'reason': 'unsettled'}} \
            if self.unsettled_pair \
            else {'tracking': {'aligned': self.tick}}
        return report

    def _seat_role(self, seat):
        state = self.seats[seat]
        if self.watch_starves and seat == 'revised' \
                and not state['owns']:
            raise urllib.error.URLError('connection refused')
        state['reads'] += 1
        if state['owns']:
            if self.control_drops and seat == 'foreign' \
                    and state['reads'] > 1:
                return {'role': 'standby', 'sync': 'unsynchronized',
                        'tick': state['tick']}
            return {'role': 'active', 'field_claim': 'held',
                    'tick': state['tick']}
        report = {'role': 'standby', 'sync': 'unsynchronized',
                  'tick': state['tick']}
        if self.claim is not None:
            report['field_claim'] = 'held'
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
        if state is None or not state['launched'] or state['exited']:
            raise urllib.error.URLError('connection refused')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, self._seat_role(seat)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class RemoteForeignModelTests(unittest.TestCase):
    """scenario_remote_foreign_model against the stubbed rig: the
    mismatched --remote launch meets the named correspondence refusal,
    the foreign field stays unclaimed and undriven, the same-model
    control claims and scans, and the deployed pair never moves — two
    passes, identical digests. Each fault flag stages a named
    failure, a nondeterministic surface, or a pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ForeignFieldFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'evidence_dir': str(self.evidence),
                'start_born_field': feed.start_field,
                'stop_born_field': feed.stop_field,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state,
                'born_field_ctl': feed.field_ctl}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FOREIGN_SETTLE', 1.5), \
                patch.object(scenarios, 'FOREIGN_POLL', 0.001):
            return scenarios.scenario_remote_foreign_model(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads((self.evidence
                           / ('remote-foreign-model-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_remote_foreign_model,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('remote-foreign-model'),
                      scenarios.scenario_remote_foreign_model)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_born_active_failure),
            order.index(scenarios.scenario_remote_foreign_model))
        self.assertLess(
            order.index(scenarios.scenario_remote_foreign_model),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/remote-foreign-model-pass-1.json',
             'evidence/remote-foreign-model-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        passed = self._pass(1)
        self.assertEqual(passed['mismatched']['disposition'], 'refused')
        self.assertTrue(passed['mismatched']['named'])
        self.assertEqual(passed['foreign']['absent_point'], 'unserved')
        self.assertEqual(passed['foreign']['kind_point'], 'bool')
        self.assertEqual(passed['foreign']['claim_after'], 'unclaimed')
        self.assertFalse(passed['foreign']['driven'])
        self.assertEqual(passed['control']['launch']['disposition'],
                         'active')
        self.assertEqual(passed['control']['claim_after'], 'claimed')
        self.assertEqual(passed['digest'],
                         {'mismatched': 'refused', 'field': 'untouched',
                          'foreignness': 'proved',
                          'control': 'claims-and-scans',
                          'pair': 'held'})
        self.assertEqual(passed['digest'], self._pass(2)['digest'])
        # Every pass ends torn down — the seats and the scratch field
        # removed, the launch configuration restored.
        self.assertFalse(self.feed.seats)
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_born_field'), 4)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        self.assertIn(('stop_born_controller', 'revised'),
                      self.feed.calls)
        self.assertIn(('stop_born_controller', 'foreign'),
                      self.feed.calls)

    def test_pending_verdict_passes(self):
        # The named pending-with-mismatch verdict is the contract's
        # deferred surface — an accepted disposition.
        self.feed.pending_mismatch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self._pass(1)['digest']['mismatched'],
                         'pending')

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = ForeignFieldFeed()
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each named defect must fail the run by
    # the named diagnostic.

    def test_claim_despite_refusal_fails(self):
        # The issue's doctored negative: the refusal asserted while
        # the mismatched controller claims and goes active.
        self.feed.claim_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_claimed_despite_refusal_fails(self):
        # The named exit lands but the field still carries the claim —
        # the refusal never reached the field's arbitration.
        self.feed.claim_lingers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_driven_despite_refusal_fails(self):
        self.feed.drives_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_met_named_refusal_fails(self):
        # A correspondence probe that convicts the matching model is
        # the contract's own defect, not a staging failure.
        self.feed.control_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_unclaimed_fails(self):
        # The control reports held but the field answers unclaimed —
        # the claim never landed.
        self.feed.control_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_scan_stalled_fails(self):
        self.feed.control_stalled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_field_stalled_fails(self):
        self.feed.control_field_stalled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_drops_fails(self):
        self.feed.control_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    # The instability the contract does not answer for must report
    # nondeterministic.

    def test_control_pending_is_nondeterministic(self):
        self.feed.control_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_launch_fails_is_nondeterministic(self):
        self.feed.control_launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_claim_after_unread_is_nondeterministic(self):
        # The post-launch claim probe never answers — the leg cannot
        # prove the claim never landed.
        self.feed.claim_probe_unread = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_disturbance_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedge_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_second_pass_pending_diverges_nondeterministic(self):
        # Pass 2's mismatched launch stands pending where pass 1's
        # exited — the digests diverge.
        self.feed.second_pass_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    # The pre-contract and unreadable surfaces must report
    # inconclusive.

    def test_predates_contract_is_inconclusive(self):
        # The recorded defect: the --remote launch claims and goes
        # active on the foreign field — no named verdict exists.
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_refusal_fails(self):
        # The named-diagnostic clause itself: on a field proven
        # foreign the refusal must say what it refused — an unnamed
        # exit is a contract failure, not an unreadable revision.
        self.feed.unnamed_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-failed',
                      record.get('detail', ''))
        self.assertIn('mismatched-unnamed',
                      str(self._pass(1)['violations']))
        report.validate_scenario(record)

    def test_not_foreign_field_is_inconclusive(self):
        self.feed.not_foreign = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not foreign', record.get('detail', ''))
        report.validate_scenario(record)

    def test_census_down_is_nondeterministic(self):
        # The field staged and bound but its tool surface never
        # answers — a mid-run flake, audited as instability.
        self.feed.census_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_watch_starves_is_nondeterministic(self):
        self.feed.watch_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_fails_is_nondeterministic(self):
        self.feed.state_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state', 'born_field_ctl'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('born-active staging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled_pair = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    # The leg's own auditors must catch their planted negatives.

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_judge_foreign',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign-model-claim-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._foreign_self_check(), [])
