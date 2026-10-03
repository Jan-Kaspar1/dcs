"""The 2370_stranded_standby_no_resync leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_stranded_standby_no_resync, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration half builds on the claim-reclaim leg's fakes the same
induction stages; EXPECTED_CASES pins this module's contribution to
the suite's case coverage so a dropped case fails the discovery
check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2350_claim_reclaim import ReclaimPlantPeer


EXPECTED_CASES = frozenset({
    'StrandedStandbyTests.test_registered',
    'StrandedStandbyTests.test_clean_pair_passes_and_validates',
    'StrandedStandbyTests.test_unkeyed_routable_pair_passes',
    'StrandedStandbyTests.test_promote_refused_reports_failed',
    'StrandedStandbyTests.test_never_demotes_reports_failed',
    'StrandedStandbyTests.test_silent_loss_reports_failed',
    'StrandedStandbyTests.test_unattributed_loss_reports_failed',
    'StrandedStandbyTests.test_misattributed_loss_reports_failed',
    'StrandedStandbyTests.test_no_adoption_reports_failed',
    'StrandedStandbyTests.test_foreign_adoption_reports_failed',
    'StrandedStandbyTests.test_wedged_rejoin_reports_failed',
    'StrandedStandbyTests.test_slow_rejoin_reports_failed',
    'StrandedStandbyTests.test_peer_never_rejoins_reports_failed',
    'StrandedStandbyTests.test_window_tracks_early_reports_failed',
    'StrandedStandbyTests.test_window_never_converges_reports_failed',
    'StrandedStandbyTests.test_dual_active_reports_failed',
    'StrandedStandbyTests.test_diverging_digests_report_'
    'nondeterministic',
    'StrandedStandbyTests.test_unkeyed_wildcard_reports_failed',
    'StrandedStandbyTests.test_claim_staging_refused_reports_'
    'inconclusive',
    'StrandedStandbyTests.test_predating_rig_reports_inconclusive',
    'StrandedStandbyTests.test_open_field_reports_inconclusive',
    'StrandedStandbyTests.test_no_active_reports_failed',
    'StrandedStandbyTests.test_unsettled_pair_reports_inconclusive',
    'StrandedStandbyTests.test_unreachable_pair_reports_inconclusive',
    'StrandedStandbyTests.test_missing_active_endpoint_reports_'
    'inconclusive',
    'StrandedStandbyTests.test_missing_plant_endpoint_reports_'
    'inconclusive',
    'StrandedStandbyTests.test_missing_plant_ctl_reports_inconclusive',
    'StrandedStandbyTests.test_missing_owner_token_reports_'
    'inconclusive',
    'StrandedStandbyTests.test_missing_journal_files_reports_'
    'inconclusive',
    'StrandedStandbyTests.test_bridge_placement_reports_inconclusive',
    'StrandedStandbyTests.test_two_runs_produce_identical_evidence',
})


class StrandedPlantPeer(ReclaimPlantPeer):
    """The stranded-standby rig's plant half: the claim-reclaim
    arbitration plus the claim record's declared-monitor and
    controller-kind stamps — decision 101's field-arbitrated
    rendezvous — and the conditional orphan grant's tool-incumbent
    exemption. Every fencing verdict names the standing claim's
    declared monitor alongside its owner (#1042); a
    `claim_writer_unless_held` refuses only a live unyielded
    *controller* claim — a tool's claim is never an incumbent
    (#1045); `release_writer` honors the `keep_claim` yield mark the
    demotion path uses. The feed drives the controllers' scan
    writes and claims through the record directly. The doctor
    flags stage the predating rig and the absent staging lever."""

    def __init__(self, owner):
        super().__init__(owner)
        self.claim['monitor'] = '0.0.0.0:8080'
        self.claim['controller'] = True
        self.claim['yielded'] = False
        # The doctors staging each named defect.
        self.declared_missing = False   # verdicts carry no monitor —
                                        # the rig predates #1042
        self.claims_refused = False     # claim_writer meets a
                                        # refusal — the claim-
                                        # staging lever is absent

    def _verdict(self, op, point=None):
        response = super()._verdict(op, point)
        monitor = None if self.declared_missing \
            else (self.claim or {}).get('monitor')
        if monitor is not None:
            response['error']['monitor'] = monitor
        return response

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if op == 'claim_writer' and self.claims_refused:
            return {'result': 'error', 'error': {
                'kind': 'invalid_request',
                'detail': 'claim_writer is not a supported op'}}
        if op == 'claim_writer_unless_held':
            claim = self.claim
            if claim is not None and claim['owner'] != owner \
                    and claim.get('controller', True) \
                    and not claim.get('yielded'):
                return self._verdict(op)
            self.claim = {'owner': owner, 'holders': {conn},
                          'monitor': request.get('monitor'),
                          'controller': True, 'yielded': False}
            return {'result': 'done'}
        if op == 'release_writer':
            if self.release_refuses:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'release refused'}}
            if self.claim is not None:
                self.claim['holders'].discard(conn)
                if request.get('keep_claim'):
                    self.claim['yielded'] = True
                elif not self.claim['holders']:
                    self.claim = None
            return {'result': 'done'}
        granted = super()._claim_for(conn, request)
        if op in ('claim_writer', 'ensure_writer') \
                and granted.get('result') in ('done', 'claimed_shared') \
                and self.claim is not None:
            self.claim['monitor'] = request.get('monitor')
            self.claim['controller'] = request.get('controller', True)
            self.claim['yielded'] = False
        return granted

    def dispatch_for(self, conn, request):
        if request.get('op') == 'claim_writer_unless_held':
            self.requests.append(request)
            return self._claim_for(conn, request)
        return super().dispatch_for(conn, request)


class _StrandedMember:
    """One stubbed controller: role, tick, sync posture, journal, and
    the tracking-source slots the contract exercises — the claimed
    monitor the last fencing verdict declared and the
    process-lifetime adopted pin a verified adoption leaves."""

    def __init__(self, key, token, bridge):
        self.key = key            # 'active'/'standby' — the ctx name
        self.token = token        # the pinned --owner-token
        self.bridge = bridge      # the monitor's routable rig address
        self.declared = '0.0.0.0:' + bridge.rsplit(':', 1)[1]
        self.role = 'standby'
        self.sync = 'unsynchronized'
        self.tick = 0
        self.seq = 0
        self.journal = []
        self.adopted = None       # the verified-source pin
        self.claimed = None       # the last verdict's declared monitor
        self.fencing_lost = False
        self.failed_writes = 0
        self.demote_tick = None
        self.demotions = 0


class StrandedPairFeed:
    """A stubbed pair for the stranded-standby leg: 'ctrl-a' is the
    launched field owner with no configured tracking source — the
    stranded half the finding names — and 'ctrl-b' the tracking
    standby on its configured --standby source. Every served monitor
    request is one scan of the addressed member: an active or
    promoting member writes the field — a fenced write journals the
    attributed field_claim_lost, marks the fencing loss, and walks
    demoting -> standby — while a standby member probes the standing
    claim's verdict (its bound conditional re-grant refused while a
    different owner stands; the verdict's declared monitor is the
    claimed-source candidate), then resolves and pulls its tracking
    source: ctrl-b follows its configured source outright, while
    ctrl-a's verified path is the keyed announced hint — journaled
    once when adopted, then held as the process-lifetime pin — or,
    on the unkeyed posture, the claim-declared monitor where the
    rig's declaration is routable (a wildcard declaration is
    unroutable — the wedge the finding reports). Every journaled
    event mirrors into the --journal-file paths the leg's durable
    audit reads. The doctor flags stage each named defect."""

    TOKENS = {'active': 424243, 'standby': 424244}
    BRIDGE = {'active': '172.18.0.2:8080',
              'standby': '172.18.0.3:8081'}

    def __init__(self, plant, keyed=True, journal_files=None):
        self.plant = plant
        self.keyed = keyed
        self.a = _StrandedMember('active', self.TOKENS['active'],
                                 self.BRIDGE['active'])
        self.b = _StrandedMember('standby', self.TOKENS['standby'],
                                 self.BRIDGE['standby'])
        self.a.role, self.b.role = 'active', 'standby'
        self.b.sync = 'tracking'
        plant.claim.update(monitor=self.a.declared,
                           controller=True, yielded=False,
                           holders={'active'})
        self.members = {'ctrl-a': self.a, 'ctrl-b': self.b}
        # The durable half: journal_files maps ctx keys to the
        # runner's --journal-file paths — ctrl-a binds 'active',
        # ctrl-b 'standby'. A feed without paths mirrors nothing.
        self.journal_paths = {}
        for member, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
                self.journal_paths[member] = path
        # The doctors staging each named defect.
        self.silent = False            # the pair never answers
        self.no_active = False         # no settled active at baseline
        self.b_unsettled = False       # b never reports tracking
        self.promote_refused = False   # /promote answers an error
        self.a_never_demotes = False   # the fenced write never demotes
        self.b_never_demotes = False
        self.no_loss = False           # the demotion goes unrecorded
        self.unattributed_loss = False # the loss names no claimant
        self.wrong_claimant = False    # the loss names a wrong token
        self.no_adoption = False       # adoptions pin unjournaled
        self.foreign_adoption = False  # the journal names a wrong port
        self.wedge = False             # ctrl-a never resolves a source
        self.slow_rejoin = False       # ctrl-a's re-join passes the bound
        self.rejoin_delay = 80
        self.b_never_rejoins = False   # ctrl-b stays wedged post-demote
        self._b_demoted = False
        self.window_tracks = False     # ctrl-a 'tracks' under the
                                       # monitor-less tool claim
        self.window_wedge = False      # ctrl-a never converges after
                                       # the controller claim re-seats
        self.tool_claim_seen = False
        self.dual_active = False       # ctrl-a re-reports 'active'
                                       # alongside the promoted peer
        self.second_pass_adopts = False  # ctrl-b journals an adoption
                                         # only on its second demotion
        self.routable_declared = False   # declared monitors are
                                         # routable IPs — no wildcard

    def _journal(self, member, event):
        """One journaled record — the served journal plus the durable
        --journal-file mirror the leg's file audit reads."""
        member.seq += 1
        entry = {'seq': member.seq, 'tick': member.tick,
                 'event': event}
        member.journal.append(entry)
        path = self.journal_paths.get(member)
        if path is not None:
            with path.open('a') as stream:
                stream.write(json.dumps({'entry': entry}) + '\n')

    def _owns(self, member):
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token

    def _declared_for(self, member):
        return member.bridge if self.routable_declared \
            else member.declared

    def _field_write(self, member):
        """One controller-scan field write: fenced while the member is
        outside the standing claim's holders — the verdict that
        demotes the owner — else the write lands."""
        claim = self.plant.claim
        if claim is not None and claim['owner'] == member.token \
                and member.key in (claim.get('holders') or set()):
            self.plant.plant_tick += 1
            self.plant.samples[self.plant.OUT].update(
                tick=self.plant.plant_tick, value={'float': 1.5})
            return 'landed'
        return 'fenced'

    def _take_claim(self, member, orphan=False):
        """The promotion's claim: unconditional for a tracking
        standby, the conditional orphan grant for an orphaned one —
        refused only by a live unyielded controller claim."""
        claim = self.plant.claim
        if orphan and claim is not None \
                and claim['owner'] != member.token \
                and claim.get('controller', True) \
                and not claim.get('yielded'):
            return False
        self.plant.claim = {'owner': member.token,
                            'holders': {member.key},
                            'monitor': self._declared_for(member),
                            'controller': True, 'yielded': False}
        return True

    def _supersede(self, member):
        """The contract's demote-in-place: count the fenced write,
        journal the attributed loss, and walk demoting -> standby
        with the fencing-loss mark standing."""
        member.failed_writes += 1
        member.fencing_lost = True
        member.demote_tick = member.tick
        member.demotions += 1
        member.sync = 'unsynchronized'
        claim = self.plant.claim or {}
        if member is self.b:
            self._b_demoted = True
        if not self.no_loss:
            loss = {'point': self.plant.OUT}
            if not self.unattributed_loss:
                loss['claimant'] = 0xDEAD if self.wrong_claimant \
                    else claim.get('owner')
            self._journal(member, {'field_claim_lost': loss})
        if member is self.b and self.second_pass_adopts \
                and member.demotions == 2:
            self._journal(member, {'tracking_source_adopted': {
                'source': self.a.bridge}})
        never_demotes = self.a_never_demotes if member is self.a \
            else self.b_never_demotes
        if never_demotes:
            return
        member.role = 'demoting'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting'}})

    def _resolve(self, member):
        """The tracking source this scan resolves, or None — ctrl-b
        follows its configured --standby source outright; ctrl-a
        adopts the keyed announced hint, or on the unkeyed posture
        the claim-declared monitor where the declaration is
        routable — a wildcard-declared monitor is unroutable, the
        wedge the finding reports."""
        if member is self.b:
            if self.b_unsettled \
                    or (self.b_never_rejoins and self._b_demoted):
                return None
            return 'configured'
        if member.adopted is not None:
            return member.adopted
        if self.wedge or (self.window_wedge and self.tool_claim_seen):
            return None
        if self.keyed:
            source = self.b.bridge
        else:
            source = member.claimed
            if source is None or str(source).startswith('0.0.0.0:'):
                return None
        member.adopted = source
        journaled = '172.18.0.9:9999' if self.foreign_adoption \
            else source
        if not self.no_adoption:
            self._journal(member, {'tracking_source_adopted': {
                'source': journaled}})
        return source

    def _serves_owner(self, source):
        """Whether the candidate's pulled document stamps
        source_owns_field — the endpoint serves this line as its
        field owner."""
        claim = self.plant.claim
        if claim is None:
            return False
        if source in ('configured', self.a.bridge):
            return claim['owner'] == self.a.token
        if source == self.b.bridge:
            return claim['owner'] == self.b.token
        return False

    def _track(self, member):
        """One standby scan: the fencing-loss bound re-grant probes
        first — refused while a different-owner claim stands — then
        the tracking-source resolution and pull."""
        claim = self.plant.claim
        member.claimed = (claim or {}).get('monitor')
        if claim is not None and claim.get('controller') is False:
            self.tool_claim_seen = True
        if member is self.a and self.window_wedge \
                and self.tool_claim_seen:
            # The staged defect: no source the demoted member pulls
            # ever produces a converged state after the re-seat.
            member.sync = 'unsynchronized'
            return
        if member.fencing_lost \
                and (claim is None or claim['owner'] == member.token):
            # The bound conditional re-grant lands: the marked
            # ex-owner re-seats the claim and walks promoting.
            self.plant.claim = {
                'owner': member.token, 'holders': {member.key},
                'monitor': self._declared_for(member),
                'controller': True, 'yielded': False}
            member.fencing_lost = False
            member.role = 'promoting'
            member.sync = 'unsynchronized'
            self._journal(member, {'role_changed': {
                'from': 'standby', 'to': 'promoting'}})
            return
        source = self._resolve(member)
        if source is None:
            member.sync = 'unsynchronized'
            return
        if self.window_tracks and claim is not None \
                and claim.get('controller') is False:
            member.sync = 'tracking'
            return
        if self._serves_owner(source):
            if self.slow_rejoin and member is self.a \
                    and member.demote_tick is not None \
                    and member.tick - member.demote_tick \
                    <= self.rejoin_delay:
                member.sync = 'unsynchronized'
            else:
                member.sync = 'tracking'
        else:
            member.sync = 'orphaned'

    def _scan(self, member):
        """One controller scan of the addressed member."""
        member.tick += 1
        if member.role == 'demoting':
            member.role = 'standby'
            self._journal(member, {'role_changed': {
                'from': 'demoting', 'to': 'standby'}})
            return
        if member.role == 'promoting':
            member.role = 'active'
            self._journal(member, {'role_changed': {
                'from': 'promoting', 'to': 'active'}})
            return
        if member.role == 'active':
            if self._field_write(member) == 'fenced':
                self._supersede(member)
            return
        self._track(member)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        member = self.members.get(host.split(':')[0])
        if member is None:
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent:
            raise urllib.error.URLError('unreachable')
        self._scan(member)
        if (method, route) == ('GET', '/role'):
            role = member.role
            if self.dual_active and member is self.a \
                    and member.sync == 'tracking':
                role = 'active'
            if self.no_active and member is self.a:
                role = 'standby'
            report = {'role': role, 'tick': member.tick}
            if role == 'standby':
                report['sync'] = {
                    member.sync: ({'aligned': member.tick}
                                  if member.sync == 'tracking'
                                  else {})}
            return 200, report
        if (method, route) == ('GET', '/journal'):
            return 200, [entry for entry in member.journal
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/checkpoint'):
            doc = {'tick': member.tick, 'generation': 7,
                   'source_owns_field': self._owns(member)
                   or member.sync == 'tracking',
                   'line_owner':
                       (self.plant.claim or {}).get('monitor')}
            if self.keyed:
                doc['line_proof'] = {'nonce': 1, 'proof': 'fake'}
            return 200, doc
        if (method, route) == ('POST', '/promote'):
            if self.promote_refused:
                return 500, {'error': 'refused'}
            if member.role in ('active', 'promoting'):
                return 409, 'already_active'
            if member.sync == 'unsynchronized':
                return 409, 'not_converged'
            if member.role == 'standby':
                if not self._take_claim(
                        member, orphan=member.sync == 'orphaned'):
                    return 409, 'field_claim_failed'
                member.fencing_lost = False
                member.role = 'promoting'
                member.sync = 'unsynchronized'
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting'}})
            return 200, {'role': member.role}
        if (method, route) == ('POST', '/demote'):
            if self._owns(member):
                self.plant.claim['yielded'] = True
            if member.role in ('active', 'promoting'):
                member.role = 'demoting'
                member.sync = 'unsynchronized'
                self._journal(member, {'role_changed': {
                    'from': 'active', 'to': 'demoting'}})
            return 200, {'role': member.role}
        raise AssertionError('unhandled ' + url)


class StrandedStandbyTests(unittest.TestCase):
    """The stranded-standby re-join scenario against the stubbed
    pair: a clean rig passes with identical digests and evidence —
    the involuntary promote fencing the owner into its attributed
    demotion, the declared monitor's verified-source adoption and
    the bounded re-join, both promote directions proving the pair
    stays promotable, and the monitor-less tool claim parking the
    demoted peer only until a controller claim re-seats the field —
    each doctored defect reports the named diagnostic, and the
    unreachable, contract-predating, or seam-less rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = StrandedPlantPeer(StrandedPairFeed.TOKENS[
            'active'])
        self.feed = StrandedPairFeed(
            self.plant, journal_files=self._journal_files())

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _journal_files(self, subdir=''):
        base = self.journal_dir / subdir if subdir \
            else self.journal_dir
        Path(base).mkdir(parents=True, exist_ok=True)
        return {key: str(Path(base) / (key + '.jsonl'))
                for key in ('active', 'standby')}

    def _ctx(self, **overrides):
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'plant_ctl': self.plant.ctl,
               'plant_owner': dict(self.feed.TOKENS),
               'journal_files': self._journal_files(),
               'endpoint_placement': {'active': 'loopback',
                                      'standby': 'loopback',
                                      'plant': 'loopback'},
               'pair_token': 'dcs-qa-pair',
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'STRANDED_SETTLE', 2), \
                patch.object(scenarios, 'STRANDED_POLL', 0.001), \
                patch.object(scenarios, 'STRANDED_ROUNDS', 2), \
                patch.object(scenarios, 'STRANDED_DEADLINE', 2), \
                patch.object(scenarios, 'STRANDED_REJOIN_TICKS', 30):
            return scenarios.scenario_stranded_standby_no_resync(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_stranded_standby_no_resync,
            scenarios.SCENARIOS)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('tracking', self.feed.b.sync)
        self.assertEqual(
            self.feed.TOKENS['active'],
            (self.plant.claim or {}).get('owner'))
        # The leg staged the tool claim the #1045 window needs —
        # explicitly controller-less, declaring no monitor.
        tools = [request for request in self.plant.requests
                 if request.get('op') == 'claim_writer'
                 and request.get('owner')
                 == scenarios.STRANDED_FOREIGN]
        self.assertTrue(tools)
        for request in tools:
            self.assertIs(request.get('controller'), False)
            self.assertIsNone(request.get('monitor'))

    def test_unkeyed_routable_pair_passes(self):
        feed = StrandedPairFeed(
            self.plant, keyed=False,
            journal_files=self._journal_files('unkeyed'))
        feed.routable_declared = True
        record = self.run_scenario(
            feed=feed, ctx=self._ctx(
                pair_token=None,
                journal_files=self._journal_files('unkeyed')))
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)

    def test_promote_refused_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('stranded-standby-no-resync-failed',
                      record.get('detail', ''))

    def test_never_demotes_reports_failed(self):
        self.feed.a_never_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_silent_loss_reports_failed(self):
        self.feed.no_loss = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('loss', record.get('detail', ''))

    def test_unattributed_loss_reports_failed(self):
        self.feed.unattributed_loss = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claimant', record.get('detail', ''))

    def test_misattributed_loss_reports_failed(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_no_adoption_reports_failed(self):
        self.feed.no_adoption = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('tracking source', record.get('detail', '')
                      .lower())

    def test_foreign_adoption_reports_failed(self):
        self.feed.foreign_adoption = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_wedged_rejoin_reports_failed(self):
        self.feed.wedge = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('re-join', record.get('detail', ''))

    def test_slow_rejoin_reports_failed(self):
        self.feed.slow_rejoin = True
        self.feed.rejoin_delay = 60
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('ticks', record.get('detail', ''))

    def test_peer_never_rejoins_reports_failed(self):
        self.feed.b_never_rejoins = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_window_tracks_early_reports_failed(self):
        self.feed.window_tracks = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('monitor-less', record.get('detail', ''))

    def test_window_never_converges_reports_failed(self):
        self.feed.window_wedge = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_dual_active_reports_failed(self):
        self.feed.dual_active = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_diverging_digests_report_nondeterministic(self):
        self.feed.second_pass_adopts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('stranded-standby-no-resync-nondeterministic',
                      record.get('detail', ''))

    def test_unkeyed_wildcard_reports_failed(self):
        feed = StrandedPairFeed(
            self.plant, keyed=False,
            journal_files=self._journal_files('unkeyed'))
        # The wildcard declaration is unroutable — the unkeyed rig
        # strands the demoted peer, the live signature the finding
        # names; the leg reports it failed, not inconclusive.
        record = self.run_scenario(
            feed=feed, ctx=self._ctx(
                pair_token=None,
                journal_files=self._journal_files('unkeyed')))
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_claim_staging_refused_reports_inconclusive(self):
        self.plant.claims_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('claim-staging', record.get('detail', ''))

    def test_predating_rig_reports_inconclusive(self):
        self.plant.declared_missing = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('predates', record.get('detail', ''))

    def test_open_field_reports_inconclusive(self):
        self.plant.open_field = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_unsettled_pair_reports_inconclusive(self):
        self.feed.b_unsettled = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_active_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['active']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_ctl_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_ctl']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_owner_token_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_owner']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_journal_files_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['journal_files']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_bridge_placement_reports_inconclusive(self):
        ctx = self._ctx(endpoint_placement={
            'active': 'loopback', 'standby': 'loopback',
            'plant': 'bridge'})
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        second = self.run_scenario()
        self.assertEqual('passed', first['outcome'], first)
        self.assertEqual('passed', second['outcome'], second)
        self.assertEqual(first['observations'],
                         second['observations'])
        self.assertEqual(
            [entry['ref'] for entry in first['evidence']],
            [entry['ref'] for entry in second['evidence']])


if __name__ == '__main__':
    unittest.main()
