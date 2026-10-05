"""The 1960_repromote_suspended_settle leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_repromote_suspended_settle, split per the
one-module-per-leg convention (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_1900_demote_carry_settle import (
    DemoteCarryFeed, DemoteCarryPeer)


EXPECTED_CASES = frozenset({
    'RepromoteSuspendedSettleTests.test_registered',
    'RepromoteSuspendedSettleTests.test_clean_pair_passes_and_validates',
    'RepromoteSuspendedSettleTests.test_parked_receipt_reports_failed',
    'RepromoteSuspendedSettleTests.test_double_settle_reports_nondeterministic',
    'RepromoteSuspendedSettleTests.test_settle_without_apply_reports_failed',
    'RepromoteSuspendedSettleTests.test_divergent_verdict_reports_nondeterministic',
    'RepromoteSuspendedSettleTests.test_owner_never_demotes_reports_failed',
    'RepromoteSuspendedSettleTests.test_never_reconverges_reports_failed',
    'RepromoteSuspendedSettleTests.test_refused_repromote_reports_failed',
    'RepromoteSuspendedSettleTests.test_refused_preempt_reports_failed',
    'RepromoteSuspendedSettleTests.test_missed_window_restages_and_passes',
    'RepromoteSuspendedSettleTests.test_no_active_reports_failed',
    'RepromoteSuspendedSettleTests.test_unconverged_pair_reports_inconclusive',
    'RepromoteSuspendedSettleTests.test_unreachable_peer_reports_inconclusive',
    'RepromoteSuspendedSettleTests.test_diverging_digests_report_nondeterministic',
    'RepromoteSuspendedSettleTests.test_silent_judge_reports_unchecked',
    'RepromoteSuspendedSettleTests.test_two_runs_produce_identical_evidence',
})


class RepromoteFeed(DemoteCarryFeed):
    """A stubbed pair for the repromote-suspended-settle leg,
    arbitrating the field claim through a real ClaimPlantPeer:
    ctrl-a owns the field under TOKEN_A at launch, ctrl-b tracks.
    Every endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans. The
    sibling's promote claims the field mid-run — its final-sync
    adoption meets only the pre-admission document, so its served
    window can never cover the submission the leg lands inside the
    incumbent's fenced-reporting window; the detection scan demotes
    the holder in place with the receipt suspended Accepted; the
    demoted peer's pulls adopt the promoted peer's stream — the
    unreached tail stays suspended — and the reconverged holder's
    re-promote re-queues the tail at its own boundary, settling it
    applied once while the demoted sibling journals the adopted
    record. Doctor flags stage each named defect the issue calls
    out. An optional `journal_files` mapping mirrors every
    journaled event into real --journal-file records for the leg's
    durable audit."""

    def __init__(self, plant, journal_files=None):
        super().__init__(plant)
        # The leg's durable audit reads each endpoint's
        # --journal-file: peer 'a' serves ctx key 'active', peer 'b'
        # ctx key 'standby'.
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
        # The doctors staging each named defect.
        self.park_suspended = False    # the re-promoted holder never
                                       # re-queues the tail — parked
        self.double_settle = False     # the settle journals twice
        self.settle_no_apply = False   # the settle journals without
                                       # the write landing
        self.divergent = False         # the adopted record mints the
                                       # wrong verdict beside the
                                       # applied settle
        self.no_reconverge = False     # the demoted holder never
                                       # reaches a promotable verdict
        self.repromote_refused = False # the reconverged holder's
                                       # promote is refused once
        self.miss_first_window = False # the first staged submission
                                       # lands past the detection
                                       # scan — the restage path
        self._miss_armed = False
        self._doubled = False
        self._diverged = False

    def _adopt(self, peer, other):
        """The tracking pull: the sync posture follows the source's
        field-ownership stamp — active or the still-settling
        promoting state `owns_field` covers — while a source whose
        submission high-water sits behind this run's cannot regress
        its log: the suspended tail past the adopted window stays
        suspended."""
        if not self.no_tracking \
                and not (self.no_reconverge and peer.name == 'a'):
            peer.sync = 'tracking' \
                if other.role in ('active', 'promoting') \
                else 'orphaned'
        if other.attempts < peer.attempts:
            return
        suspended = [receipt for receipt in peer.receipts
                     if 'accepted' in receipt['outcome']]
        peer.receipts = copy.deepcopy(other.receipts)
        peer.attempts = other.attempts
        peer.image = dict(other.image)
        for receipt in suspended:
            if any(self._key(carried) == self._key(receipt)
                   for carried in peer.receipts):
                continue            # carried — the adopted copy
                                    # journals the line's verdict
            if receipt['index'] < peer.attempts:
                self._settle(peer, self._superseded(receipt))
            else:
                peer.receipts.append(receipt)
        self._observe(peer)
        if self.divergent and peer.name == 'b' \
                and not self._diverged:
            raced = [receipt for receipt in peer.receipts
                     if 'applied' in receipt['outcome']
                     and receipt.get('reason')
                     == 'repromote-suspended-settle']
            if raced:
                self._diverged = True
                # The defect: the adopted record mints a second,
                # divergent verdict for the same admission.
                self._mark(peer, {'command_settled': {
                    'receipt': self._superseded(raced[0])}})

    def _apply(self, peer):
        """The field-owning scan boundary: pending admissions apply
        and settle — the re-promoted holder's requeue lands here."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            write = receipt['command']['write_value']
            if receipt.get('reason') == 'repromote-suspended-settle':
                if self.park_suspended:
                    continue        # the defect: the tail never
                                    # re-queued, parked Accepted
                receipt['outcome'] = {'applied': {'tick': peer.tick}}
                self._settle(peer, receipt)
                if not self.settle_no_apply:
                    peer.image[write['point']] = \
                        write['value']['bool']
                if self.double_settle and not self._doubled:
                    self._doubled = True
                    self._mark(peer, {'command_settled': {
                        'receipt': dict(receipt)}})
            else:
                receipt['outcome'] = {'applied': {'tick': peer.tick}}
                peer.image[write['point']] = write['value']['bool']
                self._settle(peer, receipt)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/command'):
            if self._miss_armed and peer.name == 'a':
                # The window closed first: the detection scan
                # demotes the holder ahead of the submission —
                # the fenced-but-still-reporting race's losing side.
                self._miss_armed = False
                self._advance(peer)
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'reason': (body or {}).get('reason'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if peer.role != 'active':
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            # The wire answer is the admission's snapshot — later
            # scans settling the logged receipt do not rewrite it.
            return 200, copy.deepcopy(receipt)
        self._advance(
            peer,
            adopt=not (method == 'POST' and route == '/promote'))
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('GET', '/role'):
            role = 'standby' if peer.name == 'a' and self.no_active \
                else peer.role
            report = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                report['sync'] = {'unsynchronized': {}} \
                    if peer.sync == 'unsynchronized' \
                    else {peer.sync: {'aligned': peer.tick}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': list(self.SIGNALS),
                         'components': []}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                {'point': point, 'direction': 'in',
                 'sample': {'value': {'bool': peer.image.get(point,
                                                           False)},
                            'quality': 'good', 'tick': peer.tick}}
                for point in self.POINTS]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'receipts': list(peer.receipts),
                         'command_admission': {
                             'attempts': peer.attempts}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [dict(entry) for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if self.repromote_refused and peer.name == 'a':
                self.repromote_refused = False
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            if peer.sync not in ('tracking', 'orphaned') \
                    or self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync transfer: the
            # source's log lands — but suspended receipts the
            # source's window never covered stay on the log, the
            # unreached tail the re-promoted holder re-queues at its
            # own boundary — and the claim preempts the incumbent.
            suspended = [receipt for receipt in peer.receipts
                         if 'accepted' in receipt['outcome']
                         and receipt['index'] >= other.attempts
                         and not any(
                             self._key(carried) == self._key(receipt)
                             for carried in other.receipts)]
            peer.receipts = copy.deepcopy(other.receipts) + [
                copy.deepcopy(receipt) for receipt in suspended]
            peer.attempts = max(peer.attempts, other.attempts)
            peer.image = dict(other.image)
            self._wire_claim(peer.token)
            if self.miss_first_window and peer.name == 'b':
                self.miss_first_window = False
                self._miss_armed = True
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class RepromoteSuspendedSettleTests(unittest.TestCase):
    """The re-promote suspended-settle leg against the stubbed pair
    over a real claim-arbitrating plant: a clean rig passes with
    identical digests and evidence — the restored suspended receipt
    re-queued and settled applied exactly once on each peer — each
    doctored defect reports the named diagnostic, and an
    unreachable or unconverged rig reports inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.plant = ClaimPlantPeer()
        self.feed = RepromoteFeed(self.plant, self.journal_files)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'journal_files': self.journal_files,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REPROMOTE_SETTLE', 2.0), \
                patch.object(scenarios, 'REPROMOTE_AUDIT', 2.0), \
                patch.object(scenarios, 'REPROMOTE_POLL', 0.001), \
                patch.object(scenarios, 'REPROMOTE_WATCH', 0.001):
            return scenarios.scenario_repromote_suspended_settle(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The same restored window as the demote-carry leg — the
        # gossip-window, settled-receipt-arbitration, and
        # command-across-promotion legs follow it, ahead of the
        # peer-announce case and the tune case's a->b switch.
        self.assertEqual(
            order.index(scenarios.scenario_demote_carry_settle) + 1,
            order.index(scenarios.scenario_repromote_suspended_settle))
        self.assertEqual(
            order.index(
                scenarios.scenario_repromote_suspended_settle) + 1,
            order.index(scenarios.scenario_gossip_repromote_settle))
        self.assertEqual(
            order.index(scenarios.scenario_gossip_repromote_settle) + 1,
            order.index(scenarios.scenario_settled_receipt_arbitration))
        self.assertEqual(
            order.index(scenarios.scenario_settled_receipt_arbitration)
            + 1,
            order.index(scenarios.scenario_adopted_receipt_regression))
        self.assertEqual(
            order.index(
                scenarios.scenario_command_across_promotion) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('repromote-suspended-settle'),
            scenarios.scenario_repromote_suspended_settle)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('repromote-settle-signals.json',
                     'repromote-settle-pass-1.json',
                     'repromote-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('repromote-settle-pass-1.json',
                               'repromote-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest']['settle'], 'single')
        self.assertEqual(passes[0]['digest']['window'], 'uncovered')
        self.assertEqual(passes[0]['digest']['roles'], 'restored')
        # The restored admission settled applied exactly once on
        # each peer — the re-promoted holder's own boundary plus
        # the adopted record on the demoted sibling — through the
        # serving monitors and the durable journals alike.
        for passed in passes:
            window = passed['window']
            for name in ('active', 'standby'):
                self.assertEqual(
                    ['applied'],
                    [scenarios._outcome_key(receipt)
                     for receipt in window['journaled'][name]],
                    window)
                self.assertEqual(
                    ['applied'],
                    [scenarios._outcome_key(receipt)
                     for receipt in window['logged'][name]],
                    window)
                self.assertEqual(1, len(passed['durable'][name]),
                                 passed['durable'])
        # The preempting promote and the re-promote each claimed
        # the field through the real wire protocol.
        self.assertGreaterEqual(
            self.plant.requests.count('claim_writer'), 4)
        report.validate_scenario(record)

    def test_parked_receipt_reports_failed(self):
        self.feed.park_suspended = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        self.assertIn('parked Accepted', record['detail'])
        report.validate_scenario(record)

    def test_double_settle_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_settle_without_apply_reports_failed(self):
        self.feed.settle_no_apply = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        self.assertIn('application', record['detail'])
        report.validate_scenario(record)

    def test_divergent_verdict_reports_nondeterministic(self):
        self.feed.divergent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_owner_never_demotes_reports_failed(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        self.assertIn('never demoted', record['detail'])
        report.validate_scenario(record)

    def test_never_reconverges_reports_failed(self):
        self.feed.no_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        self.assertIn('never reconverged', record['detail'])
        report.validate_scenario(record)

    def test_refused_repromote_reports_failed(self):
        self.feed.repromote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_refused_preempt_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-failed'), record['detail'])
        report.validate_scenario(record)

    def test_missed_window_restages_and_passes(self):
        self.feed.miss_first_window = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'repromote-settle-pass-1.json')
            .read_text())
        self.assertGreaterEqual(len(passed['attempts']), 2)
        self.assertEqual(passed['attempts'][0]['missed'],
                         'window closed')
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_peer_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'settle': 'single'}, {}, {'pass': 1}),
                       ({'settle': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_repromote_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_repromote_judge',
                          lambda *a, **k: 'single'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'repromote-suspended-settle-unchecked'),
            record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            journal_files = {
                'active': str(Path(self.tmp.name)
                              / ('a-' + str(len(runs)) + '.jsonl')),
                'standby': str(Path(self.tmp.name)
                               / ('b-' + str(len(runs)) + '.jsonl'))}
            feed = RepromoteFeed(plant, journal_files)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            ctx = {'active': 'http://ctrl-a:1',
                   'standby': 'http://ctrl-b:2',
                   'plant': plant.address,
                   'journal_files': journal_files,
                   'evidence_dir': str(evidence)}
            try:
                record = self.run_scenario(ctx=ctx, feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
