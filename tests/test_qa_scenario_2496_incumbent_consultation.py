"""The 2496_incumbent_consultation leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_incumbent_consultation, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import importlib
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam

# The leg module itself: its diagnostic names and its private helpers
# are read through the module, never through the package facade — a
# sibling leg binds the same generic names and the facade's re-export
# keeps only the last of each.
LEG = importlib.import_module(
    'qa_lane.scenarios.2496_incumbent_consultation')


EXPECTED_CASES = frozenset({
    'ConsultationGateTests.test_registered_after_keyed_announce_before_revisions',
    'ConsultationGateTests.test_fixed_shape_passes_validates_and_restores',
    'ConsultationGateTests.test_two_runs_produce_identical_evidence',
    'ConsultationGateTests.test_evidence_names_both_halves',
    'ConsultationGateTests.test_pre_gate_release_is_inconclusive',
    'ConsultationGateTests.test_pre_gate_release_digests_unrecorded',
    'ConsultationGateTests.test_vanished_container_is_inconclusive',
    'ConsultationGateTests.test_refusal_never_settles_fails',
    'ConsultationGateTests.test_zero_exit_fails',
    'ConsultationGateTests.test_unnamed_refusal_fails',
    'ConsultationGateTests.test_unnamed_remedy_fails',
    'ConsultationGateTests.test_unnamed_verdict_fails',
    'ConsultationGateTests.test_unjournaled_consultation_fails',
    'ConsultationGateTests.test_adopted_instead_of_refused_fails',
    'ConsultationGateTests.test_nonowning_verdict_record_fails',
    'ConsultationGateTests.test_incumbent_demotion_fails',
    'ConsultationGateTests.test_incumbent_stall_fails',
    'ConsultationGateTests.test_rolled_back_tunes_fail',
    'ConsultationGateTests.test_incumbent_never_stopped_fails',
    'ConsultationGateTests.test_seize_refused_fails',
    'ConsultationGateTests.test_seizing_exit_fails',
    'ConsultationGateTests.test_missing_takeover_record_fails',
    'ConsultationGateTests.test_misstated_baseline_fails',
    'ConsultationGateTests.test_baseline_not_lagging_fails',
    'ConsultationGateTests.test_unsettled_tune_fails',
    'ConsultationGateTests.test_unrestored_pair_fails',
    'ConsultationGateTests.test_restart_failure_is_nondeterministic',
    'ConsultationGateTests.test_second_pass_defect_fails',
    'ConsultationGateTests.test_no_keyed_pair_is_inconclusive',
    'ConsultationGateTests.test_probe_pair_is_the_subject',
    'ConsultationGateTests.test_missing_levers_are_inconclusive',
    'ConsultationGateTests.test_missing_journals_are_inconclusive',
    'ConsultationGateTests.test_unsettled_pair_is_inconclusive',
    'ConsultationGateTests.test_unnameable_incumbent_is_inconclusive',
    'ConsultationGateTests.test_untunable_model_is_inconclusive',
    'ConsultationGateTests.test_unchecked_self_check_fails',
    'ConsultationGateTests.test_judge_self_check_is_complete',
})


class _Peer:
    """One pair member's staged state: its role, its served tick, the
    claim it holds, and its container's process verdict."""

    def __init__(self, name):
        self.name = name
        self.role = 'active' if name == 'active' else 'standby'
        self.holds = name == 'active'
        self.tick = 0
        self.down = False
        self.exit = None
        self.logs = ''
        self.resume_tick = 0
        self.resume_attempts = 0


