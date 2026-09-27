"""The 3680_dynamics_admission_refusal leg's scenario unit coverage —
the feed fake, the stubbed doctored-document lever, and the TestCase
class for scenario_dynamics_admission_refusal, per the module-per-leg
test convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DynamicsAdmissionTests.test_registered_in_scenarios',
    'DynamicsAdmissionTests.test_clean_rig_passes_and_validates',
    'DynamicsAdmissionTests.test_two_passes_share_one_digest',
    'DynamicsAdmissionTests.test_two_runs_produce_identical_records',
    'DynamicsAdmissionTests.test_scenario_ctx_carries_the_lever',
    'DynamicsAdmissionTests.test_missing_lever_is_inconclusive',
    'DynamicsAdmissionTests.test_unreachable_rig_is_inconclusive',
    'DynamicsAdmissionTests.test_unsettled_pair_is_inconclusive',
    'DynamicsAdmissionTests.test_no_active_is_failed',
    'DynamicsAdmissionTests.test_predated_tooling_is_inconclusive',
    'DynamicsAdmissionTests.test_failed_lever_is_inconclusive',
    'DynamicsAdmissionTests.test_no_out_points_is_inconclusive',
    'DynamicsAdmissionTests.test_refused_claim_is_inconclusive',
    'DynamicsAdmissionTests.test_check_accepts_malformed_fails',
    'DynamicsAdmissionTests.test_serve_accepts_malformed_fails',
    'DynamicsAdmissionTests.test_panic_at_check_fails',
    'DynamicsAdmissionTests.test_unnamed_refusal_fails',
    'DynamicsAdmissionTests.test_served_listener_fails',
    'DynamicsAdmissionTests.test_honest_control_refused_fails',
    'DynamicsAdmissionTests.test_plant_silent_fails',
    'DynamicsAdmissionTests.test_stomped_command_fails',
    'DynamicsAdmissionTests.test_rewound_field_fails',
    'DynamicsAdmissionTests.test_moved_kind_fails',
    'DynamicsAdmissionTests.test_frozen_field_fails',
    'DynamicsAdmissionTests.test_step_refused_fails',
    'DynamicsAdmissionTests.test_moved_roles_restore_and_fail',
    'DynamicsAdmissionTests.test_swapped_launch_layout_restores',
    'DynamicsAdmissionTests.test_silenced_judges_report_unchecked',
})


class AdmissionPlantPeer(ClaimPlantPeer):
    """The dynamics-admission leg's plant half: a bound-point census
    the leg composes its doctored documents against — a float
    in-point (the self-point class's target and every threshold's
    gate input), a bool in-point (the honest control's driven
    contact), and two bool out-points (the controller-owned command
    targets whose stored values the intactness audit compares to the
    owner's commanded values). The standing writer claim is
    pre-seeded with a live holder so the leg's shared-claim probes
    answer `claimed_shared`. Doctor flags stage each field defect
    the leg's diagnostics name: `poisoned` drops every request (the
    poisoned-mutex shape), `refuse_claim` fences the shared
    attachment, `fence_steps` fences a live holder's dt:0 step."""

    def __init__(self, owner):
        super().__init__()
        self.samples = {
            10: {'value': {'float': 0.8}, 'quality': 'good',
                 'tick': 0},
            40: {'value': {'bool': False}, 'quality': 'good',
                 'tick': 0},
            100: {'value': {'bool': True}, 'quality': 'good',
                  'tick': 0},
            101: {'value': {'bool': False}, 'quality': 'good',
                  'tick': 0}}
        self.directions = {10: 'in', 40: 'in', 100: 'out', 101: 'out'}
        self.claim = {'owner': owner, 'holders': {-1}}
        self.shared_conns = set()
        # Doctor flags for the named-failure cases.
        self.poisoned = False     # every request dies mid-answer
        self.fence_steps = False  # a live holder's step meets fenced

    def ctl(self, *args):
        """The ctx['plant_ctl'] seam plus the `ping` subcommand the
        shipped tool covers — the field liveness mark the leg reads
        across the refused loads."""
        if args == ('ping',):
            if self.poisoned:
                return _ctl_process(stderr='connection reset',
                                    returncode=1)
            return _ctl_process({'result': 'alive',
                                 'tick': self.plant_tick})
        return super().ctl(*args)

    def _respond(self, conn, request):
        if self.poisoned:
            raise ConnectionError('the field stopped answering')
        if self.fence_steps and request.get('op') == 'step':
            with self.lock:
                self.requests.append(request.get('op'))
                return self._fenced()
        return super()._respond(conn, request)


