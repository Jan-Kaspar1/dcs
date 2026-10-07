"""The 2460_yielded_claim_rearm leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_yielded_claim_rearm, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration and the pair lifecycle build on the yielded-rearm leg's
fakes the same induction stages.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2450_yielded_rearm import (
    YieldedPairFeed, YieldedPlantPeer)


class RearmPlantPeer(YieldedPlantPeer):
    """The yielded-claim re-arm rig's plant half: the yielded-rearm
    arbitration plus the doctors this leg's stages name — a foreign
    conditional grant that lands over the live re-bound incumbent
    (the #1123 defect's socket shape, the doctored negative the
    issue calls out) and a same-owner conditional re-grant refused
    on the leg's own socket attachments."""

    def __init__(self, owner):
        super().__init__(owner)
        # The doctors staging each named defect.
        self.foreign_grants = False       # a foreign conditional
                                          # grant lands over the live
                                          # re-bound incumbent
        self.socket_regrant_refuses = False  # the socket re-grant
                                             # fences

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        claim = self.claim
        if op == 'claim_writer_unless_held' and claim is not None:
            if self.foreign_grants \
                    and owner == scenarios.REARM_FOREIGN \
                    and claim['owner'] != owner:
                # The doctored negative: the foreign conditional
                # claim asserts granted over the re-bound live
                # incumbent — the leg must fail by name.
                return self._grant(conn, owner, request,
                                   rebind=True, controller=True)
            if self.socket_regrant_refuses \
                    and claim['owner'] == owner \
                    and conn not in ('active', 'standby'):
                # A same-owner conditional re-grant fenced out by
                # the very claim it was meant to re-arm — scoped to
                # the leg's socket attachments so the orphan
                # re-promotion's own conditional grant still runs.
                return self._verdict(op)
        return super()._claim_for(conn, request)


class RearmPairFeed(YieldedPairFeed):
    """The yielded-rearm pair plus the durable activation record:
    a startup claim that lands journals the field-owning walk the
    leg's restartee audit names — the refused startup exits ahead
    of it, so a claimed restartee is exactly the preemption the
    contract refuses."""

    def _startup_claim(self, member):
        super()._startup_claim(member)
        if member.role == 'active' and not member.dead:
            self._journal(member, {'role_changed': {
                'from': 'standby', 'to': 'active'}})


class YieldedClaimRearmTests(unittest.TestCase):
    """The yielded-claim re-arm scenario against the stubbed pair:
    a clean rig passes — the armed peer's demote yielding its own
    claim, its orphan failover re-promoting it as the live owner of
    its own standing claim, and the cold-restarted ex-owner's
    conditional startup claim fencing rather than preempting the
    re-bound incumbent, the socket re-stage answering fenced naming
    it — each doctored defect reports the named diagnostic, and the
    unreachable, verb-predating, or seam-less rig is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = RearmPlantPeer(RearmPairFeed.TOKENS['active'])
        self.feed = RearmPairFeed(
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
                patch.object(scenarios, 'REARM_SETTLE', 1.5), \
                patch.object(scenarios, 'REARM_POLL', 0.001), \
                patch.object(scenarios, 'REARM_SWITCH', 2), \
                patch.object(scenarios, 'REARM_DEMOTE', 1.5), \
                patch.object(scenarios, 'REARM_REPROMOTE', 1.5), \
                patch.object(scenarios, 'REARM_ROUNDS', 6), \
                patch.object(scenarios, 'REARM_GRACE', 0.001), \
                patch.object(scenarios, 'REARM_RESTORE', 1.5), \
                patch.object(scenarios, 'REARM_RECLAIM', 2):
            return scenarios.scenario_yielded_claim_rearm(ctx)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The re-arm leg slots beside its contract sibling — the
        # claim-lifecycle cluster behind the doomed-startup leg and
        # ahead of the revision legs.
        self.assertEqual(
            order.index(scenarios.scenario_yielded_rearm) + 1,
            order.index(scenarios.scenario_yielded_claim_rearm))
        self.assertIs(
            verify.case_function('yielded-claim-rearm'),
            scenarios.scenario_yielded_claim_rearm)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        # The launch layout restored: ctrl-a owns the field again
        # and ctrl-b tracks it, the claim standing unyielded under
        # the pinned token.
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        claim = self.plant.claim or {}
        self.assertEqual(self.feed.TOKENS['active'],
                         claim.get('owner'))
        self.assertFalse(claim.get('yielded'))
        # The episode ran its documented lifecycle: one cold
        # restart of the demoted peer per pass, and its restore
        # start.
        kinds = [kind for _name, kind in self.feed.restarts]
        self.assertIn('cold', kinds)
        self.assertIn('start', kinds)
        for name in ('yielded-claim-rearm-pass-1.json',
                     'yielded-claim-rearm-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads(
            (self.evidence / name).read_text())
            for name in ('yielded-claim-rearm-pass-1.json',
                         'yielded-claim-rearm-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        digest = passes[0]['digest']
        self.assertEqual('fenced', digest['restartee'])
        self.assertEqual('held', digest['incumbent'])
        self.assertEqual('named', digest['socket'])
        self.assertEqual('clean', digest['journals'])
        self.assertEqual('restored', digest['roles'])

    def test_stale_yield_reports_failed(self):
        # The reported defect end to end: the orphan re-promotion's
        # same-owner re-grant never clears the yield mark, so the
        # restartee's conditional startup claim preempts the
        # re-bound incumbent.
        self.plant.stale_yield = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-failed'), record['detail'])

    def test_restartee_preempts_reports_failed(self):
        # The controller-half defect: the restartee's startup claim
        # preempts the live successor outright.
        self.feed.restartee_preempts = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-failed'), record['detail'])

    def test_foreign_grant_reports_failed(self):
        # The doctored negative the issue calls out: a foreign
        # conditional claim asserted as granted over the re-bound
        # incumbent — the socket stage's fenced verdict must hold.
        self.plant.foreign_grants = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-failed'), record['detail'])
        self.assertIn('preempted', record['detail'])

    def test_socket_regrant_refused_reports_failed(self):
        # A same-owner conditional re-grant fenced out by the very
        # claim it was meant to re-arm.
        self.plant.socket_regrant_refuses = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-failed'), record['detail'])
        self.assertIn('re-grant', record['detail'])

    def test_misattributed_probe_reports_failed(self):
        self.plant.misattributed = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_demote_refused_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('demote', record['detail'])

    def test_never_repromotes_reports_failed(self):
        self.feed.no_repromote = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('re-promot', record['detail'])

    def test_successor_demotes_reports_failed(self):
        self.feed.successor_demotes = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)

    def test_digests_diverge_reports_nondeterministic(self):
        passes = iter([({'settle': 'launch'}, {}, {'pass': 1}),
                       ({'settle': 'armed'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_rearm_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])

    def test_silent_judges_report_unchecked(self):
        # The unchecked-diagnostic self-check leg: a judge silenced
        # mid-run lets the planted negatives slip, and the leg
        # reports its own unchecked name.
        with patch.object(scenarios, '_rearm_probe_judge',
                          lambda *a, **k: 'named'):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertTrue(record['detail'].startswith(
            'yielded-claim-rearm-unchecked'), record['detail'])

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
        self.assertIn('predates', record['detail'])

    def test_unfenced_field_reports_inconclusive(self):
        self.plant.open_field = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

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

    def test_missing_journals_reports_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(journal_files={}))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        plant2 = RearmPlantPeer(RearmPairFeed.TOKENS['active'])
        feed2 = RearmPairFeed(
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


if __name__ == '__main__':
    unittest.main()
