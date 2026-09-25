"""The 0700_field_claim leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_field_claim, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'FieldClaimTests.test_registered_in_scenarios',
    'FieldClaimTests.test_passed',
    'FieldClaimTests.test_fails_when_the_field_is_open',
    'FieldClaimTests.test_fails_when_unclaimed',
    'FieldClaimTests.test_fails_when_ensure_preempts',
    'FieldClaimTests.test_fails_when_shared_answers_done',
    'FieldClaimTests.test_fails_when_the_shared_write_is_fenced',
    'FieldClaimTests.test_fails_when_release_drops_the_claim',
    'FieldClaimTests.test_fails_when_idle_release_is_refused',
    'FieldClaimTests.test_refused_rogue_claim_still_passes',
    'FieldClaimTests.test_fails_when_the_rogue_kills_the_active',
    'FieldClaimTests.test_fails_when_the_preemption_is_silent',
    'FieldClaimTests.test_fails_when_the_owner_never_demotes',
    'FieldClaimTests.test_fails_when_the_loss_names_no_claimant',
    'FieldClaimTests.test_fails_when_the_reclaim_never_recovers',
    'FieldClaimTests.test_fails_when_the_peer_ends_active',
    'FieldClaimTests.test_fails_when_the_active_stalls',
    'FieldClaimTests.test_inconclusive_without_a_plant',
    'FieldClaimTests.test_inconclusive_without_a_token',
    'FieldClaimTests.test_inconclusive_when_the_pair_is_down',
    'FieldClaimTests.test_fails_when_nothing_is_settled',
    'FieldClaimTests.test_detaches_and_restores_rig_state',
    'FieldClaimTests.test_identical_evidence_across_runs',
})


class FieldClaimFeed:
    """A stubbed monitor pair for the field-claim scenario: ctrl-a the
    launched active holding the plant's claim under TOKEN_A through
    its own sim-net attachment, ctrl-b a tracking standby. Each
    ctrl-a request is one scan boundary: an active scan writes the
    field through the held claim, a fenced answer drives the
    contract's degrade path — field_claim_lost journaled with the
    claimant the verdict named, demoting then standby — and the
    standby's fencing-loss mark drives the bound conditional reclaim
    every scan: refused while the rogue claim's holders stand, granted
    once the release frees the field, walking promoting -> active with
    no operator call. Fault flags stage each named failure the
    scenario reports."""

    TOKEN_A = 0xD5C00A
    TOKEN_B = 0xD5C00B

    def __init__(self, plant, unclaimed=False):
        self.plant = plant
        self.tick = 0
        self.role = 'active'
        self.reconverge = 0
        self.journal = []
        self.next_seq = 1
        self.failed_writes = 0
        self.stream = self._connect()
        self.holds = False
        self.dead = False
        self.peer_up = False           # ctrl-b promoted and stayed
        self.fencing_lost = False      # the demotion's loss mark —
                                       # the standby-scan reclaim
                                       # probes while it stands
        # Fault injection for the named-failure cases.
        self.no_journal = False        # the loss demotes unrecorded
        self.no_claimant = False       # the loss journal names no one
        self.dies_on_fence = False     # the preempted owner exits
        self.no_demote = False         # the loss never moves the role
        self.never_reclaims = False    # the standby never re-takes
        self.peer_promotes = False     # ctrl-b reports active later
        self.stalls = False            # the active's tick never grows
        if not unclaimed:
            self._roundtrip({'op': 'claim_writer',
                             'owner': self.TOKEN_A})
            self.holds = True

    def close(self):
        self.stream.close()

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)),
                                        timeout=5)

    def _roundtrip(self, request):
        self.stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            line += self.stream.recv(65536)
        return json.loads(line)

    def _fenced(self, response):
        error = (response or {}).get('error') or {}
        inner = error.get('error')
        return error.get('kind') == 'fenced' or (
            error.get('kind') == 'io' and isinstance(inner, dict)
            and 'fenced' in inner)

    def _journal(self, event):
        self.journal.append({'seq': self.next_seq,
                             'tick': self.tick, 'event': event})
        self.next_seq += 1

    def _supersede(self):
        # The contract's degrade path: count the fenced write,
        # journal the loss — attributed to the claimant the field's
        # own fencing verdict named — and walk demoting -> standby,
        # the loss mark standing for the standby-scan reclaim.
        self.failed_writes += 1
        self.holds = False
        self.peer_up = True
        self.fencing_lost = True
        if not self.no_journal:
            loss = {'point': 200}
            if not self.no_claimant:
                loss['claimant'] = (self.plant.claim or {}).get(
                    'owner')
            self._journal({'field_claim_lost': loss})
        if self.dies_on_fence:
            self.dead = True
            return
        if not self.no_demote:
            self.role = 'demoting'
            self._journal({'role_changed': {'from': 'active',
                                            'to': 'demoting'}})

    def _scan(self):
        self.tick += 1
        if self.role == 'demoting':
            self.role = 'standby'
            self._journal({'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
            self.reconverge = 2
            return
        if self.role == 'standby':
            if self.reconverge:
                self.reconverge -= 1
            # The fencing-loss reclaim: the demoted ex-owner probes the
            # bound conditional re-grant every standby scan — refused
            # while a different owner's claim stands at all, granted
            # the first scan the field frees or already names the
            # token — then walks promoting -> active on the
            # field-owning scans.
            if self.fencing_lost and not self.never_reclaims:
                if not self._fenced(self._roundtrip(
                        {'op': 'ensure_writer',
                         'owner': self.TOKEN_A})):
                    self.holds = True
                    self.fencing_lost = False
                    self.role = 'promoting'
                    self._journal({'role_changed': {
                        'from': 'standby', 'to': 'promoting'}})
            return
        # A field-owning role writes every scan; the gate follows the
        # role, so a promoted peer that re-claimed nothing meets the
        # standing claim's fence and supersedes again.
        if self._fenced(self._roundtrip(
                {'op': 'write', 'point': 200,
                 'value': {'bool': False}})):
            self._supersede()
            return
        if self.role == 'promoting':
            self.role = 'active'
            self._journal({'role_changed': {'from': 'promoting',
                                            'to': 'active'}})

    def _refuse(self, url):
        raise urllib.error.HTTPError(url, 409, 'conflict', None,
                                     None)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                role = 'active' if (self.peer_promotes
                                    and self.peer_up) else 'standby'
                report = {'role': role, 'tick': self.tick}
                if role == 'standby':
                    report['sync'] = {
                        'tracking': {'aligned': self.tick}}
                return 200, report
            if (method, route) == ('GET', '/checkpoint'):
                return 200, {'tick': self.tick}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if self.dead:
            raise urllib.error.URLError('connection refused')
        if not self.stalls:
            self._scan()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.role, 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            points = [{'point': p,
                       'sample': dict(self.plant.samples[p])}
                      for p in sorted(self.plant.samples)]
            return 200, {'tick': self.tick, 'points': points,
                         'io_health': {
                             'failed_reads': 0,
                             'failed_writes': self.failed_writes,
                             'consecutive_failures': 0,
                             'last_error': None,
                             'driver': {'link': 'connected'}}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/promote'):
            if self.role != 'standby' or self.reconverge:
                self._refuse(url)
            if not self.never_reclaims:
                self._roundtrip({'op': 'claim_writer',
                                 'owner': self.TOKEN_A})
                self.holds = True
            self.role = 'promoting'
            self._journal({'role_changed': {'from': 'standby',
                                            'to': 'promoting'}})
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class FieldClaimTests(unittest.TestCase):
    """The field-claim scenario under fakes: the plant enforces the
    single-writer claim — fenced/claimed_shared/unclaimed/done
    answers — and the stubbed pair walks the superseded owner's
    degrade path and the restore promotion."""

    def _ctx(self, plant, evidence):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant_owner': {
                    'active': FieldClaimFeed.TOKEN_A,
                    'standby': FieldClaimFeed.TOKEN_B},
                'plant': plant.address, 'plant_ctl': plant.ctl,
                'evidence_dir': evidence}

    def _run(self, evidence, plant_flags=None, feed_flags=None,
             unclaimed=False):
        plant = ClaimPlantPeer()
        for name, value in (plant_flags or {}).items():
            setattr(plant, name, value)
        feed = FieldClaimFeed(plant, unclaimed=unclaimed)
        for name, value in (feed_flags or {}).items():
            setattr(feed, name, value)
        try:
            with patch.object(scenarios, 'http_json', feed.http_json), \
                    patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                record = scenarios.scenario_field_claim(
                    self._ctx(plant, evidence))
        finally:
            feed.close()
            plant.close()
        return plant, feed, record

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_field_claim, order)
        # The restored pre-switch window — behind the stale-freshness
        # case that leaves the launch pair it shares, ahead of the
        # tune case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_stale_freshness),
            order.index(scenarios.scenario_field_claim))
        self.assertLess(
            order.index(scenarios.scenario_field_claim),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('field-claim'),
                      scenarios.scenario_field_claim)

    def test_passed(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            self.assertTrue(report.validate_scenario(record))
            for name in ('field-claim-probes.json',
                         'field-claim-owner.json',
                         'field-claim-lifecycle.json',
                         'field-claim-rogue.json',
                         'field-claim-superseded.json',
                         'field-claim-release.json',
                         'field-claim-reclaim.json',
                         'field-claim-restored.json'):
                path = os.path.join(evidence, name)
                self.assertTrue(os.path.exists(path), name)
                json.loads(Path(path).read_text())
            # The restore re-claimed under the owner's token; the
            # scenario's finally released its attachment's hold.
            self.assertEqual(plant.claim,
                             {'owner': feed.TOKEN_A,
                              'holders': {0}})

    def test_fails_when_the_field_is_open(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'open_field': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('enforceable writer claim',
                          record.get('detail', ''))

    def test_fails_when_unclaimed(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(evidence, unclaimed=True)
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('enforceable writer claim',
                          record.get('detail', ''))

    def test_fails_when_ensure_preempts(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_preempts': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('conditional grant preempted',
                          record.get('detail', ''))

    def test_fails_when_shared_answers_done(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_done': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('claimed_shared',
                          record.get('detail', ''))

    def test_fails_when_the_shared_write_is_fenced(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'shared_write_fenced': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('write under the shared claim',
                          record.get('detail', ''))

    def test_fails_when_release_drops_the_claim(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence,
                plant_flags={'release_drops_claim': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('release', record.get('detail', ''))

    def test_fails_when_idle_release_is_refused(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence,
                plant_flags={'idle_release_fails': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('holder of nothing',
                          record.get('detail', ''))

    def test_refused_rogue_claim_still_passes(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(
                evidence, plant_flags={'refuse_rogue': True})
            self.assertEqual(record['outcome'], 'passed')
            self.assertTrue(report.validate_scenario(record))
            # The refused claim left the owner's claim standing.
            self.assertEqual(plant.claim['owner'], feed.TOKEN_A)

    def test_fails_when_the_rogue_kills_the_active(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'dies_on_fence': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('killed', record.get('detail', ''))

    def test_fails_when_the_preemption_is_silent(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'no_journal': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('silent', record.get('detail', ''))

    def test_fails_when_the_owner_never_demotes(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'no_demote': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('never demoted',
                          record.get('detail', ''))

    def test_fails_when_the_loss_names_no_claimant(self):
        # A field_claim_lost that cannot name the preempting owner is
        # the audit gap the finding reports: the takeover must
        # attribute to the rogue token the field's verdict carried.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'no_claimant': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('attribute', record.get('detail', ''))

    def test_fails_when_the_reclaim_never_recovers(self):
        # A demoted owner whose bound reclaim never re-takes the
        # released field stays standby forever — the wedge the fix
        # exists to escape.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'never_reclaims': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('never reclaimed',
                          record.get('detail', ''))

    def test_fails_when_the_peer_ends_active(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'peer_promotes': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('return to standby',
                          record.get('detail', ''))

    def test_fails_when_the_active_stalls(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'stalls': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('stalled', record.get('detail', ''))

    def test_inconclusive_without_a_plant(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant'] = None
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_without_a_token(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant_owner'] = {}
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
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
                    patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            plant.close()

    def test_fails_when_nothing_is_settled(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        feed.role = 'standby'
        try:
            with tempfile.TemporaryDirectory() as evidence:
                with patch.object(scenarios, 'http_json',
                                  feed.http_json), \
                        patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                    record = scenarios.scenario_field_claim(
                        self._ctx(plant, evidence))
                self.assertEqual(record['outcome'], 'failed')
                self.assertIn('role=active',
                              record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_detaches_and_restores_rig_state(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            # The scenario's attachment released every hold it took:
            # only the owner's own attachment holds the claim.
            self.assertEqual(plant.claim['holders'], {0})
            # And the pair returned to its original layout.
            self.assertEqual(feed.role, 'active')

    def test_identical_evidence_across_runs(self):
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as evidence:
                _, _, record = self._run(evidence)
                runs.append((
                    record['outcome'], record['observations'],
                    [(item['kind'], item['ref'],
                      item.get('detail'))
                     for item in record['evidence']],
                    {name: Path(os.path.join(evidence, name))
                     .read_text()
                     for name in sorted(os.listdir(evidence))}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
