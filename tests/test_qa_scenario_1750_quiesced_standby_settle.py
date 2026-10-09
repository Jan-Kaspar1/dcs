"""The 1750_quiesced_standby_settle leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_quiesced_standby_settle, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2080_suspended_alias_audit import (
    SuspendedAliasFeed)


class QuiescedSettleFeed(SuspendedAliasFeed):
    """A stubbed three-peer rig for the quiesced-standby-settle
    leg — the suspended-alias rig's claim-arbitrating pair plus the
    quiesced-window doctors this leg's contract pins (#689): a
    tracking standby whose checkpoint pulls land inside the
    owner's pending window carries the adopted still-Accepted
    receipt untouched — no settle minted on the gated image, no
    command_settled journaled ahead of the boundary — and the
    promotion's final-sync adoption resolves it once at the
    line's own apply tick. The driven peer's frozen pulls are what
    stage the held pending receipt: only inside a POST /scan batch
    does its tracking pull run. Doctor flags stage each named
    defect the issue calls out."""

    def __init__(self, plant, journal_files=None):
        super().__init__(plant, journal_files=journal_files)
        # The doctors staging each named defect.
        self.phantom_settle = False  # the standby's quiesced scan
                                     # applies adopted pending
                                     # receipts on the gated image —
                                     # the #689 defect
        self.never_stages = False    # the driven peer's pulls lag
                                     # every pending window — the
                                     # contract's staging never
                                     # stands
        self.boundary_miss = False   # the promote's final-sync
                                     # window drops the carried
                                     # receipt — the boundary never
                                     # resolves it
        self.double_boundary = False # the boundary journals a
                                     # second, divergent settle on
                                     # the promoted peer
        self._promoting_d = False

    @staticmethod
    def _qa_admission(receipt):
        """Whether a receipt is one of the leg's staged admissions —
        the 'qa-quiesced-<pass>-<attempt>' actor, never the contract
        probe's."""
        actor = str(receipt.get('actor') or '')
        parts = actor.split('-')
        return actor.startswith('qa-quiesced-') and len(parts) > 2 \
            and parts[2].isdigit()

    def _pull(self, peer):
        if self.never_stages and peer.name == 'd' \
                and any('accepted' in receipt['outcome']
                        and self._qa_admission(receipt)
                        for receipt in self.a.receipts):
            # The lagged pull: the checkpoint predates the pending
            # admission — the still-Accepted adoption never stages.
            return
        super()._pull(peer)
        if self.phantom_settle and peer.role == 'standby':
            for receipt in peer.receipts:
                if 'accepted' in receipt['outcome'] \
                        and self._qa_admission(receipt):
                    # The defect the fix exists to prevent: the
                    # quiesced scan settles the adopted pending
                    # receipt on the gated image — a local mint at
                    # the standby's own tick, journaled ahead of
                    # the line's boundary.
                    write = receipt['command']['write_value']
                    receipt['outcome'] = {'applied': {
                        'tick': peer.tick}}
                    peer.image[write['point']] = \
                        write['value']['bool']
                    self._settle(peer, receipt)

    def _adopt(self, peer, src):
        if peer.name == 'd' and self._promoting_d:
            if self.boundary_miss:
                # The final-sync's adopted window drops the
                # carried receipt — the promoted peer's log never
                # held it, so the boundary never resolves it.
                peer.receipts = [
                    receipt for receipt in peer.receipts
                    if not self._qa_admission(receipt)]
                return
            super()._adopt(peer, src)
            if self.double_boundary:
                carried = [receipt for receipt in peer.receipts
                           if self._qa_admission(receipt)]
                if carried:
                    extra = dict(carried[-1])
                    extra['outcome'] = {'rejected': {'reason': {
                        'superseded': {
                            'point': extra['command']
                            ['write_value']['point']}}}}
                    # A second, divergent settle beside the adopted
                    # record — one admission, two verdicts.
                    self._mark(peer, {'command_settled': {
                        'receipt': extra}})
            return
        super()._adopt(peer, src)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        route = '/' + url.split('/', 3)[3]
        if (method, route.partition('?')[0]) \
                == ('POST', '/promote') and peer.name == 'd':
            self._promoting_d = True
            try:
                return super().http_json(method, url, body, timeout)
            finally:
                self._promoting_d = False
        return super().http_json(method, url, body, timeout)


