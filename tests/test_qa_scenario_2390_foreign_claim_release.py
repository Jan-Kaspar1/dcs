"""The 2390_foreign_claim_release leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_foreign_claim_release, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The
shared fakes and helpers live in tests/qa_scenario_support.py; the
claim arbitration half builds on the stranded-standby leg's fakes —
the same monitor-less tool-claim window the induction stages —
extended with the doctors the release-resolution bound's own
assertions name. EXPECTED_CASES pins this module's contribution to
the suite's case coverage so a dropped case fails the discovery
check in tests/test_qa_scenario_modules.py.
"""
import unittest
import urllib.error

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2370_stranded_standby_no_resync import (
    StrandedPairFeed)
from test_qa_scenario_2380_claim_monitor_rendezvous import (
    RendezvousPlantPeer)


EXPECTED_CASES = frozenset({
    'ForeignClaimReleaseTests.test_registered',
    'ForeignClaimReleaseTests.'
    'test_clean_pair_passes_and_validates',
    'ForeignClaimReleaseTests.'
    'test_adopted_resolution_passes',
    'ForeignClaimReleaseTests.'
    'test_monitorless_declared_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_clean_window_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_second_pass_clean_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_stranded_after_release_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_monitor_drops_mid_claim_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_never_demotes_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_silent_loss_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_unattributed_loss_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_misattributed_loss_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_unjournaled_reclaim_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_no_durable_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_shared_claim_reports_nondeterministic',
    'ForeignClaimReleaseTests.'
    'test_peer_moves_reports_nondeterministic',
    'ForeignClaimReleaseTests.'
    'test_diverging_digests_report_nondeterministic',
    'ForeignClaimReleaseTests.'
    'test_silent_judge_reports_unchecked',
    'ForeignClaimReleaseTests.'
    'test_claim_staging_refused_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_release_refused_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_predating_rig_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_probe_absent_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_open_field_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_keyed_pair_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_no_active_reports_failed',
    'ForeignClaimReleaseTests.'
    'test_unsettled_pair_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_unreachable_pair_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_missing_active_endpoint_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_missing_standby_endpoint_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_missing_plant_endpoint_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_missing_owner_token_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_missing_journal_files_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_bridge_placement_reports_inconclusive',
    'ForeignClaimReleaseTests.'
    'test_two_runs_produce_identical_evidence',
})


class ReleasePlantPeer(RendezvousPlantPeer):
    """The release leg's plant half: the rendezvous rig's plant —
    probe_writer's non-mutating claim surface, the verdict's
    declared monitor carried verbatim — plus the doctor stamping a
    monitor onto a claim that declared none, the defect the
    monitor-less premise names."""

    def __init__(self, owner):
        super().__init__(owner)
        self.monitorless_named = False  # verdicts carry a monitor
                                        # on a claim that declared
                                        # none

    def _claim_for(self, conn, request):
        granted = super()._claim_for(conn, request)
        if self.monitorless_named \
                and request.get('op') == 'claim_writer' \
                and request.get('monitor') is None \
                and self.claim is not None \
                and self.claim.get('owner') == request.get('owner'):
            # The doctored defect: the stored claim names a
            # monitor the requester never declared — every verdict
            # under the monitor-less hold carries it.
            self.claim['monitor'] = '172.18.0.99:8080'
        return granted


class ReleasePairFeed(StrandedPairFeed):
    """The stranded-standby feed narrowed to the unkeyed posture
    this leg stages: no announced hint, the monitor-less tool
    claim parks the fenced ex-owner at unsynchronized, and the
    claim's release lets the loss-marked reclaim re-seat the
    field — plus the doctors the release-resolution bound's own
    assertions name: the unjournaled re-seat, the durable mirror
    going silent, the serving monitor dropping mid-hold, the
    sibling promoting out from under the leg, the second pass's
    clean window, and the successor-declared adoption resolving
    instead of the reclaim."""

    def __init__(self, plant, journal_files=None):
        super().__init__(plant, keyed=False,
                         journal_files=journal_files)
        self.no_durable = False         # records never reach
                                        # --journal-file
        self.silent_reclaim = False     # the re-seat lands
                                        # unjournaled
        self.hold_silent = False        # ctrl-a's monitor drops
                                        # mid-hold
        self.peer_moves = False         # ctrl-b promotes under the
                                        # tool claim
        self.second_pass_clean = False  # the second pass serves a
                                        # clean held verdict
        self.always_adopted = False     # the successor's declared
                                        # claim resolves the
                                        # released field

    def _journal(self, member, event):
        if self.no_durable:
            member.seq += 1
            member.journal.append({'seq': member.seq,
                                   'tick': member.tick,
                                   'event': event})
            return
        if self.silent_reclaim and member is self.a \
                and (event.get('role_changed') or {}).get('to') \
                in ('promoting', 'active'):
            member.seq += 1   # the seq burns; the record never lands
            return
        super()._journal(member, event)

    def _foreign_claims(self):
        return sum(1 for request in self.plant.requests
                   if request.get('op') == 'claim_writer'
                   and request.get('controller') is False)

    def _track(self, member):
        claim = self.plant.claim
        tool = claim is not None \
            and claim.get('controller') is False
        if tool and member is self.a \
                and (self.window_tracks
                     or (self.second_pass_clean
                         and self._foreign_claims() >= 2)):
            # The doctored defect: the fenced ex-owner's serving
            # monitor reports clean while the monitor-less claim
            # stands — the honest bound is unsynchronized for the
            # claim's standing window alone.
            member.sync = 'tracking'
            return
        if tool and member is self.b and self.peer_moves:
            if member.role != 'active' \
                    and self._take_claim(member, unconditional=True):
                member.role = 'promoting'
                self._journal(member, {'role_changed': {
                    'from': 'standby', 'to': 'promoting'}})
            return
        if self.always_adopted:
            if member is self.a:
                # The staged alternative: the ex-owner's
                # loss-marked mark never re-arms — the successor's
                # declared monitor resolves the released field.
                member.fencing_lost = False
            elif member is self.b and claim is None \
                    and self._foreign_claims() >= 1:
                if self._take_claim(member, orphan=True):
                    member.role = 'promoting'
                    self._journal(member, {'role_changed': {
                        'from': 'standby', 'to': 'promoting'}})
                return
        super()._track(member)

    def http_json(self, method, url, body=None, timeout=10):
        if self.hold_silent:
            claim = self.plant.claim
            if claim is not None \
                    and claim.get('controller') is False \
                    and url.startswith('http://ctrl-a'):
                raise urllib.error.URLError('unreachable')
        return super().http_json(method, url, body, timeout)


