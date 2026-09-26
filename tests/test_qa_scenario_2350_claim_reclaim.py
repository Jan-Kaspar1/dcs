"""The 2350_claim_reclaim leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_claim_reclaim, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ClaimReclaimTests.test_registered',
    'ClaimReclaimTests.test_clean_pair_passes_and_validates',
    'ClaimReclaimTests.test_killed_owner_reports_failed',
    'ClaimReclaimTests.test_never_demotes_reports_failed',
    'ClaimReclaimTests.test_silent_loss_reports_failed',
    'ClaimReclaimTests.test_unattributed_loss_reports_failed',
    'ClaimReclaimTests.test_misattributed_loss_reports_failed',
    'ClaimReclaimTests.test_premature_grant_reports_failed',
    'ClaimReclaimTests.test_peer_move_reports_failed',
    'ClaimReclaimTests.test_foreign_write_reports_failed',
    'ClaimReclaimTests.test_open_held_claim_reports_failed',
    'ClaimReclaimTests.test_never_reclaims_reports_failed',
    'ClaimReclaimTests.test_unbound_reseat_reports_failed',
    'ClaimReclaimTests.test_unreconverged_pair_reports_failed',
    'ClaimReclaimTests.test_reclaim_restart_reports_failed',
    'ClaimReclaimTests.test_shared_claim_reports_nondeterministic',
    'ClaimReclaimTests.test_diverging_digests_report_nondeterministic',
    'ClaimReclaimTests.test_release_refusal_reports_inconclusive',
    'ClaimReclaimTests.test_predating_rig_reports_inconclusive',
    'ClaimReclaimTests.test_foreign_baseline_reports_inconclusive',
    'ClaimReclaimTests.test_open_field_reports_inconclusive',
    'ClaimReclaimTests.test_no_active_reports_failed',
    'ClaimReclaimTests.test_unsettled_pair_reports_inconclusive',
    'ClaimReclaimTests.test_unreachable_plant_reports_inconclusive',
    'ClaimReclaimTests.test_missing_plant_endpoint_reports_inconclusive',
    'ClaimReclaimTests.test_missing_owner_token_reports_inconclusive',
    'ClaimReclaimTests.test_two_runs_produce_identical_evidence',
})


class ReclaimPlantPeer(FakePlantPeer):
    """The claim-reclaim rig's plant half: FakePlantPeer plus the
    full write-ownership arbitration the scenario induces — per-
    attachment holder tracking, `claim_writer`'s unconditional
    preemption, `ensure_writer`'s conditional grant (`rebind`
    joining the holder, refused while a different owner's claim
    stands), `release_writer` dropping only this attachment's hold
    (the last-holder release unclaiming the field), disconnect
    dropping the hold but never the claim, and `write`/`step`
    fencing against the standing claim — with decision 97's
    attribution: every fencing verdict names the standing claim's
    owner token. The feed drives the controller side's scan writes
    through `owner_write` and its loss-marked reclaim probes
    through `reclaim_ensure`. The doctor flags stage each named
    defect the leg reports."""

    IN, OUT = 10, 100

    def __init__(self, owner):
        super().__init__()
        self.samples = {
            self.IN: {'value': {'float': 0.8}, 'quality': 'good',
                      'tick': 0},
            self.OUT: {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0}}
        self.owner = owner
        self.claim = {'owner': owner, 'holders': {'controller'}}
        self.plant_tick = 0
        # The doctors staging each named defect.
        self.down = False              # every attachment drops unanswered
        self.open_field = False        # mutations ignore the claim
        self.unattributed = False      # verdicts carry no owner — predates
        self.misattributed = False     # verdicts name a wrong owner
        self.shared = False            # claims answer claimed_shared
        self.release_refuses = False
        self.ensure_preempts = False   # ensure grants past a foreign claim
        self.unbound_grant = False     # ensure grants never join holders
        self.field_moves = False       # the field drifts under the hold
        self.release_opens_field = False  # mutations unfenced after
                                          # the preemptor's release

    def _holders(self):
        return (self.claim or {}).get('holders') or set()

    def _verdict(self, op, point=None):
        """The fencing answer a non-holder's mutation or refused
        claim meets — `fenced` for step/claim probes, `io.fenced`
        for a write — attributing the standing claim's owner per
        decision 97, unless a doctor strips or forges the name."""
        if self.unattributed:
            named = None
        elif self.misattributed:
            named = 0x5E1F     # a token no rig pin or induction uses
        else:
            named = self.claim['owner']
        if op == 'write':
            error = {'kind': 'io', 'error': {'fenced': point}}
        else:
            error = {'kind': 'fenced',
                     'detail': 'another attachment owns field writes'}
        if named is not None:
            error['owner'] = named
        return {'result': 'error', 'error': error}

    def owner_write(self):
        """One controller-scan field write: fenced while the
        'controller' attachment is outside the holders — the
        verdict that demotes the owner — else the write lands and
        the plant tick advances."""
        if self.claim is None or 'controller' not in self._holders():
            return 'fenced'
        self.plant_tick += 1
        self.samples[self.OUT].update(tick=self.plant_tick,
                                      value={'float': 1.5})
        return 'landed'

    def reclaim_ensure(self):
        """The demoted ex-owner's bound conditional re-grant —
        ensure_writer under its own pinned token, rebind=true:
        refused while a different owner's claim stands (unless the
        preempting doctor grants it anyway), granted into an
        unclaimed or same-owner claim — the bound grant joining
        'controller' to the holders unless the unbound doctor
        leaves the re-take holderless."""
        if self.claim is not None \
                and self.claim['owner'] != self.owner:
            if not self.ensure_preempts:
                return 'refused'
            self.claim = {'owner': self.owner,
                          'holders': {'controller'}}
            return 'granted'
        if self.claim is None:
            self.claim = {'owner': self.owner, 'holders': set()}
        if not self.unbound_grant:
            self.claim['holders'].add('controller')
        return 'granted'

    def release_conn(self, conn):
        # Disconnect drops the attachment's hold, never the claim —
        # the dead-owner fencing the standing claim provides.
        if self.claim is not None:
            self.claim['holders'].discard(conn)

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if op == 'release_writer':
            if self.release_refuses:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'release refused'}}
            if self.claim is not None:
                self.claim['holders'].discard(conn)
                if not self.claim['holders']:
                    self.claim = None
            return {'result': 'done'}
        if op == 'ensure_writer':
            if self.claim is not None \
                    and self.claim['owner'] != owner \
                    and not self.ensure_preempts:
                return self._verdict(op)
            if self.claim is None \
                    or self.claim['owner'] != owner:
                self.claim = {'owner': owner, 'holders': set()}
            if request.get('rebind', True):
                self.claim['holders'].add(conn)
            return {'result': 'done'}
        shared = self.claim is not None \
            and self.claim['owner'] == owner \
            and any(h is not conn for h in self._holders())
        if self.claim is None or self.claim['owner'] != owner:
            self.claim = {'owner': owner, 'holders': {conn}}
        else:
            self.claim['holders'].add(conn)
        if shared or self.shared:
            return {'result': 'claimed_shared', 'owner': owner}
        return {'result': 'done'}

    def dispatch_for(self, conn, request):
        if self.down:
            raise OSError('the plant is down')
        if self.field_moves:
            # The doctored defect: the field drifts while the
            # foreign claim stands — a foreign write's signature.
            self.plant_tick += 1
            self.samples[self.OUT].update(tick=self.plant_tick)
        op = request.get('op')
        if op in ('claim_writer', 'ensure_writer', 'release_writer'):
            self.requests.append(request)
            if op == 'release_writer' and self.release_opens_field:
                self.open_field = True
            return self._claim_for(conn, request)
        if op in ('write', 'step'):
            self.requests.append(request)
            if self.open_field or conn in self._holders():
                if op == 'step':
                    self.plant_tick += 1
                    return {'result': 'stepped',
                            'tick': self.plant_tick}
                self.samples[request['point']].update(
                    value=request['value'], tick=self.plant_tick)
                return {'result': 'done'}
            if self.claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            return self._verdict(op, request.get('point'))
        if op == 'list_points':
            self.requests.append(request)
            return {'result': 'points', 'points': [
                {'point': p, 'sample': self.served(p),
                 'direction': 'out' if p == self.OUT else 'in',
                 'fault': self.faults.get(p)}
                for p in sorted(self.samples)]}
        return super().dispatch_for(conn, request)


class ClaimReclaimFeed:
    """A stubbed pair for the claim-reclaim scenario: ctrl-a is the
    field owner — every served monitor request is one scan, writing
    the field while it holds the claim and driving the settled
    contract's responses to a fenced write — and ctrl-b is the
    tracking standby. A fenced scan write counts the io_health
    ledger, journals field_claim_lost attributed to the claimant
    the standing claim names, and walks demoting -> standby with
    the fencing-loss mark standing; the mark's bound conditional
    re-grant probes every standby scan — refused while the foreign
    claim holds, granted once the release frees the field —
    walking promoting -> active unattended. The doctor flags stage
    each named defect the leg reports."""

    OWNER = 424243

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.role = 'active'
        self.dead = False
        self.fencing_lost = False
        self.failed_writes = 0
        self.journal = []
        self.seq = 0
        self.peer_role_calls = 0
        self.peer_demoted = False
        # The doctors staging each named defect.
        self.silent = False            # ctrl-a never reports
        self.dies_on_fence = False     # the fenced write kills it
        self.dies_on_release = False   # a restart mid-reclaim
        self.no_demote = False         # the fenced write never demotes
        self.no_journal = False        # the demotion goes unrecorded
        self.no_claimant = False       # the loss journal names no one
        self.wrong_claimant = False    # the loss names a wrong token
        self.never_reclaims = False    # the bound grant never probes
        self.peer_moves = False        # ctrl-b reports a role change
        self.peer_wrong_role = False   # the pair never settles
        self.peer_no_tracking = False  # ctrl-b never re-tracks
        self.peer_journaled = False    # ctrl-b journals a transition
        self._peer_lost_tracking = False

    def _journal(self, event):
        self.seq += 1
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})

    def _supersede(self):
        # The contract's demote-in-place: count the fenced write,
        # journal the loss — attributed to the claimant the
        # field's arbitration named — and walk demoting -> standby
        # with the fencing-loss mark standing for the reclaim.
        self.failed_writes += 1
        self.fencing_lost = True
        if not self.no_journal:
            loss = {'point': self.plant.OUT}
            if not self.no_claimant:
                loss['claimant'] = 0xDEAD if self.wrong_claimant \
                    else (self.plant.claim or {}).get('owner')
            self._journal({'field_claim_lost': loss})
        if self.dies_on_fence:
            self.dead = True
            return
        if not self.no_demote:
            self.role = 'demoting'
            self._journal({'role_changed': {'from': 'active',
                                            'to': 'demoting'}})

    def _scan(self):
        """One controller scan: an active or promoting role writes
        the field — the fencing verdict superseding it — while a
        marked standby probes the bound conditional re-grant."""
        self.tick += 1
        if self.role == 'demoting':
            self.role = 'standby'
            self._journal({'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
            return
        if self.role == 'standby':
            if self.fencing_lost and not self.never_reclaims:
                if self.plant.reclaim_ensure() == 'granted':
                    self.fencing_lost = False
                    self.role = 'promoting'
                    self._journal({'role_changed': {
                        'from': 'standby', 'to': 'promoting'}})
            return
        if self.plant.owner_write() == 'fenced':
            self._supersede()
            return
        if self.role == 'promoting':
            self.role = 'active'
            self._journal({'role_changed': {'from': 'promoting',
                                            'to': 'active'}})

    def _peer_tracks(self):
        # The peer keeps reporting a tracking standby through the
        # launch claim; with the no-tracking doctor, once the claim
        # has left the owner the tracking report never returns.
        if not self.peer_no_tracking:
            return True
        claim = self.plant.claim
        if claim is not None and claim['owner'] != self.OWNER:
            self._peer_lost_tracking = True
        return not self._peer_lost_tracking

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        if host.startswith('ctrl-b'):
            if route == '/role':
                self.peer_role_calls += 1
                role = 'standby'
                if self.peer_moves and self.peer_role_calls > 1 \
                        and not self.peer_demoted:
                    role = 'promoting'
                if self.peer_wrong_role:
                    role = 'active'
                report = {'role': role, 'tick': 0}
                if role == 'standby' and self._peer_tracks():
                    report['sync'] = {'tracking': {'aligned': 1}}
                return 200, report
            if route == '/demote':
                self.peer_demoted = True
                return 200, {'role': 'standby'}
            if route == '/journal':
                entries = []
                if self.peer_journaled:
                    entries = [{'seq': since + 1, 'tick': 0,
                                'event': {'role_changed': {
                                    'from': 'standby',
                                    'to': 'promoting'}}}]
                return 200, entries
            raise AssertionError('unhandled ' + url)
        if not host.startswith('ctrl-a'):
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent or self.dead \
                or (self.dies_on_release
                    and self.plant.claim is None
                    and self.role == 'standby'):
            raise urllib.error.URLError('unreachable')
        self._scan()
        if route == '/role':
            return 200, {'tick': self.tick, 'role': self.role}
        if route == '/snapshot':
            return 200, {'tick': self.tick, 'points': [],
                         'io_health':
                         {'failed_writes': self.failed_writes,
                          'journal_errors': 0,
                          'journals_appended': 0}}
        if route == '/journal':
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if route == '/promote':
            if self.role == 'standby':
                # The documented operator unwedge: the same claim
                # the reclaim would take, posted by hand.
                self.plant.claim = {'owner': self.OWNER,
                                    'holders': {'controller'}}
                self.fencing_lost = False
                self.role = 'promoting'
                self._journal({'role_changed': {
                    'from': 'standby', 'to': 'promoting'}})
            return 200, {'role': self.role}
        raise AssertionError('unhandled ' + url)


class ClaimReclaimTests(unittest.TestCase):
    """The claim-reclaim scenario against the stubbed pair: a clean
    rig passes with identical digests and evidence — the held
    foreign claim fencing the owner into its attributed demotion,
    the bound grant refusing the different-owner claim through the
    window, and the release letting the marked ex-owner re-seat
    the claim and reconverge the pair — each doctored defect
    reports the named diagnostic, and the unreachable or
    contract-predating rig is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = ReclaimPlantPeer(ClaimReclaimFeed.OWNER)
        self.feed = ClaimReclaimFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.feed.OWNER,
                                'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CLAIM_RECLAIM_SETTLE', 2), \
                patch.object(scenarios, 'CLAIM_RECLAIM_POLL', 0.001), \
                patch.object(scenarios, 'CLAIM_RECLAIM_DEADLINE', 2), \
                patch.object(scenarios, 'CLAIM_RECLAIM_ROUNDS', 4), \
                patch.object(scenarios, 'CLAIM_RECLAIM_RESTORE', 0.01):
            return scenarios.scenario_claim_reclaim(ctx)

    def test_registered(self):
        self.assertIn(scenarios.scenario_claim_reclaim,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('claim-reclaim'),
                      scenarios.scenario_claim_reclaim)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('claim-reclaim-pass-1.json',
                     'claim-reclaim-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['digest'], {
                'claim': 'granted', 'seized': 'named',
                'demotion': 'in-place', 'loss': 'attributed',
                'hold': 'refused', 'release': 'done',
                'reclaim': 'reseated', 'writes': 'landed',
                'pair': 'reconverged'})
        report.validate_scenario(record)
        # The launch claim state and roles behind it: the owner's
        # token holds the claim with the controller joined, the
        # induction attachment's hold released, and no role moved.
        self.assertEqual(self.plant.claim,
                         {'owner': self.feed.OWNER,
                          'holders': {'controller'}})
        self.assertEqual(self.feed.role, 'active')
        self.assertFalse(self.feed.fencing_lost)

    def test_killed_owner_reports_failed(self):
        self.feed.dies_on_fence = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('monitor', record['detail'])
        report.validate_scenario(record)

    def test_never_demotes_reports_failed(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('never demoted', record['detail'])
        report.validate_scenario(record)

    def test_silent_loss_reports_failed(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_unattributed_loss_reports_failed(self):
        self.feed.no_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('no claimant', record['detail'])
        report.validate_scenario(record)

    def test_misattributed_loss_reports_failed(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('attributes the takeover', record['detail'])
        report.validate_scenario(record)

    def test_premature_grant_reports_failed(self):
        # The defect the held window exists to catch: the marked
        # ex-owner's conditional grant preempts the standing
        # different-owner claim instead of refusing it.
        self.plant.ensure_preempts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('refuse', record['detail'])
        report.validate_scenario(record)

    def test_peer_move_reports_failed(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('peer', record['detail'])
        report.validate_scenario(record)

    def test_foreign_write_reports_failed(self):
        self.plant.field_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('foreign write', record['detail'])
        report.validate_scenario(record)

    def test_open_held_claim_reports_failed(self):
        # The released field stops fencing third-party mutations —
        # the re-seated claim the verdict attributes to the owner is
        # no real claim at all.
        self.plant.release_opens_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('does not fence', record['detail'])
        report.validate_scenario(record)

    def test_never_reclaims_reports_failed(self):
        self.feed.never_reclaims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('never re-seated', record['detail'])
        report.validate_scenario(record)

    def test_unbound_reseat_reports_failed(self):
        # A re-grant that never joins the holders: the claim
        # re-seats but the owner's own writes stay fenced — the
        # bound grant's load-bearing half missing.
        self.plant.unbound_grant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        report.validate_scenario(record)

    def test_unreconverged_pair_reports_failed(self):
        self.feed.peer_no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('reconverg', record['detail'])
        report.validate_scenario(record)

    def test_reclaim_restart_reports_failed(self):
        self.feed.dies_on_release = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-failed'), record['detail'])
        self.assertIn('restart', record['detail'])
        report.validate_scenario(record)

    def test_shared_claim_reports_nondeterministic(self):
        self.plant.shared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'writes': 'landed'}, {}, {'pass': 1}),
                       ({'writes': 'stalled'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_claim_reclaim_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-reclaim-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_release_refusal_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('hand-back', record['detail'])
        report.validate_scenario(record)

    def test_predating_rig_reports_inconclusive(self):
        # A rig whose fencing verdicts name no standing owner
        # predates decision 97's attribution surface — the leg
        # stops before inducing anything.
        self.plant.unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        # Nothing was disturbed: the launch claim still stands.
        self.assertEqual(self.plant.claim['owner'],
                         self.feed.OWNER)
        report.validate_scenario(record)

    def test_foreign_baseline_reports_inconclusive(self):
        self.plant.misattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('foreign token', record['detail'])
        report.validate_scenario(record)

    def test_open_field_reports_inconclusive(self):
        self.plant.open_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('standing writer claim', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record['detail'])
        report.validate_scenario(record)

    def test_unsettled_pair_reports_inconclusive(self):
        self.feed.peer_wrong_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_plant_reports_inconclusive(self):
        self.plant.down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_plant_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('claim ops', record['detail'])
        report.validate_scenario(record)

    def test_missing_owner_token_reports_inconclusive(self):
        ctx = self._ctx()
        ctx['plant_owner'] = {'standby': 424244}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('owner token', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ReclaimPlantPeer(ClaimReclaimFeed.OWNER)
            feed = ClaimReclaimFeed(plant)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
