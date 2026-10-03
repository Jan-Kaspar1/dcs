"""The 2360_claim_observation leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_claim_observation, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py; the claim
arbitration and controller-pair halves build on the claim-reclaim
leg's fakes the same handover stages; EXPECTED_CASES pins this
module's contribution to the suite's case coverage so a dropped case
fails the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2350_claim_reclaim import (
    ClaimReclaimFeed, ReclaimPlantPeer)


EXPECTED_CASES = frozenset({
    'ClaimObservationTests.test_registered',
    'ClaimObservationTests.test_clean_pair_passes_and_validates',
    'ClaimObservationTests.test_killed_owner_reports_failed',
    'ClaimObservationTests.test_never_demotes_reports_failed',
    'ClaimObservationTests.test_silent_loss_reports_failed',
    'ClaimObservationTests.test_unattributed_loss_reports_failed',
    'ClaimObservationTests.test_misattributed_loss_reports_failed',
    'ClaimObservationTests.test_premature_grant_reports_failed',
    'ClaimObservationTests.test_peer_move_reports_failed',
    'ClaimObservationTests.test_foreign_write_reports_failed',
    'ClaimObservationTests.test_silent_observation_reports_failed',
    'ClaimObservationTests.'
    'test_unattributed_observation_reports_failed',
    'ClaimObservationTests.'
    'test_misattributed_observation_reports_failed',
    'ClaimObservationTests.test_reseeded_observation_reports_failed',
    'ClaimObservationTests.'
    'test_duplicated_observation_reports_failed',
    'ClaimObservationTests.test_file_observation_reports_failed',
    'ClaimObservationTests.test_never_reclaims_reports_failed',
    'ClaimObservationTests.test_unbound_reseat_reports_failed',
    'ClaimObservationTests.test_unreconverged_pair_reports_failed',
    'ClaimObservationTests.test_reclaim_restart_reports_failed',
    'ClaimObservationTests.'
    'test_shared_claim_reports_nondeterministic',
    'ClaimObservationTests.'
    'test_diverging_digests_report_nondeterministic',
    'ClaimObservationTests.'
    'test_release_refusal_reports_inconclusive',
    'ClaimObservationTests.'
    'test_claim_staging_refused_reports_inconclusive',
    'ClaimObservationTests.test_predating_rig_reports_inconclusive',
    'ClaimObservationTests.'
    'test_foreign_baseline_reports_inconclusive',
    'ClaimObservationTests.test_open_field_reports_inconclusive',
    'ClaimObservationTests.test_no_active_reports_failed',
    'ClaimObservationTests.'
    'test_unsettled_pair_reports_inconclusive',
    'ClaimObservationTests.'
    'test_unreachable_plant_reports_inconclusive',
    'ClaimObservationTests.'
    'test_missing_plant_endpoint_reports_inconclusive',
    'ClaimObservationTests.'
    'test_missing_plant_ctl_reports_inconclusive',
    'ClaimObservationTests.'
    'test_missing_journal_files_reports_inconclusive',
    'ClaimObservationTests.'
    'test_missing_owner_token_reports_inconclusive',
    'ClaimObservationTests.test_two_runs_produce_identical_evidence',
})


class ObservedPlantPeer(ReclaimPlantPeer):
    """The claim-observation rig's plant half: the claim-reclaim
    arbitration plus the staging-lever refusal — a plant that
    refuses `claim_writer` outright stages no induction at all, the
    inconclusive the leg names when the claim-staging lever is
    unavailable."""

    def __init__(self, owner):
        super().__init__(owner)
        self.claims_refused = False   # claim_writer meets a refusal —
                                      # the claim-staging lever absent

    def _claim_for(self, conn, request):
        if self.claims_refused \
                and request['op'] == 'claim_writer':
            return {'result': 'error', 'error': {
                'kind': 'invalid_request',
                'detail': 'claim_writer is not a supported op'}}
        return super()._claim_for(conn, request)


class ObservedClaimFeed(ClaimReclaimFeed):
    """A stubbed pair for the claim-observation scenario: ctrl-a is
    the field owner, ctrl-b the tracking standby — the claim-reclaim
    feed plus #987's observed-claimant journal. A marked ex-owner's
    bound conditional re-grant probes every standby scan; a refusal
    naming a standing owner the journal has not recorded mints one
    field_claim_observed{point, claimant} record, deduplicated on
    the token and seeded by the recorded loss's claimant so the
    field_claim_lost attribution never double-records. Every
    journaled event mirrors into the --journal-file paths the leg's
    durable audit reads. The doctor flags stage each named defect
    the leg reports."""

    def __init__(self, plant, journal_files=None):
        super().__init__(plant)
        # The durable half: journal_files maps ctx keys to the
        # runner's --journal-file paths — ctrl-a binds 'active',
        # ctrl-b 'standby'. A feed without paths mirrors nothing.
        self.journal_paths = {}
        for name, key in (('a', 'active'), ('b', 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[name] = path
        # The observed-claimant dedup: the standing-owner tokens the
        # journal already attributes — re-seeded per episode by the
        # recorded loss's claimant.
        self.observed_tokens = set()
        # The doctors staging each named defect.
        self.no_observe = False        # refused probes never journal
        self.observe_unnamed = False   # the record names no claimant
        self.observe_foreign = False   # the record names a wrong token
        self.observe_reseed = False    # the loss's claimant is never
                                       # seeded — it observes again
        self.observe_dupes = False     # every refused probe journals —
                                       # per probe, not per token
        self.file_omits_observed = False  # the durable file drops the
                                          # observed record

    def _journal(self, event):
        """One journaled record — the served journal plus the durable
        --journal-file mirror the leg's file audit reads."""
        self.seq += 1
        entry = {'seq': self.seq, 'tick': self.tick, 'event': event}
        self.journal.append(entry)
        if self.file_omits_observed \
                and 'field_claim_observed' in event:
            return
        path = self.journal_paths.get('a')
        if path is not None:
            with path.open('a') as stream:
                stream.write(json.dumps({'entry': entry}) + '\n')

    def _supersede(self):
        super()._supersede()
        # The observed-claimant dedup's seed: the recorded loss's
        # claimant never re-observes — re-seeded each episode, and
        # skipped under the reseed doctor.
        self.observed_tokens = set()
        if not self.observe_reseed and not self.no_journal \
                and not self.no_claimant:
            claimant = 0xDEAD if self.wrong_claimant \
                else (self.plant.claim or {}).get('owner')
            if claimant is not None:
                self.observed_tokens.add(claimant)

    def _observe_claimant(self):
        """The refused bound re-grant's audit: journal one
        field_claim_observed per distinct standing-owner token the
        refusal names — the seed keeps the recorded loss's claimant
        from double-recording; the doctors stage the contract's
        named defects."""
        if self.no_observe:
            return
        claimant = (self.plant.claim or {}).get('owner')
        if self.observe_dupes:
            # The per-probe defect: every refusal re-journals the
            # unseeded token instead of deduplicating on it.
            if claimant is not None \
                    and claimant not in self.observed_tokens:
                self._journal({'field_claim_observed': {
                    'point': self.plant.OUT, 'claimant': claimant}})
            return
        if claimant is None or claimant in self.observed_tokens:
            return
        self.observed_tokens.add(claimant)
        record = {'point': self.plant.OUT}
        if not self.observe_unnamed:
            record['claimant'] = 0x5E1F if self.observe_foreign \
                else claimant
        self._journal({'field_claim_observed': record})

    def _scan(self):
        """One controller scan: an active or promoting role writes
        the field — the fencing verdict superseding it — while a
        marked standby probes the bound conditional re-grant, its
        refusals journaling the observed claimant once per token."""
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
                else:
                    # The refused probe: the handover episode the
                    # observed-claimant contract journals.
                    self._observe_claimant()
            return
        if self.plant.owner_write() == 'fenced':
            self._supersede()
            return
        if self.role == 'promoting':
            self.role = 'active'
            self._journal({'role_changed': {'from': 'promoting',
                                            'to': 'active'}})


