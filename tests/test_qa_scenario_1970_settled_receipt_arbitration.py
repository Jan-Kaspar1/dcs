"""The 1970_settled_receipt_arbitration leg's scenario unit coverage —
the feed fake and TestCase classes for
scenario_settled_receipt_arbitration, split per the one-module-per-leg
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The stubbed pair models the #690 finding's reproduction and its fix: a
receipted batch raced against the promotion whose boundary pull lands
inside the field owner's pending window leaves the carried admission
settled at one submission index to two different terminal verdicts —
one per line's own boundary — and the dual-standby adoption window
that follows converges both served receipts on the arbitrated (earlier)
verdict while each durable journal keeps the one settle it recorded.
The doctor flags stage the pre-fix shapes the leg must name.
"""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_lane import report, scenarios, verify
from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SettledReceiptArbitrationTests.test_registered',
    'SettledReceiptArbitrationTests.test_clean_pair_passes_and_validates',
    'SettledReceiptArbitrationTests.test_oscillating_receipts_report_nondeterministic',
    'SettledReceiptArbitrationTests.test_doubled_journal_reports_nondeterministic',
    'SettledReceiptArbitrationTests.test_never_staged_reports_failed',
    'SettledReceiptArbitrationTests.test_refused_promote_reports_failed',
    'SettledReceiptArbitrationTests.test_never_reconverges_reports_failed',
    'SettledReceiptArbitrationTests.test_unsettled_window_reports_failed',
    'SettledReceiptArbitrationTests.test_no_active_reports_failed',
    'SettledReceiptArbitrationTests.test_unconverged_pair_reports_inconclusive',
    'SettledReceiptArbitrationTests.test_unreachable_peer_reports_inconclusive',
    'SettledReceiptArbitrationTests.test_missing_journals_report_inconclusive',
    'SettledReceiptArbitrationTests.test_single_endpoint_reports_inconclusive',
    'SettledReceiptArbitrationTests.test_diverging_digests_report_nondeterministic',
    'SettledReceiptArbitrationTests.test_silent_judge_reports_unchecked',
    'SettledReceiptArbitrationTests.test_two_runs_produce_identical_evidence',
})


class ArbitrationPeer:
    """One endpoint of the arbitration pair: role, tracking posture,
    its receipt log with an admission's absolute index, the settlements
    its journal already recorded, and the served image."""

    def __init__(self, name):
        self.name = name
        self.role = 'active'
        self.tracking = False
        self.orphaned = False   # following a source that owns no field
        self.tick = 0
        self.attempts = 0
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        self.image = {}
        self.journaled = {}   # index -> the verdict it recorded