class AdmissionFeed:
    """A stubbed pair for the dynamics-admission scenario: ctrl-a
    owns the field, ctrl-b tracks it. Every monitor call is one
    completed scan stepping the field, and the admit_dynamics lever
    stands the harness's doctored-document staging — the preflight
    gate answering 'check ok' for the honest control and exit-1
    'dynamics element <i> (driving point <p>)' refusals for each
    malformed class, the spawned-load gate answering a bound
    listener for an admitted document and the same named refusal
    for a refused one. The owner's commanded Out values ride
    /snapshot; doctor flags stage each named defect the leg's
    diagnostics cover."""

    def __init__(self, plant):
        self.plant = plant
        self.a_role = 'active'
        self.b_role = 'standby'
        self.a_tracking = False
        self.b_tracking = True
        self.commanded = {100: True, 101: False}
        self.calls = []
        # Doctor flags for the named-failure cases.
        self.pair_down = False       # the rig is unreachable
        self.predates = False        # the tooling lacks
                                     # --check-dynamics
        self.lever_raises = False    # the lever's staging fails
        self.accept_check = False    # the preflight admits malformed
        self.accept_serve = False    # the spawned load keeps serving
        self.panics = False          # the refusal unwinds instead
        self.unnamed = False         # the refusal names no element
        self.listening = False       # refused, yet the listener bound
        self.honest_refused = False  # a valid document meets refusal
        self.poison_field = False    # the field stops answering
                                     # after the refused loads
        self.stomp = False           # an Out store diverges from the
                                     # commanded value
        self.rewind = False          # a served sample tick rewinds
        self.move_kind = False       # a served sample's kind moves
        self.freeze = False          # the field stops stepping
        self.move_roles = False      # a peer's role moves mid-probe

    def swap(self):
        """The swapped launch layout: ctrl-b owns the field, ctrl-a
        tracks it — the restore owes the entry roles, not a fixed
        letter."""
        self.a_role, self.b_role = 'standby', 'active'
        self.a_tracking, self.b_tracking = True, False
        self.plant.claim['owner'] = PEER_TOKEN

    def _scan(self):
        """One completed scan stepping the field — unless the freeze
        doctor has the field standing still."""
        if not self.freeze:
            self.plant.plant_tick += 1

    def _role(self, peer):
        role = self.a_role if peer == 'a' else self.b_role
        tracking = self.a_tracking if peer == 'a' else self.b_tracking
        sync = {'tracking': {'aligned': self.plant.plant_tick}} \
            if role == 'standby' and tracking \
            else {'unsynchronized': {}}
        return {'role': role, 'tick': self.plant.plant_tick,
                'sync': sync}

    def http_json(self, method, url, body=None, timeout=10):
        """The measurement channel — replaces scenarios.http_json."""
        if self.pair_down:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = 'a' if host == 'ctrl-a:1' else 'b'
        self._scan()
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('POST', '/demote'):
            if peer == 'a':
                self.a_role, self.a_tracking = 'standby', True
            else:
                self.b_role, self.b_tracking = 'standby', True
            return 200, self._role(peer)
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                self.a_role, self.a_tracking = 'active', False
            else:
                self.b_role, self.b_tracking = 'active', False
            return 200, self._role(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/snapshot'):
            return 200, {
                'tick': self.plant.plant_tick,
                'points': [
                    {'point': point, 'direction': 'out',
                     'sample': {'value': {'bool': value},
                                'quality': 'good',
                                'tick': self.plant.plant_tick}}
                    for point, value
                    in sorted(self.commanded.items())]}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))

    def admit_dynamics(self, name, document):
        """The ctx['admit_dynamics'] lever — the staged-document
        admission probe's two gate verdicts: exit 0 + 'check ok'
        admits, exit 1 + per-element 'dynamics element <i> (driving
        point <p>)' lines refuse; the spawned-load gate's bound
        listener answers 'listening on' for admitted and the named
        refusal for refused."""
        self.calls.append(name)
        self._scan()
        if self.lever_raises:
            raise RuntimeError('the staging mount never landed')
        text = ''.join(
            'error: dynamics element %d (driving point %s) '
            'is invalid: <rule>\n'
            % (index, next(iter(element.values()))['output'])
            for index, element in enumerate(document))
        verdict = {'name': name,
                   'document': '/run/dynamics-probes/%s.json' % name}
        if name == 'honest':
            if self.predates:
                verdict['check'] = {
                    'exit': 2, 'stdout': '',
                    'stderr': 'error: unexpected argument '
                              '--check-dynamics'}
                verdict['serve'] = {'exit': 2, 'running': False,
                                    'logs': 'error: unexpected '
                                            'argument --dynamics'}
            elif self.honest_refused:
                verdict['check'] = {'exit': 1, 'stdout': '',
                                    'stderr': text}
                verdict['serve'] = {'exit': 1, 'running': False,
                                    'logs': text}
            else:
                verdict['check'] = {'exit': 0,
                                    'stdout': 'check ok\n'
                                              'elements: 1\n',
                                    'stderr': ''}
                verdict['serve'] = {
                    'exit': None, 'running': True,
                    'logs': 'listening on 127.0.0.1:60000'}
            return verdict
        stderr = text
        if self.unnamed:
            stderr = 'error: the dynamics document is invalid\n'
        if self.panics:
            stderr = 'thread \'main\' panicked at ' \
                'crates/dcs-sim/src/driver.rs:42\n'
        if self.listening:
            stderr += 'listening on 127.0.0.1:60000\n'
        verdict['check'] = {
            'exit': 0 if self.accept_check else 1,
            'stdout': 'check ok\n' if self.accept_check else '',
            'stderr': '' if self.accept_check else stderr}
        verdict['serve'] = {
            'exit': None if self.accept_serve else 1,
            'running': self.accept_serve,
            'logs': 'listening on 127.0.0.1:60000'
            if self.accept_serve else stderr}
        if name == 'out_point':
            # The field-side doctors take effect once the refused
            # loads are staged — the intactness half's reads.
            if self.poison_field:
                self.plant.poisoned = True
            if self.stomp:
                self.plant.samples[100]['value'] = {
                    'bool': not self.commanded[100]}
            if self.rewind:
                self.plant.samples[10]['tick'] = -1
            if self.move_kind:
                self.plant.samples[40]['value'] = {'float': 0.0}
            if self.move_roles:
                self.a_role, self.b_role = 'standby', 'active'
                self.a_tracking = self.b_tracking = False
        return verdict


