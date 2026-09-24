"""The 2400_doomed_startup_claim leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_doomed_startup_claim, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DoomedStartupClaimTests.test_registered_ahead_of_model_revision',
    'DoomedStartupClaimTests.test_fixed_shape_passes_validates_and_tears_down',
    'DoomedStartupClaimTests.test_two_runs_produce_identical_evidence',
    'DoomedStartupClaimTests.test_claim_then_die_strands_the_incumbent_fails',
    'DoomedStartupClaimTests.test_released_claim_still_disturbs_the_incumbent',
    'DoomedStartupClaimTests.test_unclaimed_window_fails',
    'DoomedStartupClaimTests.test_silently_writable_probe_fails',
    'DoomedStartupClaimTests.test_doomed_peer_serving_fails',
    'DoomedStartupClaimTests.test_journal_append_past_the_replay_fails',
    'DoomedStartupClaimTests.test_state_file_written_fails',
    'DoomedStartupClaimTests.test_partner_role_change_fails',
    'DoomedStartupClaimTests.test_incumbent_tick_stall_fails',
    'DoomedStartupClaimTests.test_field_writes_stall_fails',
    'DoomedStartupClaimTests.test_refused_command_fails',
    'DoomedStartupClaimTests.test_unlogged_receipt_fails',
    'DoomedStartupClaimTests.test_incumbent_silence_is_inconclusive',
    'DoomedStartupClaimTests.test_dead_incumbent_fails',
    'DoomedStartupClaimTests.test_failed_launch_action_is_inconclusive',
    'DoomedStartupClaimTests.test_failed_teardown_is_inconclusive',
    'DoomedStartupClaimTests.test_missing_seams_are_inconclusive',
    'DoomedStartupClaimTests.test_unreachable_plant_is_inconclusive',
    'DoomedStartupClaimTests.test_unwritable_model_is_inconclusive',
})


class DoomedStartupFeed:
    """A stubbed rig for the doomed-startup-claim scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — ctrl-a is its standby, and ctrl-f is the foreign peer
    `start` launches onto the runner-owned --journal-file the scenario
    just corrupted. The fixed shape dies inside the startup replay and
    never touches the plant's single-writer claim, so its monitor
    never answers and the journal file keeps the corrupt record as
    line 1; the pre-fix `claim_then_die` shape preempts the claim on
    its way out — a dead claim fencing the incumbent's next write,
    which demotes it — and `release_on_exit` hands the claim back,
    which still preempted the incumbent and leaves the field
    unclaimed. The plant answers the census, reads, and third-party
    `step` probes: fenced while a claim stands, unclaimed while none
    does. Each incumbent snapshot read is one completed scan landing
    its field write. Every transition is call-count keyed — never
    wall-clock — so two runs emit identical evidence. Fault flags
    stage each named failure the issue calls out."""

    WATCH = 100    # the field `out` point the window watches
    COMMAND = 302  # the writable bool in-point the receipt leg writes

    def __init__(self, journal, state, document):
        self.journal = Path(journal)
        self.state = Path(state)
        self.document = document
        self.ticks = {'a': 0, 'b': 40}
        self.field_tick = 7
        self.owner = 'b'            # the plant's standing claim owner
        self.demoted = False        # ctrl-b met the fence and demoted
        self.launched = False       # ctrl-f's container ran at all
        self.incumbent_down = False
        self.monitor_down = False
        self.open_field = False     # third-party steps slip the fence
        self.receipts = [{'command': {'write_value': {
            'point': self.COMMAND, 'kind': 'bool',
            'value': {'bool': True}}},
            'outcome': {'applied': {'tick': 30}},
            'actor': 'qa-lane'}]
        self.calls = []
        # Fault injection for the named-failure cases.
        self.claim_then_die = False   # the pre-fix stale-claim shape
        self.release_on_exit = False  # the preempting claim released
        self.unclaimed_field = False  # the claim silently evaporates
        self.serves = False           # f's monitor answers anyway
        self.journal_appends = False  # f ran past the failed replay
        self.leaves_state = False     # f persisted a checkpoint
        self.opens_field = False      # probes answer silently writable
        self.no_writable = False      # the model declares no bool target
        self.partner_promotes = False # ctrl-a reports a role change
        self.stall_incumbent = False  # ctrl-b's tick stops advancing
        self.field_stall = False      # its writes stop reaching the field
        self.command_refused = False  # it rejects the mid-window command
        self.receipt_lost = False     # the settled receipt never logged
        self.incumbent_dies = False   # its process stops answering
        self.monitor_lost = False     # only its monitor stops answering
        self.plant_down = False       # the plant refuses every probe
        self.launch_fails = False     # the start action raises
        self.stop_fails = False       # the teardown action raises

    # The runner-owned actions — replace ctx['start_foreign'] and
    # ctx['stop_foreign'].
    def start(self, name):
        self.calls.append(('start_foreign', name))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        if self.incumbent_dies:
            self.incumbent_down = True
        if self.monitor_lost:
            self.monitor_down = True
        if self.claim_then_die or self.release_on_exit:
            # The doomed startup's preemptive claim landed before its
            # replay failed — the incumbent is superseded whether the
            # dead claim stands (`claim_then_die`) or is handed back
            # (`release_on_exit`).
            self.demoted = True
            self.owner = 'f' if self.claim_then_die else None
        if self.unclaimed_field:
            self.owner = None
        if self.opens_field:
            self.open_field = True
        if self.journal_appends:
            with self.journal.open('a') as stream:
                stream.write(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        if self.leaves_state:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(json.dumps({'tick': 9}) + '\n')
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
            role = ('active'
                    if self.partner_promotes and self.launched
                    else 'standby')
            report = {'role': role, 'tick': self.ticks['a']}
            if role == 'standby':
                report['sync'] = 'unsynchronized'
            return report
        if peer == 'b':
            role = 'standby' if self.demoted else 'active'
            report = {'role': role, 'tick': self.ticks['b']}
            if role == 'standby':
                report['sync'] = 'unsynchronized'
            return report
        return {'role': 'standby', 'tick': 0, 'sync': 'unsynchronized'}

    def _snapshot(self):
        # One completed scan: the incumbent's write lands on the field
        # only while its claim stands — a preempted or stalled writer
        # leaves the field tick where the claim died.
        if not self.stall_incumbent:
            self.ticks['b'] += 1
            if not self.demoted and not self.field_stall:
                self.field_tick = self.ticks['b']
        return {'tick': self.ticks['b'],
                'points': [{'point': self.WATCH,
                            'sample': {'value': {'bool': True},
                                       'quality': 'good'}}]}

    def _command(self, body):
        if self.demoted or self.command_refused:
            return 200, {'command': body.get('command'),
                         'outcome': {'rejected': {
                             'reason': {'not_active': None}}},
                         'actor': body.get('actor')}
        receipt = {'command': body.get('command'),
                   'outcome': {'accepted': {
                       'apply_tick': self.ticks['b'] + 1}},
                   'actor': body.get('actor')}
        if not self.receipt_lost:
            self.receipts.append(receipt)
        return 200, receipt

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-f:4': 'f'}.get(host)
        if peer is None \
                or (peer == 'b' and (
                    self.incumbent_down or self.monitor_down)) \
                or (peer == 'f' and not (self.launched and self.serves)):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if peer != 'b':
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/signals'):
            points = [{'point': 10, 'signal': 10010,
                       'name': 'level-primary', 'direction': 'in',
                       'value_type': 'float', 'writable': False}]
            if not self.no_writable:
                points.insert(0, {'point': self.COMMAND,
                                  'signal': 10302, 'name': 'p101-oos',
                                  'direction': 'in',
                                  'value_type': 'bool',
                                  'writable': True})
            return 200, {'points': points}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot()
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('POST', '/command'):
            return self._command(body)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant wire protocol — replaces scenarios._plant_probe.
    def plant_request(self, ctx, request, timeout=5):
        if self.plant_down:
            raise urllib.error.URLError('connection refused')
        if self.monitor_down and self.owner == 'b' \
                and not self.field_stall:
            # A monitor-less incumbent keeps scanning — the field
            # tick moves between the window's reads.
            self.field_tick += 1
        if request['op'] == 'list_points':
            sample = {'value': {'bool': True}, 'quality': 'good',
                      'tick': self.field_tick}
            return {'result': 'points', 'points': [
                {'point': 10, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': self.WATCH, 'direction': 'out',
                 'sample': sample, 'fault': None}]}
        if request['op'] == 'read':
            return {'result': 'sample',
                    'sample': {'value': {'bool': True},
                               'quality': 'good',
                               'tick': self.field_tick}}
        if request['op'] == 'step':
            if self.open_field:
                # The silently-writable field: a third attachment's
                # mutation applied under no fencing verdict at all.
                return {'result': 'stepped', 'tick': self.field_tick}
            if self.owner is None:
                return {'result': 'error',
                        'error': {'kind': 'unclaimed',
                                  'detail': 'no attachment holds '
                                            'field writes'}}
            return {'result': 'error',
                    'error': {'kind': 'fenced',
                              'detail': 'another attachment owns '
                                        'field writes'}}
        raise AssertionError('unexpected plant request %s' % request)

    # The shipped plant tool — replaces ctx['plant_ctl'] for the
    # covered subcommands (the census and the field reads); the bare
    # `step` fencing probes stay on the _plant_probe patch above.
    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.plant_request(None, request), *args)


class DoomedStartupClaimTests(unittest.TestCase):
    """scenario_doomed_startup_claim against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the pre-fix stale claim that demotes the incumbent, the
    released claim's unclaimed window, the silently writable field,
    the doomed peer that serves or journaled past its failed replay,
    and every incumbent disturbance the ordering fix exists to
    prevent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        directory = Path(self.tmp.name) / 'controllers' / 'foreign'
        self.journal = directory / 'journal.jsonl'
        self.state = directory / 'state.json'
        self.document = Path(self.tmp.name) / 'model-foreign.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.feed = DoomedStartupFeed(self.journal, self.state,
                                      self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'plant': '127.0.0.1:9',
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_foreign': self.feed.start,
                'stop_foreign': self.feed.stop,
                'state_files': {'foreign': str(self.state)},
                'journal_files': {'foreign': str(self.journal)}}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_plant_probe',
                             feed.plant_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DOOMED_STARTUP_POLL', 0.001):
            return scenarios.scenario_doomed_startup_claim(
                ctx or self._ctx())

    def test_registered_ahead_of_model_revision(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_checkpoint_negotiation),
            order.index(scenarios.scenario_doomed_startup_claim))
        self.assertLess(
            order.index(scenarios.scenario_doomed_startup_claim),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds, ['start_foreign', 'stop_foreign'])
        self.assertEqual(dict(
            call for call in self.feed.calls if len(call) == 2
        )['start_foreign'], 'standby')
        self.assertFalse(self.feed.launched)
        # The induction artifact is gone — the seat is clean for the
        # revision cases that follow.
        self.assertFalse(self.journal.exists())

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        directory2 = Path(second_tmp.name) / 'controllers' / 'foreign'
        feed2 = DoomedStartupFeed(directory2 / 'journal.jsonl',
                                  directory2 / 'state.json',
                                  self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_foreign'] = feed2.start
        ctx2['stop_foreign'] = feed2.stop
        ctx2['state_files'] = {'foreign': str(directory2
                                            / 'state.json')}
        ctx2['journal_files'] = {'foreign': str(directory2
                                              / 'journal.jsonl')}
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_claim_then_die_strands_the_incumbent_fails(self):
        # The pre-fix shape: the doomed startup claimed before its
        # replay failed — the dead claim fences the incumbent into a
        # demotion only a restart could clear.
        self.feed.claim_then_die = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_released_claim_still_disturbs_the_incumbent(self):
        # A claim handed back on the way out still preempted the
        # incumbent mid-window — the field's unclaimed window and the
        # demotion are the disturbance either way.
        self.feed.release_on_exit = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unclaimed_window_fails(self):
        self.feed.unclaimed_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unclaimed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silently_writable_probe_fails(self):
        self.feed.opens_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fencing', record.get('detail', ''))
        report.validate_scenario(record)

    def test_doomed_peer_serving_fails(self):
        # A monitor that answers never ran the startup replay its
        # corrupt journal file demanded.
        self.feed.serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('served its monitor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_append_past_the_replay_fails(self):
        self.feed.journal_appends = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ran past the failed replay',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_file_written_fails(self):
        self.feed.leaves_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persisted a checkpoint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_partner_role_change_fails(self):
        self.feed.partner_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('spurious role change',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_tick_stall_fails(self):
        self.feed.stall_incumbent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_writes_stall_fails(self):
        self.feed.field_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_command_fails(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refused the mid-window command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unlogged_receipt_fails(self):
        self.feed.receipt_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log never carried',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_silence_is_inconclusive(self):
        # The incumbent's monitor refusing every poll while its scans
        # keep landing field writes is unreachable evidence, not a
        # disturbance.
        self.feed.monitor_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered during the window',
                      record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_dead_incumbent_fails(self):
        # A halted incumbent — monitor silent AND its field writes
        # stopped — is the disturbance itself, whatever caused it.
        self.feed.incumbent_dies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_failed_launch_action_is_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)
        # The induction artifact still comes back out.
        self.assertFalse(self.journal.exists())

    def test_failed_teardown_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never removed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        del ctx['start_foreign']
        del ctx['stop_foreign']
        del ctx['foreign']
        del ctx['journal_files']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unreachable_plant_is_inconclusive(self):
        self.feed.plant_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unwritable_model_is_inconclusive(self):
        # No writable bool in the served model — the receipted-path
        # leg cannot run, so the case reports inconclusive rather than
        # skipping the leg.
        self.feed.no_writable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