class ArbitrationFeed:
    """A stubbed pair for the settled-receipt-arbitration leg: ctrl-a
    owns the field at launch, ctrl-b tracks it.

    Every endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans, and the two
    switch requests, whose boundaries act at the request. The raced
    promote's boundary pull copies the owner's still-pending admissions
    onto the promoting peer (the carry), each peer then settles the
    carried admission at its own tick, and the field's arbitration
    fences the incumbent in place — the two lines hold one admission
    settled at one index to two verdicts. In the dual-standby window
    each peer's tracking pull adopts the other's log: the fix keeps the
    arbitrated winner, the earlier apply tick, and journals no second
    settle for the admission. An optional `journal_files` mapping (ctx
    keys 'active'/'standby' to paths) mirrors every journaled event
    into real --journal-file records for the leg's durable audit.

    Doctor flags stage each named defect: `oscillate` (adoption
    answers last-pull-wins — the #690 defect), `double_settle` (the
    loser's adoption journals the arbitrated verdict a second time),
    `never_stage` (the promote's boundary pull never carries the
    pending admissions), `promote_refused`, `no_reconverge` (the
    promoted peer never settles active), `park_served` (neither peer
    ever serves a terminal verdict), `no_active`, `no_tracking`, and
    `unreachable`.
    """

    POINTS = (302, 300, 301, 332, 333, 334)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]

    def __init__(self, journal_files=None):
        self.a = ArbitrationPeer('a')
        self.a.role = 'active'
        self.b = ArbitrationPeer('b')
        self.b.role = 'standby'
        self.b.tracking = True
        self.journal_paths = {}
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
        # The doctors staging each named defect.
        self.oscillate = False      # adoption answers last-pull-wins
        self.double_settle = False  # the loser journals the winner too
        self.never_stage = False    # the boundary pull carries nothing
        self.promote_refused = False
        self.no_reconverge = False  # the promoted peer never settles
        self.park_served = False    # no terminal verdict is ever served
        self.no_active = False
        self.no_tracking = False
        self.unreachable = False
        self.hold = set()      # peers whose scan boundary is held —
                               # the pending window the raced
                               # promotion races
        self._served_parked = set()

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _other(self, peer):
        return self.b if peer.name == 'a' else self.a

    @staticmethod
    def _index(receipt):
        return receipt.get('index')

    def _mark(self, peer, event):
        peer.journal.append({'seq': peer.next_seq, 'tick': peer.tick,
                             'event': event})
        peer.next_seq += 1
        path = self.journal_paths.get(peer.name)
        if path is not None:
            with path.open('a') as stream:
                stream.write(json.dumps(
                    {'entry': {'seq': peer.journal[-1]['seq'],
                               'tick': peer.tick,
                               'event': event}}) + '\n')

    def _settle(self, peer, receipt):
        """One settlement recorded at its own boundary — the recorder's
        per-admission mark, so a repeated drain never re-journals."""
        index = self._index(receipt)
        if peer.journaled.get(index) is not None:
            return
        peer.journaled[index] = receipt
        self._mark(peer, {'command_settled': {'receipt':
                                              copy.deepcopy(receipt)}})

    def _apply(self, peer):
        """The field-owning boundary: pending admissions apply and
        settle at this peer's own tick."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            write = receipt['command']['write_value']
            peer.image[write['point']] = write['value']['bool']
            self._settle(peer, receipt)

    @staticmethod
    def _tick_of(receipt):
        applied = (receipt.get('outcome') or {}).get('applied') or {}
        tick = applied.get('tick')
        return tick if isinstance(tick, int) else None

    def _adopt(self, peer, other):
        """The tracked pull: the peer's log adopts the source's, except
        where an index both lines settled to *different* terminal
        verdicts — the arbitrated winner, the earlier apply tick, holds
        the index, and the admission is not settled a second time. The
        pre-fix shape takes the source's record wholesale, which is
        what handed the index back and forth on every mutual
        adoption; the `double_settle` doctor keeps the recorder half of
        the finding, where every outcome change under the pull was
        journaled again."""
        mine = {self._index(receipt): copy.deepcopy(receipt)
                for receipt in peer.receipts}
        peer.receipts = copy.deepcopy(other.receipts)
        peer.attempts = other.attempts
        peer.image = dict(other.image)
        for position, served in enumerate(peer.receipts):
            if 'accepted' in served['outcome']:
                continue
            index = self._index(served)
            held = mine.get(index)
            if held is None or 'accepted' in held['outcome'] \
                    or held == served:
                peer.receipts[position] = copy.deepcopy(served)
                self._settle(peer, served)
                continue
            ours, theirs = self._tick_of(held), self._tick_of(served)
            winner = served
            if not self.oscillate and ours is not None \
                    and (theirs is None or ours < theirs):
                winner = held
            if self.double_settle:
                # The flood half of the finding: the served verdict
                # moved under this pull, and the recorder treated the
                # move as a second settle for the admission this peer
                # had already settled.
                self._mark(peer, {'command_settled': {
                    'receipt': copy.deepcopy(winner)}})
            peer.receipts[position] = copy.deepcopy(winner)
            self._settle(peer, winner)
        if self.oscillate:
            # The recorder half of the finding: every differing outcome
            # the adoption brought in was a new settle, so each flip
            # appended another command_settled line.
            for served in peer.receipts:
                if 'accepted' not in served['outcome'] \
                        and self._index(served) not in peer.journaled:
                    continue
                if 'accepted' in served['outcome']:
                    continue
                self._mark(peer, {'command_settled': {
                    'receipt': copy.deepcopy(served)}})

    def journaled_verdict(self, peer, index):
        """The receipt this peer journaled at `index`, or None."""
        for entry in peer.journal:
            receipt = entry['event'].get('command_settled', {}) \
                .get('receipt')
            if receipt and self._index(receipt) == index:
                return receipt
        return None

    def _advance(self, peer, adopt=True):
        """One completed scan: pending role transitions settle, the
        holder applies its pending admissions, and a tracking standby
        pulls its source's log. A held peer's boundary is the pending
        window the raced promotion races — its scan does not run while
        the window stands."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            if not self.no_reconverge:
                peer.role = 'active'
                self._mark(peer, {'role_changed': {'from': 'promoting',
                                                   'to': 'active'}})
        if peer.role == 'active' and peer.name not in self.hold:
            self._apply(peer)
        elif peer.role == 'standby' and peer.tracking and adopt \
                and not self.no_tracking:
            source = self._other(peer)
            self._adopt(peer, source)
            # A line following a source that owns no field owes the
            # ownerless `orphaned` verdict; behind a field owner it
            # reports `tracking`.
            peer.orphaned = source.role != 'active'
        if self.park_served:
            for receipt in peer.receipts:
                if 'accepted' not in receipt['outcome']:
                    self._served_parked.add((peer.name,
                                             self._index(receipt)))

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if not peer.tracking or self.promote_refused \
                    or self.no_tracking:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync pull: the owner's
            # whole log, including the admissions still pending inside
            # its boundary window, rides onto the promoting peer.
            carried = [copy.deepcopy(receipt) for receipt
                       in other.receipts
                       if 'accepted' in receipt['outcome']]
            if self.never_stage:
                # The owner's boundary closed the window before the
                # pull landed: nothing pending rides across, so the
                # pair never holds a contradiction.
                self.hold.discard(other.name)
                self._apply(other)
                carried = []
            peer.receipts = copy.deepcopy(other.receipts) + carried
            peer.attempts = other.attempts
            peer.image = dict(other.image)
            peer.role = 'promoting'
            self._advance(peer, adopt=False)
            if self.no_reconverge:
                return 200, {'role': 'promoting'}
            # The field's arbitration took the claim: the incumbent's
            # next scan runs before it observes the loss — its own
            # boundary settles the admission it still holds — and the
            # fencing then demotes it in place behind the new holder.
            self.hold.discard(other.name)
            self._apply(other)
            other.role = 'demoting'
            self._advance(other, adopt=False)
            other.tracking = True
            other.orphaned = False
            return 200, {'role': 'active'}
        self._advance(
            peer, adopt=not (method == 'POST' and route == '/promote'))
        if (method, route) == ('GET', '/role'):
            role = 'standby' if peer.name == 'a' and self.no_active \
                else peer.role
            report_body = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                if not peer.tracking or self.no_tracking:
                    report_body['sync'] = {'unsynchronized': {}}
                elif peer.orphaned:
                    report_body['sync'] = {'orphaned': {
                        'reason': 'the tracked line owns no field'}}
                else:
                    report_body['sync'] = {'tracking': {'aligned':
                                                       peer.tick}}
            return 200, report_body
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
        if (method, route) == ('GET', '/receipts'):
            receipts = copy.deepcopy(peer.receipts)
            if self.park_served:
                for receipt in receipts:
                    if (peer.name, self._index(receipt)) \
                            in self._served_parked:
                        receipt['outcome'] = {'accepted': {
                            'apply_tick': peer.tick}}
            return 200, receipts
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'receipts': copy.deepcopy(peer.receipts),
                         'command_admission': {
                             'attempts': peer.attempts,
                             'full_rejections': 0,
                             'high_water': 0}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [copy.deepcopy(entry) for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
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
                # The admission opens the field owner's pending window:
                # its boundary holds the receipt until a switch or the
                # window's own scan lands, which is what the raced
                # promotion's pull races.
                self.hold.add(peer.name)
            return 200, copy.deepcopy(receipt)
        raise AssertionError('unexpected request %s %s' % (method, url))


class SettledReceiptArbitrationTests(unittest.TestCase):
    """The settled-receipt-arbitration leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — the
    contradictory pair staged, both served logs converged on the
    arbitrated verdict, one durable settle per admission per peer, the
    pair back on its launch roles — each doctored defect reports its
    named diagnostic, and an unreachable, unconverged, or
    unstageable rig reports inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.feed = ArbitrationFeed(self.journal_files)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'journal_files': self.journal_files,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ARBITRATION_SETTLE', 2.0), \
                patch.object(scenarios, 'ARBITRATION_WINDOW', 3), \
                patch.object(scenarios, 'ARBITRATION_AUDIT', 2.0), \
                patch.object(scenarios, 'ARBITRATION_POLL', 0.001):
            return scenarios.scenario_settled_receipt_arbitration(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The same switch window the re-promote-suspended-settle and
        # gossip-repromote-settle legs use, ahead of the peer-announce
        # and the tune case's a->b switch.
        self.assertEqual(
            order.index(scenarios.scenario_gossip_repromote_settle)
            + 1,
            order.index(
                scenarios.scenario_settled_receipt_arbitration))
        self.assertEqual(
            order.index(scenarios.scenario_settled_receipt_arbitration)
            + 1,
            order.index(scenarios.scenario_command_across_promotion))
        self.assertEqual(
            order.index(scenarios.scenario_command_across_promotion)
            + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('settled-receipt-arbitration'),
            scenarios.scenario_settled_receipt_arbitration)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('arbitration-signals.json',
                     'arbitration-pass-1.json',
                     'arbitration-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('arbitration-pass-1.json',
                               'arbitration-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        for passed in passes:
            self.assertEqual(passed['digest']['staged'],
                             'contradictory')
            self.assertEqual(passed['digest']['converged'],
                             'arbitrated')
            self.assertEqual(passed['digest']['settles'],
                             'one-per-admission')
            self.assertEqual(passed['digest']['roles'], 'restored')
            # The contradiction really was staged: both lines
            # journaled a terminal verdict for the same admission, and
            # the two verdicts differ.
            self.assertTrue(passed['staged'], passed)
            for entry in passed['staged']:
                self.assertEqual(len(entry['verdicts']), 2, entry)
                self.assertEqual(len(set(entry['verdicts'].values())),
                                 2, entry)
            # One settle per admission per peer, durably.
            for counts in passed['durable'].values():
                for count in counts.values():
                    self.assertEqual(count, 1, passed['durable'])
            # The served verdicts agreed across every poll.
            for actor, verdict in passed['verdicts'].items():
                self.assertEqual(verdict, 'arbitrated', actor)
        report.validate_scenario(record)

    def test_oscillating_receipts_report_nondeterministic(self):
        # The #690 defect: adoption answers last-pull-wins, so the
        # served receipts keep moving under the poll.
        self.feed.oscillate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_doubled_journal_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_never_staged_reports_failed(self):
        # The promotion boundary's pull never landed inside the
        # owner's pending window: the rig never presented the
        # contradictory pair the contract arbitrates.
        self.feed.never_stage = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-failed'), record['detail'])
        self.assertIn('pending window', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-failed'), record['detail'])
        report.validate_scenario(record)

    def test_never_reconverges_reports_failed(self):
        self.feed.no_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-failed'), record['detail'])
        report.validate_scenario(record)

    def test_unsettled_window_reports_failed(self):
        # Neither peer ever serves a terminal verdict: the adoption
        # window carries nothing to arbitrate.
        self.feed.park_served = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-failed'), record['detail'])
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

    def test_missing_journals_report_inconclusive(self):
        ctx = self._ctx()
        ctx['journal_files'] = {}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('durable journal', record['detail'])
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(ctx={'active': 'http://ctrl-a:1',
                                        'evidence_dir':
                                            str(self.evidence)})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('only one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'converged': 'arbitrated'}, {}, {'pass': 1}),
                       ({'converged': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_arbitration_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_settled_arbitration_judge',
                          lambda *a, **k: 'arbitrated'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'settled-arbitration-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for number in range(2):
            journal_files = {
                'active': str(Path(self.tmp.name)
                              / ('run-a-' + str(number) + '.jsonl')),
                'standby': str(Path(self.tmp.name)
                               / ('run-b-' + str(number) + '.jsonl'))}
            feed = ArbitrationFeed(journal_files)
            evidence = Path(self.tmp.name) / ('run' + str(number))
            evidence.mkdir()
            ctx = {'active': 'http://ctrl-a:1',
                   'standby': 'http://ctrl-b:2',
                   'journal_files': journal_files,
                   'evidence_dir': str(evidence)}
            record = self.run_scenario(ctx=ctx, feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
