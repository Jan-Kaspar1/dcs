"""The 1985_receipt_index_collision leg's scenario unit coverage — the
feed fake and TestCase classes for
scenario_receipt_index_collision, split per the one-module-per-leg
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The stubbed pair models the #775 finding's reproduction and its fix: a
tracking sibling's promotion boundary fetches the pre-admission
document and its claim preempts the holder; a receipted command lands
on the still-field-owning holder inside that window, so it is admitted
at the index the sibling is about to mint from and the fence supersedes
it; a second receipted command on the new active mints the same
absolute index. The split mint's contract is the merge's: the displaced
receipt re-mints past the adopted window's high-water with its command,
actor, and terminal `superseded` verdict intact, and its already-
journaled settle is re-keyed rather than re-emitted. The doctor flags
stage the pre-fix shape the leg must name — the adoption replacing the
window wholesale, so only the successor's record stands at the collided
index.
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
    'ReceiptIndexCollisionTests.test_registered',
    'ReceiptIndexCollisionTests.test_clean_pair_passes_and_validates',
    'ReceiptIndexCollisionTests.test_displaced_admission_reports_failed',
    'ReceiptIndexCollisionTests.test_collapsed_admissions_report_nondeterministic',
    'ReceiptIndexCollisionTests.test_doubled_settle_reports_nondeterministic',
    'ReceiptIndexCollisionTests.test_never_staged_reports_failed',
    'ReceiptIndexCollisionTests.test_refused_promote_reports_failed',
    'ReceiptIndexCollisionTests.test_successor_refused_reports_failed',
    'ReceiptIndexCollisionTests.test_no_active_reports_failed',
    'ReceiptIndexCollisionTests.test_unconverged_pair_reports_inconclusive',
    'ReceiptIndexCollisionTests.test_unreachable_peer_reports_inconclusive',
    'ReceiptIndexCollisionTests.test_missing_journals_reports_inconclusive',
    'ReceiptIndexCollisionTests.test_single_endpoint_reports_inconclusive',
    'ReceiptIndexCollisionTests.test_unchecked_surface_reports_inconclusive',
    'ReceiptIndexCollisionTests.test_diverging_digests_report_nondeterministic',
    'ReceiptIndexCollisionTests.test_silent_judge_reports_unchecked',
    'ReceiptIndexCollisionTests.test_two_runs_produce_identical_evidence',
})


class CollisionPeer:
    """One endpoint of the collision pair: role, tracking posture, its
    receipt log with absolute indices, the settlements its journal
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


