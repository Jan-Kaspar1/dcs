"""The 2390_holderless_claim_recovery leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_holderless_claim_recovery, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration half builds on the stranded-standby leg's fakes the same
induction stages; EXPECTED_CASES pins this module's contribution to
the suite's case coverage so a dropped case fails the discovery
check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2370_stranded_standby_no_resync \
    import StrandedPlantPeer


EXPECTED_CASES = frozenset({
    'HolderlessClaimRecoveryTests.test_registered',
    'HolderlessClaimRecoveryTests.test_clean_pair_passes_and_'
    'validates',
    'HolderlessClaimRecoveryTests.test_wedged_reclaim_reports_failed',
    'HolderlessClaimRecoveryTests.test_never_reclaims_reports_failed',
    'HolderlessClaimRecoveryTests.test_unbound_reseat_reports_failed',
    'HolderlessClaimRecoveryTests.test_premature_grant_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_dual_active_reports_failed',
    'HolderlessClaimRecoveryTests.test_monitor_lost_reports_failed',
    'HolderlessClaimRecoveryTests.test_never_orphans_reports_failed',
    'HolderlessClaimRecoveryTests.test_never_retracks_reports_failed',
    'HolderlessClaimRecoveryTests.test_owner_never_demotes_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_peer_never_demotes_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_silent_loss_reports_failed',
    'HolderlessClaimRecoveryTests.test_unattributed_loss_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_misattributed_loss_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_release_never_frees_reports_'
    'failed',
    'HolderlessClaimRecoveryTests.test_durable_absent_reports_failed',
    'HolderlessClaimRecoveryTests.test_unrestored_reports_failed',
    'HolderlessClaimRecoveryTests.test_promote_refused_reports_'
    'nondeterministic',
    'HolderlessClaimRecoveryTests.test_diverging_digests_report_'
    'nondeterministic',
    'HolderlessClaimRecoveryTests.test_silent_judge_reports_'
    'unchecked',
    'HolderlessClaimRecoveryTests.test_claim_staging_refused_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_release_refusal_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_predating_rig_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_open_field_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_foreign_baseline_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_monitor_undeclared_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_no_active_reports_failed',
    'HolderlessClaimRecoveryTests.test_unsettled_pair_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_unreachable_pair_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_unreachable_plant_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_active_endpoint_'
    'reports_inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_standby_endpoint_'
    'reports_inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_plant_endpoint_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_plant_ctl_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_owner_token_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_failover_budget_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_missing_journal_files_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_bridge_placement_reports_'
    'inconclusive',
    'HolderlessClaimRecoveryTests.test_two_runs_produce_identical_'
    'evidence',
})


class HolderlessPlantPeer(StrandedPlantPeer):
    """The holderless-recovery rig's plant half: the stranded-standby
    claim arbitration plus the probe_writer read surface the leg's
    claim audit runs on, and an ensure_writer whose `rebind: false`
    grant on an absent or own claim raises the orphan placeholder
    without joining a holder — the dead-owner shape the reclaim must
    preempt. The doctor flags stage the predating rig and the
    absent staging levers."""

    def __init__(self, owner):
        super().__init__(owner)
        # The doctors staging each named defect.
        self.probe_absent = False       # probe_writer unknown — the
                                        # rig predates the claim
                                        # read surface
        self.release_never_frees = False  # release_writer leaves its
                                          # claim standing

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if op == 'release_writer' and self.release_never_frees:
            # The staged defect: the release reports done but the
            # claim it freed keeps fencing the field.
            if self.claim is not None:
                self.claim['holders'].discard(conn)
            return {'result': 'done'}
        if op == 'ensure_writer':
            if self.claim is not None \
                    and self.claim['owner'] != owner \
                    and not self.ensure_preempts:
                return self._verdict(op)
            if self.claim is None or self.claim['owner'] != owner:
                self.claim = {'owner': owner, 'holders': set(),
                              'monitor': request.get('monitor'),
                              'controller': request.get(
                                  'controller', True),
                              'yielded': False}
            if request.get('rebind', True):
                self.claim['holders'].add(conn)
            return {'result': 'done'}
        return super()._claim_for(conn, request)

    def dispatch_for(self, conn, request):
        if request.get('op') == 'probe_writer':
            self.requests.append(request)
            if self.probe_absent:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'unknown op'}}
            if self.claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            if conn in self.claim['holders']:
                return {'result': 'done'}
            return self._verdict('probe_writer')
        return super().dispatch_for(conn, request)


class _HolderlessMember:
    """One stubbed controller: role, tick, sync posture, the claim
    lifecycle marks (was_owner / fencing_lost / yielded), the orphan
    miss counter, and its journaled record."""

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
        self.adopted = None       # the verified tracking-source pin
        self.claimed = None       # the last verdict's declared monitor
        self.was_owner = False
        self.fencing_lost = False
        self.yielded = False
        self.misses = 0
        self.orphaned_logged = False
        self.rearmed = None       # the claim object the re-arm
                                  # journal dedupes on
        self.observed = set()     # the claimants this ownership epoch
                                  # already journaled
        self.pending_origin = None
        self.dead = False
        self.supersedes = 0


class HolderlessPairFeed:
    """A stubbed pair for the holderless-claim recovery leg: 'ctrl-a'
    is the launched field owner and 'ctrl-b' the tracking standby on
    its configured --standby source. Every served monitor request is
    one scan of the addressed member: an active or promoting member
    writes the field — a fenced write journals the attributed
    field_claim_lost, marks the fencing loss, and walks demoting ->
    standby — while a standby member runs the orphan cycle: the
    unbound ensure probe (raising or confirming its token's claim —
    the holderless placeholder an unbound grant leaves), the
    tracking-source pull (an ownerless pulled line counting a miss
    and reporting orphaned, journaled once per transition), the
    miss-budgeted failover grant, and the loss-marked bound reclaim —
    refused while a different owner's claim has live holders,
    granted over an unclaimed, same-owner, or holderless claim.
    Every granted claim ends in a gate lift: the fencing-loss mark
    re-arms and the observation dedup clears, and the fenced write's
    loss record seeds the epoch's dedup with the claimant it already
    attributed — so the claimant a peer only ever probed journals a
    `field_claim_observed` record while the fenced peer never repeats
    its own. Every journaled event mirrors into the --journal-file
    paths the leg's durable audit reads. The doctor flags stage each
    named defect, including the pre-fix shape (`defect_reclaim`: the
    arm scoped to the loss mark and the grant refusing any standing
    different owner — the ensure-semantics probe that wedged)."""

    TOKENS = {'active': 424243, 'standby': 424244}
    BRIDGE = {'active': '172.18.0.2:8080',
              'standby': '172.18.0.3:8081'}

    def __init__(self, plant, journal_files=None):
        self.plant = plant
        self.a = _HolderlessMember('active', self.TOKENS['active'],
                                   self.BRIDGE['active'])
        self.b = _HolderlessMember('standby', self.TOKENS['standby'],
                                   self.BRIDGE['standby'])
        self.a.role, self.b.role = 'active', 'standby'
        self.a.was_owner = True
        self.b.sync = 'tracking'
        plant.claim.update(monitor=self.a.declared,
                           controller=True, yielded=False,
                           holders={'active'})
        self.members = {'ctrl-a': self.a, 'ctrl-b': self.b}
        # The durable half: journal_files maps ctx keys to the
        # runner's --journal-file paths.
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
        self.demote_refused = False    # /demote answers an error —
                                       # the restore never lands
        self.a_never_demotes = False   # the fenced write never demotes
        self.b_never_demotes = False
        self.no_loss = False           # the demotions go unrecorded
        self.unattributed_loss = False # the loss names no claimant
        self.wrong_claimant = False    # the loss names a wrong token
        self.no_orphans = False        # the island's sync never
                                       # reports orphaned
        self.a_never_retracks = False  # ctrl-a stays orphaned past
                                       # the recovery
        self.never_reclaims = False    # the bound re-grant never probes
        self.defect_reclaim = False    # the pre-#1255 shape: arm on
                                       # the loss mark only, refuse
                                       # any standing different owner
        self.unbound_reseat = False    # the re-grant never joins the
                                       # holders — the winner's own
                                       # write stays fenced
        self.preempts_live = False     # the reclaim takes a live
                                       # different-owner claim
        self.dual_active = False       # ctrl-a re-reports 'active'
                                       # behind the recovered owner
        self.peer_dies = False         # ctrl-b's monitor drops once
                                       # its orphan island forms
        self.b_never_retracks = False  # ctrl-b never reconverges —
                                       # the restore never lands
        self._island_seen = False

    def _granted(self, member):
        """The gate lift every granted claim ends in: a fresh
        ownership epoch — the fencing-loss mark re-arms, and the
        observation dedup clears, so a claimant a later refused probe
        names is a new episode rather than the one the loss record
        already attributed."""
        member.fencing_lost = False
        member.yielded = False
        member.observed = set()
        member.was_owner = True

    @property
    def failover_budget(self):
        """The armed --auto-promote budget, in served scans. The rig's
        budget is 120 misses against the lane's own scan cadence —
        minutes of controller time, far outside the leg's resolution
        watch, so the orphan budget's rescue can never be what unwedges
        the holderless placeholder the watch exists to observe (the
        wedge the finding recorded, minutes before an operator
        promote). This stub's scan rides the leg's own poll rather than
        a scan timer, so the budget is sized from the watch's poll
        count instead: the whole episode's watches over, so the rescue
        stays outside at whatever cadence the leg runs."""
        polls = int(scenarios.HOLDERLESS_RESOLVE
                    / scenarios.HOLDERLESS_POLL)
        return polls * 64 + 1024

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
        """The member's checkpoint stamp — its run holds the field's
        write-ownership claim."""
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token \
            and member.key in (claim.get('holders') or set())

    def _field_write(self, member):
        """One controller-scan field write: fenced while the member
        stands outside the standing claim's holders — the verdict
        that demotes the owner — else the write lands and the plant
        tick advances."""
        claim = self.plant.claim
        if claim is not None and claim['owner'] == member.token \
                and member.key in (claim.get('holders') or set()):
            self.plant.plant_tick += 1
            self.plant.samples[self.plant.OUT].update(
                tick=self.plant.plant_tick, value={'float': 1.5})
            return 'landed'
        return 'fenced'

    def _supersede(self, member):
        """The contract's demote-in-place: count the fenced write,
        journal the attributed loss, and walk demoting -> standby
        with the fencing-loss mark standing."""
        member.fencing_lost = True
        member.yielded = False
        member.sync = 'unsynchronized'
        member.orphaned_logged = False
        member.rearmed = None
        member.supersedes += 1
        claim = self.plant.claim or {}
        if not self.no_loss:
            loss = {'point': self.plant.OUT}
            if not self.unattributed_loss:
                loss['claimant'] = 0xDEAD if self.wrong_claimant \
                    else claim.get('owner')
                # The loss record already attributes this claimant's
                # episode, so it seeds the epoch's observation dedup:
                # the reclaim probes the same standing claim refuses
                # queue no second record.
                if loss['claimant'] is not None:
                    member.observed.add(loss['claimant'])
            self._journal(member, {'field_claim_lost': loss})
        never_demotes = self.a_never_demotes if member is self.a \
            else self.b_never_demotes
        if never_demotes:
            return
        member.role = 'demoting'
        member.pending_origin = 'fenced'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': 'fenced'}})

    def _observe(self, member, claimant):
        """One field_claim_observed record per distinct claimant the
        refused conditional probes attribute — the deduplicated
        audit the leg reads."""
        if claimant in member.observed:
            return
        member.observed.add(claimant)
        self._journal(member, {'field_claim_observed': {
            'claimant': claimant}})

    def _resolve(self, member):
        """The tracking source this scan resolves, or None — ctrl-b
        follows its configured --standby source outright; ctrl-a
        adopts the announced hint, journaled once."""
        if member is self.b:
            if self.b_unsettled:
                return None
            return self.a
        if member.adopted is not None:
            return member.adopted
        member.adopted = self.b
        self._journal(member, {'tracking_source_adopted': {
            'source': self.b.bridge}})
        return member.adopted

    def _serves_owner(self, source):
        """Whether the resolved source's pulled document stamps
        source_owns_field — the endpoint serves this line as its
        field owner."""
        return source.role in ('active', 'promoting')

    def _standby(self, member):
        """One standby scan — the orphan cycle in scan order: the
        unbound ensure probe, the tracking-source pull, the
        miss-budgeted failover grant, then the bound fencing-loss
        reclaim."""
        claim = self.plant.claim
        member.claimed = (claim or {}).get('monitor')

        # The unbound re-arm probe — the orphan cycle's conditional
        # ensure, armed for any ex-owner that never yielded: grants
        # raise or confirm the token's claim without joining a
        # holder (the placeholder shape); refusals journal the
        # observed claimant once.
        if member.was_owner and not member.yielded:
            if claim is None or claim['owner'] == member.token:
                if claim is None:
                    self.plant.claim = {
                        'owner': member.token, 'holders': set(),
                        'monitor': member.declared,
                        'controller': True, 'yielded': False}
                if member.rearmed is not self.plant.claim:
                    member.rearmed = self.plant.claim
                    self._journal(member, {'field_claim_rearmed': {
                        'point': self.plant.OUT}})
            else:
                member.rearmed = None
                self._observe(member, claim['owner'])

        # The tracking pull — an ownerless source line counts a
        # miss and reports orphaned; a serving owner converges the
        # peer and clears the loss mark.
        source = self._resolve(member)
        if source is None:
            member.sync = 'unsynchronized'
        elif self._serves_owner(source) \
                and not (self.a_never_retracks and member is self.a
                         and self._island_seen) \
                and not (self.b_never_retracks and member is self.b
                         and member.supersedes):
            member.sync = 'tracking'
            member.misses = 0
            member.fencing_lost = False
            member.orphaned_logged = False
        else:
            member.misses += 1
            member.sync = 'unsynchronized' if self.no_orphans \
                else 'orphaned'
            if member.sync == 'orphaned' \
                    and not member.orphaned_logged:
                member.orphaned_logged = True
                self._island_seen = True
                self._journal(member, {'field_orphaned': {
                    'source': source.bridge}})

        # The failover grant — the miss-budgeted conditional
        # promotion: the orphan budget's own rescue, refusing only a
        # live unyielded controller claim.
        if member.misses >= self.failover_budget:
            claim = self.plant.claim
            if claim is None or claim['owner'] == member.token \
                    or not claim.get('controller', True) \
                    or not claim.get('holders') \
                    or claim.get('yielded'):
                self.plant.claim = {
                    'owner': member.token,
                    'holders': {member.key},
                    'monitor': member.declared,
                    'controller': True, 'yielded': False}
                self._granted(member)
                member.role = 'promoting'
                member.pending_origin = 'failover'
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting',
                    'origin': 'failover'}})
                return

        # The bound fencing-loss reclaim — the wedge escape: the
        # fixed contract arms on the loss mark or the orphaned pull
        # and refuses only a different owner's live claim; the
        # defect doctors stage the pre-fix arm and the
        # ensure-semantics refusal that starved it.
        claim = self.plant.claim
        if self.defect_reclaim:
            armed = member.fencing_lost
        else:
            armed = member.was_owner and not member.yielded \
                and (member.fencing_lost or member.sync == 'orphaned')
        if not armed or self.never_reclaims:
            return
        if claim is not None and claim['owner'] != member.token:
            if self.preempts_live:
                pass
            elif self.defect_reclaim or claim.get('holders'):
                self._observe(member, claim['owner'])
                return
        holders = set() if self.unbound_reseat else {member.key}
        if claim is not None and claim['owner'] == member.token \
                and not self.unbound_reseat:
            claim['holders'].add(member.key)
        else:
            self.plant.claim = {
                'owner': member.token, 'holders': holders,
                'monitor': member.declared,
                'controller': True, 'yielded': False}
        self._granted(member)
        member.role = 'promoting'
        member.pending_origin = 'reclaim'
        self._journal(member, {'role_changed': {
            'from': 'standby', 'to': 'promoting',
            'origin': 'reclaim'}})

    def _scan(self, member):
        """One controller scan of the addressed member."""
        member.tick += 1
        if member.role == 'demoting':
            member.role = 'standby'
            self._journal(member, {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': member.pending_origin}})
            return
        if member.role == 'promoting':
            member.role = 'active'
            self._journal(member, {'role_changed': {
                'from': 'promoting', 'to': 'active',
                'origin': member.pending_origin}})
            return
        if member.role == 'active':
            if self._field_write(member) == 'fenced':
                self._supersede(member)
            return
        self._standby(member)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        member = self.members.get(host.split(':')[0])
        if member is None:
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent or member.dead:
            raise urllib.error.URLError('unreachable')
        self._scan(member)
        if (method, route) == ('GET', '/role'):
            role = member.role
            if self.dual_active and member is self.a \
                    and member.role == 'standby' \
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
            if self.peer_dies and member is self.b \
                    and self._island_seen:
                member.dead = True
            return 200, report
        if (method, route) == ('GET', '/journal'):
            return 200, [entry for entry in member.journal
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'tick': member.tick, 'generation': 7,
                         'source_owns_field': self._owns(member),
                         'line_owner':
                             (self.plant.claim or {}).get('monitor')}
        if (method, route) == ('POST', '/promote'):
            if self.promote_refused:
                return 500, {'error': 'refused'}
            if member.role in ('active', 'promoting'):
                return 409, 'already_active'
            if member.role == 'standby':
                if member.sync != 'tracking':
                    return 409, 'not_converged'
                # The unconditional operator claim: preempts
                # whatever stands, binds this run, lifts the gate.
                self.plant.claim = {
                    'owner': member.token,
                    'holders': {member.key},
                    'monitor': member.declared,
                    'controller': True, 'yielded': False}
                self._granted(member)
                member.role = 'promoting'
                member.sync = 'unsynchronized'
                member.pending_origin = 'request'
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting',
                    'origin': 'request'}})
            return 200, {'role': member.role}
        if (method, route) == ('POST', '/demote'):
            if self.demote_refused:
                return 409, {'refused': {}}
            if member.role in ('active', 'promoting'):
                if self._owns(member):
                    # The deliberate hand-back: the yield mark a
                    # successor's conditional grant preempts.
                    self.plant.claim['yielded'] = True
                    self.plant.claim['holders'].discard(member.key)
                member.role = 'demoting'
                member.sync = 'unsynchronized'
                member.yielded = True
                member.pending_origin = 'request'
                self._journal(member, {'role_changed': {
                    'from': 'active', 'to': 'demoting',
                    'origin': 'request'}})
            return 200, {'role': member.role}
        raise AssertionError('unhandled ' + url)


class HolderlessClaimRecoveryTests(unittest.TestCase):
    """The holderless-claim recovery scenario against the stubbed
    pair: a clean rig passes with identical digests — the staged
    promote superseding the launch owner (the first ex-owner,
    re-tracking with its loss mark cleared), the foreign tool claim
    fencing the promoted peer into the second ex-owner, the release
    freeing the field while the cleared peer's holderless claim
    re-arms, and the marked peer's bound reclaim re-seating the
    field unattended — each doctored defect reports the named
    diagnostic, and the unreachable or contract-predating rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = HolderlessPlantPeer(
            HolderlessPairFeed.TOKENS['active'])
        self.feed = HolderlessPairFeed(
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
               'failover_misses': self.feed.failover_budget,
               'endpoint_placement': {'active': 'loopback',
                                      'standby': 'loopback',
                                      'plant': 'loopback'},
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'HOLDERLESS_SETTLE', 2), \
                patch.object(scenarios, 'HOLDERLESS_POLL', 0.001), \
                patch.object(scenarios, 'HOLDERLESS_DEADLINE', 2), \
                patch.object(scenarios, 'HOLDERLESS_ROUNDS', 2), \
                patch.object(scenarios, 'HOLDERLESS_RESOLVE', 2), \
                patch.object(scenarios, 'HOLDERLESS_RESTORE', 2):
            return scenarios.scenario_holderless_claim_recovery(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_holderless_claim_recovery,
            scenarios.SCENARIOS)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout restored: ctrl-a owns the field again
        # and ctrl-b tracks it.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        self.assertEqual(
            self.feed.TOKENS['active'],
            (self.plant.claim or {}).get('owner'))
        # The induction's attachments claimed as field tools —
        # explicitly controller-less, declaring no monitor.
        tools = [request for request in self.plant.requests
                 if request.get('op') == 'claim_writer'
                 and request.get('owner')
                 in (scenarios.HOLDERLESS_FOREIGN_1,
                     scenarios.HOLDERLESS_FOREIGN_2)]
        self.assertEqual(4, len(tools))   # two inductions, two passes
        for request in tools:
            self.assertIs(request.get('controller'), False)
            self.assertIsNone(request.get('monitor'))
        # The recovery's re-grant ran the bound reclaim — never an
        # operator promote — on the fenced peer once per pass; the
        # restore's deliberate hand-back then leaves the claim yielded
        # and holderless, which the other ex-owner's own bound reclaim
        # takes as the pair returns to its launch layout.
        reclaims = {
            member.key: [change.get('origin')
                         for entry in member.journal
                         for change in
                         [entry['event'].get('role_changed') or {}]
                         if change.get('to') == 'promoting'
                         and change.get('origin') == 'reclaim']
            for member in (self.feed.a, self.feed.b)}
        self.assertEqual(['reclaim'] * 2, reclaims['standby'])
        self.assertEqual(['reclaim'] * 2, reclaims['active'])
        # The operator promote is the staging lever alone — one per pass:
        # the restore's hand-back demotes the recovered peer, which
        # leaves its claim yielded and holderless, so the launch
        # owner takes it back through its own bound reclaim.
        self.assertEqual(
            2, len([change
                    for member in (self.feed.a, self.feed.b)
                    for entry in member.journal
                    for change in
                    [entry['event'].get('role_changed') or {}]
                    if change.get('to') == 'promoting'
                    and change.get('origin') == 'request']))

    def test_wedged_reclaim_reports_failed(self):
        # The defect build's shape: the bound reclaim arms on the
        # loss mark alone and refuses any standing different owner —
        # the holderless placeholder starves it forever.
        self.feed.defect_reclaim = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))
        self.assertIn('wedge', record.get('detail', ''))

    def test_never_reclaims_reports_failed(self):
        self.feed.never_reclaims = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_unbound_reseat_reports_failed(self):
        self.feed.unbound_reseat = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_premature_grant_reports_failed(self):
        self.feed.preempts_live = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_dual_active_reports_failed(self):
        self.feed.dual_active = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_monitor_lost_reports_failed(self):
        self.feed.peer_dies = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_never_orphans_reports_failed(self):
        self.feed.no_orphans = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_never_retracks_reports_failed(self):
        self.feed.a_never_retracks = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_owner_never_demotes_reports_failed(self):
        self.feed.a_never_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_peer_never_demotes_reports_failed(self):
        self.feed.b_never_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

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

    def test_release_never_frees_reports_failed(self):
        self.plant.release_never_frees = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_durable_absent_reports_failed(self):
        # The --journal-file paths exist but the peers mirror no
        # records into them — the durable audit's kinds are absent.
        for path in self.feed.journal_paths.values():
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        feed = self.feed
        feed.journal_paths = {}
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))
        self.assertIn('journal-file', record.get('detail', ''))

    def test_unrestored_reports_failed(self):
        self.feed.b_never_retracks = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-failed',
                      record.get('detail', ''))

    def test_promote_refused_reports_nondeterministic(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-nondeterministic',
                      record.get('detail', ''))

    def test_diverging_digests_report_nondeterministic(self):
        digests = iter(({'pass': 1}, {'pass': 2}))
        with patch.object(
                scenarios, '_holderless_digest',
                lambda record, violations: next(digests)):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))

    def test_silent_judge_reports_unchecked(self):
        # A judge silenced outright must trip the unchecked
        # diagnostic — every planted negative slips.
        with patch.object(scenarios, '_judge_holderless',
                          lambda record, note: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('holderless-reclaim-unchecked',
                      record.get('detail', ''))

    def test_claim_staging_refused_reports_inconclusive(self):
        self.plant.claims_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('claim', record.get('detail', ''))

    def test_release_refusal_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_predating_rig_reports_inconclusive(self):
        self.plant.probe_absent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_open_field_reports_inconclusive(self):
        self.plant.open_field = True
        self.plant.claim = None
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_foreign_baseline_reports_inconclusive(self):
        self.plant.misattributed = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('standing claim', record.get('detail', ''))

    def test_monitor_undeclared_reports_inconclusive(self):
        self.plant.declared_missing = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('monitor', record.get('detail', ''))

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

    def test_unreachable_plant_reports_inconclusive(self):
        self.plant.down = True
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

    def test_missing_failover_budget_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['failover_misses']
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
