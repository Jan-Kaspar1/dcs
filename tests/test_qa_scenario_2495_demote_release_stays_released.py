"""The 2495_demote_release_stays_released leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_demote_release_stays_released, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration and the pair lifecycle build on the yielded-rearm leg's
fakes the same hand-back stages; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2450_yielded_rearm import (
    YieldedPairFeed, YieldedPlantPeer)


EXPECTED_CASES = frozenset({
    'DemoteReleaseTests.test_registered',
    'DemoteReleaseTests.test_clean_pair_passes_and_validates',
    'DemoteReleaseTests.test_rearm_under_demoted_token_reports_failed',
    'DemoteReleaseTests.test_silent_rearm_reports_failed',
    'DemoteReleaseTests.test_live_rearm_fences_the_pair_reports_'
    'failed',
    'DemoteReleaseTests.test_unclaimed_reclaim_reports_failed',
    'DemoteReleaseTests.test_dual_owner_reports_failed',
    'DemoteReleaseTests.test_frozen_field_reports_failed',
    'DemoteReleaseTests.test_pre_contract_build_reports_inconclusive',
    'DemoteReleaseTests.test_demote_refused_reports_failed',
    'DemoteReleaseTests.test_promote_refused_reports_failed',
    'DemoteReleaseTests.test_demotion_never_settles_reports_failed',
    'DemoteReleaseTests.test_ex_owner_never_converges_reports_failed',
    'DemoteReleaseTests.test_digests_diverge_reports_nondeterministic',
    'DemoteReleaseTests.test_silent_judge_reports_unchecked',
    'DemoteReleaseTests.test_no_owner_reports_failed',
    'DemoteReleaseTests.test_unfenced_field_reports_inconclusive',
    'DemoteReleaseTests.test_unattributed_verdict_reports_inconclusive',
    'DemoteReleaseTests.test_no_probe_writer_reports_inconclusive',
    'DemoteReleaseTests.test_unreachable_pair_reports_inconclusive',
    'DemoteReleaseTests.test_missing_endpoint_reports_inconclusive',
    'DemoteReleaseTests.test_missing_plant_reports_inconclusive',
    'DemoteReleaseTests.test_missing_plant_ctl_reports_inconclusive',
    'DemoteReleaseTests.test_missing_tokens_reports_inconclusive',
    'DemoteReleaseTests.test_missing_journals_reports_inconclusive',
    'DemoteReleaseTests.test_two_runs_produce_identical_evidence',
})


class ReleasePlantPeer(YieldedPlantPeer):
    """The released-claim rig's plant half: the yielded-rearm
    arbitration plus the read-only claim observation the lane's
    claim-aware attachment reads — `probe_writer` answering `done`
    while this attachment holds, `fenced` naming the standing claim's
    owner while another does, and `unclaimed` while none stands,
    asserting, joining, and releasing nothing. The doctors stage the
    finding's claim shapes: a demoted member's orphan re-arm that
    joins its attachment to the claim's holders — the live claim every
    conditional, non-preemptive path then meets fenced — and a field
    whose claim observation no build answers."""

    def __init__(self, owner):
        super().__init__(owner)
        # The doctors staging each named defect.
        self.rearm_binds = False    # the re-arm leaves a live claim
        self.probe_missing = False  # probe_writer is not a served op

    def dispatch_for(self, conn, request):
        op = request.get('op')
        if op == 'probe_writer':
            if self.probe_missing:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'probe_writer is not a supported op'}}
            claim = self.claim
            if claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            if conn in self._holders():
                return {'result': 'done'}
            return self._verdict(op)
        return super().dispatch_for(conn, request)

    def _claim_for(self, conn, request):
        if request.get('op') == 'ensure_writer' and self.rearm_binds:
            # The re-arm that leaves a live claim behind: the
            # re-asserting attachment joins the claim's holders, so
            # every conditional, non-preemptive path answers fenced.
            return self._claim_rearm(conn, request)
        return super()._claim_for(conn, request)

    def _claim_rearm(self, conn, request):
        """The orphan-cycle re-arm under the demoted member's own
        token: granted where the claim already names it, and joined to
        the holders where the doctored live-claim shape is staged."""
        claim = self.claim
        if claim is not None and claim['owner'] != request.get('owner'):
            return self._verdict('ensure_writer')
        if claim is None:
            self.claim = {'owner': request.get('owner'), 'holders': set(),
                          'monitor': request.get('monitor'),
                          'controller': True, 'yielded': False}
            claim = self.claim
        claim['holders'].add(conn)
        claim['controller'] = True
        claim['yielded'] = False
        return {'result': 'done'}


class ReleasePairFeed(YieldedPairFeed):
    """A stubbed pair for the released-claim leg: ctrl-a ('active') is
    the launched field owner and ctrl-b ('standby') the tracking
    standby armed with the failover budget. Every served monitor
    request is one scan of the addressed member, and each standby scan
    runs the two claim probes the demote hands the field to: the
    orphan-cycle conditional re-arm, kept off for a run whose ownership
    ended in its own requested demotion (the `yielded` mark #1270
    records), and the fencing-loss bound reclaim, armed by the loss
    mark or by an orphaned pull over an ownership the run did not hand
    back. A granted re-arm journals `field_claim_rearmed` naming its
    field point; a granted reclaim walks the peer promoting under the
    reclaim origin. `POST /demote` runs the keep-claim release and
    sets the yield mark; `POST /promote` claims unconditionally and
    clears it. The doctor flags stage each named defect."""

    def __init__(self, plant, journal_files=None):
        super().__init__(plant, journal_files=journal_files)
        for member in (self.a, self.b):
            member.was_owner = member.key == 'active'
            member.yielded = False
        # The doctors staging each named defect.
        self.no_yield_mark = False      # the voluntary demotion's own
                                        # orphan re-arm still runs
        self.rearm_silent = False       # a granted re-arm journals
                                        # nothing
        self.reclaim_stuck = False      # the bound reclaim never lands
        self.reclaim_silent = False     # the reclaim walks the peer
                                        # back with nothing journaled
        self.reclaim_after_rearm = False  # the ex-owner's arm stood down
                                        # where the demoted member's own
                                        # orphan probe re-armed the
                                        # released claim first
        self.rearm_landings = 0
        self.dual_owner = False         # the demoted member walks onto
                                        # the field beside its
                                        # successor
        self.field_frozen = False       # the owner's writes never
                                        # reach the field
        self.owner_never_tracks = False  # the ex-owner never converges

    def _orphan_ensure(self, member):
        """The orphan cycle's conditional re-arm: granted where the
        field stands unclaimed or already names this run's token, and
        the durable record the grant lands — one per landing."""
        verdict = self.plant._claim_for(
            member.key, {'op': 'ensure_writer', 'owner': member.token,
                         'rebind': not self.plant.rearm_binds})
        if verdict.get('result') in ('done', 'claimed_shared'):
            self.rearm_landings += 1
            if not self.rearm_silent:
                self._journal(member, {'field_claim_rearmed':
                                       {'point': self.plant.OUT}})
            return
        self._journal(member, {'field_claim_observed': {
            'point': self.plant.OUT,
            'claimant': (self.plant.claim or {}).get('owner')}})

    def _reclaim(self, member):
        """The fencing-loss bound re-grant — `reclaim_writer` under this
        run's own pinned token: refused while a different owner's claim
        has live holders, granted over an unclaimed, same-owner, or
        holderless claim — the released claim among them. A granted
        grant re-seats the peer, walking it promoting under the reclaim
        origin."""
        if self.reclaim_stuck:
            return
        if self.reclaim_after_rearm and not self.rearm_landings:
            return
        verdict = self.plant._claim_for(
            member.key, {'op': 'reclaim_writer', 'owner': member.token})
        if verdict.get('result') in ('done', 'claimed_shared'):
            member.fencing_lost = False
            member.role = 'promoting'
            member.sync = 'unsynchronized'
            member.yielded = False
            if not self.reclaim_silent:
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting',
                    'origin': 'reclaim'}})

    def _track(self, member):
        """One standby scan: the orphan-cycle re-arm and the
        fencing-loss reclaim, both kept off for a run whose ownership
        ended in its own requested demotion, then the tracked line's
        own stamp — a serving owner converges the pull, an ownerless
        one reports `orphaned` and counts the heartbeat miss the
        failover budget self-promotes on."""
        if self.owner_never_tracks and member is self.a:
            member.sync = 'unsynchronized'
            return
        if member.was_owner and not member.yielded:
            self._orphan_ensure(member)
            if member.fencing_lost or member.sync == 'orphaned':
                self._reclaim(member)
                if member.role != 'standby':
                    return
        if self.dual_owner and member is self.b \
                and member.sync == 'tracking':
            member.role = 'promoting'
            member.sync = 'unsynchronized'
            self._journal(member, {'role_changed': {
                'from': 'standby', 'to': 'promoting'}})
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
                member.yielded = False
                self._repromote_scans = 0
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting'}})

    def _field_write(self, member):
        if self.field_frozen:
            return 'landed' if self._owns_field(member) else 'fenced'
        return super()._field_write(member)

    def http_json(self, method, url, body=None, timeout=10):
        status, answer = super().http_json(method, url, body, timeout)
        host = url.split('/')[2].split(':')[0]
        member = self.members.get(host)
        if member is None:
            return status, answer
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if method == 'GET' and route == '/role' and isinstance(answer,
                                                                dict):
            claim = self.plant.claim
            answer = dict(answer, field_claim='unclaimed' if claim is None
                          else 'held')
        if (method, route) == ('POST', '/demote') and status == 200:
            # The voluntary demotion's release: the claim stands
            # yielded and the run marks the hand-back, which keeps its
            # own orphan-cycle re-arm off (#1270's `yielded` mark).
            if not self.no_yield_mark:
                member.yielded = True
        if (method, route) == ('POST', '/promote') and status == 200:
            # A granted claim lift opens a new ownership: the hand-back
            # mark clears and the run is an owner again.
            member.yielded = False
            member.was_owner = True
        return status, answer


class DemoteReleaseTests(unittest.TestCase):
    """The demote-release-stays-released scenario against the stubbed
    pair: a clean rig passes — the tracking peer's promotion fences the
    launch owner, its voluntary demote hands the claim back, the
    fencing-loss-armed ex-owner's bound reclaim takes the field, and
    the pair reconverges to the launch owner's active plus the demoted
    member's tracking standby — while each doctored defect reports the
    named diagnostic, the pre-contract build reports inconclusive, and
    the unreachable, verb-predating, or seam-less rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = ReleasePlantPeer(ReleasePairFeed.TOKENS['active'])
        self.feed = ReleasePairFeed(
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
               'journal_files': self._journal_files(),
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DR_SETTLE', 1.5), \
                patch.object(scenarios, 'DR_SWITCH', 2), \
                patch.object(scenarios, 'DR_FENCE', 1.5), \
                patch.object(scenarios, 'DR_CONVERGE', 1.5), \
                patch.object(scenarios, 'DR_DEMOTE', 1.5), \
                patch.object(scenarios, 'DR_WINDOW', 1.5), \
                patch.object(scenarios, 'DR_ROUNDS', 6), \
                patch.object(scenarios, 'DR_POLL', 0.001), \
                patch.object(scenarios, 'DR_RESTORE', 1.5):
            return scenarios.scenario_demote_release_stays_released(ctx)

    def _passes(self):
        return [json.loads(
            (self.evidence / name).read_text())
            for name in ('demote-release-pass-1.json',
                         'demote-release-pass-2.json')]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_claim_skew_bound) + 1,
            order.index(scenarios.scenario_demote_release_stays_released))
        self.assertIs(
            verify.case_function('demote-release-stays-released'),
            scenarios.scenario_demote_release_stays_released)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout restored: ctrl-a owns the field again with
        # the claim under its pinned token, ctrl-b tracking it.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        claim = self.plant.claim or {}
        self.assertEqual(self.feed.TOKENS['active'], claim.get('owner'))
        self.assertFalse(claim.get('yielded'))
        self.assertTrue((self.evidence
                         / 'demote-release-pass-1.json').is_file())
        passes = self._passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        digest = passes[0]['digest']
        self.assertEqual('armed', digest['staging'])
        self.assertEqual('yielded', digest['release'])
        self.assertEqual('journaled', digest['rearm'])
        self.assertEqual('moved', digest['claim'])
        self.assertEqual('reclaim', digest['resolution'])
        self.assertEqual('converged', digest['pair'])
        self.assertEqual('clean', digest['journal'])
        self.assertEqual('complete', digest['reads'])
        self.assertEqual('restored', digest['roles'])
        # The episode ran its documented sequence: the demoted member's
        # durable window carries no re-arm of the claim it handed back,
        # while the ex-owner journaled the reclaim that took the field.
        durable = passes[0]['record']['durable']
        self.assertFalse(any('field_claim_rearmed' in (entry.get('event')
                                                       or {})
                             for entry in durable['standby']))
        self.assertIn(('promoting', 'reclaim'),
                      [row[1:] for row in scenarios._dr_walk(
                          durable['active'])])

    def test_rearm_under_demoted_token_reports_failed(self):
        # The issue's named doctored negative: the release asserted as
        # staying released while the demoted peer's own orphan
        # machinery re-armed the claim under its own token — the
        # journaled re-arm the leg must name, with nothing left to
        # take the field back.
        self.feed.no_yield_mark = True
        self.feed.reclaim_stuck = True
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-failed'), record['detail'])
        self.assertIn('re-armed', record['detail'])

    def test_silent_rearm_reports_failed(self):
        # A re-arm the durable trail cannot show: the reclaim re-seats
        # the ex-owner and journals nothing naming it.
        self.feed.reclaim_silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-failed'), record['detail'])
        self.assertIn('durable trail', record['detail'])

    def test_live_rearm_fences_the_pair_reports_failed(self):
        # The finding's own wedge: the re-arm leaves a live claim under
        # the demoted member's token, so every conditional, non-
        # preemptive path — the ex-owner's bound reclaim included —
        # answers fenced and the pair never reconverges.
        self.feed.no_yield_mark = True
        self.plant.rearm_binds = True
        self.feed.reclaim_after_rearm = True
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-failed'), record['detail'])
        self.assertIn('orphan machinery re-armed', record['detail'])

    def test_unclaimed_reclaim_reports_failed(self):
        # Nothing takes the released claim and the demoted member's
        # failover budget never fires: the claim stands pinned under
        # the token that handed it back.
        self.feed.reclaim_stuck = True
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('no documented conditional path', record['detail'])

    def test_dual_owner_reports_failed(self):
        # The demoted member walks onto the field beside the
        # successor that already owns it.
        self.feed.dual_owner = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_frozen_field_reports_failed(self):
        # The pair reconverges on paper while the field itself is never
        # written again.
        self.feed.field_frozen = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('field', record['detail'])

    def test_pre_contract_build_reports_inconclusive(self):
        # The pre-#1270 shape: the demoted member's own orphan probe
        # re-armed the released claim under its own token, nothing
        # named the re-arm, the claim never left that token, and the
        # only member that stood on the field was the demoted one
        # re-taking it through its own failover budget.
        self.feed.no_yield_mark = True
        self.feed.rearm_silent = True
        self.feed.reclaim_stuck = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('predating', record['detail'])

    def test_demote_refused_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('demote', record['detail'])

    def test_promote_refused_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('promote', record['detail'])

    def test_demotion_never_settles_reports_failed(self):
        # The demotion sticks at `demoting`: the hand-back never
        # settles, and the pair never reconverges behind it.
        self.feed.b_never_settles = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-failed'), record['detail'])
        self.assertIn('never settled standby', record['detail'])

    def test_ex_owner_never_converges_reports_failed(self):
        # The fenced ex-owner applied no checkpoint, so the
        # orphaned-pull arm never re-armed and nothing took the field.
        self.feed.owner_never_tracks = True
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-failed'), record['detail'])
        self.assertIn('never converged', record['detail'])

    def test_digests_diverge_reports_nondeterministic(self):
        # Two clean passes whose rig read the demotion's release
        # differently — the yielded hand-back and the freed claim are
        # both the contract's answer, so neither is a failure, but the
        # two passes' digests must not diverge.
        clean = scenarios._dr_clean_record()
        freed = scenarios._dr_clean_record()
        freed['release_probe'] = {'claim': 'unclaimed', 'owner': None,
                                  'monitor': None}
        passes = iter([(clean, {'pass': 1}), (freed, {'pass': 2})])

        def fake_pass(*args, **kwargs):
            return next(passes)

        with patch.object(scenarios, '_dr_pass', fake_pass):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])

    def test_silent_judge_reports_unchecked(self):
        # The unchecked-diagnostic self-check leg: a judge silenced
        # mid-run lets every planted negative slip and the leg reports
        # its own unchecked name.
        with patch.object(scenarios, '_dr_judge', lambda *a, **k: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'demote-release-rearm-unchecked'), record['detail'])

    def test_no_owner_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_unfenced_field_reports_inconclusive(self):
        self.plant.open_field = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_unattributed_verdict_reports_inconclusive(self):
        self.plant.unattributed = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('attribution', record['detail'])

    def test_no_probe_writer_reports_inconclusive(self):
        self.plant.probe_missing = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('claim observation', record['detail'])

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_endpoint_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(standby=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_plant_ctl_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant_ctl=None))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_tokens_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(plant_owner={}))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_missing_journals_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(journal_files={}))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        plant2 = ReleasePlantPeer(ReleasePairFeed.TOKENS['active'])
        feed2 = ReleasePairFeed(
            plant2, journal_files=self._journal_files('second'))
        try:
            self.plant.close()
            self.plant = plant2
            second = self.run_scenario(
                feed=feed2,
                ctx=self._ctx(feed2,
                              journal_files=self._journal_files('second')))
        finally:
            plant2.close()
        self.assertEqual(first['outcome'], second['outcome'])
        self.assertEqual(first.get('observations'),
                         second.get('observations'))


if __name__ == '__main__':
    unittest.main()
