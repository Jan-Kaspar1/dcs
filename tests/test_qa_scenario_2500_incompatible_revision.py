"""The 2500_incompatible_revision leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_incompatible_revision, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'IncompatibleRevisionTests.test_registered_in_scenarios',
    'IncompatibleRevisionTests.test_clean_refusal_passes_and_orders_the_legs',
    'IncompatibleRevisionTests.test_two_runs_produce_identical_evidence',
    'IncompatibleRevisionTests.test_silent_crossing_fails',
    'IncompatibleRevisionTests.test_fingerprint_degrade_is_the_wrong_refusal',
    'IncompatibleRevisionTests.test_never_crossing_peer_is_inconclusive',
    'IncompatibleRevisionTests.test_promotion_succeeding_fails',
    'IncompatibleRevisionTests.test_refusal_without_the_named_detail_fails',
    'IncompatibleRevisionTests.test_receipt_drift_fails',
    'IncompatibleRevisionTests.test_journal_drift_fails',
    'IncompatibleRevisionTests.test_field_regression_fails',
    'IncompatibleRevisionTests.test_control_degrading_fails',
    'IncompatibleRevisionTests.test_failed_action_is_inconclusive',
})


class IncompatibleFeed:
    """A stubbed rig for the incompatible-revision scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — and ctrl-c is the third controller the runner action
    launches twice: first on the incompatible document (its carryover
    crossing refuses, so every role poll reports the named degraded
    detail), then on the control document (the same revision minus the
    retype), which converges reinitialized carrying point 302 as its
    declared bool kind. Every transition is call-count keyed — never
    wall-clock — so two scenario runs emit identical evidence. Fault
    flags stage each named failure the issue calls out."""

    POINT = 302     # the carried internal point the spec retypes
    WATCH = 100     # the field `out` point the window watches
    DETAIL = ('checkpoint internal point 302 carries Bool but the '
             'revision declares Int — a retype must rename the point')
    FINGERPRINT_DETAIL = ('checkpoint model fingerprint 7f2a does not '
                          'match this run\'s 9c1e')

    def __init__(self, journals, incompatible_doc, control_doc):
        self.journals = {name: Path(p) for name, p in journals.items()}
        self.incompatible_doc = incompatible_doc
        self.control_doc = control_doc
        self.ticks = {'a': 0, 'b': 40, 'c': 0}
        self.roles = {'a': 'standby', 'b': 'active', 'c': 'standby'}
        self.launched = False
        self.incompatible = None
        self.c_role_polls = 0
        self.refuse_after = 3    # polls before the crossing reports
        self.receipts = {'a': [], 'b': [], 'c': []}
        self.staged_b = True
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.seqs = {'a': 1, 'b': 1, 'c': 1}
        self.calls = []
        self.reads = 0
        # Fault injection for the named-failure cases.
        self.action_fails = False       # the runner action raises
        self.never_cross = False        # c stays unsynchronized
        self.fingerprint_degrade = False  # the wrong degrade detail
        self.silent_cross = False       # the incompatible doc converges
        self.promote_succeeds = False   # /promote answers 200
        self.refusal_plain = False      # 409 without the named detail
        self.receipts_grow = False      # b's receipt log moves mid-window
        self.journal_grows = False      # b's journal file moves mid-window
        self.field_regress = False      # the field stops following b
        self.control_degrades = False   # the control relaunch refuses
        for name in ('a', 'b'):
            self.journals[name].parent.mkdir(parents=True,
                                             exist_ok=True)
            self.journals[name].write_text(
                json.dumps({'run_boundary': {'run': 1, 'tick': 0}})
                + '\n')

    def _journal(self, peer, record):
        with self.journals[peer].open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, peer, event):
        self._journal(peer, {'entry': {'seq': self.seqs[peer],
                                       'tick': self.ticks[peer],
                                       'event': event}})
        self.seqs[peer] += 1

    def _report(self):
        return {'from': 111, 'to': 222, 'resumed_at': self.ticks['b'],
                'carried': [{'point': self.POINT,
                             'value': {'bool': True}}],
                'carried_outputs': [{'point': self.WATCH,
                                     'value': {'bool': True}}],
                'carried_forces': [], 'dropped': [],
                'reinitialized': ['motor:20'], 'initialized': [900]}

    # The runner-owned action — replaces ctx['start_revised'].
    def start(self, name, incompatible=False):
        self.calls.append(('start_revised', name, incompatible))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        self.incompatible = incompatible
        self.c_role_polls = 0
        self.journals['c'].parent.mkdir(parents=True, exist_ok=True)
        self._journal('c', {'run_boundary': {'run': 1, 'tick': 0}})
        info = {'container': 'dcs-hw-qa-1-c',
                'document': str(self.incompatible_doc
                                if incompatible else self.control_doc),
                'added_points': [900], 'added_signals': [10900]}
        if incompatible:
            info['retyped_point'] = self.POINT
            info['rewired_connections'] = 3
        return info

    def _role(self, peer):
        if peer != 'c':
            return {'role': self.roles[peer], 'tick': self.ticks[peer],
                    'sync': 'unsynchronized'
                    if self.roles[peer] == 'standby' else None}
        self.c_role_polls += 1
        if self.c_role_polls < self.refuse_after or self.never_cross:
            sync = 'unsynchronized'
        elif self.incompatible:
            if self.silent_cross:
                sync = {'reinitialized': {'report': self._report()}}
            elif self.fingerprint_degrade:
                sync = {'degraded':
                        {'detail': self.FINGERPRINT_DETAIL}}
            else:
                sync = {'degraded': {'detail': self.DETAIL}}
        elif self.control_degrades:
            sync = {'degraded': {'detail': self.DETAIL}}
        else:
            sync = {'reinitialized': {'report': self._report()}}
        return {'role': self.roles['c'], 'tick': self.ticks['c'],
                'sync': sync}

    def _snapshot(self, peer):
        self.ticks[peer] += 1
        return {'tick': self.ticks[peer],
                'points': [
                    {'point': self.WATCH,
                     'sample': {'value': {'bool': self.staged_b},
                                'quality': 'good'}}],
                'io_health': {'failed_writes': 0,
                              'consecutive_failures': 0,
                              'last_error': None}}

    def _promote(self, peer):
        self.calls.append(('promote', peer))
        if self.promote_succeeds:
            self.roles[peer] = 'active'
            return 200, {'role': 'active', 'tick': self.ticks[peer]}
        detail = 'checkpoint refused' if self.refusal_plain \
            else self.DETAIL
        body = {'not_converged': {'sync': {'degraded':
                                           {'detail': detail}}}}
        raise urllib.error.HTTPError(
            'http://ctrl-c:3/promote', 409, 'Conflict', {},
            io.BytesIO(json.dumps(body).encode()))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-c:3': 'c'}[host]
        if peer == 'c' and not self.launched:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts[peer])
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant's sim-net service — the wire dispatch the ctx
    # ['plant_ctl'] seam wraps, covering the tool's list/read
    # subcommands the scenario's field legs drive.
    def field_request(self, ctx, request):
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': self.WATCH, 'direction': 'out',
                 'sample': self.field, 'fault': None}]}
        if request['op'] == 'read':
            self.reads += 1
            if self.reads == 2:
                # Mid-window mutations stage the audit-trail faults:
                # the active's receipts or journal moving under the
                # refusal, or the field leaving the active's image.
                if self.receipts_grow:
                    self.receipts['b'].append(
                        {'command': {'write_value': {'point': 302}},
                         'actor': 'intruder'})
                if self.journal_grows:
                    self._entry('b', {'command_settled':
                                      {'receipt': {'actor':
                                                   'intruder'}}})
                if self.field_regress:
                    self.field = {'value': {'bool': not self.staged_b},
                                  'quality': 'good', 'tick': 9}
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


class IncompatibleRevisionTests(unittest.TestCase):
    """scenario_incompatible_revision against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the incompatible document silently crossing, the degrade
    landing on the foreign-fingerprint detail instead of the carryover
    refusal, a promotion that answers anything but the named 409
    payload, an active whose receipts, journal, or field move during
    the observation window, and a control half that never converges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        root = Path(self.tmp.name) / 'controllers'
        self.incompatible_doc = Path(self.tmp.name) \
            / 'model-revised-incompatible.json'
        self.incompatible_doc.write_text(json.dumps(
            {'revised': True, 'retyped': 302}))
        self.control_doc = Path(self.tmp.name) / 'model-revised.json'
        self.control_doc.write_text(json.dumps({'revised': True}))
        self.journals = {name: root / name / 'journal.jsonl'
                         for name in ('a', 'b', 'c')}
        self.feed = IncompatibleFeed(self.journals,
                                     self.incompatible_doc,
                                     self.control_doc)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'plant_ctl': feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_revised': feed.start,
                'journal_files': {
                    'active': str(feed.journals['a']),
                    'standby': str(feed.journals['b']),
                    'revised': str(feed.journals['c'])}}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4):
            return scenarios.scenario_incompatible_revision(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_incompatible_revision,
                      scenarios.SCENARIOS)
        # Immediately ahead of the compatible case: the control's
        # promote leg belongs to scenario_model_revision in the same
        # run.
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_incompatible_revision) + 1,
            order.index(scenarios.scenario_model_revision))

    def test_clean_refusal_passes_and_orders_the_legs(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The incompatible launch, the refused promotion, then the
        # control relaunch — and no demote/promote of the field owner.
        kinds = [kind for kind, *_ in self.feed.calls]
        self.assertEqual(kinds, ['start_revised', 'promote',
                                 'start_revised'])
        self.assertEqual(self.feed.calls[0],
                         ('start_revised', 'standby', True))
        self.assertEqual(self.feed.calls[2],
                         ('start_revised', 'standby', False))
        self.assertEqual(self.feed.roles['b'], 'active')
        self.assertEqual(self.feed.roles['c'], 'standby')

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        journals2 = {name: Path(second_tmp.name) / 'controllers'
                     / name / 'journal.jsonl'
                     for name in ('a', 'b', 'c')}
        feed2 = IncompatibleFeed(journals2, self.incompatible_doc,
                                 self.control_doc)
        ctx2 = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'plant_ctl': feed2.plant_ctl,
                'evidence_dir': str(evidence2),
                'start_revised': feed2.start,
                'journal_files': {
                    'active': str(journals2['a']),
                    'standby': str(journals2['b']),
                    'revised': str(journals2['c'])}}
        record2 = self.run_scenario(feed=feed2, ctx=ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_silent_crossing_fails(self):
        # The contract break this case exists to catch: the
        # incompatible document converged reinitialized.
        self.feed.silent_cross = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reinitialized', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fingerprint_degrade_is_the_wrong_refusal(self):
        # The foreign-fingerprint degrade #483 exercises must not
        # satisfy this case — the detail lacks the carryover naming.
        self.feed.fingerprint_degrade = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_crossing_peer_is_inconclusive(self):
        self.feed.never_cross = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported a checkpoint crossing',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_succeeding_fails(self):
        self.feed.promote_succeeds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('409', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_the_named_detail_fails(self):
        self.feed.refusal_plain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_drift_fails(self):
        self.feed.receipts_grow = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_drift_fails(self):
        self.feed.journal_grows = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_fails(self):
        self.feed.field_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field', record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_degrading_fails(self):
        # The same document minus the retype must converge — a second
        # refusal names a rig defect, not the carryover violation.
        self.feed.control_degrades = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('control', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