class ForeignClaimReleaseTests(unittest.TestCase):
    """The foreign-claim-release leg against the stubbed unkeyed
    pair: a clean rig passes with identical digests — the
    monitor-less induction claim held through the fenced ex-owner's
    unsynchronized window and its release resolving the field
    through the journaled reclaim — the successor-declared
    adoption passing as the contract's other resolution, each
    doctored defect reporting the named diagnostic, the
    unchecked-diagnostic self-check covering the leg, and the
    unreachable, contract-predating, keyed, or seam-less rig
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = ReleasePlantPeer(
            ReleasePairFeed.TOKENS['active'])
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
               'pair_token': None,
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RELEASE_SETTLE', 2), \
                patch.object(scenarios, 'RELEASE_POLL', 0.001), \
                patch.object(scenarios, 'RELEASE_ROUNDS', 4), \
                patch.object(scenarios, 'RELEASE_DEADLINE', 2):
            return scenarios.scenario_foreign_claim_release(ctx)

    def test_registered(self):
        self.assertIn(
            scenarios.scenario_foreign_claim_release,
            scenarios.SCENARIOS)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)
        self.assertEqual('tracking', self.feed.b.sync)
        self.assertEqual(
            self.feed.TOKENS['active'],
            (self.plant.claim or {}).get('owner'))
        # The induction carried the monitor-less tool-claim shape —
        # a foreign token, controller:false, no monitor field —
        # and handed it back through release_writer.
        inductions = [request for request in self.plant.requests
                      if request.get('op') == 'claim_writer'
                      and request.get('owner')
                      == scenarios.RELEASE_FOREIGN]
        self.assertTrue(inductions)
        for request in inductions:
            self.assertIs(request.get('controller'), False)
            self.assertIsNone(request.get('monitor'))
        self.assertTrue(any(
            request.get('op') == 'release_writer'
            for request in self.plant.requests))
        # The leg audited the claim through the non-mutating probe
        # surface, not a mutation.
        self.assertTrue(any(
            request.get('op') == 'probe_writer'
            for request in self.plant.requests))

    def test_adopted_resolution_passes(self):
        # The contract's other recorded path: the successor's
        # declared claim stands and the demoted peer's journaled
        # adoption tracks it — never the ex-owner's reclaim.
        self.feed.always_adopted = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('passed', record['outcome'], record)
        self.assertEqual('active', self.feed.a.role)
        self.assertEqual('standby', self.feed.b.role)

    def test_monitorless_declared_reports_failed(self):
        self.plant.monitorless_named = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
                      record.get('detail', ''))

    def test_clean_window_reports_failed(self):
        # The doctored negative the issue names: unsynchronized
        # asserted as the only-while-claimed verdict — while the
        # rig reports clean under the held monitor-less claim.
        self.feed.window_tracks = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
                      record.get('detail', ''))

    def test_second_pass_clean_reports_failed(self):
        self.feed.second_pass_clean = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
                      record.get('detail', ''))

    def test_stranded_after_release_reports_failed(self):
        # The doctored negative the issue names: the peer asserted
        # resolved while stranded past the claim's release — the
        # window never closes.
        self.feed.window_wedge = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
                      record.get('detail', ''))

    def test_monitor_drops_mid_claim_reports_failed(self):
        self.feed.hold_silent = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
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

    def test_unjournaled_reclaim_reports_failed(self):
        self.feed.silent_reclaim = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-failed',
                      record.get('detail', ''))

    def test_no_durable_reports_failed(self):
        self.feed.no_durable = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('durable', record.get('detail', ''))

    def test_shared_claim_reports_nondeterministic(self):
        self.plant.shared = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-nondeterministic',
                      record.get('detail', ''))

    def test_peer_moves_reports_nondeterministic(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-nondeterministic',
                      record.get('detail', ''))

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(
                scenarios, '_release_digest',
                side_effect=[{'restore': 'restored'},
                             {'restore': 'unrestored'}]):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what
        # they name.
        with patch.object(scenarios, '_judge_release',
                          lambda record, note: None):
            record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('failed', record['outcome'], record)
        self.assertIn('foreign-claim-release-unchecked',
                      record.get('detail', ''))

    def test_claim_staging_refused_reports_inconclusive(self):
        self.plant.claims_refused = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_release_refused_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

    def test_predating_rig_reports_inconclusive(self):
        self.plant.declared_missing = True
        record = self.run_scenario()
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)

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

    def test_keyed_pair_reports_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(pair_token='dcs-qa-pair'))
        report.validate_scenario(record)
        self.assertEqual('inconclusive', record['outcome'], record)
        self.assertIn('keyed', record.get('detail', ''))

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