OWNER_TOKEN = 424243
PEER_TOKEN = 424244


class DynamicsAdmissionTests(unittest.TestCase):
    """scenario_dynamics_admission_refusal against the stubbed rig:
    the fake lever answers each admission gate's verdict while the
    field peer serves its census, the commanded Out audit, and the
    shared-claim probes — until a doctor flag stages a named defect
    or an inconclusive rig."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.plant = AdmissionPlantPeer(OWNER_TOKEN)
        self.feed = AdmissionFeed(self.plant)
        self.events = []

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': feed.plant.address,
                'plant_ctl': feed.plant.ctl,
                'plant_owner': {'active': OWNER_TOKEN,
                                'standby': PEER_TOKEN},
                'admit_dynamics': feed.admit_dynamics,
                'evidence_dir': str(self.evidence)}
        base.update(extra)
        return base

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        patches = {'POLL_INTERVAL': 0.001, 'ADMIT_SETTLE': 3.0,
                   'ADMIT_POLL': 0.01, 'ADMIT_CONVERGE': 0.3}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_dynamics_admission_refusal(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_dynamics_admission_refusal,
                      order)
        self.assertLess(
            order.index(scenarios.scenario_failover),
            order.index(
                scenarios.scenario_dynamics_admission_refusal))
        self.assertLess(
            order.index(
                scenarios.scenario_dynamics_admission_refusal),
            order.index(scenarios.scenario_dcs_ctl))
        self.assertIs(
            verify.case_function('dynamics-admission-refusal'),
            scenarios.scenario_dynamics_admission_refusal)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/dynamics-admission-pass-1.json',
            'evidence/dynamics-admission-pass-2.json'})
        for entry in record['evidence']:
            self.assertTrue(
                (self.evidence.parent / entry['ref']).exists(),
                entry)
        payload = json.loads(
            (self.evidence / 'dynamics-admission-pass-1.json')
            .read_text())
        self.assertEqual(payload['digest'], {
            'honest': 'admitted', 'self_point': 'named-refusal',
            'out_point': 'named-refusal', 'field': 'intact',
            'commands': 'held', 'probes': 'answered',
            'roles': 'held'})
        # The staged documents name the bound points each refusal
        # owed: the float in-point is the self-point class's target,
        # the bool out-points carry the out-point class.
        self.assertEqual(self.feed.calls[:3],
                         ['honest', 'self_point', 'out_point'])
        docs = payload['documents']
        self.assertEqual(
            docs['self_point'][0]['bool_flow']['output'], 10)
        self.assertEqual(
            [e['threshold']['output'] for e in docs['out_point']],
            [100, 101])
        # The launch layout stands untouched.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')

    def test_two_passes_share_one_digest(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = [json.loads(
            (self.evidence / name).read_text())['digest']
            for name in ('dynamics-admission-pass-1.json',
                         'dynamics-admission-pass-2.json')]
        self.assertEqual(passes[0], passes[1])

    def test_two_runs_produce_identical_records(self):
        # The deterministic-rerun contract: two scenario runs over
        # fresh feeds record identical verdict records.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            plant = AdmissionPlantPeer(OWNER_TOKEN)
            try:
                runs.append(self.run_scenario(
                    feed=AdmissionFeed(plant)))
            finally:
                plant.close()
        self.assertEqual(runs[0]['outcome'], 'passed', runs[0])
        self.assertEqual(runs[0], runs[1])

    def test_scenario_ctx_carries_the_lever(self):
        calls = []
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG), record, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: self.events.append(event))

        def fake_docker(*args, **kwargs):
            calls.append(args)
            if args[0] == 'run' and '--rm' in args:
                return subprocess.CompletedProcess(
                    args, 1, stdout='',
                    stderr='error: dynamics element 0 (driving '
                           'point 20) is invalid: <rule>')
            if args[0] == 'inspect' and 'Running' in args[2]:
                return subprocess.CompletedProcess(
                    args, 0, stdout='false\n')
            if args[0] == 'inspect':
                return subprocess.CompletedProcess(
                    args, 0, stdout='1\n')
            return subprocess.CompletedProcess(
                args, 0, stdout='', stderr='')

        self.assertTrue(callable(ctx['admit_dynamics']))
        with patch.object(runner, 'docker', fake_docker):
            verdict = ctx['admit_dynamics']('self-point',
                                            [{'bool_flow': {}}])
        # The check gate ran the released --check-dynamics preflight
        # on the staged document; the serve gate spawned a bounded
        # --dynamics load, polled it to its exit, and removed it.
        self.assertEqual(verdict['check']['exit'], 1)
        self.assertIn('dynamics element 0 (driving point 20)',
                      verdict['check']['stderr'])
        self.assertEqual(verdict['serve'],
                         {'running': False, 'exit': 1, 'logs': ''})
        check_argv = next(args for args in calls
                          if args[0] == 'run' and '--rm' in args)
        self.assertIn('--check-dynamics', check_argv)
        serve_argv = next(args for args in calls
                          if args[0] == 'run' and '--rm' not in args)
        self.assertIn('--dynamics', serve_argv)
        self.assertIn('-d', serve_argv)
        self.assertTrue(any(args[0] == 'rm' and '-f' in args
                            for args in calls))
        self.assertEqual(
            self.events, ['dynamics-admit-check',
                          'dynamics-admit-serve',
                          'dynamics-admitted'])
        self.assertTrue((self.run_dir / 'dynamics-probes'
                         / 'self-point.json').exists())

    def test_missing_lever_is_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(admit_dynamics=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('admit_dynamics', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.pair_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.b_tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_no_active_is_failed(self):
        self.feed.a_role = 'standby'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record['detail'])

    def test_predated_tooling_is_inconclusive(self):
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_failed_lever_is_inconclusive(self):
        self.feed.lever_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never ran', record['detail'])

    def test_no_out_points_is_inconclusive(self):
        self.plant.directions = {p: 'in' for p in self.plant.samples}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('Out point', record['detail'])

    def test_refused_claim_is_inconclusive(self):
        self.plant.claim['owner'] = 999999
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer claim', record['detail'])

    def test_check_accepts_malformed_fails(self):
        self.feed.accept_check = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('accepted', record['detail'])

    def test_serve_accepts_malformed_fails(self):
        self.feed.accept_serve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('serving', record['detail'])

    def test_panic_at_check_fails(self):
        self.feed.panics = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('panic', record['detail'])

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('never named', record['detail'])

    def test_served_listener_fails(self):
        self.feed.listening = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('listener bind', record['detail'])

    def test_honest_control_refused_fails(self):
        self.feed.honest_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('honest', record['detail'])

    def test_plant_silent_fails(self):
        self.feed.poison_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])

    def test_stomped_command_fails(self):
        self.feed.stomp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('commanded', record['detail'])

    def test_rewound_field_fails(self):
        self.feed.rewind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('rewound', record['detail'])

    def test_moved_kind_fails(self):
        self.feed.move_kind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('kind', record['detail'])

    def test_frozen_field_fails(self):
        self.feed.freeze = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('never stepped', record['detail'])

    def test_step_refused_fails(self):
        self.plant.fence_steps = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-failed', record['detail'])
        self.assertIn('step', record['detail'])

    def test_moved_roles_restore_and_fail(self):
        self.feed.move_roles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-nondeterministic',
                      record['detail'])
        # The exit owes the launch roles even on a failed pass.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertTrue(self.feed.b_tracking)

    def test_swapped_launch_layout_restores(self):
        self.feed.swap()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.b_role, 'active')
        self.assertEqual(self.feed.a_role, 'standby')

    def test_silenced_judges_report_unchecked(self):
        with patch.object(scenarios, '_judge_intact',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('dynamics-admission-unchecked',
                      record['detail'])


if __name__ == '__main__':
    unittest.main()