class ConsultationFeed:
    """A stubbed keyed pair for the incumbent-consultation leg.

    `active` (ctrl-a) is the launched active: no `--peer`, so the
    address its restart consults is whatever its own persisted
    checkpoint stamps — the tracking source its demotion resolved,
    here ctrl-b's in-container monitor. `standby` (ctrl-b) is the
    tracking member. Every member transition is staged by the leg's
    own lever calls — never wall-clock — so two passes on a fresh feed
    emit identical evidence.

    A ctrl-a restart reads the field's live state the way the gate
    does: while ctrl-b is up and owns the claim the consultation
    answers `live_incumbent`, the startup claim is refused, and the
    refusal lands on stderr naming the consulted endpoint and the
    `--standby` remedy, journaled beside the consultation ledger; with
    ctrl-b down the consultation is `unanswered`, the claim seizes,
    and the seizure journals the recorded takeover evidence. Fault
    flags stage each named failure, the pre-gate shape, and each
    inconclusive."""
    MEMBERS = ('active', 'standby')
    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby'}
    CONSULTED = '10.9.9.2:8081'
    TOKENS = {'active': 424243, 'standby': 424244}
    COMPONENT = 'pump-group-1'
    PARAM = 'start_level'
    WAS = 0.4
    TUNED = 0.9
    GENERATION = 4
    REFUSAL_LOGS = ("error: field write-ownership claim failed: the "
                    "incumbent consultation at " + CONSULTED
                    + " reports a live field owner — a restart cannot "
                      "preempt a reachable incumbent; relaunch with "
                      "--standby ADDRESS to rejoin as its tracking "
                      "standby instead")
    CLAIM_REFSUSAL_LOGS = ("error: field write-ownership claim failed: "
                           "a live peer holds the field's "
                           "write-ownership claim — relaunch with "
                           "--standby ADDRESS to rejoin as the "
                           "incumbent's tracking standby instead")

    def __init__(self, root, keyed=True):
        self.root = Path(root)
        self.journals = {name: self.root / 'controllers' / name
                         / 'journal.jsonl' for name in self.MEMBERS}
        self.states = {name: self.root / 'controllers' / name
                       / 'state.json' for name in self.MEMBERS}
        self.keyed = keyed
        self.peers = {name: _Peer(name) for name in self.MEMBERS}
        self.tuned = self.WAS
        self.attempts = {'active': 0, 'standby': 0}
        self.receipts = []
        self.pending = set()
        self.runs = {name: 0 for name in self.MEMBERS}
        self.seq = {name: 0 for name in self.MEMBERS}
        self.starts = 0
        self.restarts = 0
        self.stalled = False
        self.calls = []
        self.vanished = False
        # Fault injection for the named-failure, pre-gate, and
        # inconclusive cases.
        self.restart_fails = False      # the restart lever raises
        self.refusal_vanishes = False   # the refused container vanishes
        self.refusal_runs = False       # the refusal never settles
        self.refusal_exit_zero = False
        self.unnamed_refusal = False
        self.unnamed_remedy = False
        self.unnamed_verdict = False
        self.no_refusal_record = False
        self.adopts = False             # adopt-then-claim instead
        self.nonowning_verdict = False
        self.incumbent_demoted = False  # the refusal disturbs the pair
        self.incumbent_stalls = False
        self.tunes_rolled_back = False
        self.incumbent_survives = False  # ctrl-b never goes down
        self.no_seize = False
        self.seizing_exit = False
        self.no_takeover_record = False
        self.misstated_baseline = False
        self.baseline_not_lagging = False
        self.tune_unsettled = False
        self.restore_refused = False
        self.no_tunable = False
        self.second_pass_defect = False
        for name in self.MEMBERS:
            self._boundary(name, 0)
            self._persist(name)

    # --- the journal and state files the leg reads ------------------

    def _boundary(self, name, tick):
        path = self.journals[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.runs[name] += 1
        path.write_text(json.dumps({'run_boundary': {
            'run': self.runs[name], 'tick': tick}}) + '\n')
        self.seq[name] = 0

    def _journal(self, name, event):
        path = self.journals[name]
        self.seq[name] += 1
        with path.open('a') as stream:
            stream.write(json.dumps({'entry': {
                'seq': self.seq[name],
                'tick': self.peers[name].tick,
                'event': event}}) + '\n')

    def _persist(self, name):
        path = self.states[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        peer = self.peers[name]
        tick = peer.tick
        if self.baseline_not_lagging and name == 'active':
            tick = peer.tick + 10
        path.write_text(json.dumps({
            'tick': tick, 'generation': self.GENERATION,
            'command_admission': {
                'attempts': self.attempts[name],
                'full_rejections': 0, 'high_water': self.attempts[name]},
            'receipts': [], 'internal': {},
            'tracking_source': self.CONSULTED
            if name == 'active' else None,
            'source_owns_field': bool(peer.holds)}, indent=1))

    # --- the runner's lifecycle levers, faked ----------------------

    def restart_controller(self, name):
        self.calls.append(('restart_controller', name))
        self.restarts += 1
        if self.restart_fails:
            raise RuntimeError('docker start failed: driver already '
                               'running')
        self._stop(name)
        self._start(name)

    def stop_controller(self, name):
        self.calls.append(('stop_controller', name))
        if name == 'standby' and self.incumbent_survives:
            return
        self._stop(name)

    def start_controller(self, name):
        self.calls.append(('start_controller', name))
        if name == 'standby' and self.restore_refused \
                and self.peers['active'].role == 'active' \
                and not self.peers['active'].down:
            raise RuntimeError('docker start failed: name in use')
        self._start(name)

    def controller_state(self, name):
        self.calls.append(('controller_state', name))
        peer = self.peers[name]
        if self.refusal_vanishes and name == 'active' \
                and peer.exit is not None and not self.vanished:
            self.vanished = True
            return {'container': 'dcs-hw-qa-1-' + name,
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        running = not peer.down
        if name == 'standby' and self.incumbent_survives \
                and self.peers['active'].down:
            running = True
        return {'container': 'dcs-hw-qa-1-' + name,
                'running': running, 'exit': peer.exit,
                'logs': peer.logs, 'absent': False}

    def _stop(self, name):
        peer = self.peers[name]
        peer.resume_tick = peer.tick
        peer.resume_attempts = self.attempts[name]
        peer.down = True
        peer.exit = None
        peer.logs = ''
        self._persist(name)

    def _start(self, name):
        peer = self.peers[name]
        peer.down = False
        peer.exit = None
        peer.logs = ''
        self._boundary(name, peer.resume_tick)
        if name == 'standby':
            # The tracking member resumes behind the field's owner.
            peer.role = 'standby'
            peer.holds = False
            return
        self.starts += 1
        incumbent_live = (not self.peers['standby'].down
                          and self.peers['standby'].role == 'active')
        if self.refusal_runs and not self.stalled:
            # The named instability: the refused start journals its
            # consultation record and then never settles a verdict
            # inside the bound.
            self.stalled = True
            self._refuse_record('live_incumbent')
            return
        if incumbent_live:
            self._refuse(peer)
            return
        peer.role = 'active'
        peer.holds = True
        if self.no_seize:
            peer.role = 'standby'
            peer.holds = False
            return
        if self.seizing_exit:
            peer.down = True
            peer.exit = 1
            peer.logs = 'error: the run ended after the claim'
            return
        self._seize(peer)

    def _refuse(self, peer):
        """The live incumbent's consultation refuses the startup claim:
        the refusal on stderr, and the consultation ledger journaled
        beside the baseline the restart resumed and the refused grant."""
        peer.role = 'standby'
        peer.holds = False
        if self.incumbent_demoted:
            incumbent = self.peers['standby']
            incumbent.role = 'standby'
            incumbent.holds = False
        peer.down = True
        peer.exit = 0 if self.refusal_exit_zero else 1
        if self.unnamed_refusal:
            peer.logs = ('error: the startup claim was refused; '
                         'relaunch with --standby ADDRESS')
        elif self.unnamed_remedy:
            peer.logs = ('error: field write-ownership claim failed: '
                         'the incumbent consultation at '
                         + self.CONSULTED + ' reports a live owner')
        elif self.unnamed_verdict:
            peer.logs = self.CLAIM_REFSUSAL_LOGS
        else:
            peer.logs = self.REFUSAL_LOGS
        self._refuse_record(
            'adopted' if self.adopts
            else 'does_not_own' if self.nonowning_verdict
            else 'live_incumbent')

    def _refuse_record(self, verdict):
        """The consultation ledger the refused start journals beside
        the baseline the restart resumed and the refused grant."""
        if self.no_refusal_record:
            return
        self._journal('active', {'restart_consultation': {
            'source': self.CONSULTED,
            'resumed': self._baseline(self.peers['active']),
            'consultations': [{'address': self.CONSULTED,
                               'verdict': verdict}],
            'grant': {'verdict': 'refused'}}})

    def _baseline(self, peer):
        return {'tick': peer.resume_tick, 'generation': self.GENERATION,
                'command_admission': {
                    'attempts': peer.resume_attempts}}

    def _seize(self, peer):
        """The dead incumbent's consultation is unanswered, so the
        startup claim seizes — and the claiming line journals the
        recorded takeover evidence."""
        if self.incumbent_demoted:
            self.peers['standby'].role = 'standby'
            self.peers['standby'].holds = False
        if (self.no_takeover_record
                or (self.second_pass_defect and self.starts > 3)):
            return
        baseline = self._baseline(peer)
        if self.misstated_baseline:
            baseline['generation'] = self.GENERATION + 5
        if self.baseline_not_lagging:
            baseline['tick'] = peer.tick + 10
        self._journal('active', {'startup_takeover': {
            'resumed': baseline,
            'consultations': [{'address': self.CONSULTED,
                               'verdict': 'unanswered'}],
            'grant': 'granted'}})

    # --- the monitor channel ---------------------------------------

    def _advance(self):
        """One scan for every running member — the request boundary is
        the tick boundary, so served ticks are call-count
        deterministic — plus the owner's admission boundary."""
        for name, peer in self.peers.items():
            if peer.down or self.incumbent_survives and name == 'standby' \
                    and self.peers['active'].down:
                continue
            if self.incumbent_stalls and name == 'standby':
                continue
            peer.tick += 1
            if name == 'active':
                self._persist(name)
            elif peer.role == 'active':
                for index in list(self.pending):
                    receipt = self.receipts[index]
                    if 'accepted' in receipt['outcome']:
                        if not self.tune_unsettled:
                            receipt['outcome'] = {
                                'applied': {'tick': peer.tick}}
                            self._journal(
                                'standby',
                                {'command_settled': {'receipt':
                                                     dict(receipt)}})
                    if 'applied' in receipt['outcome'] \
                            and not self.tunes_rolled_back:
                        self.tuned = receipt['command']['set_parameter'][
                            'value']['float']
                    self.pending.discard(index)

    def _report(self, name):
        peer = self.peers[name]
        report = {'role': peer.role, 'tick': peer.tick}
        if peer.role == 'active':
            report['field_claim'] = 'held'
        elif peer.holds is False and peer.role == 'standby':
            report['field_claim'] = 'held' if any(
                other.holds for other in self.peers.values()) else None
        if peer.role == 'standby':
            report['sync'] = {'tracking': {'aligned': peer.tick}}
        return report

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = self.HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        peer = self.peers[name]
        self._advance()
        if peer.down:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'standby'
            peer.holds = False
            self._journal(name, {'role_changed': {'from': 'active',
                                                  'to': 'demoting'}})
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            peer.role = 'active'
            peer.holds = True
            self._journal(name, {'role_changed': {'from': 'standby',
                                                  'to': 'promoting'}})
            return 200, {'role': 'promoting'}
        if (method, route) == ('GET', '/role'):
            return 200, self._report(name)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {
                'receipts': [dict(entry) for entry in self.receipts],
                'command_admission': {
                    'attempts': self.attempts['standby'],
                    'full_rejections': 0,
                    'high_water': self.attempts['standby']},
                'tracking_source': self.CONSULTED
                if name == 'active' else None,
                'source_owns_field': bool(peer.holds)}
        if (method, route) == ('GET', '/schema'):
            if self.no_tunable:
                return 200, {'interfaces': []}
            return 200, {'interfaces': [{
                'name': self.COMPONENT,
                'interface': {'configuration': [{
                    'name': self.PARAM, 'kind': 'float',
                    'capability': 'tunable',
                    'range': {'min': {'float': 0.1},
                              'max': {'float': 1.0}}}]}}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {
                'tick': peer.tick,
                'parameters': [{'name': self.COMPONENT,
                                'values': {self.PARAM: {
                                    'float': self.tuned}}}],
                'points': []}
        if (method, route) == ('GET', '/receipts'):
            return 200, [dict(entry) for entry in self.receipts]
        if (method, route) == ('POST', '/command'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'index': self.attempts['standby'],
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            self.attempts['standby'] += 1
            self.pending.add(len(self.receipts))
            self.receipts.append(receipt)
            return 200, dict(receipt)
        raise AssertionError('unexpected request %s %s' % (method, url))


class ConsultationGateTests(unittest.TestCase):
    """scenario_incumbent_consultation against the stubbed pair: the
    feed's transitions are lever-call keyed so each pass emits
    identical evidence, and every fault flag stages a named acceptance
    failure, a pre-gate shape that must report inconclusive, or an
    instability the contract does not answer for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ConsultationFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None, keyed=True, **overrides):
        feed = feed or self.feed
        ctx = {
            'active': 'http://ctrl-a:1',
            'standby': 'http://ctrl-b:2',
            'evidence_dir': str(self.evidence),
            'pair_token': 'dcs-qa-pair' if keyed else None,
            'probe': None,
            'journal_files': {name: str(feed.journals[name])
                              for name in ConsultationFeed.MEMBERS},
            'state_files': {name: str(feed.states[name])
                            for name in ConsultationFeed.MEMBERS},
            'restart_controller': feed.restart_controller,
            'stop_controller': feed.stop_controller,
            'start_controller': feed.start_controller,
            'controller_state': feed.controller_state}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CONSULT_SETTLE', 2.0), \
                patch.object(scenarios, 'CONSULT_POLL', 0.001):
            return scenarios.scenario_incumbent_consultation(
                ctx or self._ctx(feed))

    def _read(self, ref):
        return json.loads((self.evidence.parent / ref).read_text())

    def test_registered_after_keyed_announce_before_revisions(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_keyed_announced_source),
            order.index(scenarios.scenario_incumbent_consultation))
        self.assertLess(
            order.index(scenarios.scenario_incumbent_consultation),
            order.index(scenarios.scenario_incompatible_revision))
        self.assertLess(
            order.index(scenarios.scenario_incumbent_consultation),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_restores(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/incumbent-consultation-pass-1.json',
             'evidence/incumbent-consultation-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Each pass switches the field once onto ctrl-b, stops and
        # restarts ctrl-a twice, and brings ctrl-b back.
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('restart_controller'), 2)
        self.assertEqual(kinds.count('stop_controller'), 4)
        self.assertEqual(kinds.count('start_controller'), 4)
        for name in ConsultationFeed.MEMBERS:
            self.assertFalse(self.feed.peers[name].down, name)
        self.assertEqual(self.feed.peers['active'].role, 'active')
        self.assertEqual(self.feed.peers['standby'].role, 'standby')

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = ConsultationFeed(second_tmp.name)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_evidence_names_both_halves(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        evidence = self._read(record['evidence'][0]['ref'])
        staged = evidence['record']
        self.assertEqual(staged['consulted'], ConsultationFeed.CONSULTED)
        self.assertTrue(staged['refusal']['verdict'])
        self.assertEqual([name for name, _facts
                          in staged['refusal_records']],
                         ['restart_consultation'])
        self.assertEqual([name for name, _facts
                          in staged['takeover_records']],
                         ['startup_takeover'])
        self.assertEqual(staged['takeover']['grant'], 'granted')
        self.assertEqual(staged['takeover']['ledger'],
                         [[ConsultationFeed.CONSULTED, 'unanswered']])
        self.assertEqual(len(staged['tunes']), 2)
        self.assertTrue(all(tune['settled'] == 'applied'
                            for tune in staged['tunes']))
        self.assertEqual(staged['incumbent_tuned'],
                         staged['tune_target']['tuned'])
        self.assertTrue(staged['restored']['settled'])
        self.assertEqual(evidence['digest'], {
            'live-incumbent': 'refused-named',
            'incumbent': 'undisturbed',
            'unreachable-incumbent': 'seized',
            'takeover-evidence': 'baseline+ledger+grant',
            'resumed-baseline': 'lagging',
            'pair-restored': 'settled'})
        self.assertEqual(evidence['violations'], {})

    # The pre-gate release: the restart consult is journaled as one
    # source and a prose detail, and the granted startup claim carries
    # no takeover record — the staged revision cannot be judged.

    def test_pre_gate_release_is_inconclusive(self):
        self.feed.no_refusal_record = True
        self.feed.no_takeover_record = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates decision 98', record['detail'])
        self.assertIn('neither a consultation ledger', record['detail'])
        self.assertIn('a takeover record carrying the baseline',
                      record['detail'])
        report.validate_scenario(record)

    def test_pre_gate_release_digests_unrecorded(self):
        self.feed.no_refusal_record = True
        self.feed.no_takeover_record = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        evidence = self._read(record['evidence'][0]['ref'])
        self.assertEqual(evidence['digest']['live-incumbent'],
                         'unrecorded')
        self.assertEqual(evidence['digest']['takeover-evidence'],
                         'unrecorded')
        self.assertEqual(evidence['digest']['unreachable-incumbent'],
                         'unrecorded')
        self.assertEqual(evidence['digest']['incumbent'], 'undisturbed')

    def test_vanished_container_is_inconclusive(self):
        # The container read is a dropped sample, never a verdict: the
        # evidence is collected and the pass reports what it holds.
        self.feed.refusal_vanishes = True
        record = self.run_scenario()
        self.assertIn(record['outcome'], ('failed', 'inconclusive'),
                      record)
        evidence = self._read(record['evidence'][0]['ref'])
        self.assertEqual(evidence['violations'].get('refusal-vanished'),
                         LEG.DIAG_NONDET)

    # The doctored negatives — each wrong disposition must fail the
    # run by the named diagnostic.

    def _fails(self, fragment):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn(LEG.DIAG_FAILED, record['detail'])
        self.assertIn(fragment, record['detail'])
        report.validate_scenario(record)
        return record

    def test_refusal_never_settles_fails(self):
        # The refusal that never lands inside the bound is the
        # instability the contract does not answer for.
        self.feed.refusal_runs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn(LEG.DIAG_NONDET, record['detail'])
        self.assertIn('never settled a verdict', record['detail'])

    def test_zero_exit_fails(self):
        self.feed.refusal_exit_zero = True
        self._fails('exited zero')

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed_refusal = True
        self._fails('never named the startup claim')

    def test_unnamed_remedy_fails(self):
        self.feed.unnamed_remedy = True
        self._fails('never named the relaunch remedy')

    def test_unnamed_verdict_fails(self):
        self.feed.unnamed_verdict = True
        self._fails("not the consultation's verdict")

    def test_unjournaled_consultation_fails(self):
        self.feed.no_refusal_record = True
        self._fails('journaled no consultation ledger')

    def test_adopted_instead_of_refused_fails(self):
        self.feed.adopts = True
        self._fails('adopted the incumbent')

    def test_nonowning_verdict_record_fails(self):
        self.feed.nonowning_verdict = True
        self._fails('reports no incumbent for the address it asked')

    def test_incumbent_demotion_fails(self):
        self.feed.incumbent_demoted = True
        self._fails('lost the field to the refused restart')

    def test_incumbent_stall_fails(self):
        self.feed.incumbent_stalls = True
        self._fails('stopped scanning')

    def test_rolled_back_tunes_fail(self):
        self.feed.tunes_rolled_back = True
        self._fails('applied tune')

    def test_incumbent_never_stopped_fails(self):
        self.feed.incumbent_survives = True
        self._fails('never went down')

    def test_seize_refused_fails(self):
        self.feed.no_seize = True
        self._fails('never seized the field')

    def test_seizing_exit_fails(self):
        self.feed.seizing_exit = True
        self._fails('exited instead of holding the field')

    def test_missing_takeover_record_fails(self):
        # Half the recorded evidence landed: the refusal's ledger is
        # there, so the gate is present and the missing takeover record
        # is a contract miss rather than a pre-gate absence.
        self.feed.no_takeover_record = True
        self._fails('journaled no takeover record')

    def test_misstated_baseline_fails(self):
        self.feed.misstated_baseline = True
        self._fails('resumed generation reads')

    def test_baseline_not_lagging_fails(self):
        self.feed.baseline_not_lagging = True
        self._fails('not behind the incumbent')

    def test_unsettled_tune_fails(self):
        self.feed.tune_unsettled = True
        self._fails('never settled applied')

    def test_unrestored_pair_fails(self):
        self.feed.restore_refused = True
        self._fails('not restored to its launch layout')

    def test_restart_failure_is_nondeterministic(self):
        self.feed.restart_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertEqual(record['detail'].split(':')[0],
                         LEG.DIAG_NONDET)

    def test_second_pass_defect_fails(self):
        self.feed.second_pass_defect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled no takeover record', record['detail'])
        self.assertEqual(
            sorted(entry['ref'] for entry in record['evidence']),
            ['evidence/incumbent-consultation-pass-1.json',
             'evidence/incumbent-consultation-pass-2.json'])

    # The inconclusive shapes — absent capability, absent evidence, an
    # unstaged rig.

    def test_no_keyed_pair_is_inconclusive(self):
        record = self.run_scenario(self._ctx(keyed=False))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no keyed pair', record['detail'])

    def test_probe_pair_is_the_subject(self):
        # The deployed pair unkeyed: the staged keyed probe pair is the
        # subject, and the leg drives its own members.
        probe = self._ctx(keyed=False)
        probe['probe'] = dict(probe, pair_token='dcs-qa-pair')
        record = self.run_scenario(probe)
        self.assertEqual(record['outcome'], 'passed', record)

    def test_missing_levers_are_inconclusive(self):
        ctx = self._ctx()
        del ctx['restart_controller']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no controller-lifecycle levers', record['detail'])

    def test_missing_journals_are_inconclusive(self):
        ctx = self._ctx()
        ctx['journal_files'] = {'active': ctx['journal_files']['active']}
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no per-member journal files', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.peers['standby'].role = 'active'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not in the settled launch shape', record['detail'])

    def test_unnameable_incumbent_is_inconclusive(self):
        # The restartee names no incumbent address — decision 98's
        # honest residual hole, never a contract miss.
        def without_source(*args, **kwargs):
            return 200, {'command_admission': {'attempts': 0},
                         'receipts': []}
        feed = self.feed
        original = feed.http_json

        def patched(method, url, body=None, timeout=10):
            if method == 'GET' and url.endswith('/checkpoint') \
                    and '/ctrl-a:1/' in url:
                return without_source()
            return original(method, url, body, timeout)

        with patch.object(scenarios, 'http_json', patched), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CONSULT_SETTLE', 2.0), \
                patch.object(scenarios, 'CONSULT_POLL', 0.001):
            record = scenarios.scenario_incumbent_consultation(
                self._ctx(feed))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('names no incumbent checkpoint stream',
                      record['detail'])
        self.assertIn('residual hole', record['detail'])

    def test_untunable_model_is_inconclusive(self):
        self.feed.no_tunable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no descriptor-declared Float parameter',
                      record['detail'])

    # The unchecked-diagnostic self-check.

    def test_unchecked_self_check_fails(self):
        with patch.object(LEG, '_consult_self_check',
                          lambda: ['refusal-exit-zero']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn(LEG.DIAG_UNCHECKED, record['detail'])
        self.assertIn('refusal-exit-zero', record['detail'])

    def test_judge_self_check_is_complete(self):
        self.assertEqual(LEG._consult_self_check(), [])
        # Every planted negative names a diagnostic the leg's own
        # narrative documents.
        self.assertEqual(LEG.DIAG_FAILED,
                         'incumbent-consultation-failed')
        self.assertEqual(LEG.DIAG_NONDET,
                         'incumbent-consultation-nondeterministic')
        self.assertEqual(LEG.DIAG_UNCHECKED,
                         'incumbent-consultation-unchecked')