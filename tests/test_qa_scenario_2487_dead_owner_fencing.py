"""The 2487_dead_owner_fencing leg's scenario unit coverage — the feed fake
and TestCase class for scenario_dead_owner_fencing, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import importlib
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam

# The leg's own module, reached by its numbered stem — the scenarios
# facade re-exports every leg's names, so a same-named DIAG_FAILED
# there would resolve to whichever leg was re-exported last. The
# numbered stem is a legal import name only through importlib.
leg = importlib.import_module(
    'qa_lane.scenarios.2487_dead_owner_fencing')


EXPECTED_CASES = frozenset({
    'DeadOwnerFencingTests.test_registered_in_scenarios',
    'DeadOwnerFencingTests.test_passed',
    'DeadOwnerFencingTests.test_the_dead_owner_claim_survives_every_round',
    'DeadOwnerFencingTests.test_fails_when_a_non_holder_release_dissolves',
    'DeadOwnerFencingTests.test_fails_when_the_claim_stops_fencing_writes',
    'DeadOwnerFencingTests.test_fails_when_a_foreign_ensure_is_granted',
    'DeadOwnerFencingTests.test_fails_when_the_owner_cannot_re_arm',
    'DeadOwnerFencingTests.test_fails_when_the_rearm_write_is_fenced',
    'DeadOwnerFencingTests.test_fails_when_a_foreign_token_takes_over',
    'DeadOwnerFencingTests.test_fails_when_the_preempt_is_refused',
    'DeadOwnerFencingTests.test_fails_when_the_preempt_names_the_dead_owner',
    'DeadOwnerFencingTests.test_fails_when_the_induction_write_is_refused',
    'DeadOwnerFencingTests.test_nondeterministic_on_a_dropped_read',
    'DeadOwnerFencingTests.test_inconclusive_without_a_plant',
    'DeadOwnerFencingTests.test_inconclusive_without_a_plant_ctl',
    'DeadOwnerFencingTests.test_inconclusive_without_an_owner_token',
    'DeadOwnerFencingTests.test_inconclusive_when_the_pair_is_down',
    'DeadOwnerFencingTests.test_inconclusive_on_an_open_field',
    'DeadOwnerFencingTests.test_inconclusive_on_an_unattributed_claim',
    'DeadOwnerFencingTests.test_restores_the_launch_owners_claim',
    'DeadOwnerFencingTests.test_identical_evidence_across_runs',
    'DeadOwnerFencingTests.test_the_self_check_names_every_diagnostic',
})


class DeadOwnerFeed:
    """A stubbed monitor pair for the dead-owner scenario: ctrl-a the
    launched active holding the field's claim under TOKEN_A through
    its own sim-net attachment, ctrl-b a tracking standby. The leg's
    staging rides the plant's claim surface, so the pair answers role
    reports and nothing else — no scan, no demotion, no reclaim: the
    field's arbitration alone is the authority the leg reads. Fault
    flags stage each named failure the scenario reports."""

    TOKEN_A = 0xD5EE0A
    TOKEN_B = 0xD5EE0B

    def __init__(self, plant, roles=None):
        self.plant = plant
        self.roles = dict(roles or {'active': 'active',
                                    'standby': 'standby'})
        self.stream = self._connect()
        # The launch owner holds the claim, as a settled pair's active
        # does — the baseline the dead-owner window stands against.
        self._roundtrip({'op': 'claim_writer', 'owner': self.TOKEN_A})

    def close(self):
        self.stream.close()

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)), timeout=5)

    def _roundtrip(self, request):
        self.stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            line += self.stream.recv(65536)
        return json.loads(line)

    def http_json(self, method, url, body=None, timeout=10):
        """The monitor seam the leg reads: `/role` on either peer. A
        peer whose role was moved off the launch assignment reports it,
        so the leg's "roles never moved" assertion has something to
        catch."""
        name = 'active' if url.startswith('http://ctrl-a') else 'standby'
        role = self.roles.get(name)
        if role is None:
            raise urllib.error.URLError('connection refused')
        return 200, {'role': role, 'sync': {'tracking': {}},
                     'tick': 1}


class DeadOwnerFencingTests(unittest.TestCase):
    """The dead-owner fencing contract on a stubbed field: the claim a
    closed owner leaves standing keeps fencing every third attachment's
    mutations across the server-side hold reap, a release from an
    attachment holding nothing answers done and changes nothing, the
    recorded owner's token re-arms and writes, a foreign token stays
    fenced, and a preempting claim takes the field per the declared
    contract."""

    def _ctx(self, plant, evidence):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant_owner': {
                    'active': DeadOwnerFeed.TOKEN_A,
                    'standby': DeadOwnerFeed.TOKEN_B},
                'plant': plant.address, 'plant_ctl': plant.ctl,
                'evidence_dir': evidence}

    def _run(self, evidence, plant_flags=None, feed_flags=None,
             roles=None):
        plant = ClaimPlantPeer()
        for name, value in (plant_flags or {}).items():
            setattr(plant, name, value)
        feed = DeadOwnerFeed(plant, roles=roles)
        for name, value in (feed_flags or {}).items():
            setattr(feed, name, value)
        try:
            with patch.object(scenarios, 'http_json', feed.http_json), \
                    patch.object(scenarios, 'DEAD_OWNER_POLL', 0), \
                    patch.object(scenarios, 'DEAD_OWNER_SETTLE', 2), \
                    patch.object(scenarios, 'DEAD_OWNER_DEADLINE', 2):
                record = scenarios.scenario_dead_owner_fencing(
                    self._ctx(plant, evidence))
        finally:
            feed.close()
            plant.close()
        return plant, feed, record

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_dead_owner_fencing, order)
        self.assertIs(verify.case_function('dead-owner-fencing'),
                      scenarios.scenario_dead_owner_fencing)

    def test_passed(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            self.assertTrue(report.validate_scenario(record))
            for number in (1, 2):
                name = 'dead-owner-fencing-pass-%d.json' % number
                path = os.path.join(evidence, name)
                self.assertTrue(os.path.exists(path), name)
                json.loads(Path(path).read_text())
            self.assertTrue(os.path.exists(
                os.path.join(evidence, 'dead-owner-fencing-restored.json')))
            self.assertIn('identical digests',
                          ' '.join(record['observations']))

    def test_the_dead_owner_claim_survives_every_round(self):
        # The finding's own claim, read off the saved pass: the dead
        # owner's token names the standing claim on every reap round
        # and every non-holder release answered done.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            body = json.loads(Path(
                evidence, 'dead-owner-fencing-pass-1.json').read_text())
            window = body['window']
            self.assertEqual(
                len(window), leg.DEAD_OWNER_ROUNDS)
            for row in window:
                self.assertEqual(row['probe'], 'fenced')
                self.assertEqual(
                    row['named'],
                    leg.DEAD_OWNER_TOKEN)
                self.assertEqual(row['mutation'], 'fenced')
                self.assertEqual(row['release'], 'done')

    def test_fails_when_a_non_holder_release_dissolves(self):
        # The pre-#638 shape: a release from an attachment holding
        # nothing dissolves the standing claim. The field answers
        # `unclaimed` inside the window and the leg names the
        # dissolution.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'release_dissolves': True})
            self.assertEqual(record['outcome'], 'failed')
            detail = record.get('detail', '')
            self.assertIn(leg.DIAG_FAILED, detail)
            self.assertIn('dissolved', detail)

    def test_fails_when_the_claim_stops_fencing_writes(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'open_field': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('did not fence a mutation probe',
                          record.get('detail', ''))

    def test_fails_when_a_foreign_ensure_is_granted(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_preempts': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('foreign token', record.get('detail', ''))

    def test_fails_when_the_owner_cannot_re_arm(self):
        # A claim that stands but refuses its own recorded owner's
        # re-attach: the fence holds against everyone and against the
        # one attachment it exists for.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_rearms_refused': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn("re-attach ensure_writer",
                          record.get('detail', ''))

    def test_fails_when_the_rearm_write_is_fenced(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'shared_write_fenced': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn("re-armed owner's write",
                          record.get('detail', ''))

    def test_fails_when_a_foreign_token_takes_over(self):
        # The foreign ensure runs twice — once inside the window, once
        # after the recorded owner has re-armed. A surface that refuses
        # the first and grants the second is the takeover the leg's
        # last foreign clause names.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'foreign_grants_after': 2})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('granted after the recorded owner re-armed',
                          record.get('detail', ''))

    def test_fails_when_the_preempt_is_refused(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant = ClaimPlantPeer()
            feed = DeadOwnerFeed(plant)
            # A claim surface on which the unconditional preempt is
            # refused — the leg's staging lever gone.
            real = plant._respond

            def refuse(conn, request):
                if request.get('op') == 'claim_writer' \
                        and request.get('owner') \
                        == leg.DEAD_OWNER_PREEMPT:
                    return plant._fenced()
                return real(conn, request)

            plant._respond = refuse
            try:
                with tempfile.TemporaryDirectory() as evidence:
                    with patch.object(scenarios, 'http_json',
                                      feed.http_json), \
                            patch.object(scenarios, 'DEAD_OWNER_POLL',
                                         0), \
                            patch.object(scenarios, 'DEAD_OWNER_SETTLE',
                                         2), \
                            patch.object(scenarios,
                                         'DEAD_OWNER_DEADLINE', 2):
                        record = scenarios.scenario_dead_owner_fencing(
                            self._ctx(plant, evidence))
                    self.assertEqual(record['outcome'], 'failed')
                    self.assertIn('preempting claim_writer was refused',
                                  record.get('detail', ''))
            finally:
                feed.close()
                plant.close()

    def test_fails_when_the_preempt_names_the_dead_owner(self):
        # The preempt is granted but the standing verdict keeps naming
        # the dead owner: the claim never actually moved.
        with tempfile.TemporaryDirectory() as evidence:
            plant = ClaimPlantPeer()
            feed = DeadOwnerFeed(plant)
            real = plant._respond
            state = {'claims': 0}

            def stuck(conn, request):
                if request.get('op') == 'claim_writer' \
                        and request.get('owner') \
                        == leg.DEAD_OWNER_PREEMPT:
                    state['claims'] += 1
                    # Accept the grant but leave the standing claim
                    # naming the dead owner — the preempt that never
                    # took.
                    return {'result': 'done'}
                return real(conn, request)

            plant._respond = stuck
            try:
                with tempfile.TemporaryDirectory() as evidence:
                    with patch.object(scenarios, 'http_json',
                                      feed.http_json), \
                            patch.object(scenarios, 'DEAD_OWNER_POLL',
                                         0), \
                            patch.object(scenarios, 'DEAD_OWNER_SETTLE',
                                         2), \
                            patch.object(scenarios,
                                         'DEAD_OWNER_DEADLINE', 2):
                        record = scenarios.scenario_dead_owner_fencing(
                            self._ctx(plant, evidence))
                    self.assertEqual(record['outcome'], 'failed')
                    self.assertIn('after the preempt names',
                                  record.get('detail', ''))
            finally:
                feed.close()
                plant.close()

    def test_fails_when_the_induction_write_is_refused(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant = ClaimPlantPeer()
            feed = DeadOwnerFeed(plant)
            real = plant._respond

            def refuse(conn, request):
                if request.get('op') == 'write' \
                        and request.get('owner') is None:
                    return {'result': 'error',
                            'error': {'kind': 'io',
                                      'error': {'fenced': 1}}}
                return real(conn, request)

            plant._respond = refuse
            try:
                with tempfile.TemporaryDirectory() as evidence:
                    with patch.object(scenarios, 'http_json',
                                      feed.http_json), \
                            patch.object(scenarios, 'DEAD_OWNER_POLL',
                                         0), \
                            patch.object(scenarios, 'DEAD_OWNER_SETTLE',
                                         2), \
                            patch.object(scenarios,
                                         'DEAD_OWNER_DEADLINE', 2):
                        record = scenarios.scenario_dead_owner_fencing(
                            self._ctx(plant, evidence))
                    self.assertEqual(record['outcome'], 'failed')
                    self.assertIn("induction write",
                                  record.get('detail', ''))
            finally:
                feed.close()
                plant.close()

    def test_nondeterministic_on_a_dropped_read(self):
        # A claim surface that answers nothing at all is an
        # unreachable rig, not a contract failure. The baseline's
        # dropped read is the nondeterministic half; every clause after
        # it is the pass's own absence, never a verdict.
        violations = {}
        leg._judge_dead_owner(
            {},
            lambda key, diagnostic, detail:
            violations.setdefault(key, (diagnostic, detail)))
        self.assertEqual(
            list(violations), ['baseline'])
        self.assertEqual(
            [name for name, _ in violations.values()], [leg.DIAG_NONDET])
        # A window round the surface did not answer is the same
        # instability, mid-pass.
        violations = {}
        leg._judge_dead_owner(
            {'baseline': {'result': 'error',
                          'error': {'kind': 'fenced', 'owner': 7}},
             'claim': {'result': 'done'},
             'induction_write': {'result': 'done'},
             'window': [{'round': 0, 'answered': False,
                         'probe': None, 'named': None,
                         'mutation': None, 'release': None,
                         'released_error': None}]},
            lambda key, diagnostic, detail:
            violations.setdefault(key, (diagnostic, detail)))
        self.assertEqual(
            [name for name, _ in violations.values()],
            [leg.DIAG_NONDET])

    def test_inconclusive_without_a_plant(self):
        plant = ClaimPlantPeer()
        feed = DeadOwnerFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant'] = None
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_dead_owner_fencing(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_without_a_plant_ctl(self):
        plant = ClaimPlantPeer()
        feed = DeadOwnerFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant_ctl'] = None
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_dead_owner_fencing(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
            self.assertIn('plant_ctl', record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_without_an_owner_token(self):
        plant = ClaimPlantPeer()
        feed = DeadOwnerFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant_owner'] = {}
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_dead_owner_fencing(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
            self.assertIn('owner token', record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_when_the_pair_is_down(self):
        plant = ClaimPlantPeer()
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())

            def down(method, url, body=None, timeout=10):
                raise urllib.error.URLError('connection refused')

            with patch.object(scenarios, 'http_json', down), \
                    patch.object(scenarios, 'DEAD_OWNER_SETTLE', 2):
                record = scenarios.scenario_dead_owner_fencing(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
            self.assertIn('unreachable', record.get('detail', ''))
        finally:
            plant.close()

    def test_inconclusive_on_an_open_field(self):
        plant = ClaimPlantPeer()
        feed = DeadOwnerFeed(plant)
        try:
            # The launch owner's claim released by a stranger: the
            # field stands open, so there is no standing claim for the
            # dead-owner window to lose.
            stranger = socket.create_connection(
                tuple(plant.address.rpartition(':')[:1])
                + (int(plant.address.rpartition(':')[2]),), timeout=5)
            stranger.sendall(b'{"op":"claim_writer","owner":99}\n')
            stranger.recv(65536)
            stranger.close()
            plant.claim = None
            with tempfile.TemporaryDirectory() as evidence:
                with patch.object(scenarios, 'http_json',
                                  feed.http_json), \
                        patch.object(scenarios, 'DEAD_OWNER_POLL', 0), \
                        patch.object(scenarios, 'DEAD_OWNER_SETTLE', 2), \
                        patch.object(scenarios, 'DEAD_OWNER_DEADLINE', 2):
                    record = scenarios.scenario_dead_owner_fencing(
                        self._ctx(plant, evidence))
            self.assertEqual(record['outcome'], 'inconclusive')
            self.assertIn('no writer claim', record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_on_an_unattributed_claim(self):
        plant = ClaimPlantPeer()
        feed = DeadOwnerFeed(plant)
        try:
            # A verdict naming no standing owner: the rig predates the
            # attribution contract the leg reads the claim through.
            real = plant._respond

            def blind(conn, request):
                if request.get('op') == 'probe_writer':
                    return {'result': 'error',
                            'error': {'kind': 'fenced',
                                      'detail': 'held'}}
                return real(conn, request)

            plant._respond = blind
            with tempfile.TemporaryDirectory() as evidence:
                with patch.object(scenarios, 'http_json',
                                  feed.http_json), \
                        patch.object(scenarios, 'DEAD_OWNER_POLL', 0), \
                        patch.object(scenarios, 'DEAD_OWNER_SETTLE', 2), \
                        patch.object(scenarios, 'DEAD_OWNER_DEADLINE', 2):
                    record = scenarios.scenario_dead_owner_fencing(
                        self._ctx(plant, evidence))
            self.assertEqual(record['outcome'], 'inconclusive')
            self.assertIn('names no standing owner',
                          record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_restores_the_launch_owners_claim(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            self.assertIn('the field restored to active',
                          ' '.join(record['observations']))

    def test_identical_evidence_across_runs(self):
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as evidence:
                _, _, record = self._run(evidence)
                runs.append((
                    record['outcome'], record['observations'],
                    json.dumps(record['evidence'], sort_keys=True),
                    json.loads(Path(evidence,
                                    'dead-owner-fencing-pass-1.json')
                               .read_text())['digest']))
        self.assertEqual(runs[0], runs[1])

    def test_the_self_check_names_every_diagnostic(self):
        # Every planted negative the leg declares must be named by the
        # judge, and the honest record must name nothing — a judge that
        # fires on the clean record would fail every rig.
        self.assertEqual(leg._dead_owner_self_check(), [])
        clean = {
            'baseline': {'result': 'error',
                         'error': {'kind': 'fenced', 'owner': 7}},
            'claim': {'result': 'done'},
            'induction_write': {'result': 'done'},
            'window': [{'round': 0, 'answered': True,
                        'probe': 'fenced', 'named': 7,
                        'mutation': 'fenced', 'release': 'done',
                        'released_error': None}],
            'foreign_ensure': {'result': 'error',
                               'error': {'kind': 'fenced'}},
            'rearm': {'result': 'claimed_shared'},
            'rearm_write': {'result': 'done'},
            'foreign_after': {'result': 'error',
                              'error': {'kind': 'fenced'}},
            'preempt': {'result': 'done'},
            'preempted': {'result': 'error',
                          'error': {'kind': 'fenced', 'owner': 9}},
            'dead_token': 7, 'preempt_token': 9}
        violations = {}
        leg._judge_dead_owner(
            clean, lambda key, diagnostic, detail:
            violations.setdefault(key, (diagnostic, detail)))
        self.assertEqual(violations, {})
        self.assertEqual(
            leg._dead_owner_digest(clean, {}),
            {'induction': 'claimed', 'fence': 'standing',
             'rounds': 'fenced', 'foreign': 'refused',
             'rearm': 'seated', 'preempt': 'taken',
             'restore': 'restored'})