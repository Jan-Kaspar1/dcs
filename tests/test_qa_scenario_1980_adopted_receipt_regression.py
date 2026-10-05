"""The 1980_adopted_receipt_regression leg's scenario unit coverage —
the feed fake and TestCase classes for
scenario_adopted_receipt_regression, split per the one-module-per-leg
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The stubbed pair models the #709 finding's reproduction and its fix: a
receipted command is submitted and the holder demoted inside the
receipt's suspension window, so the receipt persists `accepted` at the
owner's index; the sibling promotes, its boundary carry lifts the
pending admission, and its first field-owning scan applies it and
journals the settle — while the fenced predecessor's pull is paced
behind that settle, so its window is the staler view the finding is
about. The successor then demotes again and tracks back, adopting the
predecessor's staler window: the merge must refuse the regression, the
served terminal verdict must hold on both peers, and each peer's
served and durable journal must carry exactly one `command_settled` for
the admission. The doctor flags stage the pre-fix shapes the leg must
name.
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
    'AdoptedReceiptRegressionTests.test_registered',
    'AdoptedReceiptRegressionTests.test_clean_pair_passes_and_validates',
    'AdoptedReceiptRegressionTests.test_regressed_receipt_reports_nondeterministic',
    'AdoptedReceiptRegressionTests.test_doubled_settle_reports_nondeterministic',
    'AdoptedReceiptRegressionTests.test_doubled_durable_reports_nondeterministic',
    'AdoptedReceiptRegressionTests.test_never_staged_reports_failed',
    'AdoptedReceiptRegressionTests.test_refused_demote_reports_failed',
    'AdoptedReceiptRegressionTests.test_refused_promote_reports_failed',
    'AdoptedReceiptRegressionTests.test_never_settles_reports_failed',
    'AdoptedReceiptRegressionTests.test_no_active_reports_failed',
    'AdoptedReceiptRegressionTests.test_unconverged_pair_reports_inconclusive',
    'AdoptedReceiptRegressionTests.test_unreachable_peer_reports_inconclusive',
    'AdoptedReceiptRegressionTests.test_missing_journals_report_inconclusive',
    'AdoptedReceiptRegressionTests.test_single_endpoint_reports_inconclusive',
    'AdoptedReceiptRegressionTests.test_unchecked_surface_reports_inconclusive',
    'AdoptedReceiptRegressionTests.test_diverging_digests_report_nondeterministic',
    'AdoptedReceiptRegressionTests.test_silent_judge_reports_unchecked',
    'AdoptedReceiptRegressionTests.test_two_runs_produce_identical_evidence',
})


class RegressionPeer:
    """One endpoint of the regression pair: role, tracking posture,
    its receipt log with absolute indices, the settlements its journal
    recorded, and the served image."""

    def __init__(self, name):
        self.name = name
        self.role = 'active'
        self.tracking = False
        self.orphaned = False
        self.tick = 0
        self.attempts = 0
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        self.image = {}
        self.journaled = {}   # index -> the verdict it recorded
        self.observed = {}    # index -> the last outcome it served


class RegressionFeed:
    """A stubbed pair for the adopted-receipt-regression leg: ctrl-a
    owns the field at launch, ctrl-b tracks it.

    Every endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans, and the two
    switch requests, whose boundaries act at the request. `POST /promote`
    runs the promoting peer's boundary final-sync carry — the owner's
    still-pending admissions ride across — settles them at the
    promoting peer's own boundary, and fences the predecessor: its
    receipt stays `accepted` and its pull is paced behind the settle,
    which is the staler window the successor's next adoption finds.
    The fix keeps the successor's own terminal verdict through that
    adoption and journals no second settle for the admission.

    Doctor flags stage each named defect: `regress` (the adoption takes
    the adopted window verbatim — the #709 defect), `double_settle`
    (the restored transition is journaled a second time — the
    recorder half of the finding), `never_stage` (the demote never
    suspends the receipt), `demote_refused`, `promote_refused`,
    `never_settle` (the carried admission never applies),
    `park_served` (no terminal verdict is ever served), `no_active`,
    `no_tracking`, `no_surface` (the served checkpoint carries no
    receipt window), and `unreachable`.
    """

    POINTS = (302, 300, 301, 332, 333, 334)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]

    def __init__(self, journal_files=None):
        self.a = RegressionPeer('a')
        self.a.role = 'active'
        self.b = RegressionPeer('b')
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
        self.regress = False        # adoption takes the window verbatim
        self.double_settle = False  # the restored verdict journals again
        self.durable_only = False   # ... in the bound file alone
        self.never_stage = False    # the demote never suspends
        self.demote_refused = False
        self.promote_refused = False
        self.never_settle = False   # the carried admission never applies
        self.park_served = False    # no terminal verdict is ever served
        self.no_active = False
        self.no_tracking = False
        self.no_surface = False
        self.unreachable = False
        self.hold = set()      # peers whose scan boundary is held —
                               # the pending window the demote
                               # suspends inside
        self.paced = set()     # peers whose tracking pull answers the
                               # window captured before the settle —
                               # the staler view
        self.paced_release = None   # the peer whose adoption releases
                                   # the paced pull
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

    @staticmethod
    def _outcome(receipt):
        outcome = receipt.get('outcome') or {}
        name = next(iter(outcome), 'unknown')
        if name == 'rejected':
            return 'rejected'
        return name

    def _settle(self, peer, receipt):
        """One settlement recorded at its own boundary — the recorder's
        per-admission mark, so a repeated drain never re-journals."""
        index = self._index(receipt)
        peer.observed[index] = self._outcome(receipt)
        if peer.journaled.get(index) is not None:
            return
        peer.journaled[index] = receipt
        self._mark(peer, {'command_settled': {'receipt':
                                              copy.deepcopy(receipt)}})

    def _observe(self, peer, receipt):
        """Record the outcome a peer now serves at an index without
        journaling it — the pending observations the recorder diffs
        against."""
        peer.observed[self._index(receipt)] = self._outcome(receipt)

    def _rejournal(self, peer, receipt, durable_only=False):
        """A second `command_settled` for an admission this peer
        already settled — the finding's duplicate, where the restored
        transition after the regression read as a fresh observable
        settle. `durable_only` writes it into the bound journal file
        alone, the shape a served-journal dedup with an unwritten file
        produces."""
        record = {'command_settled': {'receipt': copy.deepcopy(receipt)}}
        if durable_only:
            path = self.journal_paths.get(peer.name)
            if path is not None:
                with path.open('a') as stream:
                    stream.write(json.dumps(
                        {'entry': {'seq': peer.next_seq,
                                   'tick': peer.tick,
                                   'event': record}}) + '\n')
            return
        self._mark(peer, record)

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
    def _terminal(receipt):
        return receipt is not None \
            and 'accepted' not in receipt['outcome']

    def _adopt(self, peer, other):
        """The tracked pull: the peer's log adopts the source's window,
        extended by whatever of its own tail the source's high-water
        never reached. The contract's half: an adopted `accepted` view
        of an admission this run already settled is the source's staler
        record, so the run's own terminal verdict stands. The pre-fix
        shape (`regress`) takes the source's record verbatim — the
        silent regression the finding recorded — and the recorder half
        (`double_settle`) journals the restored transition as a second
        settle for the admission."""
        mine = {self._index(receipt): copy.deepcopy(receipt)
                for receipt in peer.receipts}
        adopted = copy.deepcopy(other.receipts)
        adopted_end = (adopted[-1]['index'] + 1) if adopted else 0
        merged = adopted + [copy.deepcopy(receipt)
                            for receipt in peer.receipts
                            if receipt['index'] >= adopted_end]
        for position, served in enumerate(merged):
            index = self._index(served)
            held = mine.get(index)
            keep = served
            if held is not None and self._terminal(held) \
                    and not self._terminal(served) \
                    and held['command'] == served['command'] \
                    and held.get('actor') == served.get('actor') \
                    and not self.regress:
                # The adoption is refused: the run's own terminal
                # verdict stays the served truth.
                keep = copy.deepcopy(held)
            merged[position] = keep
            # The recorder's diff: an outcome this record observes for
            # the first time since the admission went pending is a
            # fresh settle for it — the half of the finding that
            # journaled the restored verdict a second time.
            rejournal = self.double_settle \
                and self._outcome(keep) != 'accepted' \
                and peer.observed.get(index) == 'accepted'
            self._settle(peer, keep)
            if rejournal:
                self._rejournal(peer, keep, self.durable_only)
        peer.receipts = merged
        peer.attempts = max(peer.attempts, other.attempts)
        peer.image = dict(other.image)
        for receipt in peer.receipts:
            self._observe(peer, receipt)
        if self.paced_release == peer.name:
            # The paced pull has now answered the staler window — the
            # adoption the leg audits happened — so the pair converges.
            self.paced.clear()
            self.paced_release = None

    def _advance(self, peer, adopt=True):
        """One completed scan: pending role transitions settle, the
        holder applies its pending admissions, and a tracking standby
        pulls its source's window. A held peer's boundary is the
        pending window the demote suspends inside — its scan does not
        run while the window stands."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        if peer.role == 'active' and peer.name not in self.hold:
            self._apply(peer)
        elif peer.role == 'standby' and peer.tracking and adopt \
                and not self.no_tracking and peer.name not in self.paced:
            source = self._other(peer)
            self._adopt(peer, source)
            # A line following a source that owns no field owes the
            # ownerless `orphaned` verdict; behind a field owner it
            # reports `tracking`.
            peer.orphaned = source.role != 'active'
        if self.park_served:
            for receipt in peer.receipts:
                if not self._terminal(receipt):
                    continue
                index = self._index(receipt)
                if index in peer.journaled:
                    self._served_parked.add((peer.name, index))

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
            if peer.role != 'active' or self.demote_refused:
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            if self.never_stage:
                # The boundary ran before the demote: the receipt
                # settled on the holder's own line, so nothing was
                # ever suspended.
                self.hold.discard(peer.name)
                self._apply(peer)
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active' or not peer.tracking \
                    or self.promote_refused or self.no_tracking:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync pull: the owner's
            # whole log, including the admissions still pending inside
            # its boundary window, rides onto the promoting peer.
            carried = [copy.deepcopy(receipt) for receipt
                       in other.receipts
                       if 'accepted' in receipt['outcome']]
            peer.receipts = copy.deepcopy(other.receipts) + carried
            peer.attempts = other.attempts
            peer.image = dict(other.image)
            peer.role = 'promoting'
            if self.never_settle:
                self.hold.add(peer.name)
            else:
                self.hold.discard(peer.name)
            self._advance(peer, adopt=False)
            if self.never_settle:
                # The carried admission never reached a boundary that
                # applied it: the promoting peer takes the field still
                # holding the pending receipt.
                return 200, {'role': 'active'}
            # The field's arbitration took the claim: the incumbent's
            # next scan fences and demotes it in place behind the new
            # holder, its own receipt still `accepted` and its pull
            # paced behind the settlement it never learned.
            self.hold.add(other.name)
            other.role = 'demoting'
            self._advance(other, adopt=False)
            other.tracking = True
            other.orphaned = False
            self.paced = {other.name}
            self.paced_release = peer.name
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
            if self.no_surface:
                return 200, {'tick': peer.tick, 'components': {}}
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
                # The admission opens the field owner's pending window:
                # its boundary holds the receipt until a switch or the
                # window's own scan lands, which is the window the
                # demote suspends inside.
                self.hold.add(peer.name)
            return 200, copy.deepcopy(receipt)
        raise AssertionError('unexpected request %s %s' % (method, url))


class AdoptedReceiptRegressionTests(unittest.TestCase):
    """The adopted-receipt-regression leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — the receipt
    suspended `accepted` at the demote, applied and journaled on the
    promoted sibling, the paced predecessor's staler window adopted
    without regressing the settle, one `command_settled` per admission
    on each peer's served and durable journal, the pair back on its
    entry roles — each doctored defect reports its named diagnostic,
    and an unreachable, unconverged, or unstageable rig reports
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.feed = RegressionFeed(self.journal_files)

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
                patch.object(scenarios, 'REGRESSION_SETTLE', 2.0), \
                patch.object(scenarios, 'REGRESSION_WINDOW', 3), \
                patch.object(scenarios, 'REGRESSION_AUDIT', 2.0), \
                patch.object(scenarios, 'REGRESSION_POLL', 0.001), \
                patch.object(scenarios, 'REGRESSION_WATCH', 0.001):
            return scenarios.scenario_adopted_receipt_regression(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(
                scenarios.scenario_settled_receipt_arbitration) + 1,
            order.index(scenarios.scenario_adopted_receipt_regression))
        # The collision leg stages through the same window directly
        # after, and both restore the entry layout before the
        # peer-announce case.
        self.assertEqual(
            order.index(scenarios.scenario_adopted_receipt_regression)
            + 1,
            order.index(scenarios.scenario_receipt_index_collision))
        self.assertEqual(
            order.index(scenarios.scenario_receipt_index_collision) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('adopted-receipt-regression'),
            scenarios.scenario_adopted_receipt_regression)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('receipt-regression-signals.json',
                     'receipt-regression-pass-1.json',
                     'receipt-regression-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('receipt-regression-pass-1.json',
                               'receipt-regression-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        for passed in passes:
            self.assertEqual(passed['digest']['outcome'], 'held',
                             passed['digest'])
            self.assertEqual(passed['digest']['settles'],
                             'one-per-admission')
            self.assertEqual(passed['digest']['roles'], 'restored')
            # The staler view the adoption finds really was staged: the
            # fenced predecessor's window still read `accepted` at the
            # admission's index when the successor had settled it.
            self.assertTrue(passed['stale_view']['observed'], passed)
            self.assertTrue(passed['stale_view']['demoted_receipt']
                            .startswith('accepted'),
                            passed['stale_view'])
            # One settle per admission on each peer, served and durable.
            self.assertEqual(passed['journaled'],
                             {'active': 1, 'standby': 1})
            self.assertEqual(passed['durable'],
                             {'active': 1, 'standby': 1})
            # The first poll is the settle surfacing on the successor
            # while the predecessor's window is still behind it; every
            # poll after the adoption window answers the one settled
            # verdict on both peers.
            first, rest = passed['polls'][0], passed['polls'][1:]
            self.assertTrue(first['standby'].startswith('applied@'),
                            first)
            self.assertTrue(first['active'].startswith('accepted'),
                            first)
            for poll in rest:
                self.assertEqual(len(set(poll.values())), 1, poll)
                for verdict in poll.values():
                    self.assertTrue(verdict.startswith('applied@'), poll)
        report.validate_scenario(record)

    def test_regressed_receipt_reports_nondeterministic(self):
        # The #709 defect: the adoption takes the staler window
        # verbatim, so the settling peer's own receipt reads `accepted`
        # again once the pair reconverges.
        self.feed.regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_doubled_settle_reports_nondeterministic(self):
        # The recorder half of the finding: the restored transition is
        # journaled a second time for one admission.
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_doubled_durable_reports_nondeterministic(self):
        # The durable half's duplicate: the served journal deduped and
        # the bound journal file did not.
        feed = RegressionFeed(self.journal_files)
        feed.double_settle = True
        feed.durable_only = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_never_staged_reports_failed(self):
        # The holder's boundary ran before the demote: nothing was ever
        # suspended, so the rig never presented the sequence the
        # contract audits.
        self.feed.never_stage = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-failed'), record['detail'])
        self.assertIn('suspension window', record['detail'])
        report.validate_scenario(record)

    def test_refused_demote_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-failed'), record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-failed'), record['detail'])
        report.validate_scenario(record)

    def test_never_settles_reports_failed(self):
        # The carried admission never reached a boundary that applied
        # it: there is no terminal outcome to keep from regressing.
        self.feed.never_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-failed'), record['detail'])
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

    def test_unchecked_surface_reports_inconclusive(self):
        # A run whose served checkpoint predates the receipt window and
        # the admission counters: the contract never engaged there.
        self.feed.no_surface = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('admission counters', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'outcome': 'held'}, {}, {'pass': 1}),
                       ({'outcome': 'regressed'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_regression_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_adoption_judge',
                          lambda *a, **k: 'held'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-regression-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for number in range(2):
            journal_files = {
                'active': str(Path(self.tmp.name)
                              / ('run-a-' + str(number) + '.jsonl')),
                'standby': str(Path(self.tmp.name)
                               / ('run-b-' + str(number) + '.jsonl'))}
            feed = RegressionFeed(journal_files)
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