class QuiescedSettleTests(unittest.TestCase):
    """The quiesced-standby-settle leg against the stubbed
    three-peer rig over a real claim-arbitrating plant: a clean rig
    passes with identical digests and evidence — the adopted
    receipt held pending on the gated image, the carried command
    resolved once per peer at the line's apply tick — each
    doctored defect reports the named diagnostic, and an
    unreachable, unconverged, seam-less, unstaged, or pre-contract
    run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_dir = Path(self.tmp.name) / 'journals'
        self.journal_dir.mkdir()
        self.plant = ClaimPlantPeer()
        self.feed = QuiescedSettleFeed(
            self.plant, journal_files=self._journal_files())

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _journal_files(self):
        return {key: str(self.journal_dir / (key + '.jsonl'))
                for key in ('active', 'standby', 'driven')}

    def _ctx(self, **overrides):
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'journal_files': self._journal_files(),
               'evidence_dir': str(self.evidence),
               'start_driven': self.feed.start_driven,
               'stop_driven': self.feed.stop_driven}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'QUIESCED_SETTLE', 2.0), \
                patch.object(scenarios, 'QUIESCED_AUDIT', 1.0), \
                patch.object(scenarios, 'QUIESCED_POLL', 0.001), \
                patch.object(scenarios, 'QUIESCED_EDGE', 0.05):
            return scenarios.scenario_quiesced_standby_settle(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The quiesced-settle leg's window: behind the standby-loss
        # case, inside the launch-layout window the settle family
        # and the tune case's a->b switch share.
        self.assertLess(
            order.index(scenarios.scenario_standby_loss),
            order.index(
                scenarios.scenario_quiesced_standby_settle))
        self.assertLess(
            order.index(
                scenarios.scenario_quiesced_standby_settle),
            order.index(
                scenarios.scenario_demote_settle_uniqueness))
        self.assertIs(
            verify.case_function('quiesced-standby-settle'),
            scenarios.scenario_quiesced_standby_settle)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('quiesced-settle-signals.json',
                     'quiesced-settle-pass-1.json',
                     'quiesced-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('quiesced-settle-pass-1.json',
                               'quiesced-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'adopted': 'pending', 'gated': 'held',
             'boundary': 'single', 'roles': 'restored'})
        # The quiesced window: the adopted receipt stood pending —
        # no terminal verdict, no journaled settle on the standby,
        # the gated image at baseline.
        held = passes[0]['held']
        self.assertEqual(
            scenarios._outcome_key(held['receipt']), 'accepted')
        self.assertEqual(held['journaled'], [])
        self.assertEqual(held['durable'], [])
        self.assertFalse(held['image'])
        # The boundary: exactly one applied settle per peer,
        # carrying the line's own apply tick verbatim.
        line_tick = passes[0]['line']['outcome']['applied']['tick']
        window = passes[0]['window']
        for name in ('active', 'standby', 'driven'):
            journaled = window['journaled'][name]
            self.assertEqual(len(journaled), 1, name)
            self.assertEqual(journaled[0]['outcome'],
                             {'applied': {'tick': line_tick}}, name)
            self.assertEqual(
                [scenarios._outcome_key(receipt)
                 for receipt in window['logged'][name]],
                ['applied'], name)
        self.assertEqual(
            passes[0]['durable'],
            {name: ['applied']
             for name in ('active', 'standby', 'driven')})
        report.validate_scenario(record)

    def test_phantom_settle_reports_failed(self):
        # The defect #689 fixed: the standby's quiesced scan
        # applied the adopted pending receipt on the gated image —
        # a phantom settle minted and journaled ahead of the line's
        # boundary.
        self.feed.phantom_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-failed'), record['detail'])
        report.validate_scenario(record)

    def test_boundary_miss_reports_failed(self):
        # The promoted standby's final-sync window dropped the
        # carried receipt — the boundary never resolved it.
        self.feed.boundary_miss = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-failed'), record['detail'])
        self.assertIn('exactly once', record['detail'])
        report.validate_scenario(record)

    def test_double_boundary_reports_nondeterministic(self):
        self.feed.double_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-nondeterministic'), record['detail'])
        self.assertIn('exactly once', record['detail'])
        report.validate_scenario(record)

    def test_never_stages_reports_inconclusive(self):
        self.feed.never_stages = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pending window', record['detail'])
        report.validate_scenario(record)

    def test_driven_promote_refused_reports_failed(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-failed'), record['detail'])
        self.assertIn('/promote', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.restore_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-failed'), record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_predates_contract_reports_inconclusive(self):
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_missing_driven_action_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(start_driven=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_driven', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(self._ctx(journal_files={}))
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

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'unrestored'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_quiesced_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            feed = QuiescedSettleFeed(
                plant, journal_files=self._journal_files())
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

    def test_silent_audit_reports_unchecked(self):
        # The self-check leg: the pending-window audit monkey-
        # patched silent can no longer name the phantom settle —
        # the leg reports it unchecked.
        with patch.object(scenarios, '_quiesced_pending',
                          lambda *a: 'held'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'quiesced-settle-unchecked'), record['detail'])
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
