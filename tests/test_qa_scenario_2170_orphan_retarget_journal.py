"""The 2170_orphan_retarget_journal leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_orphan_retarget_journal, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'OrphanRetargetTests.test_registered',
    'OrphanRetargetTests.test_clean_rig_passes_and_validates',
    'OrphanRetargetTests.test_retarget_silent_fails',
    'OrphanRetargetTests.test_retarget_misattributed_fails',
    'OrphanRetargetTests.test_retarget_misordered_fails',
    'OrphanRetargetTests.test_orphan_evidence_absent_fails',
    'OrphanRetargetTests.test_island_never_forms_fails',
    'OrphanRetargetTests.test_never_resolves_fails',
    'OrphanRetargetTests.test_line_owner_stale_fails',
    'OrphanRetargetTests.test_restore_wedged_fails',
    'OrphanRetargetTests.test_demote_refused_reports_nondeterministic',
    'OrphanRetargetTests.test_silent_adoption_reports_'
    'nondeterministic',
    'OrphanRetargetTests.test_driven_unconverged_reports_'
    'nondeterministic',
    'OrphanRetargetTests.test_driven_promote_refused_reports_'
    'nondeterministic',
    'OrphanRetargetTests.test_restore_demote_refused_reports_'
    'nondeterministic',
    'OrphanRetargetTests.test_restore_promote_refused_reports_'
    'nondeterministic',
    'OrphanRetargetTests.test_diverging_digests_report_'
    'nondeterministic',
    'OrphanRetargetTests.test_silent_judge_reports_unchecked',
    'OrphanRetargetTests.test_unreachable_pair_reports_inconclusive',
    'OrphanRetargetTests.test_unconverged_pair_reports_inconclusive',
    'OrphanRetargetTests.test_swapped_layout_reports_inconclusive',
    'OrphanRetargetTests.test_unkeyed_run_reports_inconclusive',
    'OrphanRetargetTests.test_unkeyed_deployed_pair_runs_on_the_'
    'probe_pair',
    'OrphanRetargetTests.test_missing_driven_action_reports_'
    'inconclusive',
    'OrphanRetargetTests.test_missing_journal_files_reports_'
    'inconclusive',
    'OrphanRetargetTests.test_launch_failure_reports_inconclusive',
    'OrphanRetargetTests.test_driven_never_serves_reports_'
    'inconclusive',
    'OrphanRetargetTests.test_predates_contract_reports_inconclusive',
    'OrphanRetargetTests.test_single_endpoint_reports_inconclusive',
    'OrphanRetargetTests.test_two_runs_produce_identical_evidence',
})


class RetargetJournalFeed:
    """A stubbed three-peer rig for the orphan-retarget leg: ctrl-a
    owns the field and configured no tracking source — its demotion
    verifies the announced hints and adopts the newest, the sibling
    standby — and ctrl-b tracks a through its configured --standby
    pull; the `start_driven`/`stop_driven` ctx actions stand the
    run's driven third controller on ctrl-d — `--standby a
    --driven`, its scans and checkpoint pulls running only inside a
    POST /scan batch.

    The tracking/claim half models the #1137 contract the leg pins:
    every tracking pull announces the puller on its source, a demoted
    owner releases its claim yielded, and an orphaned peer's
    resolution probe follows the field's claim verdict, the
    propagated line_owner, the tracked source, and the recorded
    announcers onto whichever candidate serves the line as its field
    owner — re-targeting the pulls through the resolved pin and
    journaling the switch as a `tracking_source_adopted` record
    naming the verified owner, exactly like the announced- and
    claimed-source adoptions. Every endpoint call on a paced peer is
    one completed scan, so two runs emit identical evidence. Doctor
    flags stage each named defect the issue calls out."""

    ADDRS = {'a': 'probe-a:8080', 'b': 'probe-b:8081',
             'd': 'probe-d:8082'}
    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b', 'ctrl-d:3': 'd'}

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = 100
        self.up = {'a': True, 'b': True, 'd': False}
        self.role = {'a': 'active', 'b': 'standby', 'd': 'standby'}
        # The checkpoint source each standby pulls — ctrl-a launched
        # with none; its demotion owes the announced adoption. The
        # resolved pin is the orphan probe's re-target, outranking
        # the configured source.
        self.source = {'a': None, 'b': 'a', 'd': 'a'}
        self.resolved = {'a': None, 'b': None, 'd': None}
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
        self.restoring = False
        self.seq = {'a': 0, 'b': 0, 'd': 0}
        self.served = {'a': [], 'b': [], 'd': []}
        self.pair_token = 'probe-pair'
        self.journals = {}
        for peer in ('a', 'b', 'd'):
            path = self.tmp / ('journal-' + peer + '.jsonl')
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            self.journals[peer] = path
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_tracking = False      # the standby never converges
        self.swapped = False          # ctrl-b already owns the field
        self.demote_refused = False   # the island-inducing demote
                                      # answers no_tracking_source
        self.silent_adoption = False  # the parity adoption never
                                      # journals
        self.never_orphaned = False   # peers keep reporting tracking
                                      # on an ownerless line
        self.orphan_silent = False    # the orphaned verdict never
                                      # journals
        self.driven_wedges = False    # the driven peer never reports
                                      # a tracking standby
        self.driven_promote_refused = False  # the driven peer's
                                             # promote is refused
        self.retarget_silent = False  # the resolved re-target lands
                                      # unjournaled — the #1137 defect
        self.retarget_misattributed = False  # the re-target record
                                             # names the wrong source
        self.retarget_early = False   # the re-target record lands
                                      # ahead of its orphan bracket
        self.never_resolves = False   # the orphan probes never
                                      # re-target the live owner
        self.stale_line_owner = False  # the reconverged peers'
                                       # checkpoints keep propagating
                                       # the dead owner's stamp
        self.restore_demote_refused = False  # the driven owner's
                                             # demote is refused
        self.restore_promote_refused = False  # the entry owner's
                                              # re-promote is refused
        self.wedged_restore = False   # the sibling never re-tracks
                                      # the restored owner
        self.launch_fails = False     # start_driven raises
        self.driven_down = False      # the launched peer never serves
        self.predates_contract = False  # served checkpoints carry no
                                        # ownership stamps

    # ---- the served surface --------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _journal(self, peer, kind, body, seq=None):
        self.seq[peer] += 1
        entry = {'seq': seq if seq is not None else self.seq[peer],
                 'tick': self.tick, 'event': {kind: body}}
        self.served[peer].append(entry)
        with self.journals[peer].open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick}
        if self.role[peer] == 'standby':
            sync = self.sync[peer]
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.tick}}
            else:
                report['sync'] = sync
        return report

    def _checkpoint(self, peer):
        doc = {'format_version': 1,
               'model_fingerprint': 'retarget-fp',
               'generation': 424242, 'tick': self.tick,
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
        """One tracking pull of the peer's current source — the
        resolved pin outranking the configured one: the pull announces
        on the source, applies its served verdict, and — while the
        verdict is orphaned — runs the re-resolution probe onto
        whichever candidate serves the line as its field owner,
        journaling the re-target like the announced/claimed pins."""
        source = self.resolved[peer] or self.source[peer]
        if source is None:
            return
        if peer == 'd' and self.driven_wedges:
            self.sync[peer] = 'unsynchronized'
            return
        if not self.up[source]:
            self.sync[peer] = 'unsynchronized'
            return
        ann = self.announced[source]
        if peer in ann:
            ann.remove(peer)
        ann.insert(0, peer)
        if self.no_tracking and peer == 'b':
            self.sync[peer] = 'unsynchronized'
            return
        if self.role[source] == 'active':
            self.sync[peer] = 'tracking'
            self.stamp[peer] = self.stamp[source]
            return
        self.stamp[peer] = self.stamp[source]
        if self.never_orphaned:
            self.sync[peer] = 'tracking'
            return
        if self.sync[peer] != 'orphaned' and not self.orphan_silent:
            self._journal(peer, 'field_orphaned',
                          {'aligned': self.tick})
        self.sync[peer] = 'orphaned'
        if self.never_resolves \
                or (self.wedged_restore and self.restoring
                    and peer == 'b'):
            return
        # The orphan-resolution probe: the field's claim verdict leads
        # — the monitor the standing claim's owner declared — then
        # the propagated line owner, the tracked source, and the
        # recorded announcers; a candidate must serve the line as its
        # field owner to earn the re-target.
        candidates = []
        if self.claim and not self.claim['yielded'] \
                and self.claim['owner'] != peer:
            candidates.append(self.claim['owner'])
        candidates.append(self.stamp[peer])
        if source not in candidates:
            candidates.append(source)
        candidates += [hint for hint in self.announced[peer]
                       if hint not in candidates]
        for candidate in candidates:
            if candidate == peer or candidate not in self.role \
                    or not self.up.get(candidate) \
                    or self.role[candidate] != 'active':
                continue
            self.resolved[peer] = candidate
            if not self.retarget_silent:
                source_addr = self.ADDRS[candidate]
                if self.retarget_misattributed:
                    source_addr = self.ADDRS[
                        'b' if candidate != 'b' else 'a']
                self._journal(
                    peer, 'tracking_source_adopted',
                    {'source': source_addr},
                    seq=self.seq[peer] if self.retarget_early
                    else None)
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

    # ---- the runner-owned lifecycle actions ------------------------

    def start_driven(self, active):
        assert active == 'active', active
        if self.launch_fails:
            raise RuntimeError('docker run failed: launch refused')
        self.up['d'] = not self.driven_down
        self.role['d'] = 'standby'
        self.sync['d'] = 'unsynchronized'
        self.source['d'] = 'a'
        self.resolved['d'] = None
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
            # demoted line, so it is the provisional adoption and the
            # parity baseline the re-target is compared against.
            adopted = self.announced['a'][0]
            if not self.silent_adoption:
                self._journal('a', 'tracking_source_adopted',
                              {'source': self.ADDRS[adopted]})
            self.source['a'] = adopted
        elif self.restore_demote_refused and peer == 'd':
            self._raise(409, 'no_tracking_source')
        else:
            # A configured --standby source covers the demotion — no
            # verify, no adoption.
            self.source[peer] = 'a'
            self.resolved[peer] = None
        if peer == 'd':
            self.restoring = True
        self._journal(peer, 'role_changed',
                      {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        self.sync[peer] = 'unsynchronized'
        self.resolved[peer] = None
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
            self._raise(409, {'field_claim_failed': {
                'detail': 'writer claim held by '
                          + self.ADDRS['a']}})
        if self.restore_promote_refused and peer == 'a':
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer]}})
        claim = self.claim
        if self.sync[peer] == 'orphaned' and claim \
                and not claim['yielded'] \
                and claim['owner'] != peer:
            # The conditional orphan claim meets a live incumbent's
            # unyielded claim — the named refusal, never a preemption.
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
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class OrphanRetargetTests(unittest.TestCase):
    """The orphan-retarget-journal leg against the stubbed three-peer
    rig: a clean rig passes with identical digests and evidence — the
    demotion adopting the sibling as the parity baseline, the
    island's orphaned verdicts and their journaled transitions, each
    islanded peer's durable trail carrying the re-target's
    tracking_source_adopted naming the driven owner behind the
    orphan record, the reconvergence, and the role restore — each
    doctored contract breach reports retarget-journal-failed, each
    instability reports retarget-journal-nondeterministic, and an
    unreachable, unconverged, unkeyed, seam-less, or pre-contract
    run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RetargetJournalFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'journal_files': {
                   'active': str(feed.journals['a']),
                   'standby': str(feed.journals['b'])},
               'start_driven': feed.start_driven,
               'stop_driven': feed.stop_driven}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RETARGET_SETTLE', 1.0), \
                patch.object(scenarios, 'RETARGET_FORM', 1.0), \
                patch.object(scenarios, 'RETARGET_POLL', 0.001):
            return scenarios.scenario_orphan_retarget_journal(
                self.ctx(feed, **overrides))

    def passes(self):
        names = ('orphan-retarget-journal-pass-1.json',
                 'orphan-retarget-journal-pass-2.json')
        for name in names:
            self.assertTrue((self.evidence / name).is_file(), name)
        return [json.loads((self.evidence / name).read_text())
                for name in names]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The orphan-retarget leg's window: behind the
        # orphan-episode leg whose island staging it reuses, before
        # the failover case whose launch roles it restores.
        self.assertLess(
            order.index(scenarios.scenario_orphan_episode_bound),
            order.index(
                scenarios.scenario_orphan_retarget_journal))
        self.assertLess(
            order.index(
                scenarios.scenario_orphan_retarget_journal),
            order.index(scenarios.scenario_failover))
        self.assertIs(verify.case_function(
            'orphan-retarget-journal'),
            scenarios.scenario_orphan_retarget_journal)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'settled': 'held', 'driven': 'tracking',
             'adopted': 'sibling', 'island': 'formed',
             'orphans': 'journaled', 'retarget': 'attributed',
             'resolution': 'reconverged',
             'line_owner': 'propagated', 'roles': 'restored'})
        record1 = passes[0]['record']
        # The parity baseline: the demotion journaled one adoption
        # naming the sibling standby's :8081 listen port.
        self.assertEqual(len(record1['adoptions']), 1)
        self.assertTrue(
            record1['adoptions'][0]['source'].endswith(':8081'),
            record1['adoptions'])
        # The durable trail: the owner's journal carries the
        # announced :8081 adoption, its orphan transition, then the
        # resolved :8082 re-target — the same named record the
        # announced path produces; the standby's carries its orphan
        # transition, then its own :8082 re-target.
        for name in ('active', 'standby'):
            trail = record1['trail'][name]
            retargets = [item for item in trail
                         if item['kind'] == 'tracking_source_adopted'
                         and item['body']['source'].endswith(':8082')]
            self.assertEqual(len(retargets), 1, trail)
            orphans = [item for item in trail
                       if item['kind'] == 'field_orphaned']
            self.assertTrue(orphans, trail)
            self.assertGreater(retargets[0]['seq'],
                               max(item['seq'] for item in orphans))
        sources = [item['body']['source'] for item in
                   record1['trail']['active']
                   if item['kind'] == 'tracking_source_adopted']
        self.assertEqual(len(sources), 2, sources)
        self.assertTrue(sources[0].endswith(':8081'), sources)
        self.assertTrue(sources[1].endswith(':8082'), sources)
        # The reconvergence and the propagated ownership evidence.
        for name in ('active', 'standby'):
            self.assertIn('tracking',
                          record1['resolved'][name]['sync'])
            self.assertTrue(
                record1['line_owner'][name].endswith(':8082'),
                record1['line_owner'])
        self.assertEqual(record1['resolved']['driven']['role'],
                         'active')
        report.validate_scenario(record)

    def test_retarget_silent_fails(self):
        # The doctored negative the issue names: the islanded peers
        # re-resolve and reconverge while the durable journal stays
        # silent — the re-point with no attribution the contract
        # exists to forbid.
        self.feed.retarget_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('tracking_source_adopted', record['detail'])
        report.validate_scenario(record)

    def test_retarget_misattributed_fails(self):
        # The journaled re-target names the wrong endpoint — the
        # durable trail attributes the switch to a source the probe
        # never verified.
        self.feed.retarget_misattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        report.validate_scenario(record)

    def test_retarget_misordered_fails(self):
        # The re-target record lands ahead of the field_orphaned
        # transition that explains it — the durable trail cannot
        # show why the pulls moved.
        self.feed.retarget_early = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('ahead', record['detail'])
        report.validate_scenario(record)

    def test_orphan_evidence_absent_fails(self):
        self.feed.orphan_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('field_orphaned', record['detail'])
        report.validate_scenario(record)

    def test_island_never_forms_fails(self):
        self.feed.never_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('island', record['detail'])
        report.validate_scenario(record)

    def test_never_resolves_fails(self):
        # The islanded peers' probes never re-target the live owner —
        # the wedged island whose resolution never lands.
        self.feed.never_resolves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        report.validate_scenario(record)

    def test_line_owner_stale_fails(self):
        self.feed.stale_line_owner = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('line_owner', record['detail'])
        report.validate_scenario(record)

    def test_restore_wedged_fails(self):
        # The sibling never re-tracks the restored owner — the launch
        # layout the cases behind this one meet is unrestored.
        self.feed.wedged_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-failed'), record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_silent_adoption_reports_nondeterministic(self):
        self.feed.silent_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('adopt', record['detail'])
        report.validate_scenario(record)

    def test_driven_unconverged_reports_nondeterministic(self):
        self.feed.driven_wedges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('driven', record['detail'])
        report.validate_scenario(record)

    def test_driven_promote_refused_reports_nondeterministic(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('/promote', record['detail'])
        report.validate_scenario(record)

    def test_restore_demote_refused_reports_nondeterministic(self):
        self.feed.restore_demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_restore_promote_refused_reports_nondeterministic(self):
        self.feed.restore_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_retarget_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_retarget',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'retarget-journal-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_swapped_layout_reports_inconclusive(self):
        # ctrl-b already owns the field and ctrl-a never converged:
        # the unconfigured peer the island induction needs cannot be
        # restored onto it.
        self.feed.swapped = True
        self.feed.role = {'a': 'standby', 'b': 'active',
                          'd': 'standby'}
        self.feed.sync = {'a': 'unsynchronized', 'b': None,
                          'd': 'unsynchronized'}
        self.feed.source = {'a': None, 'b': 'a', 'd': 'a'}
        self.feed.stamp = {'a': 'b', 'b': 'b', 'd': 'b'}
        self.feed.claim = {'owner': 'b', 'yielded': False}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
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

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
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

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = RetargetJournalFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
