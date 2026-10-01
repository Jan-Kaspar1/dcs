"""The 1700_standby_loss leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_standby_loss, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'StandbyLossTests.test_clean_run_passes_and_validates',
    'StandbyLossTests.test_refusal_named_wrong_fails',
    'StandbyLossTests.test_refused_write_reaching_the_field_fails',
    'StandbyLossTests.test_refused_write_in_receipt_log_fails',
    'StandbyLossTests.test_refused_write_on_active_journal_fails',
    'StandbyLossTests.test_refusal_journaled_misnamed_fails',
    'StandbyLossTests.test_stalled_scan_while_standby_down_fails',
    'StandbyLossTests.test_role_move_while_standby_down_fails',
    'StandbyLossTests.test_refused_window_command_fails',
    'StandbyLossTests.test_unsettled_window_command_fails',
    'StandbyLossTests.test_unjournaled_window_command_fails',
    'StandbyLossTests.test_spurious_role_entry_fails',
    'StandbyLossTests.test_stop_not_holding_is_inconclusive',
    'StandbyLossTests.test_failing_stop_action_is_inconclusive',
    'StandbyLossTests.test_failing_start_action_is_inconclusive',
    'StandbyLossTests.test_missing_lifecycle_actions_are_inconclusive',
    'StandbyLossTests.test_standby_never_returns_is_inconclusive',
    'StandbyLossTests.test_standby_never_tracks_fails',
    'StandbyLossTests.test_promotion_without_gate_refusal_fails',
    'StandbyLossTests.test_promotion_gate_other_refusal_fails',
    'StandbyLossTests.test_tracking_promotion_refused_fails',
    'StandbyLossTests.test_restore_demote_refused_fails',
    'StandbyLossTests.test_pair_never_resettling_fails',
    'StandbyLossTests.test_two_runs_produce_identical_evidence',
})


class StandbyLossFeed:
    """A stubbed pair for the standby-loss scenario. ctrl-a owns the
    field — every GET on it is one completed scan: the tick advancing,
    a queued command applying at the boundary, its settlement
    journaled — while ctrl-b is the tracking standby the legs stop,
    lose, and return. Each peer's --journal-file is a real append-only
    record the feed writes: a command_settled echo for every receipt
    each side records — the standby's not_active refusals included —
    and a role_changed entry for each transition. After a start the
    returned standby's cadence converges it to tracking across its
    first /role polls, and POST /promote answers the cadence's
    standing — not_converged before the first applied transfer — so
    the scenario's first answered promote is deterministically the
    refusal. Every transition is call-count keyed — never wall-clock —
    so two scenario runs emit identical evidence. Fault flags stage
    each named failure the issue calls out."""

    def __init__(self, journal_a, journal_b):
        self.tick = 200
        self.point = False
        self.pending = []            # ctrl-a's accepted receipts
        self.receipts_a = []         # ctrl-a's adopted receipt log
        self.receipts_b = []         # ctrl-b's adopted receipt log
        self.a_role = 'active'
        self.a_sync = None           # standing while a is a standby
        self.a_track_left = 0        # /role polls until a demoted a tracks
        self.b_role = 'standby'
        self.b_sync = 'tracking'
        self.b_up = True
        self.b_track_left = 0        # /role polls until a returned b tracks
        self.stops = []
        self.starts = []
        self.seq_a = 1
        self.seq_b = 1
        self.path_a = Path(journal_a)
        self.path_b = Path(journal_b)
        self._append(self.path_a, {'run_boundary': {'run': 1, 'tick': 0}})
        self._append(self.path_b, {'run_boundary': {'run': 1, 'tick': 0}})
        # Fault injection — each named failure the issue calls out.
        self.refusal_wrong = False    # the standby answers not not_active
        self.refusal_applies = False  # the refused write reaches the field
        self.leak_receipts = False    # the refused write enters a receipt log
        self.phantom_journal = False  # the refused write lands on a's journal
        self.misnamed_journal = False # b journals it as something else
        self.stall = False            # a's tick stops while b is down
        self.role_move = False        # a reports non-active while b is down
        self.command_refused = False  # the window command is rejected
        self.command_lost = False     # the window command never settles
        self.command_unjournaled = False  # the settle never journals
        self.spurious_role = False    # a's journal gains role_changed mid-window
        self.stop_leaks = False       # b keeps answering while "down"
        self.stop_fails = False       # the stop action raises
        self.start_fails = False      # the start action raises
        self.never_returns = False    # b's monitor never answers again
        self.never_tracks = False     # b answers but never converges
        self.gate_open = False        # the first promote answers 200
        self.gate_other = False       # the first promote answers another refusal
        self.promote_refuses = False  # a tracking promote answers a refusal
        self.demote_refuses = False   # a demote answers a refusal
        self.never_settles = False    # the pair never settles post-restore

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry_a(self, event):
        entry = {'seq': self.seq_a, 'tick': self.tick, 'event': event}
        self._append(self.path_a, {'entry': entry})
        self.seq_a += 1

    def _entry_b(self, event):
        entry = {'seq': self.seq_b, 'tick': self.tick, 'event': event}
        self._append(self.path_b, {'entry': entry})
        self.seq_b += 1

    def _conflict(self, url, payload):
        return urllib.error.HTTPError(
            url, 409, 'conflict', {},
            io.BytesIO(json.dumps(payload).encode()))

    def _scan_a(self):
        """One completed scan on the field writer — the tick advances
        and the boundary's command phase applies the queued receipts,
        journaling each settlement."""
        if not (self.stall and not self.b_up):
            self.tick += 1
        if self.command_lost:
            return
        for receipt in self.pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            new = write['value']['bool']
            if new != self.point:
                self._entry_a({'point_changed': {
                    'point': write['point'],
                    'from': {'bool': self.point},
                    'to': {'bool': new}}})
                self.point = new
            if not self.command_unjournaled:
                self._entry_a({'command_settled': {'receipt': receipt}})
        self.pending = []

    def _demote_a(self):
        self.a_role = 'standby'
        self.a_sync = 'unsynchronized'
        self.a_track_left = 2
        self._entry_a({'role_changed': {'from': 'active',
                                        'to': 'standby'}})

    def _demote_b(self):
        self.b_role = 'standby'
        self.b_sync = 'unsynchronized'
        self.b_track_left = 10 ** 9 if self.never_settles else 2
        self._entry_b({'role_changed': {'from': 'active',
                                        'to': 'standby'}})

    def _a_poll(self):
        # The demoted writer's paced pulls converge it to tracking —
        # each /role poll models one tracking cadence.
        if self.a_role == 'standby' and self.a_track_left > 0:
            self.a_track_left -= 1
            if self.a_track_left == 0:
                self.a_sync = 'tracking'

    def _b_poll(self):
        if self.b_role == 'standby' and self.b_track_left > 0:
            self.b_track_left -= 1
            if self.b_track_left == 0 and not self.never_tracks:
                self.b_sync = 'tracking'

    def _b_sync_report(self):
        return self.b_sync

    # The runner-owned lifecycle actions — replace ctx['stop_controller']
    # and ctx['start_controller']: the rig launches with --restart no, so
    # the stop holds a real down-window until the start.
    def stop(self, name):
        self.stops.append(name)
        if self.stop_fails:
            raise RuntimeError('docker stop failed: no such container')
        if self.spurious_role:
            self._entry_a({'role_changed': {'from': 'active',
                                            'to': 'standby'}})
        if not self.stop_leaks:
            self.b_up = False

    def start(self, name):
        self.starts.append(name)
        if self.start_fails:
            raise RuntimeError('docker start failed: no such container')
        if self.never_returns:
            return
        self.b_up = True
        self.b_role = 'standby'
        self.b_sync = 'unsynchronized'
        self.b_track_left = 3

    def _a(self, method, route, url, body):
        if method == 'GET':
            self._scan_a()
        if (method, route) == ('GET', '/role'):
            report = {'role': self.a_role, 'tick': self.tick}
            if self.a_role == 'standby':
                self._a_poll()
                report['sync'] = self.a_sync or 'unsynchronized'
            if self.role_move and not self.b_up \
                    and self.a_role == 'active':
                report['role'] = 'standby'
                report['sync'] = {'tracking': {'aligned': self.tick}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {
                    'value': {'bool': self.point},
                    'quality': 'good'}}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts_a)
        if (method, route) == ('POST', '/command'):
            if self.command_refused:
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'queue_full': {'depth': 8}}}},
                           'actor': body.get('actor')}
            else:
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
            self.receipts_a.append(receipt)
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if self.a_role != 'active':
                raise self._conflict(url, {'not_active': {}})
            self._demote_a()
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.a_role == 'active':
                raise self._conflict(url, 'already_active')
            if self.a_sync != 'tracking' or self.promote_refuses:
                raise self._conflict(url, {'not_converged': {
                    'sync': self.a_sync or 'unsynchronized'}})
            self.a_role = 'active'
            self.a_sync = None
            if self.b_role == 'active':
                self._demote_b()
            self._entry_a({'role_changed': {'from': 'standby',
                                            'to': 'active'}})
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _b(self, method, route, url, body):
        if not self.b_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            self._b_poll()
            report = {'role': self.b_role, 'tick': self.tick}
            if self.b_role == 'standby':
                report['sync'] = self._b_sync_report()
            return 200, report
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts_b)
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'actor': body.get('actor')}
            if self.refusal_wrong:
                receipt['outcome'] = {'accepted': {
                    'apply_tick': self.tick + 1}}
            else:
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {'point': body['command']
                                   ['write_value']['point'],
                                   'role': 'standby'}}}}
            echo = receipt
            if self.misnamed_journal:
                echo = dict(receipt)
                echo['outcome'] = {'applied': {'tick': self.tick}}
            self._entry_b({'command_settled': {'receipt': echo}})
            if self.refusal_applies:
                self.point = body['command']['write_value'] \
                    ['value']['bool']
            if self.leak_receipts:
                self.receipts_a.append(dict(receipt))
            if self.phantom_journal:
                self._entry_a({'command_settled': {'receipt': receipt}})
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if self.b_role != 'active' or self.demote_refuses:
                raise self._conflict(url, {'not_active': {}})
            self._demote_b()
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.b_role == 'active':
                raise self._conflict(url, 'already_active')
            if not self.gate_open \
                    and self._b_sync_report() != 'tracking':
                payload = {'no_tracking_source': {}} \
                    if self.gate_other else {'not_converged': {
                        'sync': self._b_sync_report()}}
                raise self._conflict(url, payload)
            if self.promote_refuses:
                raise self._conflict(url, {'not_converged': {
                    'sync': 'unsynchronized'}})
            self.b_role = 'active'
            self._entry_b({'role_changed': {'from': 'standby',
                                            'to': 'active'}})
            if self.a_role == 'active':
                self._demote_a()
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        if peer == 'b':
            return self._b(method, route, url, body)
        return self._a(method, route, url, body)


class StandbyLossTests(unittest.TestCase):
    """scenario_standby_loss against the stubbed pair: the four legs —
    the named not_active refusal with no field effect, the down-window
    non-interference, the reconvergence wait, and the promotion gate
    ahead of the restored role assignment — plus each named failure
    and inconclusive induction the issue calls out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal_a.parent.mkdir(parents=True)
        self.journal_b = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.journal_b.parent.mkdir(parents=True)
        self.feed = StandbyLossFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, **ctx_overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'stop_controller': feed.stop,
               'start_controller': feed.start,
               'journal_files': {'active': str(self.journal_a),
                                 'standby': str(self.journal_b)}}
        ctx.update(ctx_overrides)
        defaults = {'POLL_INTERVAL': 0.001, 'STANDBY_LOSS_POLL': 0.001,
                    'STANDBY_LOSS_PROBE': 0.001,
                    'STANDBY_LOSS_RETURN_DEADLINE': 0.3,
                    'STANDBY_LOSS_SETTLE_DEADLINE': 0.5}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_standby_loss(ctx)

    def test_clean_run_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.stops, ['standby'])
        self.assertEqual(self.feed.starts, ['standby'])
        # The pair ends on its pre-scenario role assignment.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertEqual(self.feed.b_sync, 'tracking')
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_refusal_named_wrong_fails(self):
        # The standby-directed command answering anything but the
        # named not_active rejection is the first named failure.
        self.feed.refusal_wrong = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_active', record['detail'])
        report.validate_scenario(record)

    def test_refused_write_reaching_the_field_fails(self):
        self.feed.refusal_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reached the field', record['detail'])

    def test_refused_write_in_receipt_log_fails(self):
        self.feed.leak_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record['detail'])

    def test_refused_write_on_active_journal_fails(self):
        self.feed.phantom_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record['detail'])

    def test_refusal_journaled_misnamed_fails(self):
        self.feed.misnamed_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('named', record['detail'])

    def test_stalled_scan_while_standby_down_fails(self):
        self.feed.stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stalled', record['detail'])
        # The induction still unwinds: the peer is started back.
        self.assertEqual(self.feed.starts, ['standby'])

    def test_role_move_while_standby_down_fails(self):
        self.feed.role_move = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('standby', record['detail'])

    def test_refused_window_command_fails(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refused', record['detail'])

    def test_unsettled_window_command_fails(self):
        self.feed.command_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled', record['detail'])

    def test_unjournaled_window_command_fails(self):
        self.feed.command_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record['detail'])

    def test_spurious_role_entry_fails(self):
        self.feed.spurious_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('transition', record['detail'])

    def test_stop_not_holding_is_inconclusive(self):
        self.feed.stop_leaks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('did not hold', record['detail'])

    def test_failing_stop_action_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_failing_start_action_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_missing_lifecycle_actions_are_inconclusive(self):
        record = self.run_scenario(stop_controller=None,
                                 start_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_standby_never_returns_is_inconclusive(self):
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered', record['detail'])

    def test_standby_never_tracks_fails(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reached tracking', record['detail'])

    def test_promotion_without_gate_refusal_fails(self):
        # The returning standby promoting on its first answered
        # request — no not_converged refusal precedes the switch — is
        # the gate failure the issue names; the pair still restores.
        self.feed.gate_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record['detail'])
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')

    def test_promotion_gate_other_refusal_fails(self):
        self.feed.gate_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record['detail'])

    def test_tracking_promotion_refused_fails(self):
        self.feed.promote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promote', record['detail'])

    def test_restore_demote_refused_fails(self):
        # The restore's demote on the promoted peer refusing leaves the
        # pair off its pre-scenario role assignment.
        self.feed.demote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restore', record['detail'])

    def test_pair_never_resettling_fails(self):
        self.feed.never_settles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role assignment', record['detail'])

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            run_dir = Path(self.tmp.name) / ('run' + str(index))
            evidence = run_dir / 'evidence'
            evidence.mkdir(parents=True)
            journal_a = run_dir / 'a' / 'journal.jsonl'
            journal_a.parent.mkdir(parents=True)
            journal_b = run_dir / 'b' / 'journal.jsonl'
            journal_b.parent.mkdir(parents=True)
            feed = StandbyLossFeed(journal_a, journal_b)
            record = self.run_scenario(
                feed=feed, evidence_dir=str(evidence),
                journal_files={'active': str(journal_a),
                               'standby': str(journal_b)})
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
