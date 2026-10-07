"""The 2450_yielded_rearm leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_yielded_rearm, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration half builds on the stranded-standby leg's fakes the same
induction stages.
"""
import unittest
import urllib.error

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2370_stranded_standby_no_resync import (
    StrandedPlantPeer)


class YieldedPlantPeer(StrandedPlantPeer):
    """The yielded-rearm rig's plant half: the stranded-standby
    arbitration plus the yield mark's lifecycle end — a same-owner
    *controller* re-grant through `claim_writer_unless_held`,
    `claim_writer`, or a bound `ensure_writer` lands the attachment
    in the holder set and clears `yielded`, while an unbound ensure
    probe joins no holder and a tool's bound ensure
    (`controller: false`) holds without incumbency, both leaving
    the deliberate hand-off preemptable for a different owner's
    conditional claim. The doctor flags stage the #1123 defect and
    each named rig gap the leg reports."""

    def __init__(self, owner):
        super().__init__(owner)
        # The doctors staging each named defect.
        self.unsupported_verbs = False  # lifecycle ops invalid_request
        self.stale_yield = False        # re-grants never clear yielded
        self.unbound_clears = False     # unbound probe ends the yield
        self.tool_clears = False        # a tool hold ends the yield
        self.probe_grants_once = False  # pass one's probe grants only —
                                        # the digests diverge
        self.regrant_refuses = False    # a same-owner re-grant fenced
        self._once_seen = False
        self._write_fails = False       # the re-bound holder's write
                                        # fences

    def _grant(self, conn, owner, request, rebind, controller):
        """The grant body every claim verb shares: a fresh claim for
        an unclaimed or different-owner grant, else the same-owner
        join — whose bound *controller* re-grant is the yield mark's
        lifecycle end. Returns the grant verdict."""
        claim = self.claim
        shared = claim is not None and claim['owner'] == owner \
            and any(h is not conn for h in claim['holders'])
        if claim is None or claim['owner'] != owner:
            self.claim = claim = {
                'owner': owner, 'holders': set(),
                'monitor': request.get('monitor'),
                'controller': controller, 'yielded': False}
        else:
            claim['monitor'] = request.get('monitor') \
                or claim.get('monitor')
            claim['controller'] = claim['controller'] or controller
            # The yield's lifecycle end: only a bound re-join by a
            # *controller* attachment clears the mark — the defect
            # doctors keep it standing anyway, or clear it from the
            # shapes the contract says must leave it.
            clears = rebind and controller
            if self.unbound_clears and not rebind:
                clears = True
            if self.tool_clears and not controller:
                clears = True
            if clears and not self.stale_yield:
                claim['yielded'] = False
        if rebind:
            claim['holders'].add(conn)
        if shared or self.shared:
            return {'result': 'claimed_shared', 'owner': owner}
        return {'result': 'done'}

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if self.unsupported_verbs and op in (
                'claim_writer', 'claim_writer_unless_held',
                'ensure_writer', 'release_writer'):
            return {'result': 'error', 'error': {
                'kind': 'invalid_request',
                'detail': str(op) + ' is not a supported op'}}
        claim = self.claim
        if op == 'release_writer':
            if self.release_refuses:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'release refused'}}
            if claim is not None and conn in claim['holders']:
                claim['holders'].discard(conn)
                if request.get('keep_claim'):
                    claim['yielded'] = True
                elif not claim['holders']:
                    self.claim = None
            return {'result': 'done'}
        if op == 'claim_writer_unless_held':
            if claim is not None and claim['owner'] == owner \
                    and self.regrant_refuses:
                return self._verdict(op)
            if claim is not None and claim['owner'] != owner:
                incumbent = claim.get('controller', True) \
                    and bool(claim['holders']) \
                    and not claim.get('yielded')
                grants_once = self.probe_grants_once \
                    and owner == scenarios.YIELDED_REARM_FOREIGN \
                    and not self._once_seen
                if incumbent and not grants_once:
                    return self._verdict(op)
                if grants_once:
                    self._once_seen = True
            return self._grant(conn, owner, request,
                               rebind=True, controller=True)
        if op == 'claim_writer':
            return self._grant(
                conn, owner, request, rebind=True,
                controller=request.get('controller', True))
        if op == 'ensure_writer':
            if claim is not None and claim['owner'] != owner \
                    and not self.ensure_preempts:
                return self._verdict(op)
            return self._grant(
                conn, owner, request,
                rebind=request.get('rebind', True),
                controller=request.get('controller', True))
        return super()._claim_for(conn, request)

    def dispatch_for(self, conn, request):
        if self.down:
            raise OSError('the plant is down')
        if request.get('op') == 'write' and self._write_fails \
                and conn in self._holders():
            return self._verdict('write', request.get('point'))
        return super().dispatch_for(conn, request)