class CollisionFeed:
    """A stubbed pair for the receipt-index-collision leg: ctrl-a owns
    the field at launch, ctrl-b tracks it.

    Every endpoint call is one scan on the called peer except POST
    /command, whose admission lands pending between scans, and the two
    switch requests, whose boundaries act at the request. `POST /promote`
    runs the promoting peer's boundary final-sync fetch — meeting only
    the pre-admission document, so its high-water never covers the
    admission the still-field-owning peer takes next — and then fences
    the predecessor: its pending receipt is superseded, its own adoption
    of the successor's window re-mints the displaced receipt past that
    window's high-water with its command, actor, and `superseded`
    verdict intact, and its already-journaled settle is re-keyed rather
    than re-emitted. The pre-fix shape (`displace`) takes the adopted
    window wholesale, so only the successor's record stands at the
    collided index.

    Doctor flags stage each named defect: `displace` (the adoption
    drops the displaced receipt), `collapse` (both admissions answer
    with one record), `double_settle` (the re-homed settle is emitted a
    second time), `never_stage` (the promotion's fence never supersedes
    the raced admission), `promote_refused`, `successor_refused`,
    `no_active`, `no_tracking`, `no_surface` (the served checkpoint
    carries no receipt window), and `unreachable`.
    """

    POINTS = (302, 300, 301, 332, 333, 334)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]

    def __init__(self, journal_files=None):
        self.a = CollisionPeer('a')
        self.a.role = 'active'
        self.b = CollisionPeer('b')
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
        self.displace = False        # the adoption drops the displaced
        self.collapse = False        # both admissions share one record
        self.double_settle = False   # the re-homed settle journals twice
        self.never_stage = False     # the fence never supersedes
        self.promote_refused = False
        self.successor_refused = False
        self.no_active = False
        self.no_tracking = False
        self.no_surface = False
        self.unreachable = False
        self.hold = set()      # peers whose scan boundary is held —
                               # the promote/fence window the raced
                               # admission races
        self.fenced = set()    # peers whose field claim the promotion
                               # preempted — the detection scan that
                               # supersedes and demotes them in place
        self.promoted = None    # the peer this run's promotion
                               # installed as the field owner

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _other(self, peer):
        return self.b if peer.name == 'a' else self.a

    @staticmethod
    def _index(receipt):
        return receipt.get('index')

    @staticmethod
    def _outcome(receipt):
        outcome = receipt.get('outcome') or {}
        return next(iter(outcome), 'unknown')

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
        per-admission mark, so the re-home re-keys it rather than
        emitting it again. Answers whether this call emitted."""
        index = self._index(receipt)
        peer.observed[index] = self._outcome(receipt)
        if peer.journaled.get(index) is not None:
            return False
        peer.journaled[index] = receipt
        self._mark(peer, {'command_settled': {'receipt':
                                              copy.deepcopy(receipt)}})
        return True

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

    def _supersede(self, peer):
        """The fence: the fenced peer's pending admissions settle
        `superseded` — the terminal verdict the displaced admission's
        preserved audit names."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            receipt['outcome'] = {'rejected': {'reason': {
                'superseded': {'point': receipt['command']
                               ['write_value']['point']}}}}
            self._settle(peer, receipt)

    def _adopt(self, peer, other):
        """The tracked pull: the peer's log adopts the source's window.
        The contract's half: a contested index whose entries name
        *different* submissions is the split mint, so the peer's own
        entry re-mints past the adopted window's high-water with its
        command, actor, and terminal verdict intact — the displaced
        admission keeps a servable record instead of being replaced at
        the collided index — and its already-journaled settle is
        re-keyed rather than re-emitted. The pre-fix shape
        (`displace`) takes the adopted window wholesale, and
        `collapse` answers both admissions with the peer's own record
        alone."""
        mine = [copy.deepcopy(receipt) for receipt in peer.receipts]
        adopted = copy.deepcopy(other.receipts)
        adopted_end = (adopted[-1]['index'] + 1) if adopted else 0
        rehomed = []
        for receipt in mine:
            contested = any(served['index'] == receipt['index']
                            and served.get('actor') != receipt.get('actor')
                            for served in adopted)
            if contested:
                previous = receipt['index']
                receipt['index'] = max(
                    [self._index(served) for served in adopted] or [-1]) + 1
                # The re-home re-keys the admission's recorded settle
                # onto its new index rather than emitting it again.
                recorded = peer.journaled.pop(previous, None)
                if recorded is not None:
                    peer.journaled[receipt['index']] = recorded
                rehomed.append(receipt)
            elif receipt['index'] >= adopted_end:
                # The unreached tail the adopted window's high-water
                # never saw — the re-minted entry rides it, so the
                # displaced admission keeps its record under every
                # later pull.
                rehomed.append(receipt)
        if not self.displace:
            adopted = adopted + [receipt for receipt in rehomed
                                 if receipt not in adopted]
            adopted.sort(key=self._index)
        if self.collapse:
            adopted = [receipt for receipt in adopted
                       if any(entry.get('actor') == receipt.get('actor')
                              for entry in rehomed)]
        peer.receipts = adopted
        # The high-water follows the merged window: a re-minted entry
        # past the adopted end keeps the next admission numbered one
        # past it rather than colliding inside it.
        peer.attempts = max(
            [peer.attempts, other.attempts]
            + [entry['index'] + 1 for entry in adopted])
        peer.image = dict(other.image)
        for receipt in peer.receipts:
            index = self._index(receipt)
            peer.observed[index] = self._outcome(receipt)
            if self._outcome(receipt) == 'accepted':
                continue
            rehome = receipt in rehomed
            recorded = self._settle(peer, receipt)
            if rehome and self.double_settle and not recorded:
                self._mark(peer, {'command_settled': {
                    'receipt': copy.deepcopy(receipt)}})

    def _advance(self, peer, adopt=True):
        """One completed scan: the fenced holder's detection scan
        supersedes its pending admissions and demotes it in place, the
        holder applies its own pending admissions, and a tracking
        standby pulls its source's window. A held peer's boundary is
        the promote/fence window the raced admission races."""
        peer.tick += 1
        if peer.role == 'active' and peer.name in self.fenced:
            if self.displace:
                # The pre-fix shape: the fenced holder's pending
                # admission vanishes with the window replacement, so
                # neither the served surface nor the journals name the
                # command that lost its place.
                peer.receipts = [
                    receipt for receipt in peer.receipts
                    if 'accepted' not in receipt['outcome']]
            elif not self.never_stage:
                self._supersede(peer)
            peer.role = 'demoting'
            self.fenced.discard(peer.name)
            self.hold.discard(peer.name)
            self._mark(peer, {'role_changed': {'from': 'active',
                                               'to': 'demoting'}})
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
                and not self.no_tracking:
            source = self._other(peer)
            self._adopt(peer, source)
            peer.orphaned = source.role != 'active'

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
            if peer.role == 'active' or not peer.tracking \
                    or self.promote_refused or self.no_tracking:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promotion boundary's final-sync fetch: the promoted
            # peer's log converges on the document it pulled — its own
            # unreached tail rides along and a contested index
            # re-mints — and the boundary carry then lifts the
            # holder's still-pending admissions across.
            self._adopt(peer, other)
            carried = [copy.deepcopy(receipt) for receipt
                       in other.receipts
                       if 'accepted' in receipt['outcome']]
            peer.receipts = peer.receipts + carried
            peer.attempts = max(
                [peer.attempts, other.attempts]
                + [entry['index'] + 1 for entry in peer.receipts])
            peer.image = dict(other.image)
            peer.role = 'promoting'
            self._advance(peer, adopt=False)
            # The field's arbitration took the claim: the holder is
            # fenced but still reporting, so the raced admission can
            # still be admitted at the index the successor is about to
            # mint from. Its detection scan supersedes and demotes it
            # in place.
            self.fenced = {other.name}
            self.promoted = peer.name
            return 200, {'role': 'active'}
        if not (method == 'POST' and route == '/command'):
            # POST /command's admission lands between scans: the peer's
            # own boundary runs on the next read, not on the request
            # that admits the command.
            self._advance(
                peer,
                adopt=not (method == 'POST' and route == '/promote'))
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
            return 200, copy.deepcopy(peer.receipts)
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
            if self.successor_refused \
                    and peer.name == self.promoted:
                # The promoted peer's role boundary refuses the
                # successor's own admission: no colliding mint.
                self._raise(409, {'not_active': {}})
            if peer.role != 'active':
                if self.successor_refused:
                    self._raise(409, {'not_active': {}})
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            return 200, copy.deepcopy(receipt)
        raise AssertionError('unexpected request %s %s' % (method, url))


