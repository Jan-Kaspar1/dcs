"""The 2470_sim_bus_startup_claim_refusal leg's scenario unit coverage —
the feed fake and TestCase class for scenario_sim_bus_startup_claim_refusal,
split out per the leg-module convention (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES pins this
module's contribution to the suite's case coverage so a dropped case
fails the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SimBusClaimRefusalTests.test_registered',
    'SimBusClaimRefusalTests.test_clean_passes_validates_and_tears_down',
    'SimBusClaimRefusalTests.test_two_passes_produce_identical_evidence',
    'SimBusClaimRefusalTests.test_claim_despite_refusal_fails',
    'SimBusClaimRefusalTests.test_clean_exit_fails',
    'SimBusClaimRefusalTests.test_unnamed_exit_fails',
    'SimBusClaimRefusalTests.test_unnamed_missing_pair_fails',
    'SimBusClaimRefusalTests.test_unnamed_remedy_fails',
    'SimBusClaimRefusalTests.test_unjournaled_verdict_fails',
    'SimBusClaimRefusalTests.test_wrong_claimant_fails',
    'SimBusClaimRefusalTests.test_incumbent_lost_its_claim_fails',
    'SimBusClaimRefusalTests.test_incumbent_fenced_demotion_fails',
    'SimBusClaimRefusalTests.test_incumbent_scan_wedged_fails',
    'SimBusClaimRefusalTests.test_control_orphaned_fails',
    'SimBusClaimRefusalTests.test_control_promoted_fails',
    'SimBusClaimRefusalTests.test_control_admitted_command_fails',
    'SimBusClaimRefusalTests.test_incumbent_never_claimed_is_nondeterministic',
    'SimBusClaimRefusalTests.test_launch_failure_is_nondeterministic',
    'SimBusClaimRefusalTests.test_device_stage_failure_is_nondeterministic',
    'SimBusClaimRefusalTests.test_unread_verdict_is_nondeterministic',
    'SimBusClaimRefusalTests.test_standing_verdict_is_nondeterministic',
    'SimBusClaimRefusalTests.test_journal_unread_is_nondeterministic',
    'SimBusClaimRefusalTests.test_control_never_converged_is_nondeterministic',
    'SimBusClaimRefusalTests.test_control_watch_starved_is_nondeterministic',
    'SimBusClaimRefusalTests.test_control_signals_unread_is_nondeterministic',
    'SimBusClaimRefusalTests.test_pair_disturbance_is_nondeterministic',
    'SimBusClaimRefusalTests.test_pair_wedge_is_nondeterministic',
    'SimBusClaimRefusalTests.test_pair_moves_after_sweep_is_nondeterministic',
    'SimBusClaimRefusalTests.test_rig_left_standing_is_nondeterministic',
    'SimBusClaimRefusalTests.test_device_left_serving_is_nondeterministic',
    'SimBusClaimRefusalTests.test_diverging_digests_fail',
    'SimBusClaimRefusalTests.test_predates_contract_is_inconclusive',
    'SimBusClaimRefusalTests.test_unstaged_device_is_inconclusive',
    'SimBusClaimRefusalTests.test_missing_seams_are_inconclusive',
    'SimBusClaimRefusalTests.test_unpinned_token_is_inconclusive',
    'SimBusClaimRefusalTests.test_unreachable_rig_is_inconclusive',
    'SimBusClaimRefusalTests.test_unsettled_pair_is_inconclusive',
    'SimBusClaimRefusalTests.test_unchecked_self_check_fails',
    'SimBusClaimRefusalTests.test_self_check_is_complete',
})


class BusFeed:
    """A stubbed rig for the sim-bus born-active claim-refusal leg.
    The deployed pair owns a sim-tcp field and never moves; the lane's
    device server serves the staged bus document, and every seat the
    leg launches on it carries a document-addressed launch — no
    `--remote` at all, the staged document mounted in place of the
    run's own model — with the seat's own runner-owned journal file
    recording the events the audit reads. The incumbent takes the claim
    and scans; the second born-active declaring no pair exits with the
    named refusal; the `--standby` control converges tracking behind
    the incumbent with its gate closed. Every transition keys off the
    leg's lever calls so two passes emit identical evidence; the fault
    flags stage each named defect, each inconclusive rig state, and
    the pre-contract shape."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-f:4': 'foreign',
             'ctrl-d:5': 'driven'}
    ADDRESS = 'dcs-hw-qa-1-bus:9005'
    MODEL = '/run/qa-1/sim-bus/model.json'
    INCUMBENT_TOKEN = 424243
    REFUSAL = ("error: startup: a live peer holds the field's "
               "write-ownership claim — a controller restarting into a "
               "pair cannot prove its resumed state is current with "
               "the incumbent's and must not preempt it; relaunch with "
               "--standby ADDRESS to rejoin as the incumbent's tracking "
               "standby instead — no --peer was declared, so there is "
               "no pair to rejoin: relaunch with --standby ADDRESS to "
               "track the field's live owner")

    def __init__(self, journals):
        self.tick = 900            # the deployed pair's scan tick
        self.device = None         # None while no device server serves
        self.claim = None          # the seat holding the bus claim
        self.seats = {}
        self.calls = []
        self.reads = {'ctrl-a:1': 0, 'ctrl-b:2': 0}
        self.journals = journals   # {seat: path} the runner owns
        self.written = {seat: [] for seat in
                        ('revised', 'foreign', 'driven')}
        # Doctors staging each named defect, each unread surface, and
        # each pre-contract or inconclusive rig shape.
        self.predates = False          # the claim lands — no refusal
        self.claim_despite_refusal = False  # named logs, claim+active
        self.exit_zero = False         # the refusal exits zero
        self.unnamed = False           # exits without the verdict
        self.no_undeclared = False     # exits without the missing pair
        self.no_remedy = False         # exits without the remedy flag
        self.unjournaled = False       # no startup_claim_refused
        self.wrong_claimant = False    # names a foreign owner token
        self.incumbent_fenced = False  # the preempting write lands
        self.incumbent_unclaimed = False  # the incumbent lost the claim
        self.incumbent_wedged = False  # the incumbent's scan stops
        self.incumbent_unclaimed_launch = False  # never takes the claim
        self.journal_vanishes = False  # the durable evidence is gone
        self.standing = False          # a claimless standby surface
        self.refused_monitor_dead = False  # the launch's /role dies
        self.control_orphaned = False  # journals a source refusal
        self.control_promoted = False  # promotes onto the field
        self.control_command = 'not_active'  # the gate's verdict
        self.control_signals = False   # /signals never answers
        self.control_watch_starves = False  # the control's monitor dies
        self.control_never_converges = False  # never tracks
        self.launch_fails = False      # a seat launch raises
        self.incumbent_launch_fails = False
        self.stage_fails = False       # the device server raises
        self.unstaged = False          # the rig stages no device server
        self.pair_moves = False        # the standby reports active
        self.pair_wedged = False       # the owner's tick freezes
        self.pair_moves_after_sweep = False  # the pair moves once the
        # leg's own claim is gone — the launch roles it must restore
        self.unsettled_pair = False    # the standby never tracks
        self.silent_rig = False        # every endpoint refuses
        self.teardown_keeps = frozenset()   # seats that outlive the
        # sweep — the rig's claim state the legs behind inherit
        self.device_stop_fails = False  # the device server survives
        self.sweeps = 0                # completed teardown sweeps

    # --- the durable journal the audit reads ------------------------

    def _journal(self, seat, event):
        records = self.written[seat]
        records.append({'entry': {'seq': len(records), 'tick': 10,
                                  'event': event}})

    def _write_journals(self):
        for seat, records in self.written.items():
            path = self.journals.get(seat)
            if not path:
                continue
            Path(path).write_text(
                ''.join(json.dumps(record) + '\n'
                        for record in records))

    # --- the runner's device and born-seat levers, faked ------------

    def start_device(self):
        self.calls.append(('start_sim_bus_device',))
        if self.unstaged:
            raise RuntimeError('the run config stages no sim-bus '
                               'device server — set sim_bus_device to '
                               'the device, port, and model fixture '
                               'the legs stage')
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self.device = self.ADDRESS
        # A relaunched device server's arbitration is fresh.
        self.claim = None
        return {'container': 'dcs-hw-qa-1-bus', 'address': self.ADDRESS,
                'port': 9005, 'device': 1, 'model': self.MODEL}

    def stop_device(self):
        self.calls.append(('stop_sim_bus_device',))
        if self.device_stop_fails:
            raise RuntimeError('docker rm failed: device is busy')
        self.sweeps += 1
        self.device = None
        self.claim = None

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document))
        if remote is not None or document != self.MODEL:
            raise AssertionError('a bus launch is document-addressed: '
                                 'remote=%r document=%r'
                                 % (remote, document))
        if self.launch_fails or (self.incumbent_launch_fails
                                 and seat == 'revised'):
            raise RuntimeError('docker run failed: name in use')
        self.written[seat] = []
        state = {'launched': True, 'exited': False, 'exit': None,
                 'logs': '', 'owns': False, 'tick': 0, 'reads': 0}
        self.seats[seat] = state
        if seat == 'revised':
            # The first controller — the live claim holder the refusal
            # must meet.
            if not self.incumbent_unclaimed_launch:
                self.claim = seat
                state['owns'] = True
            return self._launched(seat)
        if seat == 'foreign':
            # The reproduction's second launch — the same shape with
            # no declared pair. `predates` stages the recorded defect:
            # no conditional grant on the register protocol, so the
            # launch's unconditional claim preempts the live holder.
            if self.predates:
                self._preempt(seat)
            elif self.claim_despite_refusal:
                # The named verdict and the claim both land — the
                # contract's own breach, never a predating revision.
                self.claim = seat
                state['owns'] = True
                state['tick'] = 5
                state['logs'] = self.REFUSAL
                self._preempt(seat)
            elif self.standing:
                pass          # a claimless standby surface, no verdict
            elif self.refused_monitor_dead:
                pass          # running, but its monitor never comes up
            else:
                state['exited'] = True
                state['exit'] = 0 if self.exit_zero else 1
                state['logs'] = self._refusal_text()
                self._journal(seat, {'field_claim_observed': {
                    'point': 20,
                    'claimant': 424244 if self.wrong_claimant
                    else self.INCUMBENT_TOKEN}})
                if not self.unjournaled:
                    self._journal(seat, {'startup_claim_refused': {
                        'error': {'field_claim_failed': self.REFUSAL}}})
            return self._launched(seat)
        # The --standby control — the launch shape's rejoin half.
        if self.control_orphaned:
            self._journal(seat, {'tracking_source_refused': {
                'source': 'dcs-hw-qa-1-c:8082',
                'detail': 'no_tracking_source'}})
        return self._launched(seat)

    def _launched(self, seat):
        self._write_journals()
        return {'container': 'dcs-hw-' + seat, 'seat': seat,
                'address': 'dcs-hw-' + seat + ':8082', 'remote': None,
                'peer': None, 'standby': self._standby_of(seat),
                'model': self.MODEL,
                'monitor': 'http://' + self._host(seat)}

    def _standby_of(self, seat):
        return 'revised' if seat == 'driven' else None

    def _preempt(self, seat):
        """The second launch took the field: the incumbent's next
        write fences, its role demotes under the field-arbitration
        origin, and it stops being the claim holder."""
        self.claim = seat
        self.seats['foreign']['owns'] = True
        self.seats['foreign']['tick'] = 5
        incumbent = self.seats['revised']
        incumbent['owns'] = False
        self._journal('revised', {
            'role_changed': {'from': 'active', 'to': 'standby',
                             'origin': 'fenced'}})
        if self.incumbent_fenced:
            self._journal('revised', {
                'field_claim_lost': {'point': 20,
                                     'claimant': 424244}})

    def _refusal_text(self):
        """The exit log the named refusal writes — each flag drops one
        of the three sentences the verdict must carry."""
        if self.unnamed:
            return 'error: startup failed'
        if self.no_undeclared:
            return ("error: startup: a live peer holds the field's "
                    "write-ownership claim — relaunch with --standby "
                    "ADDRESS instead")
        if self.no_remedy:
            return ("error: startup: a live peer holds the field's "
                    "write-ownership claim — no --peer was declared, so "
                    "there is no pair to rejoin")
        return self.REFUSAL

    def _host(self, seat):
        for host, name in self.HOSTS.items():
            if name == seat:
                return host
        return 'ctrl-' + seat + ':9'

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if seat in self.teardown_keeps:
            return          # a seat the sweep could not remove
        if self.claim == seat:
            self.claim = None
            if self.seats.get(seat):
                self.seats[seat]['owns'] = False
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        state = self.seats.get(seat)
        if state is None or not state['launched']:
            return {'container': 'dcs-hw-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        if self.journal_vanishes and seat == 'foreign' \
                and state['exited']:
            Path(self.journals['foreign']).unlink(missing_ok=True)
        return {'container': 'dcs-hw-' + seat,
                'running': not state['exited'], 'exit': state['exit'],
                'logs': state['logs'], 'absent': False}

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _pair_role(self, host):
        self.reads[host] += 1
        reads = self.reads[host]
        if host == 'ctrl-a:1':
            if not (self.pair_wedged and reads > 1):
                self.tick += 1
            return {'role': 'active', 'tick': self.tick,
                    'field_claim': 'held'}
        if (self.pair_moves and reads > 1) \
                or (self.pair_moves_after_sweep and self.sweeps):
            return {'role': 'active', 'tick': self.tick,
                    'field_claim': 'held'}
        report = {'role': 'standby', 'tick': self.tick}
        report['sync'] = {'degraded': {'reason': 'unsettled'}} \
            if self.unsettled_pair \
            else {'tracking': {'aligned': self.tick}}
        return report

    def _seat_role(self, seat, state):
        state['reads'] += 1
        if state['owns']:
            if self.incumbent_unclaimed and state['reads'] > 1:
                return {'role': 'standby', 'sync': 'unsynchronized',
                        'field_claim': None, 'tick': state['tick']}
            return {'role': 'active', 'field_claim': 'held',
                    'tick': state['tick']}
        report = {'role': 'standby', 'tick': state['tick']}
        if self.control_never_converges:
            report['sync'] = 'unsynchronized'
        else:
            report['sync'] = {'tracking': {'aligned': self.tick}}
        if self.claim is not None:
            # A standby observing a live claim reads it held — the
            # incumbent's claim, not one of its own.
            report['field_claim'] = 'held'
        if self.control_promoted and seat == 'driven' \
                and state['reads'] > 1:
            report['role'] = 'active'
        return report

    def _advance(self):
        for seat, state in self.seats.items():
            if not state['launched'] or state['exited']:
                continue
            if self.incumbent_wedged and seat == 'revised':
                continue
            state['tick'] += 1

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
        if self.control_watch_starves and seat == 'driven':
            raise urllib.error.URLError('connection refused')
        if self.refused_monitor_dead and seat == 'foreign':
            raise urllib.error.URLError('connection refused')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, self._seat_role(seat, state)
        if (method, route) == ('GET', '/signals') and seat == 'driven':
            if self.control_signals:
                raise urllib.error.URLError('connection refused')
            return 200, {'points': [
                {'point': 10, 'direction': 'in', 'value_type': 'float',
                 'writable': True},
                {'point': 51, 'direction': 'in', 'value_type': 'bool',
                 'writable': True, 'name': 'pump-start-request'}]}
        if (method, route) == ('POST', '/command') and seat == 'driven':
            reason = self.control_command
            if reason is None:
                return 200, {'outcome': {'applied': {}}}
            return 200, {'outcome': {'rejected': {
                'reason': {reason: 'the run is not active'}}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class SimBusClaimRefusalTests(unittest.TestCase):
    """scenario_sim_bus_startup_claim_refusal against the stubbed rig:
    the second born-active over the live-held bus claim exits naming
    the refusal, the incumbent keeps its claim and its scan with no
    fenced demotion journaled, the `--standby` control converges
    tracking owning nothing, and the deployed pair never moves — two
    passes, identical digests. Each fault flag stages a named failure,
    a nondeterministic surface, or a pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.journals = {seat: str(self.root / (seat + '.jsonl'))
                         for seat in ('revised', 'foreign', 'driven')}
        self.feed = BusFeed(self.journals)

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'driven': 'http://ctrl-d:5',
                'evidence_dir': str(self.evidence),
                'journal_files': dict(self.journals),
                'plant_owner': {'active': 424240,
                                'standby': 424241,
                                'revised': BusFeed.INCUMBENT_TOKEN,
                                'foreign': 424244, 'driven': 424245},
                'start_sim_bus_device': feed.start_device,
                'stop_sim_bus_device': feed.stop_device,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'BUS_SETTLE', 1.5), \
                patch.object(scenarios, 'BUS_POLL', 0.001):
            return scenarios.scenario_sim_bus_startup_claim_refusal(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads((self.evidence
                           / ('sim-bus-startup-claim-refusal-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_sim_bus_startup_claim_refusal,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('sim-bus-startup-claim-refusal'),
                      scenarios.scenario_sim_bus_startup_claim_refusal)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_ownerless_remote_backoff),
            order.index(scenarios.scenario_sim_bus_startup_claim_refusal))
        self.assertLess(
            order.index(scenarios.scenario_sim_bus_startup_claim_refusal),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/sim-bus-startup-claim-refusal-pass-1.json',
             'evidence/sim-bus-startup-claim-refusal-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        first = self._pass(1)
        self.assertEqual(first['field']['address'], BusFeed.ADDRESS)
        # Both ends of the register protocol read one declaration.
        self.assertEqual(first['field']['mounted'], [BusFeed.MODEL])
        self.assertEqual(first['incumbent']['granted'],
                         {'role': 'active', 'field_claim': 'held',
                          'tick': 1})
        self.assertEqual(first['refused']['disposition'], 'refused')
        self.assertEqual(first['refused']['exit'], 1)
        self.assertTrue(first['refused']['named'])
        self.assertTrue(first['refused']['undeclared'])
        self.assertTrue(first['refused']['remedy'])
        self.assertTrue(first['refused']['journaled'])
        self.assertEqual(first['refused']['claimants'],
                         [BusFeed.INCUMBENT_TOKEN])
        self.assertEqual(first['incumbent']['after']['role'], 'active')
        self.assertFalse(first['incumbent']['fenced'])
        self.assertTrue(first['control']['converged'])
        self.assertEqual(first['control']['refused_command'], 'not_active')
        self.assertEqual(first['control']['source_refusals'], [])
        # The sweep is audited back over the rig: every born seat
        # proven gone, the device server's own removal clean, and the
        # deployed pair's launch roles undisturbed in the framing the
        # leg reads once its claim is gone.
        self.assertEqual(first['rig'],
                         {'seats': {'revised': True, 'foreign': True,
                                    'driven': True},
                          'device_error': None})
        self.assertEqual(first['roles']['final']['active']['role'],
                         'active')
        self.assertIs(first['roles']['final']['standby']['tracking'], True)
        self.assertEqual(
            first['digest'],
            {'incumbent': 'holds-and-scans', 'refused': 'exits-named',
             'audit': 'attributed', 'control': 'tracks-nothing-owned',
             'pair': 'held', 'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # Every pass ends torn down — the three seats and the device
        # server removed, so the rig's claim state is free and the
        # launch roles the next pass finds are the rig's own.
        self.assertFalse(self.feed.seats)
        self.assertIsNone(self.feed.device)
        self.assertIsNone(self.feed.claim)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_sim_bus_device'), 2)
        self.assertEqual(kinds.count('stop_sim_bus_device'), 2)
        self.assertEqual(kinds.count('start_born_controller'), 6)
        for seat in ('revised', 'foreign', 'driven'):
            self.assertIn(('stop_born_controller', seat),
                          self.feed.calls)

    def test_two_passes_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {path.name: path.read_bytes()
                 for path in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed = BusFeed(self.journals)
        ctx = self._ctx(feed)
        ctx['evidence_dir'] = str(evidence2)
        second_record = self.run_scenario(ctx, feed)
        self.assertEqual(second_record['outcome'], 'passed', second_record)
        second = {path.name: path.read_bytes() for path in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    # The doctored negatives — each named defect must fail the run by
    # the named diagnostic.

    def test_claim_despite_refusal_fails(self):
        # The issue's doctored negative: the refusal asserted while the
        # second launch claims the field and goes active.
        self.feed.claim_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        self.assertIn('refused-claimed',
                      str(self._pass(1)['violations']))
        report.validate_scenario(record)

    def test_clean_exit_fails(self):
        self.feed.exit_zero = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_exit_fails(self):
        self.feed.unnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_missing_pair_fails(self):
        self.feed.no_undeclared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_remedy_fails(self):
        self.feed.no_remedy = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_verdict_fails(self):
        self.feed.unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_claimant_fails(self):
        # The refusal names a standing claim the incumbent never took —
        # the field's attribution is the audit's subject.
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_lost_its_claim_fails(self):
        # The claim flipped: the incumbent no longer reports itself the
        # field's writer.
        self.feed.incumbent_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_fenced_demotion_fails(self):
        # The refused launch held the field, the preempting write
        # landed, and the incumbent journaled the fenced
        # `active → standby` demotion and the lost claim beside it.
        self.feed.claim_despite_refusal = True
        self.feed.incumbent_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        self.assertIn('incumbent', str(self._pass(1)['violations']))
        report.validate_scenario(record)

    def test_incumbent_scan_wedged_fails(self):
        self.feed.incumbent_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_orphaned_fails(self):
        # The `--standby` arm the defect orphaned: the control journals
        # a tracking-source refusal.
        self.feed.control_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_promoted_fails(self):
        self.feed.control_promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_admitted_command_fails(self):
        # The tracking standby's gate must stay closed — a command it
        # admits is a second writer.
        self.feed.control_command = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    # The instability the contract does not answer for must report
    # nondeterministic.

    def test_incumbent_never_claimed_is_nondeterministic(self):
        self.feed.incumbent_unclaimed_launch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_device_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unread_verdict_is_nondeterministic(self):
        # The second launch's process runs but its monitor never comes
        # up: the leg cannot read a verdict at all.
        self.feed.refused_monitor_dead = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standing_verdict_is_nondeterministic(self):
        # A stable claimless standby surface: the register protocol
        # answers its conditional grant, so nothing verdict-shaped
        # arrived.
        self.feed.standing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_unread_is_nondeterministic(self):
        # The refused launch's durable evidence is gone when the leg
        # goes to read it — instability, not a missing record.
        self.feed.journal_vanishes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_never_converged_is_nondeterministic(self):
        self.feed.control_never_converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_watch_starved_is_nondeterministic(self):
        self.feed.control_watch_starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_signals_unread_is_nondeterministic(self):
        # The control's SignalIndex never named a writable bool
        # in-point, so the leg had nothing to submit at the closed
        # gate — an unread surface, never a crossed one.
        self.feed.control_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertIsNone(self._pass(1)['control']['command_point'])
        report.validate_scenario(record)

    def test_pair_disturbance_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedge_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_moves_after_sweep_is_nondeterministic(self):
        # The deployed pair holds across the staging and moves only
        # once the leg's own claim is gone — the launch roles the leg
        # owes the rig behind it.
        self.feed.pair_moves_after_sweep = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertEqual(self._pass(1)['roles']['after']['standby']
                         ['role'], 'standby')
        self.assertEqual(self._pass(1)['roles']['final']['standby']
                         ['role'], 'active')
        report.validate_scenario(record)

    def test_rig_left_standing_is_nondeterministic(self):
        # A born seat the sweep could not remove is a claim the legs
        # behind this one would inherit.
        self.feed.teardown_keeps = frozenset({'driven'})
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertIs(self._pass(1)['rig']['seats']['driven'], False)
        report.validate_scenario(record)

    def test_device_left_serving_is_nondeterministic(self):
        # The device server's own removal failed: the register
        # protocol outlives the sweep under the leg's device name.
        self.feed.device_stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertIsNotNone(self._pass(1)['rig']['device_error'])
        report.validate_scenario(record)

    def test_diverging_digests_fail(self):
        # Both passes audit clean, yet their normalized digests differ —
        # the determinism contract's own failure, staged at the digest
        # seam the leg compares.
        digests = iter([
            {'incumbent': 'holds-and-scans', 'refused': 'exits-named',
             'audit': 'attributed', 'control': 'tracks-nothing-owned',
             'pair': 'held', 'rig': 'restored'},
            {'incumbent': 'holds-and-scans', 'refused': 'exits-named',
             'audit': 'attributed', 'control': 'defect',
             'pair': 'held', 'rig': 'restored'}])
        with patch.object(scenarios, '_bus_digest',
                          lambda record, violations: next(digests)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    # The pre-contract and unstageable surfaces must report
    # inconclusive.

    def test_predates_contract_is_inconclusive(self):
        # The recorded defect: the second born-active's unconditional
        # claim preempts the live holder and goes active — no named
        # verdict exists on this revision.
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        # The pass is recorded without a digest: the leg declined to
        # judge it, so publishing a verdict set would misreport it.
        self.assertNotIn('digest', self._pass(1))
        self.assertEqual(self._pass(1)['refused']['disposition'],
                         'claimed')
        report.validate_scenario(record)

    def test_unstaged_device_is_inconclusive(self):
        # The run config stages no device server — an absent
        # capability, not a flake.
        self.feed.unstaged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stages no sim-bus device server',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_sim_bus_device', 'stop_sim_bus_device',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sim-bus staging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unpinned_token_is_inconclusive(self):
        # No pinned --owner-token for the incumbent seat: the
        # refusal's attributed claimant could not be audited against a
        # live holder, so the leg declines rather than passing an
        # unaudited attribution.
        ctx = self._ctx()
        ctx['plant_owner'] = {'revised': None, 'foreign': 424244,
                              'driven': 424245}
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('--owner-token', record.get('detail', ''))
        self.assertEqual(self.feed.calls, [])
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
        with patch.object(scenarios, '_judge_bus',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-claim-refusal-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._bus_self_check(), [])