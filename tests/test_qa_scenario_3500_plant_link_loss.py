"""The 3500_plant_link_loss leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_plant_link_loss, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PlantLinkLossTests.test_registered_in_scenarios',
    'PlantLinkLossTests.test_clean_loss_recovery_passes_and_validates',
    'PlantLinkLossTests.test_fresh_telemetry_through_outage_fails',
    'PlantLinkLossTests.test_unchecked_link_failure_fails',
    'PlantLinkLossTests.test_promotion_during_outage_fails',
    'PlantLinkLossTests.test_aborted_run_fails',
    'PlantLinkLossTests.test_unreturned_plant_is_inconclusive',
    'PlantLinkLossTests.test_failed_start_action_is_inconclusive',
    'PlantLinkLossTests.test_unreclaimed_writer_claim_fails',
    'PlantLinkLossTests.test_unrecovered_reads_fail',
    'PlantLinkLossTests.test_reset_io_health_fails',
    'PlantLinkLossTests.test_missing_actions_is_inconclusive',
    'PlantLinkLossTests.test_non_bad_degradation_fails',
    'PlantLinkLossTests.test_flat_counters_fail',
    'PlantLinkLossTests.test_unjournaled_loss_fails',
    'PlantLinkLossTests.test_unreachable_journal_is_inconclusive',
    'PlantLinkLossTests.test_untracked_baseline_is_inconclusive',
    'PlantLinkLossTests.test_standby_leaving_tracking_mid_outage_fails',
    'PlantLinkLossTests.test_silent_standby_is_inconclusive',
    'PlantLinkLossTests.test_stepped_window_fails',
    'PlantLinkLossTests.test_silent_write_through_window_fails',
    'PlantLinkLossTests.test_unobserved_window_fails',
    'PlantLinkLossTests.test_restarted_controller_fails',
    'PlantLinkLossTests.test_window_probes_close_and_recovery_fences',
    'PlantLinkLossTests.test_two_runs_produce_identical_evidence',
})


class PlantLinkFeed:
    """A stubbed pair plus shared plant for the plant-link-loss
    scenario. ctrl-a owns the field; the plant's wire protocol answers
    the scenario's census and fencing probes through `plant_request`.
    `stop`/`start` replace ctx['stop_plant']/ctx['start_plant'] — the
    runner-owned lifecycle actions — and flip `plant_up`; every
    snapshot read advances one scan whose point qualities and
    io_health reflect the link state. The served journal carries the
    loss as quality_changed past the scenario's baseline cursor, and
    each scan appends to the durable journal files with continuing
    seqs in one lifetime. The post-restart claim stays empty until
    the owner's scans re-arm it — the fail-closed unclaimed window —
    unless a fault flag stages the named failure the issue calls
    out."""

    REARM_SCANS = 2  # owner scans from the restart to the re-arm

    def __init__(self, journal_a=None, journal_b=None):
        self.tick = 0
        self.plant_tick = 0
        self.plant_up = True
        self.active_up = True
        self.cycled = False       # the plant went through stop+start
        self.claimed = True       # a writer claim stands on the plant
        self.rearm_scans = 0      # owner scans since the restart
        self.failed_reads = 0
        self.failed_writes = 0
        self.consecutive = 0
        self.last_error = None
        self.calls = []           # the lifecycle actions run
        self.journal = []         # the served journal entries
        self.next_seq = 1
        self.was_down = False     # the last scan's link state
        self.journal_files = {}   # name -> (path, next seq)
        for name, path in (('active', journal_a),
                           ('standby', journal_b)):
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_files[name] = [path, 1]
        # Fault injection for the named-failure cases.
        self.fresh_through = False    # telemetry never degrades
        self.wrong_quality = False    # telemetry degrades past Bad
        self.clean_health = False     # io_health never counts the loss
        self.no_growth = False        # io_health counts once, never grows
        self.no_journal = False       # the loss never journals
        self.journal_down = False     # the served journal never answers
        self.standby_untracked = False  # the standby leaves tracking
        self.peer_down = False        # the standby never answers /role
        self.promoted = False         # the standby reports active
        self.aborts = False           # the active's monitor dies on loss
        self.never_returns = False    # start leaves the plant dead
        self.start_raises = False     # the start action itself fails
        self.never_recovers = False   # plant back, reads stay bad
        self.never_reclaims = False   # the claim is never re-taken
        self.instant_rearm = False    # the claim never lapses at restart
        self.window_open = False      # a step mutates through the window
        self.write_through = False    # a write lands through the window
        self.tick_regress = False     # the active restarts past the return
        self.resets_health = False    # io_health zeroes on recovery

    # The runner-owned lifecycle actions — replace
    # ctx['stop_plant']/ctx['start_plant'].
    def stop(self):
        self.calls.append('stop')
        self.plant_up = False
        self.claimed = False        # the claim dies with the server
        if self.aborts:
            self.active_up = False

    def start(self):
        self.calls.append('start')
        if self.start_raises:
            raise RuntimeError('docker start failed: no such container')
        if not self.never_returns:
            self.plant_up = True
            self.cycled = True
        self.rearm_scans = 0
        if self.tick_regress:
            self.tick = 0           # a restarted process re-serves tick 0
        if self.instant_rearm:
            self.claimed = True     # no lapse — the window never opens
        elif not self.never_reclaims:
            self.claimed = False    # the owner's scans owe the re-arm

    def _journal_entry(self, event):
        entry = {'seq': self.next_seq, 'tick': self.tick,
                 'event': event}
        self.journal.append(entry)
        self.next_seq += 1

    def _file_entry(self, name):
        path, seq = self.journal_files[name]
        with path.open('a') as stream:
            stream.write(json.dumps(
                {'entry': {'seq': seq, 'tick': self.tick,
                           'event': {}}}) + '\n')
        self.journal_files[name][1] += 1

    # One completed scan per snapshot read: reads fail at the dead
    # link, writes keep landing while the plant is up.
    def _scan(self):
        self.tick += 1
        down = self._down()
        if down and not self.was_down and not self.no_journal:
            for point in (10, 11):
                self._journal_entry(
                    {'quality_changed': {'point': point,
                                         'from': 'good',
                                         'to': 'bad:communication_fault'}})
        if not down and self.was_down and not self.no_journal:
            for point in (10, 11):
                self._journal_entry(
                    {'quality_changed': {'point': point,
                                         'from':
                                         'bad:communication_fault',
                                         'to': 'good'}})
        self.was_down = down
        if down:
            if not self.clean_health and not (
                    self.no_growth and self.failed_reads):
                self.failed_reads += 5
                self.failed_writes += 2
                self.consecutive += 7
                self.last_error = {
                    'tick': self.tick, 'point': 10, 'direction': 'in',
                    'error': {'disconnected': 10}}
        else:
            self.consecutive = 0
            if self.resets_health:
                self.failed_reads = 0
                self.failed_writes = 0
                self.last_error = None
        if self.plant_up and self.cycled and not self.never_reclaims \
                and not self.instant_rearm:
            # The owner's scans re-arm the recorded claim once the
            # plant serves again — the re-attach runs over the live
            # plant connection even while served reads stay bad.
            self.rearm_scans += 1
            if self.rearm_scans >= self.REARM_SCANS:
                self.claimed = True  # the owner's re-arm landed
        for name in self.journal_files:
            self._file_entry(name)

    def _down(self):
        # The link is severed while the plant is stopped; a feed whose
        # never_recovers flag is set keeps it severed past the restart.
        return not self.plant_up \
            or (self.never_recovers and self.cycled)

    def _link(self):
        return 'disconnected' if self._down() else 'connected'

    def _quality(self):
        if self._down():
            if self.wrong_quality:
                return {'uncertain': 'stale'}
            return {'bad': 'communication_fault'}
        return 'good'

    # The plant wire protocol — replaces scenarios._plant_probe.
    def plant_request(self, ctx, request, timeout=5):
        if not self.plant_up:
            raise urllib.error.URLError('connection refused')
        if request['op'] == 'list_points':
            sample = {'value': {'float': 1.5}, 'quality': 'good',
                      'tick': self.plant_tick}
            return {'result': 'points', 'points': [
                {'point': 10, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': 11, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': 100, 'direction': 'out', 'sample': sample,
                 'fault': None}]}
        if request['op'] == 'read':
            return {'result': 'sample',
                    'sample': {'value': {'float': 1.5},
                               'quality': 'good',
                               'tick': self.plant_tick}}
        if request['op'] == 'step':
            if self.claimed:
                return {'result': 'error',
                        'error': {'kind': 'fenced',
                                  'detail': 'another attachment owns '
                                            'field writes'}}
            if self.window_open:
                # The defect: the restarted field stood open — a
                # mutation lands with no claim standing.
                self.plant_tick += 1
                return {'result': 'stepped', 'tick': self.plant_tick}
            # The field fails closed while unclaimed: a restarted
            # plant's claim-less window is a named refusal, not an
            # open one — so the probe can tell "waiting for the owner's
            # re-arm" from "fenced by a standing owner".
            return {'result': 'error',
                    'error': {'kind': 'unclaimed',
                              'detail': 'no attachment holds '
                                        'field writes'}}
        if request['op'] == 'write':
            if self.write_through:
                return {'result': 'done'}
            if self.claimed:
                return {'result': 'error',
                        'error': {'kind': 'io',
                                  'error': {'fenced': request.get(
                                      'point')}}}
            return {'result': 'error',
                    'error': {'kind': 'unclaimed',
                              'detail': 'no attachment holds '
                                        'field writes'}}
        raise AssertionError('unexpected plant request %s' % request)

    # The shipped plant tool — replaces ctx['plant_ctl'] for the
    # covered census; the bare `step` fencing probes stay on the
    # _plant_probe patch above.
    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.plant_request(None, request), *args)

    # The monitor surface — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = url.split('/', 3)[3].partition('?')[0]
        route = '/' + route
        query = url.partition('?')[2] if '?' in url else ''
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                if self.peer_down:
                    raise urllib.error.URLError('connection refused')
                role = 'active' if self.promoted else 'standby'
                report = {'role': role, 'tick': self.tick}
                if role == 'standby' and not self.standby_untracked:
                    report['sync'] = {'tracking': {'aligned': self.tick}}
                elif role == 'standby':
                    report['sync'] = {'degraded': {'reason': 'test'}}
                return 200, report
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.active_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/journal'):
            if self.journal_down:
                raise urllib.error.URLError('connection refused')
            since = 0
            for part in query.split('&'):
                if part.startswith('since='):
                    since = int(part.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry.get('seq', 0) > since]
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False},
                {'point': 11, 'signal': None, 'name': 'level-backup',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            quality = 'good' if self.fresh_through else self._quality()
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {'value': {'float': 1.5},
                                         'quality': quality}},
                {'point': 11, 'sample': {'value': {'float': 2.5},
                                         'quality': quality}},
                {'point': 100, 'sample': {'value': {'bool': False},
                                          'quality': 'good'}}],
                'io_health': {
                    'failed_reads': self.failed_reads,
                    'failed_writes': self.failed_writes,
                    'consecutive_failures': self.consecutive,
                    'last_error': self.last_error,
                    'scan_overruns': 0,
                    'driver': {'link': self._link(),
                               'last_error': self.last_error
                               and 'connection reset'}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class PlantLinkLossTests(unittest.TestCase):
    """scenario_plant_link_loss against the stubbed feed: the lifecycle
    actions cycle the shared plant while the monitor surface and the
    wire protocol carry the degradation and recovery evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'journal-a.jsonl'
        self.journal_b = Path(self.tmp.name) / 'journal-b.jsonl'
        self.feed = PlantLinkFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, ctx_extra=None, feed=None):
        feed = feed if feed is not None else self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': '127.0.0.1:9',
               'plant_ctl': feed.plant_ctl,
               'evidence_dir': str(self.evidence),
               'stop_plant': feed.stop,
               'start_plant': feed.start,
               'journal_files': {'active': str(self.journal_a),
                                 'standby': str(self.journal_b)}}
        ctx.update(ctx_extra or {})
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_plant_probe',
                             feed.plant_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LINK_POLL', 0.001), \
                patch.object(scenarios, 'LINK_WINDOW_POLL', 0.001), \
                patch.object(scenarios, 'LINK_DEGRADE_DEADLINE', 0.05), \
                patch.object(scenarios, 'LINK_SETTLE', 0.005), \
                patch.object(scenarios, 'LINK_RECOVERY_DEADLINE', 0.5):
            return scenarios.scenario_plant_link_loss(ctx)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_plant_link_loss,
                      scenarios.SCENARIOS)

    def test_clean_loss_recovery_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.calls, ['stop', 'start'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        outage = json.loads(
            (self.evidence / 'plant-link-loss-outage.json').read_text())
        self.assertGreater(
            outage['health']['failed_reads'], 0)
        self.assertEqual(outage['health']['driver']['link'],
                         'disconnected')

    def test_fresh_telemetry_through_outage_fails(self):
        self.feed.fresh_through = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('kept reading good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unchecked_link_failure_fails(self):
        self.feed.clean_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('io_health', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_during_outage_fails(self):
        # The standby promoting behind the field loss: the role watch
        # names the moved peer.
        feed = self.feed
        original_scan = feed._scan

        def promoting_scan():
            original_scan()
            if feed.plant_up is False:
                feed.promoted = True

        feed._scan = promoting_scan
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role', record.get('detail', ''))
        report.validate_scenario(record)

    def test_aborted_run_fails(self):
        # The run that dies on field loss: the active's monitor never
        # answers after the stop — the named 'scans continuing rather
        # than aborting' violation.
        self.feed.aborts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('aborted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_plant_is_inconclusive(self):
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never served again', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_start_action_is_inconclusive(self):
        self.feed.start_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreclaimed_writer_claim_fails(self):
        self.feed.never_reclaims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('single-writer claim',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrecovered_reads_fail(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('Good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_reset_io_health_fails(self):
        self.feed.resets_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reset', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_actions_is_inconclusive(self):
        record = self.run_scenario({'stop_plant': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_non_bad_degradation_fails(self):
        self.feed.wrong_quality = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('Bad', record.get('detail', ''))
        report.validate_scenario(record)

    def test_flat_counters_fail(self):
        self.feed.no_growth = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('advanced', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_loss_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_journal_is_inconclusive(self):
        self.feed.journal_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_untracked_baseline_is_inconclusive(self):
        self.feed.standby_untracked = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_standby_leaving_tracking_mid_outage_fails(self):
        feed = self.feed
        original_scan = feed._scan

        def losing_scan():
            original_scan()
            if feed.plant_up is False:
                feed.standby_untracked = True

        feed._scan = losing_scan
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_standby_is_inconclusive(self):
        self.feed.peer_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_stepped_window_fails(self):
        self.feed.window_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stepped', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_write_through_window_fails(self):
        self.feed.write_through = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('silently', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unobserved_window_fails(self):
        self.feed.instant_rearm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('window', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restarted_controller_fails(self):
        self.feed.tick_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restart', record.get('detail', ''))
        report.validate_scenario(record)

    def test_window_probes_close_and_recovery_fences(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        window = json.loads(
            (self.evidence / 'plant-link-loss-window.json').read_text())
        kinds = [round_['step'] for round_ in window
                 if round_['step'] is not None]
        self.assertIn('unclaimed', kinds)
        self.assertIn('fenced', kinds)
        self.assertNotIn('stepped',
                         [round_.get('step_result') for round_ in window])
        self.assertNotIn('done',
                         [round_.get('write_result') for round_ in window])
        digest = json.loads(
            (self.evidence / 'plant-link-loss-digest.json').read_text())
        self.assertEqual(digest['window'], 'unclaimed-then-fenced')
        self.assertEqual(digest['restarts'], 'none')
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # over the same rig layout record the same normalized digest —
        # the categorical verdicts, never the wall-clock poll counts.
        digests = []
        for _index in range(2):
            for path in (self.journal_a, self.journal_b):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = PlantLinkFeed(self.journal_a, self.journal_b)
            record = self.run_scenario(feed=feed)
            self.assertEqual(record['outcome'], 'passed', record)
            report.validate_scenario(record)
            digests.append(
                (self.evidence / 'plant-link-loss-digest.json')
                .read_text())
        self.assertEqual(digests[0], digests[1])


if __name__ == '__main__':
    unittest.main()
