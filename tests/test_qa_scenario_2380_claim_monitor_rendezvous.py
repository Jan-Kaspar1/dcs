"""The 2380_claim_monitor_rendezvous leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_claim_monitor_rendezvous, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration half builds on the stranded-standby leg's fakes the same
induction stages.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2370_stranded_standby_no_resync \
    import StrandedPlantPeer


class RendezvousPlantPeer(StrandedPlantPeer):
    """The rendezvous rig's plant half: the stranded-standby claim
    arbitration — the standing claim's owner, declared monitor, and
    controller-kind stamps carried on every fencing verdict — plus
    `probe_writer`, the non-mutating claim read surface the leg
    polls: the verdict a mutation from the probing attachment would
    meet, answered without mutating (`fenced` with owner and
    declared monitor while a foreign claim stands, `unclaimed`
    while none does — the leg's probe connection never holds, so a
    `done` answer cannot appear). The doctor flag stages a rig whose
    claim surface predates the read op."""

    def __init__(self, owner):
        super().__init__(owner)
        self.probe_absent = False   # probe_writer meets a refusal —
                                    # the claim read surface predates
                                    # the contract

    def dispatch_for(self, conn, request):
        if request.get('op') == 'probe_writer':
            self.requests.append(request)
            if self.probe_absent:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'probe_writer is not a supported op'}}
            if self.claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            return self._verdict('probe_writer')
        return super().dispatch_for(conn, request)


class _RendezvousMember:
    """One stubbed controller: role, tick, sync posture, journal, and
    the tracking-source slots the rendezvous contract exercises —
    the process-lifetime adopted pin a verified adoption leaves."""

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
        self.fencing_lost = False
        self.demote_tick = None


class RendezvousPairFeed:
    """A stubbed pair for the claim-monitor-rendezvous leg: 'ctrl-a'
    is the launched field owner with no configured tracking source
    — the half whose demote owes the claim-declared rendezvous —
    and 'ctrl-b' the tracking standby on its configured --standby
    source. Every served monitor request is one scan of the
    addressed member: an active member writes the field — a fenced
    write journals the attributed field_claim_lost and walks
    demoting -> standby — while a standby member resolves and pulls
    its tracking source: ctrl-b follows its configured source
    outright, while ctrl-a resolves the standing claim's declared
    monitor — the field-arbitrated rendezvous — journaled once when
    adopted, then held as the process-lifetime pin; a stored
    wildcard or dead declaration is undialable, the defect shape.
    Claims declare the wildcard listener shape the deployed rig
    binds and the plant stores the substituted routable address —
    #1135's dialable-monitor normalization — unless a doctor stores
    the declaration verbatim. Every journaled event mirrors into the
    --journal-file paths the leg's durable audit reads. Bridge hosts
    resolve the same members as the ctx names so a foreign dial of
    the declared monitor lands on the owning monitor. The doctor
    flags stage each named defect."""

    TOKENS = {'active': 424243, 'standby': 424244}
    BRIDGE = {'active': '172.18.0.2:8080',
              'standby': '172.18.0.3:8081'}

    def __init__(self, plant, keyed=True, journal_files=None):
        self.plant = plant
        self.keyed = keyed
        self.a = _RendezvousMember('active', self.TOKENS['active'],
                                   self.BRIDGE['active'])
        self.b = _RendezvousMember('standby', self.TOKENS['standby'],
                                   self.BRIDGE['standby'])
        self.a.role, self.b.role = 'active', 'standby'
        self.b.sync = 'tracking'
        self.members = {'ctrl-a': self.a, 'ctrl-b': self.b,
                        '172.18.0.2': self.a, '172.18.0.3': self.b}
        # The doctors staging each named defect.
        self.silent = False            # the pair never answers
        self.no_active = False         # no settled active at baseline
        self.b_unsettled = False       # b never reports tracking
        self.wildcard_stored = False   # the stored monitor stays
                                       # wildcard — the #1135 defect
        self.dead_declared = False     # the stored monitor names a
                                       # dead bridge address
        self.wrong_port = False        # the stored monitor names a
                                       # foreign port
        self.foreign_serving = False   # the dialed endpoint answers
                                       # without the claim state
        self.promote_refused = False   # /promote answers an error
        self.a_never_demotes = False   # the fenced write never demotes
        self.no_loss = False           # the demotion goes unrecorded
        self.unattributed_loss = False # the loss names no claimant
        self.wrong_claimant = False    # the loss names a wrong token
        self.no_adoption = False       # adoptions pin unjournaled
        self.foreign_adoption = False  # the journal names a wrong
                                       # endpoint
        self.wedge = False             # ctrl-a never resolves a source
        self.slow_rejoin = False       # ctrl-a's re-join passes the
                                       # bound
        self.rejoin_delay = 80
        self.b_never_rejoins = False   # ctrl-b stays wedged post-demote
        self._b_demoted = False
        self.no_durable = False        # journal events never reach the
                                       # --journal-file mirror
        plant.claim = {'owner': self.a.token, 'holders': {'active'},
                       'monitor': self._stored_for(self.a),
                       'controller': True, 'yielded': False}
        self.journal_paths = {}
        for member, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
                self.journal_paths[member] = path

    def _stored_for(self, member):
        """The monitor the standing claim carries for `member` — the
        declaration normalized to the routable rig address (#1135)
        unless a doctor stores the defect's verbatim wildcard or a
        dead or mis-ported address."""
        port = member.bridge.rsplit(':', 1)[1]
        if self.wildcard_stored:
            return member.declared
        if self.dead_declared:
            return '172.18.0.99:' + port
        if self.wrong_port:
            return member.bridge.rsplit(':', 1)[0] + ':9999'
        return member.bridge

    def restamp_claim(self):
        """Re-store the standing claim's declared monitor under the
        current declaration doctors — the baseline claim was stamped
        at feed construction, before the test armed its doctor."""
        if self.plant.claim is not None:
            self.plant.claim['monitor'] = self._stored_for(self.a)

    def _journal(self, member, event):
        """One journaled record — the served journal plus the durable
        --journal-file mirror the leg's file audit reads."""
        member.seq += 1
        entry = {'seq': member.seq, 'tick': member.tick,
                 'event': event}
        member.journal.append(entry)
        path = self.journal_paths.get(member)
        if path is not None and not self.no_durable:
            with path.open('a') as stream:
                stream.write(json.dumps({'entry': entry}) + '\n')

    def _owns(self, member):
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token

    def _field_write(self, member):
        """One controller-scan field write: fenced while a
        different-owner claim stands — the verdict that demotes the
        owner — else the write lands (an unclaimed field fences no
        one; there is no claimant to demote to)."""
        claim = self.plant.claim
        if claim is None \
                or (claim['owner'] == member.token
                    and member.key in (claim.get('holders') or set())):
            self.plant.plant_tick += 1
            self.plant.samples[self.plant.OUT].update(
                tick=self.plant.plant_tick, value={'float': 1.5})
            return 'landed'
        return 'fenced'

    def _take_claim(self, member):
        """The promotion's claim — the unconditional preempt a
        tracking standby's /promote drives; the stored monitor is the
        normalized declaration."""
        self.plant.claim = {'owner': member.token,
                            'holders': {member.key},
                            'monitor': self._stored_for(member),
                            'controller': True, 'yielded': False}

    def _supersede(self, member):
        """The contract's demote-in-place: journal the attributed
        loss and walk demoting -> standby with the fencing-loss mark
        standing."""
        member.fencing_lost = True
        member.demote_tick = member.tick
        member.sync = 'unsynchronized'
        if member is self.b:
            self._b_demoted = True
        if not self.no_loss:
            loss = {'point': self.plant.OUT}
            if not self.unattributed_loss:
                loss['claimant'] = 0xDEAD if self.wrong_claimant \
                    else (self.plant.claim or {}).get('owner')
            self._journal(member, {'field_claim_lost': loss})
        if member is self.a and self.a_never_demotes:
            return
        member.role = 'demoting'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting'}})

    def _resolve(self, member):
        """The tracking source this scan resolves, or None — ctrl-b
        follows its configured --standby source outright; ctrl-a
        resolves the standing claim's declared monitor — the
        field-arbitrated rendezvous — where the declaration is
        routable: a wildcard declaration is undialable, the defect's
        own wedge. The verified adoption journals once, then holds
        as the process-lifetime pin."""
        if member is self.b:
            if self.b_unsettled \
                    or (self.b_never_rejoins and self._b_demoted):
                return None
            return 'configured'
        if member.adopted is not None:
            return member.adopted
        if self.wedge:
            return None
        source = (self.plant.claim or {}).get('monitor')
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
        if member.fencing_lost \
                and (claim is None or claim['owner'] == member.token):
            # The bound conditional re-grant lands: the marked
            # ex-owner re-seats the claim and walks promoting.
            self._take_claim(member)
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
                   'source_owns_field':
                       False if self.foreign_serving else
                       (self._owns(member)
                        or member.sync == 'tracking'),
                   'line_owner':
                       member.declared if self._owns(member)
                       else (self.plant.claim or {}).get('monitor')}
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
                self._take_claim(member)
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
                if member is self.b:
                    self._b_demoted = True
                self._journal(member, {'role_changed': {
                    'from': 'active', 'to': 'demoting'}})
            return 200, {'role': member.role}
        raise AssertionError('unhandled ' + url)