class _YieldedMember:
    """One stubbed controller: role, sync posture, failover budget,
    miss counter, journal, and the claim-attachment bookkeeping the
    leg's fencing-loss reclaim and orphan-failover paths consume."""

    def __init__(self, key, token):
        self.key = key            # 'active'/'standby' — the ctx name
        self.token = token        # the pinned --owner-token
        self.role = 'standby'
        self.sync = 'unsynchronized'
        self.armed = 0            # failover budget in misses; 0 unarmed
        self.misses = 0
        self.tick = 0
        self.seq = 0
        self.journal = []
        self.dead = False         # the container is exited
        self.fencing_lost = False
        self.failed_writes = 0


class YieldedPairFeed:
    """A stubbed pair for the yielded-rearm leg: ctrl-a (the 'active'
    endpoint key) is the launched field owner — unarmed, configured
    with no tracking source — and ctrl-b ('standby') the tracking
    standby armed with the failover budget and configured on
    ctrl-a's monitor. Every served monitor request is one scan of
    the addressed member: an active member writes the field — a
    fenced write journals the attributed field_claim_lost and walks
    demoting -> standby with the fencing-loss mark whose bound
    conditional reclaim probes every standby scan — while a standby
    member pulls its tracker's checkpoint stamp: a serving owner
    reads `tracking`; an ownerless one reads `orphaned` and counts
    the failover miss ctrl-b's budget self-promotes on through the
    conditional claim under its own token. `POST /demote` runs the
    keep-claim release that leaves the standing claim yielded;
    `POST /promote` claims unconditionally. The lifecycle actions
    cold-restart ctrl-a as its configured active — a fresh
    attachment's startup `claim_writer_unless_held`, refused by a
    live unyielded incumbent — and start an exited container back
    through the same claim. Doctor flags stage each named defect."""

    TOKENS = {'active': 424243, 'standby': 424244}
    BUDGET = 4

    def __init__(self, plant, journal_files=None):
        self.plant = plant
        self.a = _YieldedMember('active', self.TOKENS['active'])
        self.b = _YieldedMember('standby', self.TOKENS['standby'])
        self.a.role = 'active'
        self.b.sync = 'tracking'
        self.b.armed = self.BUDGET
        plant.claim.update(owner=self.a.token, holders={'active'},
                           monitor='ctrl-a:8080', controller=True,
                           yielded=False)
        self.members = {'ctrl-a': self.a, 'ctrl-b': self.b}
        self.keys = {'active': self.a, 'standby': self.b}
        self.restarts = []
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
        self.silent = False              # both endpoints unreachable
        self.no_active = False           # no settled owner at baseline
        self.no_loss = False             # the fencing loss goes unjournaled
        self.unattributed_loss = False   # the loss names no claimant
        self.demote_refused = False      # /demote answers an error
        self.promote_refused = False     # /promote answers an error
        self.b_unsettled = False         # b never converges for promote
        self.b_never_settles = False     # demote sticks at demoting
        self.no_repromote = False        # the orphan failover never fires
        self.restartee_preempts = False  # startup claim granted regardless
        self.successor_demotes = False   # b demotes spuriously post-regrant
        self._repromote_scans = None

    def _journal(self, member, event):
        member.seq += 1
        entry = {'seq': member.seq, 'tick': member.tick,
                 'event': event}
        member.journal.append(entry)
        path = self.journal_paths.get(member)
        if path is not None:
            with path.open('a') as stream:
                stream.write(json.dumps({'entry': entry}) + '\n')

    def _owns_field(self, member):
        """Whether the member's checkpoint stamp would report
        `source_owns_field` — its gate open over a claim it holds."""
        claim = self.plant.claim
        return claim is not None and claim['owner'] == member.token \
            and member.key in (claim.get('holders') or set()) \
            and not member.dead

    def _field_write(self, member):
        """One controller-scan field write: landed while the member
        holds the standing claim, else the fencing verdict that
        superseded owners demote on."""
        if self._owns_field(member):
            self.plant.plant_tick += 1
            self.plant.samples[self.plant.OUT].update(
                tick=self.plant.plant_tick, value={'float': 1.5})
            return 'landed'
        return 'fenced'

    def _supersede(self, member):
        """The contract's demote-in-place: count the fenced write,
        journal the attributed loss, and walk demoting -> standby
        with the fencing-loss mark standing."""
        member.failed_writes += 1
        member.fencing_lost = True
        claim = self.plant.claim or {}
        if not self.no_loss:
            loss = {'point': self.plant.OUT}
            if not self.unattributed_loss:
                loss['claimant'] = claim.get('owner')
            self._journal(member, {'field_claim_lost': loss})
        if member.role == 'demoting':
            return
        member.role = 'demoting'
        member.sync = 'unsynchronized'
        self._journal(member, {'role_changed': {
            'from': 'active', 'to': 'demoting'}})

    def _reclaim(self, member):
        """The fencing-loss mark's bound conditional re-grant —
        refused while a different owner's claim stands, granted
        into an unclaimed or same-owner claim — re-seating the
        ex-owner and walking it promoting."""
        claim = self.plant.claim
        if claim is not None and claim['owner'] != member.token:
            return
        verdict = self.plant._claim_for(
            member.key, {'op': 'ensure_writer',
                         'owner': member.token})
        if verdict.get('result') in ('done', 'claimed_shared'):
            member.fencing_lost = False
            member.role = 'promoting'
            member.sync = 'unsynchronized'
            self._journal(member, {'role_changed': {
                'from': 'standby', 'to': 'promoting'}})

    def _track(self, member):
        """One standby scan's tracking half: the reclaim probe first
        for a marked ex-owner, then the source's checkpoint stamp —
        a serving owner converges the pull, an ownerless one reports
        `orphaned` and counts the heartbeat miss the failover budget
        self-promotes on."""
        if member.fencing_lost:
            self._reclaim(member)
            return
        source = self.b if member is self.a else self.a
        if self._owns_field(source):
            member.sync = 'tracking'
            member.misses = 0
            return
        member.sync = 'orphaned'
        member.misses += 1
        if member.armed and member.misses >= member.armed \
                and not self.no_repromote:
            verdict = self.plant._claim_for(
                member.key, {'op': 'claim_writer_unless_held',
                             'owner': member.token})
            if verdict.get('result') in ('done', 'claimed_shared'):
                member.role = 'promoting'
                member.sync = 'unsynchronized'
                member.misses = 0
                self._repromote_scans = 0
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting'}})

    def _scan(self, member):
        """One controller scan of the addressed member."""
        member.tick += 1
        if member.role == 'demoting':
            if self.b_never_settles and member is self.b:
                return
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
            if self.successor_demotes and member is self.b \
                    and self._repromote_scans is not None:
                self._repromote_scans += 1
                if self._repromote_scans > 3:
                    member.role = 'demoting'
                    member.sync = 'unsynchronized'
                    self._journal(member, {'role_changed': {
                        'from': 'active', 'to': 'demoting'}})
                    self._repromote_scans = None
                    return
            if self._field_write(member) == 'fenced':
                self._supersede(member)
            return
        self._track(member)

    def _startup_claim(self, member):
        """The restartee's startup shape: a fresh attachment's
        `claim_writer_unless_held` under its pinned token — refused
        by a live unyielded incumbent the process exits on, granted
        into an unclaimed, yielded, or tool-held claim it then owns."""
        if self.restartee_preempts:
            # The doctored defect: granted regardless of the standing
            # incumbent's state.
            self.plant.claim = {
                'owner': member.token, 'holders': {member.key},
                'monitor': 'ctrl-a:8080', 'controller': True,
                'yielded': False}
            member.dead = False
            member.role = 'active'
            return
        verdict = self.plant._claim_for(
            member.key, {'op': 'claim_writer_unless_held',
                         'owner': member.token})
        if verdict.get('result') in ('done', 'claimed_shared'):
            member.dead = False
            member.role = 'active'
        else:
            # The refused startup: the activation aborts before the
            # monitor binds — the container exits.
            member.dead = True

    def cold_restart(self, name):
        """ctx['cold_restart_controller']: drop the member's resumed
        state, drop its dead attachment's hold, and run the
        configured-active startup claim."""
        member = self.keys[name]
        self.restarts.append((name, 'cold'))
        claim = self.plant.claim
        if claim is not None:
            claim['holders'].discard(member.key)
        member.misses = 0
        member.fencing_lost = False
        member.sync = 'unsynchronized'
        member.role = 'standby'
        self._startup_claim(member)

    def start_controller(self, name):
        """ctx['start_controller']: an exited member's plain start —
        the same configured-active startup claim."""
        member = self.keys[name]
        self.restarts.append((name, 'start'))
        if member.dead:
            self._startup_claim(member)

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
        if (method, route) == ('POST', '/demote'):
            if self.demote_refused:
                return 500, {'error': 'refused'}
            if member.role not in ('active', 'promoting'):
                return 409, {'error': 'not_active'}
            self.plant._claim_for(
                member.key, {'op': 'release_writer',
                             'keep_claim': True})
            member.role = 'demoting'
            member.sync = 'unsynchronized'
            member.misses = 0
            self._journal(member, {'role_changed': {
                'from': 'active', 'to': 'demoting'}})
            return 200, {'role': member.role}
        if (method, route) == ('POST', '/promote'):
            if self.promote_refused:
                return 500, {'error': 'refused'}
            if member.role in ('active', 'promoting'):
                return 409, 'already_active'
            if member.sync == 'unsynchronized' \
                    or (member is self.b and self.b_unsettled):
                return 409, 'not_converged'
            verdict = self.plant._claim_for(
                member.key, {'op': 'claim_writer',
                             'owner': member.token})
            if verdict.get('result') not in ('done', 'claimed_shared'):
                return 409, 'field_claim_failed'
            member.fencing_lost = False
            member.role = 'promoting'
            member.sync = 'unsynchronized'
            member.misses = 0
            self._journal(member, {'role_changed': {
                'from': 'standby', 'to': 'promoting'}})
            return 200, {'role': member.role}
        raise AssertionError('unhandled ' + url)