class ReceiptIndexCollisionTests(unittest.TestCase):
    """The receipt-index-collision leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — the raced
    admission superseded at the fence, the successor minted at the same
    absolute index, both admissions' terminal verdicts retrievable on
    the pair's served surface and journals, one `command_settled` per
    admission per peer, the pair back on its entry roles — each
    doctored defect reports its named diagnostic, and an unreachable,
    unconverged, or unstageable rig reports inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_files = {
            'active': str(Path(self.tmp.name) / 'a-journal.jsonl'),
            'standby': str(Path(self.tmp.name) / 'b-journal.jsonl')}
        self.feed = CollisionFeed(self.journal_files)

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
                patch.object(scenarios, 'COLLISION_SETTLE', 2.0), \
                patch.object(scenarios, 'COLLISION_WINDOW', 3), \
                patch.object(scenarios, 'COLLISION_AUDIT', 2.0), \
                patch.object(scenarios, 'COLLISION_POLL', 0.001), \
                patch.object(scenarios, 'COLLISION_WATCH', 0.001):
            return scenarios.scenario_receipt_index_collision(
                ctx or self._ctx())

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(
                scenarios.scenario_adopted_receipt_regression) + 1,
            order.index(scenarios.scenario_receipt_index_collision))
        self.assertEqual(
            order.index(scenarios.scenario_receipt_index_collision) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertIs(
            verify.case_function('receipt-index-collision'),
            scenarios.scenario_receipt_index_collision)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('receipt-collision-signals.json',
                     'receipt-collision-pass-1.json',
                     'receipt-collision-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('receipt-collision-pass-1.json',
                               'receipt-collision-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        for passed in passes:
            self.assertEqual(passed['digest']['displaced'], 'preserved',
                             passed['digest'])
            self.assertEqual(passed['digest']['settles'],
                             'one-per-admission')
            self.assertEqual(passed['digest']['roles'], 'restored')
            # The collision was staged: both admissions reached a
            # terminal verdict and the racer's peer settled the
            # displaced one exactly once, served and durable.
            self.assertTrue(passed['polls'], passed)
            final = passed['polls'][-1]
            minted = [verdict for verdict
                      in final['successor']['served'].values()
                      if verdict and verdict.startswith('applied@')]
            self.assertTrue(minted, final['successor']['served'])
            displaced = final['displaced']['served']['active']
            self.assertTrue(displaced
                            and displaced.startswith('rejected:superseded'),
                            ('displaced', 'active', displaced))
            for count in passed['counts'].values():
                self.assertLessEqual(count, 1, passed['counts'])
            self.assertEqual(
                {key: value for key, value in passed['counts'].items()
                 if key.startswith("('displaced'")},
                {"('displaced', 'active', 'durable')": 1,
                 "('displaced', 'active', 'journaled')": 1,
                 "('displaced', 'standby', 'durable')": 0,
                 "('displaced', 'standby', 'journaled')": 0})
            # The two admissions stand side by side on the demoted
            # peer: distinct records, never one standing in place of
            # the other.
            self.assertNotEqual(
                final['displaced']['served']['active'],
                final['successor']['served']['active'],
                final)
        report.validate_scenario(record)

    def test_displaced_admission_reports_failed(self):
        # The #775 defect: the adoption takes the successor's window
        # wholesale, so the raced admission's superseded verdict is
        # nowhere retrievable and only the successor's receipt stands at
        # the collided index.
        self.feed.displace = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-failed'), record['detail'])
        self.assertIn('silently replaced', record['detail'])
        report.validate_scenario(record)

    def test_collapsed_admissions_report_nondeterministic(self):
        # Both admissions answered with one shared served record.
        self.feed.collapse = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_doubled_settle_reports_nondeterministic(self):
        # The re-home emits the already-journaled settle a second time.
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_never_staged_reports_failed(self):
        # The promotion's fence never superseded the raced admission, so
        # the rig never presented the split mint the contract
        # adjudicates.
        self.feed.never_stage = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-failed'), record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-failed'), record['detail'])
        report.validate_scenario(record)

    def test_successor_refused_reports_failed(self):
        self.feed.successor_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-failed'), record['detail'])
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

    def test_missing_journals_reports_inconclusive(self):
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
        self.feed.no_surface = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('admission counters', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'displaced': 'preserved'}, {}, {'pass': 1}),
                       ({'displaced': 'displaced'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_collision_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_collision_judge',
                          lambda *a, **k: 'preserved'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'receipt-collision-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for number in range(2):
            journal_files = {
                'active': str(Path(self.tmp.name)
                              / ('run-a-' + str(number) + '.jsonl')),
                'standby': str(Path(self.tmp.name)
                               / ('run-b-' + str(number) + '.jsonl'))}
            feed = CollisionFeed(journal_files)
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