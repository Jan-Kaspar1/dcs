"""The 3050_shelving_reason leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_shelving_reason, following the per-leg
test-module split (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ShelvingReasonTests.test_registered',
    'ShelvingReasonTests.test_clean_feed_passes_and_validates',
    'ShelvingReasonTests.test_two_runs_produce_identical_evidence',
    'ShelvingReasonTests.test_rejected_reasoned_shelve_fails',
    'ShelvingReasonTests.test_unsettled_shelve_fails',
    'ShelvingReasonTests.test_settled_receipt_without_reason_fails',
    'ShelvingReasonTests.test_unserved_shelved_flag_fails',
    'ShelvingReasonTests.test_silent_journal_fails',
    'ShelvingReasonTests.test_journaled_receipt_without_reason_fails',
    'ShelvingReasonTests.test_mispaired_transition_fails',
    'ShelvingReasonTests.test_unexpired_shelve_fails',
    'ShelvingReasonTests.test_early_expiry_fails',
    'ShelvingReasonTests.test_refused_release_fails',
    'ShelvingReasonTests.test_rejected_reasonless_shelve_fails',
    'ShelvingReasonTests.test_reasonless_receipt_with_reason_fails',
    'ShelvingReasonTests.test_drifted_reread_fails_nondeterministic',
    'ShelvingReasonTests.test_predates_index_is_inconclusive',
    'ShelvingReasonTests.test_predates_envelope_is_inconclusive',
    'ShelvingReasonTests.test_no_managed_alarm_is_inconclusive',
    'ShelvingReasonTests.test_no_shelvable_alarm_is_inconclusive',
    'ShelvingReasonTests.test_all_mandating_is_inconclusive',
    'ShelvingReasonTests.test_missing_journal_path_is_inconclusive',
    'ShelvingReasonTests.test_no_active_peer_fails',
    'ShelvingReasonTests.test_unreachable_pair_is_inconclusive',
})


class ShelvingReasonFeed:
    """A stubbed field owner for the shelving-reason scenario. ctrl-a is
    the settled active serving the rig's shelvable managed alarm —
    managed-latching-alarm:8 with shelve request point 1011 (writable,
    journaled, not requires_reason), shelved status point 1015, and a
    declared max_shelve_ticks of 8: the `shelved` flag asserts the scan
    the request applies and drops at applied + bound with the request
    still standing. POST /command receipts a write_value that applies
    at the next request; the served /journal and the run's journal file
    carry the same command_settled and point_changed records — the
    durable record's real format, which the feed writes itself. Every
    request is one completed scan, so the expiry window is request-count
    keyed. Fault flags stage each named miss the issue calls out."""

    COMPONENT = 'managed-latching-alarm:8'
    SHELVE = 1011
    SHELVED = 1015
    ALARM = 1013
    BOUND = 8

    def __init__(self, journal_path):
        self.journal_path = Path(journal_path)
        self.records = [{'run_boundary': {'run': 1, 'tick': 0}}]
        self._flush()
        self.journal = []       # the served /journal entries
        self.seq = 0
        self.tick = 0
        self.receipts = []
        self.posts = 0
        self.request = False    # the shelve request's applied level
        self.elapsed = 0        # scans the request has stood
        self.shelved = False
        # Fault injection for the named-failure cases.
        self.not_active = False        # no peer reports role=active
        self.unreachable = False       # the pair answers nothing
        self.predates_index = False    # /signals drops requires_reason
        self.no_managed = False        # /snapshot carries no descriptor
        self.unshelvable = False       # the shelve input is not writable
        self.mandatory_only = False    # the shelve point mandates a reason
        self.predates_envelope = False  # a reasoned envelope refuses 400
        self.reject_reasoned = False   # the reasoned shelve is rejected
        self.never_settle = False      # the write never applies
        self.drop_reason = False       # the settled receipt loses reason
        self.never_shelved = False     # the managed list never routes it
        self.journal_silent = False    # the durable record never lands
        self.journal_drop_reason = False  # the journaled copy loses it
        self.wrong_pairing = False     # the rise journals off the tick
        self.no_expiry = False         # the flag never drops
        self.wrong_expiry = False      # the drop lands a tick early
        self.refuse_release = False    # the held request never releases
        self.refuse_reasonless = False  # the reasonless shelve is refused
        self.reasonless_reason = False  # the reasonless receipt gains one
        self.drift_reread = False      # the file record moves between reads

    def _flush(self):
        self.journal_path.write_text(
            ''.join(json.dumps(record) + '\n' for record in self.records))

    def _journal(self, event, tick=None):
        if self.journal_silent:
            return
        self.seq += 1
        entry = {'seq': self.seq,
                 'tick': self.tick if tick is None else tick,
                 'event': event}
        self.journal.append(entry)
        self.records.append({'entry': dict(entry)})
        self._flush()

    def _drift(self):
        # The named nondeterministic miss: the durable record re-read
        # disagrees — the shelved rise's journaled tick moved.
        for record in self.records:
            entry = record.get('entry') or {}
            change = (entry.get('event') or {}).get('point_changed') or {}
            if change.get('point') == self.SHELVED \
                    and change.get('to') == {'bool': True}:
                entry['tick'] = (entry.get('tick') or 0) + 1
                break
        self._flush()

    # The plant half: one completed scan per measurement read — the
    # command phase applies due writes and journals their settlements,
    # then the managed step re-evaluates `shelved`: asserted while the
    # request stands inside the declared bound, dropped the scan the
    # bound lapses.
    def _scan(self):
        self.tick += 1
        for receipt in self.receipts:
            accepted = (receipt.get('outcome') or {}).get('accepted')
            if not accepted or self.tick < accepted['apply_tick'] \
                    or self.never_settle:
                continue
            write = receipt['command']['write_value']
            before = self.request
            self.request = bool(write['value']['bool'])
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if self.drop_reason:
                receipt.pop('reason', None)
            journaled = dict(receipt)
            if self.journal_drop_reason:
                journaled.pop('reason', None)
            self._journal({'command_settled': {'receipt': journaled}})
            if before != self.request:
                self._journal({'point_changed': {
                    'point': write['point'],
                    'from': {'bool': before},
                    'to': {'bool': self.request}}})
        if self.request:
            self.elapsed += 1
            bound = self.BOUND - 1 if self.wrong_expiry else self.BOUND
            shelved = self.elapsed <= bound
        else:
            self.elapsed = 0
            shelved = False
        if self.never_shelved:
            shelved = False
        if self.no_expiry:
            shelved = self.request
        if shelved != self.shelved:
            self.shelved = shelved
            tick = self.tick + 1 if self.wrong_pairing and shelved \
                else self.tick
            self._journal({'point_changed': {
                'point': self.SHELVED,
                'from': {'bool': not shelved},
                'to': {'bool': shelved}}}, tick=tick)

    # The measurement channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if self.unreachable:
            raise ConnectionError('the pair is unreachable')
        self._scan()
        if host != 'ctrl-a:1':
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/role'):
            role = 'standby' if self.not_active else 'active'
            return 200, {'role': role, 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = [
                {'point': self.SHELVE, 'signal': 11000 + self.SHELVE,
                 'name': 'lal-shelve', 'direction': 'in',
                 'value_type': 'bool',
                 'writable': not self.unshelvable},
                {'point': self.SHELVED, 'signal': 11000 + self.SHELVED,
                 'name': 'lal-shelved', 'direction': 'out',
                 'value_type': 'bool', 'writable': False},
                {'point': self.ALARM, 'signal': 11000 + self.ALARM,
                 'name': 'lal-alarm', 'direction': 'out',
                 'value_type': 'bool', 'writable': False}]
            if not self.predates_index:
                for entry in points:
                    entry['requires_reason'] = \
                        bool(entry['writable'] and self.mandatory_only)
            return 200, {'points': points, 'components': [
                {'name': self.COMPONENT,
                 'kind': 'managed-latching-alarm'}]}
        if (method, route) == ('GET', '/snapshot'):
            descriptors = [] if self.no_managed else [{
                'name': self.COMPONENT, 'kind': 'managed-latching-alarm',
                'label': self.COMPONENT,
                'ports': [
                    {'name': 'shelve', 'direction': 'in', 'kind': 'bool',
                     'role': 'command', 'point': self.SHELVE},
                    {'name': 'shelved', 'direction': 'out',
                     'kind': 'bool', 'role': 'status',
                     'point': self.SHELVED},
                    {'name': 'alarm', 'direction': 'out', 'kind': 'bool',
                     'role': 'status', 'point': self.ALARM}],
                'parameters': [{'name': 'max_shelve_ticks',
                                'kind': 'int'}],
                'commands': [], 'events': []}]
            parameters = [] if self.no_managed else [{
                'name': self.COMPONENT,
                'values': {'max_shelve_ticks': {'int': self.BOUND}}}]
            return 200, {
                'tick': self.tick,
                'points': [
                    {'point': self.SHELVE, 'sample': {
                        'value': {'bool': self.request},
                        'quality': {'quality': 'good'}}},
                    {'point': self.SHELVED, 'sample': {
                        'value': {'bool': self.shelved},
                        'quality': {'quality': 'good'}}}],
                'descriptors': descriptors,
                'parameters': parameters}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'receipts': list(self.receipts),
                         'command_admission': {'attempts': self.posts}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1].split('&')[0])
            return 200, [dict(entry) for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            self.posts += 1
            if self.predates_envelope and 'reason' in body:
                raise urllib.error.HTTPError(
                    url, 400, 'bad request', {}, io.BytesIO(b'null'))
            if self.drift_reread and self.posts == 4:
                # The final restore landed — the file record moves
                # before the leg's re-read.
                self._drift()
            rejected = None
            if self.reject_reasoned and self.posts == 1:
                rejected = {'validation': {'detail': 'refused'}}
            if self.refuse_release and self.posts == 2:
                rejected = {'validation': {'detail': 'refused'}}
            if self.refuse_reasonless and self.posts == 3:
                rejected = {'reason_required': {'point': self.SHELVE}}
            receipt = {'command': body['command'],
                       'actor': body.get('actor'),
                       'outcome': {'rejected': {'reason': rejected}}
                       if rejected else
                       {'accepted': {'apply_tick': self.tick + 1}}}
            if 'reason' in body or self.reasonless_reason:
                receipt['reason'] = body.get('reason', 'injected')
            if rejected:
                self._journal({'command_settled': {
                    'receipt': dict(receipt)}})
            self.receipts.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ShelvingReasonTests(unittest.TestCase):
    """scenario_shelving_reason against the stubbed field owner: the
    reasoned shelve's settled receipt, the durable journal's
    receipt-beside-transition record, the served managed-state surface,
    the declared-bound expiry, the reasonless variant, and the
    inconclusive cases when the rig predates the contract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal.parent.mkdir(parents=True)
        self.feed = ShelvingReasonFeed(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, journal=True):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'evidence_dir': str(self.evidence)}
        if journal:
            ctx['journal_files'] = {'active': str(self.journal)}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'SHELVING_SETTLE', 0.3), \
                patch.object(scenarios, 'SHELVING_DEADLINE', 0.5), \
                patch.object(scenarios, 'SHELVING_EXPIRY', 1.0), \
                patch.object(scenarios, 'SHELVING_LIVE_POLL', 0.001):
            return scenarios.scenario_shelving_reason(ctx)

    def test_registered(self):
        self.assertIn(scenarios.scenario_shelving_reason,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('shelving-reason'),
            scenarios.scenario_shelving_reason)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two passes against the same
        # rig layout record the same report and the same evidence files
        # — the feed's transitions are request-count keyed, never
        # wall-clock.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = ShelvingReasonFeed(self.journal)
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])

    def test_rejected_reasoned_shelve_fails(self):
        self.feed.reject_reasoned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('rejected', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_shelve_fails(self):
        self.feed.never_settle = True
        self.feed.never_shelved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('never settled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_settled_receipt_without_reason_fails(self):
        # The receipt settles applied but the declared reason never
        # rode it — the in-contract carriage broke at the receipt.
        self.feed.drop_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('reason', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unserved_shelved_flag_fails(self):
        # The receipt settles applied but the served snapshot never
        # reports the managed flag — the managed list never routed.
        self.feed.never_shelved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('shelved asserted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_journal_fails(self):
        self.feed.journal_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('never recorded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_receipt_without_reason_fails(self):
        # The settled receipt carries the reason but the durable
        # journal's copy dropped it — the lifecycle record's
        # attribution broke.
        self.feed.journal_drop_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('journaled receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_mispaired_transition_fails(self):
        # The shelved rise journaled one tick off the applied tick —
        # the receipt-beside-transition pairing resolves nothing.
        self.feed.wrong_pairing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('rise journaled at tick',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unexpired_shelve_fails(self):
        self.feed.no_expiry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('never expired', record.get('detail', ''))
        report.validate_scenario(record)

    def test_early_expiry_fails(self):
        # The drop lands one tick inside the declared bound — expiry
        # proceeded, but not per the declared max_shelve_ticks.
        self.feed.wrong_expiry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('expiry drop journaled at tick',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_release_fails(self):
        # The shelve expires per the bound but the restore is refused —
        # the leg leaves the request standing.
        self.feed.refuse_release = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('release', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rejected_reasonless_shelve_fails(self):
        # The non-mandating point refuses the reasonless shelve — the
        # pre-contract path changed.
        self.feed.refuse_reasonless = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('non-mandating', record.get('detail', ''))
        report.validate_scenario(record)

    def test_reasonless_receipt_with_reason_fails(self):
        # The reasonless submission's receipt carries a reason the
        # operator never declared.
        self.feed.reasonless_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-failed',
                      record.get('detail', ''))
        self.assertIn('never declared', record.get('detail', ''))
        report.validate_scenario(record)

    def test_drifted_reread_fails_nondeterministic(self):
        # The durable record moved between reads — the same surface
        # disagreeing with itself is the named nondeterministic miss.
        self.feed.drift_reread = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shelving-reason-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_predates_index_is_inconclusive(self):
        # No served point carries the requires_reason mark — the
        # deployed build predates the shelving-reason contract.
        self.feed.predates_index = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('requires_reason', record.get('detail', ''))
        report.validate_scenario(record)

    def test_predates_envelope_is_inconclusive(self):
        # The attributed reason envelope answers 400 — the deployed
        # build predates the contract's parser.
        self.feed.predates_envelope = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_managed_alarm_is_inconclusive(self):
        self.feed.no_managed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_no_shelvable_alarm_is_inconclusive(self):
        # The managed alarm's shelve input is not writable — the rig
        # offers the leg nothing to shelve.
        self.feed.unshelvable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_all_mandating_is_inconclusive(self):
        # Every shelvable declaration mandates a reason — the rig
        # offers no non-mandating point for the reasonless leg.
        self.feed.mandatory_only = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('requires_reason', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_path_is_inconclusive(self):
        record = self.run_scenario(journal=False)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_peer_fails(self):
        self.feed.not_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_pair_is_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)
