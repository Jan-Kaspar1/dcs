"""The 2300_checkpoint_negotiation leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_checkpoint_negotiation, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'CheckpointNegotiationTests.test_registered_ahead_of_model_revision',
    'CheckpointNegotiationTests.test_clean_refusal_passes_validates_and_tears_down',
    'CheckpointNegotiationTests.test_two_runs_produce_identical_evidence',
    'CheckpointNegotiationTests.test_never_refusing_peer_is_inconclusive',
    'CheckpointNegotiationTests.test_converged_foreign_peer_fails',
    'CheckpointNegotiationTests.test_other_rejection_detail_fails',
    'CheckpointNegotiationTests.test_detail_omitting_the_pair_fingerprint_fails',
    'CheckpointNegotiationTests.test_convergence_inside_the_window_fails',
    'CheckpointNegotiationTests.test_promotion_accepted_fails',
    'CheckpointNegotiationTests.test_other_409_refusal_fails',
    'CheckpointNegotiationTests.test_refusal_without_the_sync_state_fails',
    'CheckpointNegotiationTests.test_active_tick_stall_fails',
    'CheckpointNegotiationTests.test_field_regression_during_the_window_fails',
    'CheckpointNegotiationTests.test_receipt_drift_fails',
    'CheckpointNegotiationTests.test_failed_launch_action_is_inconclusive',
    'CheckpointNegotiationTests.test_failed_teardown_is_inconclusive',
    'CheckpointNegotiationTests.test_missing_actions_are_inconclusive',
})


class NegotiationFeed:
    """A stubbed rig for the checkpoint-negotiation scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — ctrl-a is its unsynchronized demoted partner, and ctrl-f
    is the foreign peer the runner action launches on the derived
    document without --revised: every pull produces a checkpoint the
    peer refuses, so once its monitor answers it reports the named
    degraded negotiation failure and never converges, and POST
    /promote on it answers the 409 not_converged refusal. Each ctrl-b
    snapshot read is one completed scan landing its staged write on
    the faked sim-net field. Every transition is call-count keyed —
    never wall-clock — so two scenario runs emit identical evidence.
    Fault flags stage each named failure the issue calls out."""

    V1, V2 = 111, 222  # the pair's and the foreign model fingerprints
    WATCH = 100        # the field `out` point the window watches

    def __init__(self, document):
        self.document = document
        self.ticks = {'a': 0, 'b': 40, 'f': 0}
        self.launched = False   # f's monitor answers
        self.f_polls = 0
        self.degrade_after = 2  # f's first polls stay unsynchronized
        self.promote_seen = False
        self.staged = True      # the value ctrl-b's scans write
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.receipts = {'b': [
            {'command': {'write_value': {'point': 302, 'kind': 'bool',
                                         'value': {'bool': True}}},
             'outcome': {'applied': {'tick': 30}},
             'actor': 'qa-lane'}]}
        self.calls = []
        # Fault injection for the named-failure cases.
        self.action_fails = False   # the launch action raises
        self.stop_fails = False     # the teardown action raises
        self.never_refuse = False   # f stays unsynchronized
        self.converges = False      # f reports tracking — violation
        self.late_converge = False  # f leaves degraded mid-window
        self.wrong_detail = False   # degraded names another rejection
        self.wrong_fp = False       # the detail omits the pair's fp
        self.promote_ok = False     # promote answers 200
        self.promote_other = False  # 409 without not_converged
        self.refusal_bare = False   # not_converged without the sync
        self.stall_active = False   # the active's tick stops advancing
        self.field_regress = False  # the field stops following writes
        self.receipt_drift = False  # a receipt appears after promote

    def _sync(self):
        if self.wrong_detail:
            return {'degraded': {
                'detail': 'checkpoint carries output point 9 the '
                          'point map does not serve as out'}}
        found = self.V1 + 1 if self.wrong_fp else self.V1
        return {'degraded': {
            'detail': 'checkpoint model fingerprint %016x does not '
                      "match this run's %016x" % (found, self.V2)}}

    # The runner-owned actions — replace ctx['start_foreign'] and
    # ctx['stop_foreign'].
    def start(self, name):
        self.calls.append(('start_foreign', name))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        return {'container': 'dcs-hw-qa-1-foreign',
                'document': str(self.document),
                'added_points': [900], 'added_signals': [10900]}

    def stop(self):
        self.calls.append(('stop_foreign',))
        if self.stop_fails:
            raise RuntimeError('docker rm failed: no such container')
        self.launched = False  # the foreign endpoint stops answering

    def _role(self, peer):
        if peer == 'a':
            return {'role': 'standby', 'tick': self.ticks['a'],
                    'sync': 'unsynchronized'}
        if peer == 'b':
            return {'role': 'active', 'tick': self.ticks['b']}
        self.f_polls += 1
        self.ticks['f'] += 1
        if self.never_refuse or self.f_polls < self.degrade_after:
            sync = 'unsynchronized'
        elif self.converges or (
                self.late_converge
                and self.f_polls >= self.degrade_after + 3):
            sync = {'tracking': {'aligned': self.ticks['b']}}
        else:
            sync = self._sync()
        return {'role': 'standby', 'tick': self.ticks['f'],
                'sync': sync}

    def _snapshot(self, peer):
        if peer == 'b':
            if not self.stall_active:
                self.ticks['b'] += 1
                if not self.field_regress:
                    self.field = {'value': {'bool': self.staged},
                                  'quality': 'good',
                                  'tick': self.ticks['b']}
        else:
            self.ticks[peer] += 1
        return {'tick': self.ticks[peer],
                'points': [{'point': self.WATCH,
                            'sample': {'value': {'bool': self.staged},
                                       'quality': 'good'}}]}

    def _promote(self, url, peer):
        self.calls.append(('promote', peer))
        self.promote_seen = True
        if self.promote_ok:
            return 200, {'role': 'active', 'tick': self.ticks[peer]}
        if self.promote_other:
            payload = {'already_active': None}
        elif self.refusal_bare:
            payload = {'not_converged': {'sync': 'unsynchronized'}}
        else:
            payload = {'not_converged': {'sync': self._sync()}}
        raise urllib.error.HTTPError(
            url, 409, 'conflict', {},
            io.BytesIO(json.dumps(payload).encode()))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-f:4': 'f'}.get(host)
        if peer is None or (peer == 'f' and not self.launched):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/checkpoint'):
            fp = self.V1 if peer != 'f' else self.V2
            return 200, {'model_fingerprint': fp,
                         'tick': self.ticks[peer]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            receipts = list(self.receipts.get(peer, []))
            if peer == 'b' and self.receipt_drift \
                    and self.promote_seen:
                receipts.append(
                    {'command': {'write_value': {
                        'point': 302, 'kind': 'bool',
                        'value': {'bool': False}}},
                     'outcome': {'applied': {'tick': self.ticks['b']}},
                     'actor': 'qa-lane'})
            return 200, receipts
        if (method, route) == ('POST', '/promote'):
            return self._promote(url, peer)
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
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


class CheckpointNegotiationTests(unittest.TestCase):
    """scenario_checkpoint_negotiation against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the foreign peer that never reports the refusal, converges anyway,
    reports a different rejection, accepts promotion, or leaves the
    active peer's ticks, field writes, or receipt log disturbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.document = Path(self.tmp.name) / 'model-foreign.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.feed = NegotiationFeed(self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'plant': 'plant:9',
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_foreign': self.feed.start,
                'stop_foreign': self.feed.stop}

    def run_scenario(self, ctx=None):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_POLL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'NEGOTIATION_ROUNDS', 6):
            return scenarios.scenario_checkpoint_negotiation(
                ctx or self._ctx())

    def test_registered_ahead_of_model_revision(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_checkpoint_negotiation),
            order.index(scenarios.scenario_model_revision))

    def test_clean_refusal_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds,
                         ['start_foreign', 'promote', 'stop_foreign'])
        self.assertEqual(dict(
            call for call in self.feed.calls if len(call) == 2
        )['start_foreign'], 'standby')
        self.assertFalse(self.feed.launched)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = NegotiationFeed(self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_foreign'] = feed2.start
        ctx2['stop_foreign'] = feed2.stop
        with patch.object(scenarios, 'http_json', feed2.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_POLL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'NEGOTIATION_ROUNDS', 6):
            record2 = scenarios.scenario_checkpoint_negotiation(ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_never_refusing_peer_is_inconclusive(self):
        self.feed.never_refuse = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reached a negotiation verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)
        # Teardown still ran — the rig stays clean either way.
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_converged_foreign_peer_fails(self):
        self.feed.converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_other_rejection_detail_fails(self):
        self.feed.wrong_detail = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the fingerprint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_detail_omitting_the_pair_fingerprint_fails(self):
        self.feed.wrong_fp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not name the pair', record.get('detail', ''))
        report.validate_scenario(record)

    def test_convergence_inside_the_window_fails(self):
        self.feed.late_converge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('left the refused state',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_accepted_fails(self):
        self.feed.promote_ok = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record.get('detail', ''))
        report.validate_scenario(record)
        # Teardown still ran even on the failed leg.
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_other_409_refusal_fails(self):
        self.feed.promote_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_the_sync_state_fails(self):
        self.feed.refusal_bare = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not carry the degraded',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_active_tick_stall_fails(self):
        self.feed.stall_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_during_the_window_fails(self):
        self.feed.field_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_drift_fails(self):
        self.feed.receipt_drift = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_launch_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_teardown_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never removed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_actions_are_inconclusive(self):
        ctx = self._ctx()
        del ctx['start_foreign']
        del ctx['stop_foreign']
        del ctx['foreign']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
