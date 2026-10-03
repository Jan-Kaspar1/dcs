"""The 2600_model_revision leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_model_revision, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ModelRevisionTests.test_registered_in_scenarios',
    'ModelRevisionTests.test_clean_roll_passes_validates_and_orders_the_roll',
    'ModelRevisionTests.test_two_runs_produce_identical_evidence',
    'ModelRevisionTests.test_never_converging_revised_peer_is_inconclusive',
    'ModelRevisionTests.test_wrong_convergence_state_fails',
    'ModelRevisionTests.test_persistent_degraded_pulls_fail',
    'ModelRevisionTests.test_carryover_missing_the_operator_write_fails',
    'ModelRevisionTests.test_receipt_loss_across_the_roll_fails',
    'ModelRevisionTests.test_field_regression_in_the_gap_fails',
    'ModelRevisionTests.test_field_regression_after_promotion_fails',
    'ModelRevisionTests.test_demoted_peer_serving_writes_fails',
    'ModelRevisionTests.test_run_ending_off_the_revised_fingerprint_fails',
    'ModelRevisionTests.test_missing_reinitialized_journal_entry_fails',
    'ModelRevisionTests.test_demoted_journal_lifetime_restart_fails',
    'ModelRevisionTests.test_failed_action_is_inconclusive',
})


class RevisionFeed:
    """A stubbed rig for the model-revision scenario. ctrl-b owns the
    field — the post-failover layout the suite reaches this case in —
    ctrl-a is its demoted partner, and ctrl-c is the third controller
    the runner action launches on the revised document. Every monitor
    read on a peer is one completed scan: an active peer's scan lands
    its staged field write on the faked sim-net plant. Each peer's
    --journal-file is a real append-only record the feed writes itself.
    Every transition is call-count keyed — never wall-clock — so two
    scenario runs emit identical evidence. Fault flags stage each named
    failure the issue calls out."""

    V1, V2 = 111, 222  # the mounted and revised model fingerprints
    POINT = 302        # the writable bool the carryover must name
    WATCH = 100        # the field `out` point the roll watches

    def __init__(self, journals, document):
        self.journals = {name: Path(p) for name, p in journals.items()}
        self.document = document
        self.ticks = {'a': 0, 'b': 40, 'c': 0}
        self.roles = {'a': 'standby', 'b': 'active', 'c': 'standby'}
        self.launched = False
        self.c_role_polls = 0
        self.converge_after = 3
        self.reinit_journaled = False
        self.point = False
        self.receipts = {'a': [], 'b': [], 'c': []}
        self.staged = {'a': True, 'b': True, 'c': True}
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.health = {'b': {'failed_writes': 0,
                             'consecutive_failures': 0,
                             'last_error': None}}
        self.seqs = {'a': 1, 'b': 1, 'c': 1}
        self.calls = []
        self.gap_open = False
        self.gap_reads = 0
        # Fault injection for the named-failure cases.
        self.action_fails = False      # the runner action raises
        self.never_converge = False    # c stays unsynchronized
        self.bad_convergence = None    # 'degraded'|'diverged'|'tracking'
        self.lose_receipts = False     # the adopted log drops the tail
        self.empty_carry = False       # the report names no carried point
        self.regress_field = False     # the field moves in the gap
        self.field_lag = False         # post-roll writes never land
        self.demoted_writes = False    # b keeps attempting writes
        self.wrong_fp = False          # c's checkpoint keeps v1's fp
        self.no_reinit_entry = False   # c's journal lacks the crossing
        self.demoted_restart = False   # b's journal gains a lifetime
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
        carried = [] if self.empty_carry else [
            {'point': self.POINT, 'value': {'bool': self.point}}]
        return {'from': self.V1, 'to': self.V2,
                'resumed_at': self.ticks['b'],
                'carried': carried,
                'carried_outputs': [{'point': self.WATCH,
                                     'value': {'bool': True}}],
                'carried_forces': [], 'dropped': [],
                'reinitialized': ['motor:20', 'digital-input:12'],
                'initialized': [900]}

    # The runner-owned action — replaces ctx['start_revised'].
    def start(self, name):
        self.calls.append(('start_revised', name))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        self.journals['c'].parent.mkdir(parents=True, exist_ok=True)
        self._journal('c', {'run_boundary': {'run': 1, 'tick': 0}})
        return {'container': 'dcs-hw-qa-1-c',
                'document': str(self.document),
                'added_points': [900], 'added_signals': [10900]}

    def _role(self, peer):
        if peer != 'c':
            return {'role': self.roles[peer], 'tick': self.ticks[peer],
                    'sync': 'unsynchronized'
                    if self.roles[peer] == 'standby' else None}
        self.c_role_polls += 1
        if self.never_converge:
            sync = 'unsynchronized'
        elif self.bad_convergence:
            sync = {self.bad_convergence: {'detail': 'staged'}}
        elif self.c_role_polls >= self.converge_after:
            sync = {'reinitialized': {'report': self._report()}}
            if not self.reinit_journaled:
                self.reinit_journaled = True
                # The crossing journals its carryover report and the
                # adopted receipt log arrives with the checkpoint.
                if not self.no_reinit_entry:
                    self._entry('c', {'reinitialized':
                                      {'report': self._report()}})
                receipts = list(self.receipts['b'])
                if self.lose_receipts:
                    receipts = receipts[:-1]
                self.receipts['c'] = receipts
        else:
            sync = 'unsynchronized'
        return {'role': self.roles['c'], 'tick': self.ticks['c'],
                'sync': sync}

    def _snapshot(self, peer):
        self.ticks[peer] += 1
        if self.roles[peer] == 'active' and not (
                peer == 'c' and self.field_lag):
            self.field = {'value': {'bool': self.staged[peer]},
                          'quality': 'good',
                          'tick': self.ticks[peer]}
        if peer == 'b' and self.demoted_writes \
                and self.roles['b'] == 'standby':
            # A demoted peer still attempting writes meets the plant's
            # fence, which counts each rejection in its io_health.
            self.health['b']['failed_writes'] += 1
            self.health['b']['consecutive_failures'] += 1
            self.health['b']['last_error'] = {
                'tick': self.ticks['b'], 'point': self.WATCH,
                'direction': 'out', 'error': {'fenced': None}}
        health = dict(self.health.get(
            peer, {'failed_writes': 0, 'consecutive_failures': 0,
                   'last_error': None}))
        return {'tick': self.ticks[peer],
                'points': [
                    {'point': self.WATCH,
                     'sample': {'value': {'bool': self.staged[peer]},
                                'quality': 'good'}},
                    {'point': self.POINT,
                     'sample': {'value': {'bool': self.point},
                                'quality': 'good'}}],
                'io_health': health}

    def _command(self, peer, body):
        write = body['command']['write_value']
        if write['point'] == self.POINT:
            self.point = write['value']['bool']
        receipt = {'command': body['command'],
                   'outcome': {'applied': {'tick': self.ticks[peer]}},
                   'actor': body.get('actor')}
        self.receipts[peer].append(receipt)
        self._entry(peer, {'command_settled': {'receipt': receipt}})
        return 200, receipt

    def _demote(self, peer):
        self.calls.append(('demote', peer))
        self.gap_open = True
        self.roles[peer] = 'standby'
        self._entry(peer, {'role_changed': {'from': 'active',
                                            'to': 'demoting'}})
        self._entry(peer, {'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
        if self.demoted_restart:
            self._journal(peer, {'run_boundary': {'run': 2,
                                                  'tick': 0}})
        return 200, {'role': 'standby', 'tick': self.ticks[peer]}

    def _promote(self, peer):
        self.calls.append(('promote', peer))
        self.gap_open = False
        self.roles[peer] = 'active'
        self._entry(peer, {'role_changed': {'from': 'standby',
                                            'to': 'promoting'}})
        self._entry(peer, {'role_changed': {'from': 'promoting',
                                            'to': 'active'}})
        return 200, {'role': 'active', 'tick': self.ticks[peer]}

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
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 10, 'signal': 10010, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/checkpoint'):
            fp = self.V1 if peer != 'c' or self.wrong_fp else self.V2
            return 200, {'model_fingerprint': fp,
                         'tick': self.ticks[peer]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts[peer])
        if (method, route) == ('POST', '/command'):
            return self._command(peer, body)
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
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
                 'sample': self.field, 'fault': None},
                {'point': 10, 'direction': 'in',
                 'sample': {'value': {'float': 1.5},
                            'quality': 'good', 'tick': 7},
                 'fault': None}]}
        if request['op'] == 'read':
            if self.gap_open:
                self.gap_reads += 1
            if self.regress_field and self.gap_open \
                    and self.gap_reads > 1:
                # The named failure: the field moves inside the
                # writer-less window — after the scenario's held read.
                self.regress_field = False
                self.field = {'value': {'bool': not self.field
                                        ['value']['bool']},
                              'quality': 'good', 'tick': 9}
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


class ModelRevisionTests(unittest.TestCase):
    """scenario_model_revision against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the revised peer that never converges, the wrong terminal sync,
    receipts or journal seqs lost across the boundary, a field
    regression, a demoted peer still serving writes, and a run that
    ends off the revised fingerprint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        root = Path(self.tmp.name) / 'controllers'
        self.document = Path(self.tmp.name) / 'model-revised.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.journals = {name: root / name / 'journal.jsonl'
                         for name in ('a', 'b', 'c')}
        self.feed = RevisionFeed(self.journals, self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_revised': self.feed.start,
                'journal_files': {
                    'active': str(self.journals['a']),
                    'standby': str(self.journals['b']),
                    'revised': str(self.journals['c'])}}

    def run_scenario(self):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_SETTLE_DEADLINE',
                             0.5), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4), \
                patch.object(scenarios, 'RESTART_JOURNAL_DEADLINE',
                             0.5):
            return scenarios.scenario_model_revision(self._ctx())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_model_revision,
                      scenarios.SCENARIOS)

    def test_clean_roll_passes_validates_and_orders_the_roll(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented order: the third controller launches first,
        # the old active demotes, and only then the revised peer
        # promotes.
        kinds = [kind for kind, _ in self.feed.calls]
        self.assertEqual(kinds,
                         ['start_revised', 'demote', 'promote'])
        self.assertEqual(dict(self.feed.calls)['demote'], 'b')
        self.assertEqual(dict(self.feed.calls)['promote'], 'c')
        self.assertEqual(self.feed.roles['b'], 'standby')
        self.assertEqual(self.feed.roles['c'], 'active')

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
        feed2 = RevisionFeed(journals2, self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_revised'] = feed2.start
        ctx2['journal_files'] = {
            'active': str(journals2['a']),
            'standby': str(journals2['b']),
            'revised': str(journals2['c'])}
        with patch.object(scenarios, 'http_json', feed2.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_SETTLE_DEADLINE',
                             0.5), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4), \
                patch.object(scenarios, 'RESTART_JOURNAL_DEADLINE',
                             0.5):
            record2 = scenarios.scenario_model_revision(ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_never_converging_revised_peer_is_inconclusive(self):
        self.feed.never_converge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_convergence_state_fails(self):
        self.feed.bad_convergence = 'tracking'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reinitialized', record.get('detail', ''))
        report.validate_scenario(record)

    def test_persistent_degraded_pulls_fail(self):
        # A degraded pull that never clears is the revised peer's
        # rejected crossing — terminal at the convergence deadline.
        self.feed.bad_convergence = 'degraded'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('degraded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_carryover_missing_the_operator_write_fails(self):
        self.feed.empty_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover report', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_loss_across_the_roll_fails(self):
        self.feed.lose_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_in_the_gap_fails(self):
        self.feed.regress_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('writer-less window', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_after_promotion_fails(self):
        self.feed.staged['c'] = False
        self.feed.field_lag = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed across the roll',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_demoted_peer_serving_writes_fails(self):
        self.feed.demoted_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('serving writes', record.get('detail', ''))
        report.validate_scenario(record)

    def test_run_ending_off_the_revised_fingerprint_fails(self):
        self.feed.wrong_fp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fingerprint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_reinitialized_journal_entry_fails(self):
        self.feed.no_reinit_entry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demoted_journal_lifetime_restart_fails(self):
        self.feed.demoted_restart = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('lifetimes', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('model-revision action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