class ClaimMonitorRendezvousTests(unittest.TestCase):
    """The claim-monitor-rendezvous leg against the stubbed pair: a
    clean rig passes with identical digests — the settled owner's
    declared monitor answering a foreign dial with the recorded
    claim state, the field-claim demote staging the successor's
    declared monitor, and the demoted peer's journaled adoption and
    bounded re-join through it — each doctored defect reports the
    named diagnostic, the unchecked-diagnostic self-check covers
    the leg, and the unreachable, contract-predating, or seam-less
    rig is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = RendezvousPlantPeer(RendezvousPairFeed.TOKENS[
            'active'])
        self.feed = RendezvousPairFeed(
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
                patch.object(scenarios, 'RENDEZVOUS_SETTLE', 2), \
                patch.object(scenarios, 'RENDEZVOUS_POLL', 0.001), \
                patch.object(scenarios, 'RENDEZVOUS_DEADLINE', 2), \
                patch.object(scenarios, 'RENDEZVOUS_DIAL', 2), \
                patch.object(scenarios, 'RENDEZVOUS_REJOIN_TICKS', 30):
            return scenarios.scenario_claim_monitor_rendezvous(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_claim_monitor_rendezvous,
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
        # The leg read the standing claim through the plant
        # protocol's non-mutating claim surface.
        probes = [request for request in self.plant.requests
                  if request.get('op') == 'probe_writer']
        self.assertTrue(probes)

    def test_unkeyed_pair_passes(self):
        feed = RendezvousPairFeed(
            self.plant, keyed=False,
            journal_files=self._journal_files('unkeyed'))
        record = self.run_scenario(
            feed=feed, ctx=self._ctx(
                pair_token=None,
                journal_files=self._journal_files('unkeyed')))
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)

    def test_wildcard_declared_reports_failed(self):
        # The doctored negative the issue names: the declared monitor
        # stored verbatim under the wildcard bind — undialable —
        # asserted as rendezvous-capable.
        self.feed.wildcard_stored = True
        self.feed.restamp_claim()
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-failed',
                      record.get('detail', ''))
        self.assertIn('wildcard', record.get('detail', ''))

    def test_dead_declared_reports_failed(self):
        self.feed.dead_declared = True
        self.feed.restamp_claim()
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-failed',
                      record.get('detail', ''))

    def test_wrong_port_declared_reports_failed(self):
        self.feed.wrong_port = True
        self.feed.restamp_claim()
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-failed',
                      record.get('detail', ''))

    def test_foreign_serving_reports_failed(self):
        self.feed.foreign_serving = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-failed',
                      record.get('detail', ''))

    def test_promote_refused_reports_nondeterministic(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-nondeterministic',
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
        self.assertIn('field_claim_lost', record.get('detail', ''))

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
        self.assertIn('adopt', record.get('detail', ''))

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
        self.assertIn('claim-monitor-rendezvous-failed',
                      record.get('detail', ''))

    def test_slow_rejoin_reports_failed(self):
        self.feed.slow_rejoin = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('ticks', record.get('detail', ''))

    def test_peer_never_rejoins_reports_failed(self):
        self.feed.b_never_rejoins = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_durable_absent_reports_failed(self):
        self.feed.no_durable = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('durable', record.get('detail', ''))

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_digest',
                          side_effect=[{'restore': 'restored'},
                                       {'restore': 'unrestored'}]):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_rendezvous',
                          lambda record, note: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('claim-monitor-rendezvous-unchecked',
                      record.get('detail', ''))

    def test_predating_rig_reports_inconclusive(self):
        self.plant.declared_missing = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('predates', record.get('detail', ''))

    def test_probe_absent_reports_inconclusive(self):
        self.plant.probe_absent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_open_field_reports_inconclusive(self):
        self.plant.claim = None
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_foreign_claim_reports_inconclusive(self):
        # The verdict names a foreign owner while the incumbent's
        # writes still land — the baseline claim-state gate the leg
        # refuses to stage over.
        self.plant.misattributed = True
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

    def test_missing_standby_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['standby']
        record = self.run_scenario(ctx=ctx)
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
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