class YieldedRearmTests(unittest.TestCase):
    """The yielded-claim re-grant scenario against the stubbed pair:
    a clean rig passes — the socket legs proving every same-owner
    controller re-grant re-arms the incumbent refusal while the
    unbound probe and tool hold keep the yielded claim preemptable,
    and the controller half proving the armed peer's orphan
    re-promotion stands against the restartee's startup claim —
    each doctored defect reports the named diagnostic, and the
    unreachable, verb-predating, or seam-less rig is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = YieldedPlantPeer(YieldedPairFeed.TOKENS['active'])
        self.feed = YieldedPairFeed(
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

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'plant_ctl': self.plant.ctl,
               'plant_owner': dict(feed.TOKENS),
               'failover_misses': feed.BUDGET,
               'journal_files': self._journal_files(),
               'cold_restart_controller': feed.cold_restart,
               'start_controller': feed.start_controller,
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'YIELDED_REARM_SETTLE', 1.5), \
                patch.object(scenarios, 'YIELDED_REARM_POLL', 0.001), \
                patch.object(scenarios, 'YIELDED_REARM_SWITCH', 2), \
                patch.object(scenarios, 'YIELDED_REARM_DEMOTE', 1.5), \
                patch.object(scenarios, 'YIELDED_REARM_REPROMOTE', 1.5), \
                patch.object(scenarios, 'YIELDED_REARM_RECLAIM', 2), \
                patch.object(scenarios, 'YIELDED_REARM_GRACE', 0.001), \
                patch.object(scenarios, 'YIELDED_REARM_RESTORE', 1.5):
            return scenarios.scenario_yielded_rearm(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_yielded_rearm,
            scenarios.SCENARIOS)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout restored: ctrl-a owns the field again and
        # ctrl-b tracks it, the claim standing under the pinned token.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        claim = self.plant.claim or {}
        self.assertEqual(self.feed.TOKENS['active'], claim.get('owner'))
        self.assertFalse(claim.get('yielded'))
        # The episode ran its documented lifecycle: one cold restart of
        # the demoted peer, and its restore start.
        kinds = [kind for _name, kind in self.feed.restarts]
        self.assertIn('cold', kinds)

    def test_stale_yield_reports_failed(self):
        # The reported defect: the same-owner re-grant never clears
        # yielded — the foreign conditional grant lands.
        self.plant.stale_yield = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('yielded-rearm-failed',
                      record.get('detail', ''))
        self.assertIn('preemptable', record.get('detail', ''))

    def test_unbound_clears_yield_reports_failed(self):
        # An unbound orphan probe that ends the yield wrongly re-arms
        # the incumbent — the foreign grant must land on the still-
        # yielded claim and now fences instead.
        self.plant.unbound_clears = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('yielded-rearm-failed',
                      record.get('detail', ''))

    def test_tool_clears_yield_reports_failed(self):
        self.plant.tool_clears = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('yielded-rearm-failed',
                      record.get('detail', ''))

    def test_regrant_refused_reports_failed(self):
        # A same-owner conditional re-grant fenced out by the very
        # claim it was meant to re-arm.
        self.plant.regrant_refuses = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('re-grant', record.get('detail', ''))

    def test_bound_write_fenced_reports_failed(self):
        self.plant._write_fails = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_probe_misnamed_reports_failed(self):
        self.plant.misattributed = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_digests_diverge_reports_nondeterministic(self):
        self.plant.probe_grants_once = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('yielded-rearm-nondeterministic',
                      record.get('detail', ''))

    def test_demote_refused_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('demote', record.get('detail', ''))

    def test_never_repromotes_reports_failed(self):
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('re-promot', record.get('detail', ''))

    def test_restartee_preempts_reports_failed(self):
        # The controller-half defect: the restartee's startup claim
        # preempts the live successor.
        self.feed.restartee_preempts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('yielded-rearm-failed',
                      record.get('detail', ''))

    def test_successor_demotes_reports_failed(self):
        self.feed.successor_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_no_owner_reports_failed(self):
        self.feed.no_active = True
        # No peer ever reports active — the settle wait gives out.
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_unsupported_verbs_reports_inconclusive(self):
        self.plant.unsupported_verbs = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('predates', record.get('detail', ''))

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_lifecycle_reports_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(cold_restart_controller=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_budget_reports_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(failover_misses=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_tokens_reports_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(plant_owner={}))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_probe_point_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant_ctl=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        plant2 = YieldedPlantPeer(YieldedPairFeed.TOKENS['active'])
        feed2 = YieldedPairFeed(
            plant2, journal_files=self._journal_files('second'))
        try:
            self.plant.close()
            self.plant = plant2
            second = self.run_scenario(
                feed=feed2,
                ctx=self._ctx(feed2,
                              journal_files=self._journal_files(
                                  'second')))
        finally:
            plant2.close()
        self.assertEqual(first['outcome'], second['outcome'])
        self.assertEqual(first.get('observations'),
                         second.get('observations'))
