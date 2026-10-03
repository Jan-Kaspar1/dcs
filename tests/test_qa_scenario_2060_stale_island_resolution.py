"""The 2060_stale_island_resolution leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_stale_island_resolution, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'StaleIslandTests.test_registered',
    'StaleIslandTests.test_clean_rig_passes_and_validates',
    'StaleIslandTests.test_demote_refused_fails',
    'StaleIslandTests.test_silent_adoption_fails',
    'StaleIslandTests.test_adopted_driven_fails',
    'StaleIslandTests.test_island_never_forms_fails',
    'StaleIslandTests.test_orphan_journal_absent_fails',
    'StaleIslandTests.test_driven_promote_refused_fails',
    'StaleIslandTests.test_islanded_promote_preempts_fails',
    'StaleIslandTests.test_wrong_refusal_verdict_fails',
    'StaleIslandTests.test_never_resolves_fails',
    'StaleIslandTests.test_line_owner_stale_fails',
    'StaleIslandTests.test_restore_fails',
    'StaleIslandTests.test_no_active_reports_failed',
    'StaleIslandTests.test_unconverged_pair_reports_inconclusive',
    'StaleIslandTests.test_unreachable_pair_reports_inconclusive',
    'StaleIslandTests.test_unkeyed_run_reports_inconclusive',
    'StaleIslandTests.'
    'test_unkeyed_deployed_pair_runs_on_the_probe_pair',
    'StaleIslandTests.test_missing_driven_action_reports_inconclusive',
    'StaleIslandTests.test_launch_failure_reports_inconclusive',
    'StaleIslandTests.test_driven_never_serves_reports_inconclusive',
    'StaleIslandTests.test_predates_contract_reports_inconclusive',
    'StaleIslandTests.test_diverging_digests_report_nondeterministic',
    'StaleIslandTests.test_two_runs_produce_identical_evidence',
})


class StaleIslandFeed:
    """A stubbed three-peer rig for the stale-island leg: ctrl-a owns
    the field and configured no tracking source — its demotion adopts
    the newest announced hint; ctrl-b tracks a through its configured
    --standby pull; and the `start_driven`/`stop_driven` ctx actions
    stand the run's driven third controller on ctrl-d — `--standby a
    --driven`, its scans and checkpoint pulls running only inside a
    POST /scan batch.

    The tracking/claim half models the #831 contract the leg pins:
    every tracking pull announces the puller on its source, a demoted
    owner releases its claim yielded, an orphaned peer's probe follows
    the propagated line_owner and the recorded announcers onto whoever
    serves the line as its field owner, and an orphaned promote claims
    conditionally — granted over a yielded claim, refused
    field_claim_failed while a live different-owner unyielded
    controller claim stands. Every endpoint call on a paced peer is
    one completed scan, so two runs emit identical evidence. Doctor
    flags stage each named defect the issue calls out."""

    ADDRS = {'a': '172.18.0.2:8080', 'b': '172.18.0.3:8081',
             'd': '172.18.0.4:8082'}
    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b', 'ctrl-d:3': 'd'}
    GENERATION = 777777

    def __init__(self):
        self.tick = 100
        self.up = {'a': True, 'b': True, 'd': False}
        self.role = {'a': 'active', 'b': 'standby', 'd': 'standby'}
        # The checkpoint source each standby pulls — None while it
        # owns the field; 'd' launches --standby a.
        self.source = {'a': None, 'b': 'a', 'd': 'a'}
        self.sync = {'a': None, 'b': 'tracking', 'd': 'unsynchronized'}
        # The line_owner stamp each peer's served checkpoint
        # propagates — the owner stamps itself, a non-owner the stamp
        # its last pull carried.
        self.stamp = {'a': 'a', 'b': 'a', 'd': 'a'}
        # The bounded announced-hint sets, newest first.
        self.announced = {'a': ['b'], 'b': [], 'd': []}
        # The simulated field's writer claim: owner token plus the
        # yielded mark a keep-claim demotion leaves.
        self.claim = {'owner': 'a', 'yielded': False}
        self.journal = {'a': [], 'b': [], 'd': []}
        self.seq = {'a': 0, 'b': 0, 'd': 0}
        self.pair_token = 'test-pair'
        # Fault injection — each named failure the issue calls out.
        self.demote_refused = False    # the owner's demote refuses
        self.silent_adoption = False   # the adoption never journals
        self.adopted_driven = False    # the demotion adopts the
                                       # driven peer — no pair island
        self.never_orphaned = False    # peers keep reporting tracking
                                       # on an ownerless line — the
                                       # pre-fix wedge
        self.orphan_journal_absent = False  # the orphaned verdict
                                            # never journals
        self.driven_promote_refused = False  # the driven peer's
                                             # promote is refused
        self.islanded_preempts = False  # the conditional claim
                                        # preempts a live incumbent —
                                        # the defect the contract
                                        # closes
        self.wrong_refusal = False    # the islanded refusal carries
                                      # another verdict
        self.never_resolves = False   # the orphan probes never
                                      # re-target the live owner
        self.stale_line_owner = False  # the reconverged peers'
                                       # checkpoints keep propagating
                                       # the dead owner's stamp
        self.restore_fails = False    # the driven owner's demote is
                                      # refused — no restore path
        self.launch_fails = False     # start_driven raises
        self.driven_down = False      # the launched peer never serves
        self.unreachable = False      # the monitors never answer
        self.no_active = False        # no peer reports role=active
        self.no_tracking = False      # the standby never converges
        self.predates_contract = False  # served checkpoints carry no
                                        # ownership stamps

    # ---- the served surface --------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _journal(self, peer, kind, body):
        self.seq[peer] += 1
        self.journal[peer].append({'seq': self.seq[peer],
                                   'tick': self.tick,
                                   'event': {kind: body}})

    def _report(self, peer):
        if self.no_active and peer == 'a':
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        report = {'role': self.role[peer], 'tick': self.tick}
        if self.role[peer] == 'standby':
            sync = self.sync[peer]
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.tick}}
            else:
                report['sync'] = sync
            report['field_claim'] = 'held' if self.claim else None
        return report

    def _checkpoint(self, peer):
        doc = {'format_version': 1,
               'model_fingerprint': 'stale-island-fp',
               'generation': self.GENERATION, 'tick': self.tick,
               'receipts': [], 'command_admission': {
                   'attempts': 0, 'full_rejections': 0,
                   'high_water': 0}}
        if not self.predates_contract:
            doc['source_owns_field'] = self.role[peer] == 'active'
            stamp = 'a' if self.stale_line_owner else self.stamp[peer]
            doc['line_owner'] = self.ADDRS.get(stamp) \
                if stamp else None
        return doc

    # ---- the tracking model ---------------------------------------

    def _pull(self, peer):
        """One tracking pull of the peer's current source: the pull
        announces on the source, applies its served verdict, and —
        while the verdict is orphaned — runs the re-resolution probe
        onto whichever candidate serves the line as its owner."""
        source = self.source[peer]
        if source is None:
            return
        if not self.up[source]:
            self.sync[peer] = 'unsynchronized'
            return
        ann = self.announced[source]
        if peer in ann:
            ann.remove(peer)
        ann.insert(0, peer)
        if self.no_tracking:
            self.sync[peer] = 'unsynchronized'
            return
        if self.role[source] == 'active':
            self.sync[peer] = 'tracking'
            self.stamp[peer] = source
            return
        self.stamp[peer] = self.stamp[source]
        if self.never_orphaned:
            self.sync[peer] = 'tracking'
            return
        if self.sync[peer] != 'orphaned':
            if not self.orphan_journal_absent:
                self._journal(peer, 'field_orphaned',
                              {'aligned': self.tick})
        self.sync[peer] = 'orphaned'
        if self.never_resolves:
            return
        # The orphan-resolution probe: the propagated line owner
        # leads, then the recorded announcers — a candidate must serve
        # the line as its field owner to earn the re-target.
        for candidate in [self.stamp[peer]] + list(self.announced[peer]):
            if candidate != peer and candidate in self.role \
                    and self.up[candidate] \
                    and self.role[candidate] == 'active':
                self.source[peer] = candidate
                return

    def _scan(self, peer):
        """One completed scan: role transitions settle at the boundary
        and a standby pulls its tracked source."""
        self.tick += 1
        if self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
        elif self.role[peer] == 'promoting':
            self.role[peer] = 'active'
            self.stamp[peer] = peer
            self.sync[peer] = None
        if self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the runner-owned lifecycle actions -----------------------

    def start_driven(self, active):
        assert active == 'active', active
        if self.launch_fails:
            raise RuntimeError('docker run failed: launch refused')
        self.up['d'] = not self.driven_down
        self.role['d'] = 'standby'
        self.sync['d'] = 'unsynchronized'
        self.source['d'] = 'a'
        return {'container': 'dcs-hw-qa-1-d'}

    def stop_driven(self):
        self.up['d'] = False

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        if peer == 'a':
            if self.demote_refused or not self.announced['a']:
                self._raise(409, 'no_tracking_source')
            # The announced-source verify: the newest hint is the
            # sibling standby's — its checkpoint merely replays the
            # demoted line, so it is the provisional adoption.
            adopted = 'd' if self.adopted_driven \
                else self.announced['a'][0]
            if not self.silent_adoption:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.ADDRS[adopted]})
            self.source['a'] = adopted
        elif self.restore_fails:
            self._raise(409, 'no_tracking_source')
        else:
            # A configured --standby source covers the demotion — no
            # verify, no adoption.
            self.source[peer] = 'a'
        self._journal(peer, 'role_changed',
                      {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        # The keep-claim release: the claim stands, marked yielded.
        self.claim = {'owner': peer, 'yielded': True}
        return 200, {'role': 'demoting', 'tick': self.tick}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.role[peer] != 'standby' \
                or self.sync[peer] in (None, 'unsynchronized'):
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        if self.driven_promote_refused and peer == 'd':
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer]}})
        claim = self.claim
        if self.sync[peer] == 'orphaned':
            # The conditional orphan claim: granted over a yielded
            # claim, refused while a live different-owner unyielded
            # claim stands.
            held = claim and not claim['yielded'] \
                and claim['owner'] != peer
            if held and not self.islanded_preempts:
                if self.wrong_refusal:
                    self._raise(409, {'not_converged': {
                        'sync': self.sync[peer]}})
                self._raise(409, {'field_claim_failed': {
                    'detail': 'writer claim held by '
                              + self.ADDRS.get(claim['owner'],
                                               claim['owner'])}})
        self.claim = {'owner': peer, 'yielded': False}
        self.role[peer] = 'promoting'
        return 200, {'role': 'promoting', 'tick': self.tick}

    # ---- the endpoint dispatch -------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/scan'):
            if peer != 'd':
                self._raise(409, 'paced')
            for _ in range(int((body or {}).get('scans', 0))):
                self._scan('d')
            return 200, {'role': self.role['d'], 'tick': self.tick}
        if peer != 'd':
            # The paced peers: every endpoint call is one scan.
            self._scan(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, self._checkpoint(peer)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if query else 0
            return 200, [entry for entry in self.journal[peer]
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class StaleIslandTests(unittest.TestCase):
    """The stale-island leg against the stubbed three-peer rig: a
    clean rig passes with identical digests and evidence — the
    demotion adopting the sibling, the islanded verdicts and their
    journal records, the islanded promote's field_claim_failed
    refusal, the re-resolution onto the driven owner, and the role
    restore — each doctored defect reports the named diagnostic, and
    an unconverged, unreachable, unkeyed, seam-less, or pre-contract
    run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = StaleIslandFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'start_driven': feed.start_driven,
               'stop_driven': feed.stop_driven}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ISLAND_SETTLE', 1.0), \
                patch.object(scenarios, 'ISLAND_FORM', 1.0), \
                patch.object(scenarios, 'ISLAND_POLL', 0.001):
            return scenarios.scenario_stale_island_resolution(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The stale-island leg's window: behind the peer-announce and
        # forged-demote legs, still inside the launch-layout window
        # the tune case's a->b switch closes.
        self.assertLess(
            order.index(scenarios.scenario_peer_announce),
            order.index(
                scenarios.scenario_stale_island_resolution))
        self.assertLess(
            order.index(
                scenarios.scenario_stale_island_resolution),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function(
            'stale-island-resolution'),
            scenarios.scenario_stale_island_resolution)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('stale-island-pass-1.json',
                     'stale-island-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('stale-island-pass-1.json',
                               'stale-island-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'adopted': 'sibling', 'island': 'formed',
             'islanded_promote': 'field_claim_failed',
             'resolution': 'resolved', 'roles': 'restored'})
        # The demotion journaled its adoption of the sibling — the
        # sibling's :8081 listen port, not the driven peer's :8082.
        adopted = passes[0]['adoptions']
        self.assertEqual(len(adopted), 1)
        self.assertTrue(adopted[0]['source'].endswith(':8081'),
                        adopted)
        # The island: both demoted-pair members reported orphaned and
        # journaled the transition.
        self.assertTrue(passes[0]['island']['active'])
        self.assertTrue(passes[0]['island']['standby'])
        self.assertTrue(passes[0]['field_orphaned']['active'])
        self.assertTrue(passes[0]['field_orphaned']['standby'])
        # The islanded promote refused while the incumbent's claim
        # stood; the incumbent's role never moved.
        self.assertEqual(
            passes[0]['islanded_promote']['status'], 409)
        self.assertIn('field_claim_failed',
                      passes[0]['islanded_promote']['body'])
        self.assertEqual(
            passes[0]['incumbent']['role'], 'promoting')
        # The re-resolution: every islanded peer's checkpoint
        # propagates the driven owner's :8082 stamp.
        for name in ('active', 'standby'):
            self.assertTrue(
                passes[0]['line_owner'][name].endswith(':8082'),
                passes[0]['line_owner'])
        report.validate_scenario(record)

    def test_demote_refused_fails(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_silent_adoption_fails(self):
        self.feed.silent_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('adoptions', record['detail'])
        report.validate_scenario(record)

    def test_adopted_driven_fails(self):
        # The demotion adopted the driven peer — the demoted-pair
        # island the leg induces never formed.
        self.feed.adopted_driven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('sibling', record['detail'])
        report.validate_scenario(record)

    def test_island_never_forms_fails(self):
        # The pre-fix wedge: the demoted pair keeps reporting tracking
        # on an ownerless line — no orphaned verdict ever surfaces.
        self.feed.never_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('orphaned', record['detail'])
        report.validate_scenario(record)

    def test_orphan_journal_absent_fails(self):
        self.feed.orphan_journal_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('field_orphaned', record['detail'])
        report.validate_scenario(record)

    def test_driven_promote_refused_fails(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('driven', record['detail'])
        report.validate_scenario(record)

    def test_islanded_promote_preempts_fails(self):
        # The defect the contract closes: the islanded promote lands —
        # the incumbent's live unyielded claim was preempted.
        self.feed.islanded_preempts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('field_claim_failed', record['detail'])
        report.validate_scenario(record)

    def test_wrong_refusal_verdict_fails(self):
        self.feed.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('field_claim_failed', record['detail'])
        report.validate_scenario(record)

    def test_never_resolves_fails(self):
        # The islanded pair's probes never re-target the live owner —
        # the wedged island the contract exists to break.
        self.feed.never_resolves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_line_owner_stale_fails(self):
        # The reconverged peers keep propagating the dead owner's
        # stamp — the propagated line_owner never named the live one.
        self.feed.stale_line_owner = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('line_owner', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.restore_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-failed'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_run_reports_inconclusive(self):
        self.feed.pair_token = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pair-token', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_deployed_pair_runs_on_the_probe_pair(self):
        # The deployed pair carries no --pair-token; the lane-staged
        # probe pair the ctx['probe'] subject names is keyed — the
        # leg exercises the contract on it and reports a real
        # verdict instead of a capability skip (#1058).
        record = self.run_scenario(pair_token=None,
                                   probe=self.ctx())
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertIn('probe pair',
                      ' '.join(record['observations']))
        report.validate_scenario(record)

    def test_missing_driven_action_reports_inconclusive(self):
        record = self.run_scenario(start_driven=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_driven', record['detail'])
        report.validate_scenario(record)

    def test_launch_failure_reports_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch', record['detail'])
        report.validate_scenario(record)

    def test_driven_never_serves_reports_inconclusive(self):
        self.feed.driven_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('/role', record['detail'])
        report.validate_scenario(record)

    def test_predates_contract_reports_inconclusive(self):
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_island_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'island-resolution-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = StaleIslandFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
