"""The 0400_controller_restart leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_controller_restart, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ControllerRestartTests.test_clean_restart_passes_and_validates',
    'ControllerRestartTests.test_applied_before_stop_passes',
    'ControllerRestartTests.test_dropped_pending_command_fails',
    'ControllerRestartTests.test_replayed_receipts_fail',
    'ControllerRestartTests.test_phantom_census_fails',
    'ControllerRestartTests.test_missing_served_boundary_fails',
    'ControllerRestartTests.test_disturbed_peer_journal_fails',
    'ControllerRestartTests.test_cold_start_resume_fails',
    'ControllerRestartTests.test_stale_resume_fails',
    'ControllerRestartTests.test_seq_restart_fails',
    'ControllerRestartTests.test_missing_boundary_fails',
    'ControllerRestartTests.test_spurious_peer_promotion_fails',
    'ControllerRestartTests.test_unfinished_restart_is_inconclusive',
    'ControllerRestartTests.test_unreturned_monitor_is_inconclusive',
    'ControllerRestartTests.test_two_runs_produce_identical_evidence',
})


class RestartFeed:
    """A stubbed pair for the controller-restart scenario. ctrl-a owns
    the field and persists every scan — the --state-file checkpoint is
    `persisted` — while ctrl-b tracks it and degrades during the
    restart gap. Commands queue at admission and apply at the next
    scan's boundary, so a stop inside the admission-to-application
    window leaves the Accepted receipt riding the checkpoint — the
    resumed run re-queues it. Each peer's --journal-file is a real
    append-only record the feed writes itself: a run_boundary marker
    per process lifetime, the restart's served run_boundary entry, one
    entry per settled command, and the declared-journaled point's
    value transitions — matching the durable record's format. Fault
    flags stage each named failure the issue calls out."""

    def __init__(self, journal_path, peer_journal_path):
        self.tick = 100      # well past the resume slack
        self.persisted = 100
        self.point = False
        self.pending = []        # accepted receipts awaiting a scan
        self.settled = []        # journaled receipts (the replay fold)
        self.served = []         # the monitor's served journal ring
        self.up = True           # ctrl-a's monitor answers
        self.serves = True       # False: the monitor never returns
        self.returns = True      # False: the restart action fails
        self.cold = False        # restart resumes nothing
        self.regress = False     # resume lands far behind
        self.seq_restarts = False  # journal seq numbering restarts
        self.peer_promoted = False
        # The extended scenario's named failures.
        self.applies_early = False    # the second write lands pre-stop
        self.drops_pending = False    # resume loses the carried receipt
        self.replays_receipts = False  # resume re-journals settlements
        self.phantom_census = False   # resume re-journals the census
        self.peer_disturbed = False   # the peer's journal gains a run
        self.skips_served_boundary = False  # the served marker never seeds
        self.restarts = []
        self.path = Path(journal_path)
        self.peer_path = Path(peer_journal_path)
        self.next_seq = 1
        self.runs = 1
        self._append(self.path, {'run_boundary': {'run': 1, 'tick': 0}})
        # Run 1's observation record — the baseline the resumed run's
        # replayed fold diffs its standing points against.
        self._entry({'quality_changed': {'point': 10, 'from': None,
                                         'to': 'good'}})
        self._entry({'point_changed': {'point': 10, 'from': None,
                                       'to': {'bool': False}}})
        self._append(self.peer_path,
                     {'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, event):
        entry = {'seq': self.next_seq, 'tick': self.tick,
                 'event': event}
        self._append(self.path, {'entry': entry})
        self.served.append(entry)
        self.next_seq += 1

    def _apply_pending(self):
        # The scan boundary's command phase: queued receipts settle,
        # the journaled point's transition records, and the receipt
        # log carries the settled outcome.
        for receipt in self.pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            new = write['value']['bool']
            if new != self.point:
                self._entry({'point_changed': {
                    'point': write['point'],
                    'from': {'bool': self.point},
                    'to': {'bool': new}}})
                self.point = new
            self._entry({'command_settled': {'receipt': receipt}})
            self.settled.append(receipt)
        self.pending = []

    def _scan(self):
        # One completed scan per snapshot read; queued commands apply
        # at its boundary and the state file follows at end of cycle.
        self.tick += 1
        self._apply_pending()
        self.persisted = self.tick

    # The runner-owned lifecycle action — replaces
    # ctx['restart_controller'].
    def restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        if self.applies_early:
            # The stop landed late: the queued write's applying scan
            # ran before the process died — the settlement journals
            # ahead of the run boundary.
            self.tick += 1
            self._apply_pending()
            self.persisted = self.tick
        self.up = False
        self.down_left = 2  # refused polls before the monitor returns
        resumed = self.persisted
        if self.cold:
            resumed = 0
        if self.regress:
            resumed = max(1, resumed - 100)
        self.tick = resumed
        if self.drops_pending:
            # The checkpoint never carried the Accepted receipt — the
            # command is lost unaudited, the failure the admission-time
            # persist closed. The pre-application value stands.
            self.pending = []
        self.runs += 1
        self._append(self.path, {'run_boundary': {'run': self.runs,
                                                  'tick': resumed}})
        # The served form of the marker: journaled once on replay,
        # taking the next seq like any event.
        if not self.skips_served_boundary:
            self._entry({'run_boundary': {'run': self.runs}})
        if self.seq_restarts:
            self.next_seq = 1
        if self.replays_receipts:
            # The resumed run re-journals the settled receipts its
            # checkpoint still carries — the restart-integrity
            # regression the finding closed.
            for receipt in self.settled:
                self._entry({'command_settled': {'receipt': receipt}})
        if self.phantom_census:
            # The resumed run diffs its standing points against
            # nothing and re-journals the whole census as first
            # observations.
            self._entry({'quality_changed': {'point': 10, 'from': None,
                                             'to': 'good'}})
            self._entry({'point_changed': {'point': 10, 'from': None,
                                           'to': {'bool': self.point}}})
        if self.peer_disturbed:
            self._append(self.peer_path,
                         {'run_boundary': {'run': 2, 'tick': resumed}})

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                if self.peer_promoted:
                    return 200, {'role': 'active', 'tick': self.tick}
                sync = {'tracking': {'aligned': self.tick}} if self.up \
                    else {'degraded': {'detail': 'checkpoint pull '
                                       'failed'}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.up:
            self.down_left -= 1
            if self.down_left <= 0 and self.serves:
                self.up = True
            else:
                raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 20, 'signal': None, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {
                    'value': {'bool': self.point},
                    'quality': {'quality': 'good'}}}]}
        if (method, route) == ('GET', '/journal'):
            return 200, list(self.served)
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.pending.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ControllerRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal.parent.mkdir(parents=True)
        self.peer_journal = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.peer_journal.parent.mkdir(parents=True)
        self.feed = RestartFeed(self.journal, self.peer_journal)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, **patches):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'restart_controller': feed.restart,
               'journal_files': {'active': str(self.journal),
                                 'standby': str(self.peer_journal)},
               'state_files': {}}
        defaults = {'POLL_INTERVAL': 0.001, 'RESTART_POLL': 0.001,
                    'RESTART_RETURN_DEADLINE': 0.5,
                    'RESTART_SETTLE_DEADLINE': 0.5,
                    'RESTART_JOURNAL_DEADLINE': 0.3,
                    'RESTART_COMMAND_DEADLINE': 0.3}
        defaults.update(patches)
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_controller_restart(ctx)

    def test_clean_restart_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.restarts, ['active'])
        # The restart-window command rode the checkpoint: its
        # settlement landed past the resumed run's boundary.
        self.assertTrue(any('post-boundary' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_applied_before_stop_passes(self):
        # The other admissible answer: the queued write's applying
        # scan ran before the container died — the settlement journals
        # ahead of the run boundary and still satisfies the contract.
        self.feed.applies_early = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('pre-boundary' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)

    def test_dropped_pending_command_fails(self):
        # The failure the admission-time persist closed: the carried
        # Accepted receipt never re-queues — the point keeps its
        # pre-application value and the journal stays silent on the
        # settlement.
        self.feed.drops_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('silently lost', record.get('detail', ''))
        report.validate_scenario(record)

    def test_replayed_receipts_fail(self):
        # The resumed run re-journals the settled receipts its
        # checkpoint still carries past the run boundary — the
        # restart-integrity regression.
        self.feed.replays_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('re-journaled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_phantom_census_fails(self):
        self.feed.phantom_census = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('phantom', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_served_boundary_fails(self):
        self.feed.skips_served_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('served journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_disturbed_peer_journal_fails(self):
        self.feed.peer_disturbed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('peer', record.get('detail', ''))
        report.validate_scenario(record)

    def test_cold_start_resume_fails(self):
        self.feed.cold = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_resume_fails(self):
        self.feed.regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_seq_restart_fails(self):
        self.feed.seq_restarts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('seqs', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_boundary_fails(self):
        # A restarted lifetime that never marks its boundary: the file
        # holds run 1's records only.
        feed = self.feed

        def restart(name):
            feed.restarts.append(name)
            feed.up, feed.down_left = False, 2
            feed.tick = feed.persisted

        self.feed.restart = restart
        ctx_record = self.run_scenario()
        self.assertEqual(ctx_record['outcome'], 'failed', ctx_record)
        self.assertIn('run-boundary', ctx_record.get('detail', ''))
        report.validate_scenario(ctx_record)

    def test_spurious_peer_promotion_fails(self):
        self.feed.peer_promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reported active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unfinished_restart_is_inconclusive(self):
        self.feed.returns = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # against the same rig layout record the same report and the
        # same evidence files — the feed's transitions are call-count
        # keyed, never wall-clock.
        runs = []
        for _index in range(2):
            for path in (self.journal, self.peer_journal):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = RestartFeed(self.journal, self.peer_journal)
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