class ClaimObservationTests(unittest.TestCase):
    """The claim-observation scenario against the stubbed pair: a
    clean rig passes with identical digests and evidence — the
    induction claim fencing the owner into its attributed demotion,
    the foreign handover the demoted ex-owner meets only through its
    refused re-grant probes journaling exactly one attributed
    field_claim_observed, and the release letting the marked
    ex-owner re-seat the claim and reconverge the pair — each
    doctored defect reports the named diagnostic, and the
    unreachable, contract-predating, or seam-less rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = ObservedPlantPeer(ObservedClaimFeed.OWNER)
        self.feed = ObservedClaimFeed(
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
               'plant_owner': {'active': self.feed.OWNER,
                               'standby': 424244},
               'journal_files': self._journal_files(),
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CLAIM_OBSERVATION_SETTLE',
                             2), \
                patch.object(scenarios, 'CLAIM_OBSERVATION_POLL',
                             0.001), \
                patch.object(scenarios, 'CLAIM_OBSERVATION_DEADLINE',
                             2), \
                patch.object(scenarios, 'CLAIM_OBSERVATION_ROUNDS',
                             4), \
                patch.object(scenarios, 'CLAIM_OBSERVATION_RESTORE',
                             0.01):
            return scenarios.scenario_claim_observation(ctx)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The claim-observation leg's window: behind the
        # claim-reclaim leg whose dead-owner handover it extends.
        self.assertLess(
            order.index(scenarios.scenario_claim_reclaim),
            order.index(scenarios.scenario_claim_observation))
        self.assertIs(verify.case_function('claim-observation'),
                      scenarios.scenario_claim_observation)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('claim-observation-pass-1.json',
                     'claim-observation-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['digest'], {
                'claim': 'granted', 'seized': 'named',
                'demotion': 'in-place', 'loss': 'attributed',
                'handover': 'granted', 'seized2': 'named',
                'observed': 'deduplicated', 'hold': 'refused',
                'release': 'done', 'reclaim': 'reseated',
                'writes': 'landed', 'pair': 'reconverged'})
            # Exactly one attributed observed-claimant record per
            # pass — beside the loss and the role walk, in the
            # served journal and the durable file alike.
            observed = [entry for entry in saved['journal']['owner']
                        if 'field_claim_observed'
                        in (entry.get('event') or {})]
            self.assertEqual(len(observed), 1)
            self.assertEqual(
                (observed[0]['event']['field_claim_observed'])
                .get('claimant'),
                scenarios.CLAIM_OBSERVATION_FOREIGN)
            self.assertEqual(len(saved['file']['observed']), 1)
        report.validate_scenario(record)
        # The launch claim state and roles behind it: the owner's
        # token holds the claim with the controller joined, both
        # foreign attachments' holds released, and no role moved.
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
            'claim-observation-failed'), record['detail'])
        self.assertIn('monitor', record['detail'])
        report.validate_scenario(record)

    def test_never_demotes_reports_failed(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('never demoted', record['detail'])
        report.validate_scenario(record)

    def test_silent_loss_reports_failed(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_unattributed_loss_reports_failed(self):
        self.feed.no_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('no claimant', record['detail'])
        report.validate_scenario(record)

    def test_misattributed_loss_reports_failed(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
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
            'claim-observation-failed'), record['detail'])
        self.assertIn('refuse', record['detail'])
        report.validate_scenario(record)

    def test_peer_move_reports_failed(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('peer', record['detail'])
        report.validate_scenario(record)

    def test_foreign_write_reports_failed(self):
        self.plant.field_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('foreign write', record['detail'])
        report.validate_scenario(record)

    def test_silent_observation_reports_failed(self):
        # The contract's core gap: the demoted owner probed the
        # foreign claim across the held window and journaled no
        # observed-claimant record.
        self.feed.no_observe = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('field_claim_observed', record['detail'])
        report.validate_scenario(record)

    def test_unattributed_observation_reports_failed(self):
        self.feed.observe_unnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('no claimant', record['detail'])
        report.validate_scenario(record)

    def test_misattributed_observation_reports_failed(self):
        self.feed.observe_foreign = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('standing owner', record['detail'])
        report.validate_scenario(record)

    def test_reseeded_observation_reports_failed(self):
        # The seed the recorded loss owes: the induction claimant
        # the field_claim_lost already attributes must never
        # re-observe through the refused probes.
        self.feed.observe_reseed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('journal nothing further', record['detail'])
        report.validate_scenario(record)

    def test_duplicated_observation_reports_failed(self):
        # One record per refused probe instead of one per observed
        # token — the dedup the contract pins.
        self.feed.observe_dupes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('per observed token', record['detail'])
        report.validate_scenario(record)

    def test_file_observation_reports_failed(self):
        # The served journal carries the record but the durable
        # journal file drops it — the persisted audit incomplete.
        self.feed.file_omits_observed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('durable journal file', record['detail'])
        report.validate_scenario(record)

    def test_never_reclaims_reports_failed(self):
        self.feed.never_reclaims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('never re-seated', record['detail'])
        # The restore still ran: the finally's operator-promote
        # fallback re-seated the launch owner's claim.
        self.assertEqual((self.plant.claim or {}).get('owner'),
                         self.feed.OWNER)
        report.validate_scenario(record)

    def test_unbound_reseat_reports_failed(self):
        # A re-grant that never joins the holders: the claim
        # re-seats but the owner's own writes stay fenced — the
        # bound grant's load-bearing half missing.
        self.plant.unbound_grant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        report.validate_scenario(record)

    def test_unreconverged_pair_reports_failed(self):
        self.feed.peer_no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('reconverg', record['detail'])
        report.validate_scenario(record)

    def test_reclaim_restart_reports_failed(self):
        self.feed.dies_on_release = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-failed'), record['detail'])
        self.assertIn('restart', record['detail'])
        report.validate_scenario(record)

    def test_shared_claim_reports_nondeterministic(self):
        self.plant.shared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'writes': 'landed'}, {}, {'pass': 1}),
                       ({'writes': 'stalled'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_claim_observation_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'claim-observation-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_release_refusal_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('hand-back', record['detail'])
        report.validate_scenario(record)

    def test_claim_staging_refused_reports_inconclusive(self):
        # A plant that refuses claim_writer outright offers no
        # claim-staging lever — the induction cannot be staged, so
        # the leg reports inconclusive rather than failed.
        self.plant.claims_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('claim-staging lever', record['detail'])
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

    def test_missing_plant_ctl_reports_inconclusive(self):
        ctx = self._ctx()
        ctx['plant_ctl'] = None
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant_ctl', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal file', record['detail'])
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
        for index in range(2):
            plant = ObservedPlantPeer(ObservedClaimFeed.OWNER)
            feed = ObservedClaimFeed(
                plant, journal_files=self._journal_files(
                    'run' + str(index)))
            evidence = Path(self.tmp.name) / ('run' + str(index))
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
