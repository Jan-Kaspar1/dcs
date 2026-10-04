"""The 3670_journal_sink_isolation leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_journal_sink_isolation, split out of the test_qa_scenarios
monolith (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The leg's mount lever is the drain-stall tracer park/release pair the
runner exposes on the scenario ctx — a `--journal-file` writer opens
its file once at bind, so no staged node can park a mid-run write and
the writer thread itself is what has to stall. The fakes below emulate
that mechanism rather than the kernel: the controller's drain writer is
a real thread appending to a real durable file in `seq` order behind a
real bounded queue, the recording point's own ring serves the retained
`GET /journal` window, and the feed's `park()` holds the writer exactly
where the lever's ptrace park holds the released binary's writer.
"""
import collections
import copy
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'JournalSinkIsolationTests.test_registered_in_scenarios',
    'JournalSinkIsolationTests.test_clean_rig_passes_and_validates',
    'JournalSinkIsolationTests.test_lag_never_surfaces_fails',
    'JournalSinkIsolationTests.test_tick_stall_under_lag_fails',
    'JournalSinkIsolationTests.test_late_serving_read_fails',
    'JournalSinkIsolationTests.test_io_growth_under_stall_fails',
    'JournalSinkIsolationTests.test_overrun_growth_under_stall_fails',
    'JournalSinkIsolationTests.test_sink_failed_state_fails',
    'JournalSinkIsolationTests.test_unbounded_depth_fails',
    'JournalSinkIsolationTests.test_lost_records_fail',
    'JournalSinkIsolationTests.test_early_journal_answer_is_nondeterministic',
    'JournalSinkIsolationTests.test_durable_gap_is_nondeterministic',
    'JournalSinkIsolationTests.test_duplicate_durable_record_is_nondeterministic',
    'JournalSinkIsolationTests.test_reordered_window_is_nondeterministic',
    'JournalSinkIsolationTests.test_retained_window_without_settlements_fails',
    'JournalSinkIsolationTests.test_unanswered_admission_fails',
    'JournalSinkIsolationTests.test_released_writer_never_drains_fails',
    'JournalSinkIsolationTests.test_owner_move_fails',
    'JournalSinkIsolationTests.test_untracked_peer_fails',
    'JournalSinkIsolationTests.test_missing_lever_is_inconclusive',
    'JournalSinkIsolationTests.test_unattributable_writer_is_inconclusive',
    'JournalSinkIsolationTests.test_park_refused_is_inconclusive',
    'JournalSinkIsolationTests.test_missing_journal_file_is_inconclusive',
    'JournalSinkIsolationTests.test_predated_contract_is_inconclusive',
    'JournalSinkIsolationTests.test_torn_baseline_is_inconclusive',
    'JournalSinkIsolationTests.test_unreachable_rig_is_inconclusive',
    'JournalSinkIsolationTests.test_unsettled_pair_is_inconclusive',
    'JournalSinkIsolationTests.test_no_drain_writer_is_inconclusive',
    'JournalSinkIsolationTests.test_scenario_ctx_carries_the_lever',
    'JournalSinkIsolationTests.test_ctx_without_tracer_carries_no_lever',
    'JournalSinkIsolationTests.test_two_runs_produce_identical_evidence',
})


class _FakeJournalSink:
    """One controller's --journal-file drain, emulated with the real
    mechanism the isolation decision adopts: the recording point offers
    each record into a bounded queue without ever waiting, a dedicated
    writer thread receives them in push order and appends each to the
    durable file, and the named healthy/lagging/failed health with its
    accepted/drained/lost counters rides every publication. `parked`
    holds the writer thread exactly where the lever's ptrace park holds
    the released binary's `dcs-drain` writer — the append that cannot
    complete while the records behind it queue."""

    def __init__(self, path, feed):
        self.path = Path(path)
        self.feed = feed
        self.served = []
        self.seq = 0
        self.accepted = 0
        self.drained = 0
        self.lost = 0
        self.parked = False
        self._pending = collections.deque()
        self._in_flight = 0
        self._cv = threading.Condition()
        self._stop = False
        self._writer = threading.Thread(target=self._drain,
                                        daemon=True)
        self._writer.start()

    # ---- the recording point: a non-blocking offer, never a wait ----

    def offer(self, event):
        """Queue one journaled record and return its ordinal — what the
        request worker's own attest wait rides. `feed.direct_journal_writes`
        stages the bypass defect: the append runs inside the recording
        point, so the park never bites and the queue never holds."""
        with self._cv:
            self.seq += 1
            ordinal = self.accepted + 1
            if not self.feed.direct_journal_writes:
                if len(self._pending) + self._in_flight \
                        >= self.feed.capacity:
                    return None
                self._pending.append((self.seq, event))
            self.accepted = ordinal
        self.served.append({'seq': self.seq,
                            'tick': self.feed.a_tick, 'event': event})
        if self.feed.direct_journal_writes:
            self._append((self.seq, event))
            self.drained = ordinal
        return ordinal

    def attest(self, ordinal, bound=30):
        """The request worker's own bounded wait: the answer only once
        the drain covered the record the request journaled."""
        if ordinal is None:
            return False
        end = time.monotonic() + bound
        with self._cv:
            while self.drained < ordinal:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.01))
            return self.drained >= ordinal

    def caught_up(self):
        """The served health's caught-up claim: every accepted record
        the writer took."""
        with self._cv:
            return self.drained >= self.accepted

    def health(self):
        """The publication.journal_sink section the monitor serves."""
        with self._cv:
            depth = len(self._pending) + self._in_flight
            if self.feed.fails_sink and depth:
                # The sink's own write error, served as its named
                # terminal state.
                state = 'failed'
            elif self.feed.hides_lag and depth:
                # The misnamed state: records wait in the queue while the
                # sink claims the standing health.
                state = 'healthy'
            else:
                state = 'lagging' if depth else 'healthy'
            reported = depth
            if self.feed.overreports_depth and depth:
                reported = self.feed.capacity + 1
            lost = self.lost
            if self.feed.loses_records and depth:
                lost += 1
            return {'state': state, 'accepted': self.accepted,
                    'drained': self.drained, 'lost': lost,
                    'depth': reported,
                    'high_water': max(reported, depth),
                    'capacity': self.feed.capacity}

    def stop(self):
        """End the drain loop without writing."""
        with self._cv:
            self._stop = True
            self._pending.clear()
            self._cv.notify_all()
        self._writer.join(2)

    # ---- the dedicated writer thread ----

    def _drain(self):
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait(0.01)
                if self._stop:
                    return
                record = self._pending.popleft()
                self._in_flight += 1
            while self.parked and not self._stop:
                time.sleep(0.005)
            with self._cv:
                self._in_flight -= 1
                self._cv.notify_all()
            self._append(record)
            with self._cv:
                self.drained += 1
                self._cv.notify_all()

    def _append(self, record):
        seq, event = record
        try:
            if self.feed.corrupt_durable and seq == 3:
                # The torn append: a record the file took only in part.
                with self.path.open('a') as handle:
                    handle.write('{"entry":{"seq":' + str(seq))
                return
            if self.feed.duplicate_durable and seq == 3:
                with self.path.open('a') as handle:
                    handle.write(json.dumps(
                        {'entry': {'seq': 1, 'tick': 1, 'event': {}}})
                        + '\n')
            with self.path.open('a') as handle:
                handle.write(json.dumps({'entry': {'seq': seq, 'tick': 1,
                                                   'event': event}})
                             + '\n')
        except FileNotFoundError:
            # Teardown removed the run directory under a writer still
            # inside its append — the record is abandoned, not drained.
            pass


class JournalSinkFeed:
    """A stubbed pair for the journal-sink-isolation scenario. ctrl-a
    owns the field: every request on it is one completed scan — the tick
    advances and whatever the request journals is recorded into the
    recorder's own served ring, the journal sink's writer thread
    draining each record to the durable file. ctrl-b tracks its stream.
    The feed's flags stage each named defect the leg's diagnostics
    cover, and `park`/`release` are the lever's hold on the
    controller's drain writers."""

    def __init__(self, dirs):
        self.dirs = {key: Path(value) for key, value in dirs.items()}
        self.a_tick = 400
        self.b_tick = 400
        self.a_role = 'active'
        self.b_role = 'standby'
        self.b_tracking = True
        self.receipts = []
        self.point = False
        self.capacity = 64
        self.parked = None
        self.journal_tid = 12
        self.was_parked = False
        self.release_ignored = False
        # Fault flags staging the named failures.
        self.down = False
        self.no_sink_section = False
        self.hides_lag = False
        self.direct_journal_writes = False
        self.stall_on_lag = False
        self.fails_sink = False
        self.loses_records = False
        self.overreports_depth = False
        self.degrades_io = False
        self.overruns_under_stall = False
        self.late_reads = False
        self.serve_without_attest = False
        self.corrupt_durable = False
        self.duplicate_durable = False
        self.reorder_window = False
        self.drop_settlements = False
        self.refuse_attest = 0     # the nth submission never attests
        self.move_roles = False
        self.untracks = False
        self.submissions = 0
        self._lock = threading.Lock()
        self.sinks = {key: _FakeJournalSink(
            self.dirs[key] / 'journal.jsonl', self)
            for key in ('a', 'b')}

    def seed_journal(self):
        """The bind-time durable record every launched controller writes
        before the leg's window: the run-boundary marker and the
        standing census's first observations, drained before the park."""
        for key in ('a', 'b'):
            path = self.dirs[key] / 'journal.jsonl'
            with path.open('w') as handle:
                handle.write(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            for point in (10, 11):
                self.sinks[key].offer({'point_changed': {
                    'point': point, 'from': None,
                    'to': {'int': point}}})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(sink.caught_up() for sink in self.sinks.values()):
                return
            time.sleep(0.01)

    # ---- the lever: the controller's `dcs-drain` writer tids ----

    def candidates(self, name):
        return [11, 12]

    def park(self, tid):
        self.parked = tid
        self.was_parked = True
        for sink in self.sinks.values():
            sink.parked = tid == self.journal_tid

    def release(self, tid):
        if self.release_ignored or self.parked != tid:
            raise RuntimeError('the detached drain writer never resumed')
        self.parked = None
        for sink in self.sinks.values():
            sink.parked = False

    def close(self):
        for sink in self.sinks.values():
            sink.stop()

    # ---- the served monitor ----

    def _scan(self):
        """One completed scan cycle: the tick advances and, under the
        staged defects, the cadence or the sink's named state lies."""
        sink = self.sinks['a']
        lagging = bool(sink.health()['depth'])
        if self.stall_on_lag and lagging:
            # The lengthened scan the contract forbids — no tick.
            return
        if self.late_reads and lagging:
            time.sleep(2.0)
        self.a_tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and self.a_tick >= accepted['apply_tick']:
                write = receipt['command']['write_value']
                self.point = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.a_tick}}
        if self.was_parked and self.parked is None:
            if self.move_roles:
                # The stall failed the pair over — the field owner does
                # not come back on restore.
                self.a_role = 'standby'
                self.b_role = 'active'
                self.b_tracking = False
            if self.untracks:
                self.b_tracking = False

    def _io_health(self):
        """ctrl-a's io_health section — flat counters while the journal
        sink lags, growing only under the staged defects."""
        failures = overruns = 0
        if self.parked is not None:
            if self.degrades_io:
                failures = self.a_tick - 400
            if self.overruns_under_stall:
                overruns = self.a_tick - 400
        return {'failed_reads': failures, 'failed_writes': 0,
                'failed_exchanges': 0,
                'consecutive_failures': failures,
                'scan_overruns': overruns, 'last_error': None}

    def _admit(self, body):
        """POST /command: admission and the journaled settlement at
        submission, under the executor lock the scan just took."""
        receipt = {'command': body['command'],
                   'outcome': {'accepted': {'apply_tick': self.a_tick + 1}},
                   'actor': body.get('actor')}
        self.submissions += 1
        self.receipts.append(receipt)
        ordinal = self.sinks['a'].offer({'command_settled': {
            'receipt': copy.deepcopy(receipt)}})
        return receipt, ordinal

    def _attest(self, ordinal):
        """The durability-attesting wait — the request worker's own,
        off the lock the scans serialize on: the answer only once the
        drain covered the record the request journaled."""
        if self.submissions == self.refuse_attest \
                or not self.sinks['a'].attest(ordinal):
            raise urllib.error.URLError('the journal drain never '
                                        'attested the record')

    def _window(self):
        """The served retained window — the recorder's own ring, never
        the durable file's."""
        entries = [copy.deepcopy(entry)
                   for entry in self.sinks['a'].served]
        if self.drop_settlements:
            entries = [entry for entry in entries
                       if 'command_settled' not in entry['event']]
        if self.reorder_window and len(entries) > 1:
            entries = entries[:1] + list(reversed(entries[1:]))
        return entries

    def plant_ctl(self, *args):
        """The shipped plant-side tool's stub: `fault` and `clear-fault`
        take, every other op answers the named refusal."""
        if args[:1] in (('fault',), ('clear-fault',)):
            return subprocess.CompletedProcess(
                ['dcs-plant-ctl'] + list(args), 0,
                json.dumps({'result': 'done', 'point': int(args[1])}),
                '')
        return subprocess.CompletedProcess(
            ['dcs-plant-ctl'] + list(args), 2, '', 'unsupported op')

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if self.down:
            raise urllib.error.URLError('connection refused')
        if host == 'ctrl-b:2':
            self.b_tick += 1
            if (method, route) == ('GET', '/role'):
                sync = ({'tracking': {'aligned': self.a_tick}}
                        if self.b_tracking and self.b_role == 'standby'
                        else None)
                return 200, {'role': self.b_role, 'tick': self.b_tick,
                             'sync': sync}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('POST', '/command'):
            # Admission and the journaled settlement hold the executor
            # lock; the attesting wait and the answer are the request
            # worker's own, off it — the serving lane keeps answering
            # while the answer parks.
            with self._lock:
                self._scan()
                receipt, ordinal = self._admit(body)
            self._attest(ordinal)
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('GET', '/journal'):
            sink = self.sinks['a']
            if not self.serve_without_attest:
                # The attesting read waits the standing queue out on its
                # own worker — the request that must park while the
                # writer stands held.
                sink.attest(sink.accepted, bound=min(timeout, 5))
            return 200, self._window()
        with self._lock:
            self._scan()
            if (method, route) == ('GET', '/role'):
                return 200, {'role': self.a_role, 'tick': self.a_tick,
                             'sync': None}
            if (method, route) == ('GET', '/snapshot'):
                publication = {'published': self.a_tick, 'coalesced': 0,
                               'depth': 0, 'window': 4}
                if not self.no_sink_section:
                    publication['journal_sink'] = self.sinks['a'].health()
                return 200, {
                    'tick': self.a_tick,
                    'points': [{'point': 10, 'sample': {
                        'value': {'bool': self.point},
                        'quality': {'quality': 'good'}}}],
                    'publication': publication,
                    'io_health': self._io_health()}
            if (method, route) == ('GET', '/signals'):
                return 200, {'points': [
                    {'point': 10, 'signal': None, 'name': 'p101-oos',
                     'direction': 'in', 'value_type': 'bool',
                     'writable': True}], 'components': []}
            if (method, route) == ('GET', '/receipts'):
                return 200, list(self.receipts)
            if (method, route) == ('GET', '/checkpoint'):
                return 200, {'receipts': list(self.receipts),
                             'command_admission': {
                                 'attempts': len(self.receipts)}}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))


class JournalSinkIsolationTests(unittest.TestCase):
    """scenario_journal_sink_isolation against the stubbed pair: the
    feed's drain writer is a real thread appending to a real durable
    file behind a real bounded queue, and the ctx's park/release lever
    holds it there — so the leg's named-sink-health, bounded-cadence,
    retained-window, drain-in-order, and reconvergence claims are
    exercised against the mechanism they name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            self.dirs[name] = directory
        self.feed = JournalSinkFeed(self.dirs)
        self.feeds = [self.feed]
        self.feed.seed_journal()

    def tearDown(self):
        # Every staged pass's writer threads stop before the run
        # directory goes: a writer still inside its append would race
        # the teardown.
        for feed in self.feeds:
            feed.close()
        self.tmp.cleanup()

    def fresh_run(self):
        """A second pass's run directory, seeded journal, and empty
        evidence set — the same starting state each pass stages from."""
        for name in ('a', 'b'):
            (self.dirs[name] / 'journal.jsonl').unlink(missing_ok=True)
        for stale in self.evidence.iterdir():
            stale.unlink()
        feed = JournalSinkFeed(self.dirs)
        self.feeds.append(feed)
        feed.seed_journal()
        return feed

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': '127.0.0.1:9999',
                'evidence_dir': str(evidence),
                'drain_writers': feed.candidates,
                'park_drain_writer': feed.park,
                'release_drain_writer': feed.release,
                'plant_ctl': feed.plant_ctl,
                'journal_files': {
                    'active': str(self.dirs['a'] / 'journal.jsonl'),
                    'standby': str(self.dirs['b'] / 'journal.jsonl')}}
        if ctx is not None:
            base.update(ctx)
        patches = {'JOURNAL_POLL': 0.005, 'JOURNAL_DEADLINE': 5,
                   'JOURNAL_SETTLE': 5, 'JOURNAL_ANSWER': 3,
                   'JOURNAL_PROBE': 0.3, 'JOURNAL_TICKS': 3,
                   'JOURNAL_STIR_POLL': 0.005}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_journal_sink_isolation(base)
        return record

    def durable(self):
        """The field owner's durable journal file, parsed line by line."""
        return [json.loads(line) for line
                in (self.dirs['a'] / 'journal.jsonl').read_text()
                .splitlines() if line.strip()]

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_journal_sink_isolation,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # The lever ran and the sink was restored: the writer resumed,
        # the durable file carries the window's settlements in seq
        # order, and the pair stands as it was launched.
        self.assertIsNone(self.feed.parked)
        self.assertTrue(self.feed.was_parked)
        durable = self.durable()
        seqs = [item['entry']['seq'] for item in durable
                if 'entry' in item]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        settlements = [item['entry'] for item in durable
                       if 'entry' in item
                       and 'command_settled' in item['entry']['event']]
        self.assertTrue(settlements, durable)
        self.assertTrue(self.feed.point)
        self.assertEqual(self.feed.sinks['a'].lost, 0)
        self.assertEqual(self.feed.a_role, 'active')
        self.assertTrue(self.feed.b_tracking)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/journal-sink-baseline.json',
            'evidence/journal-sink-window.json',
            'evidence/journal-sink-command.json',
            'evidence/journal-sink-restored.json'})
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent / entry['ref']).exists(),
                            entry)
        window = json.loads(
            (self.evidence / 'journal-sink-window.json').read_text())
        self.assertEqual(window['lag'], 'lagging')
        self.assertFalse(window['answered_while_lagging'])
        restored = json.loads(
            (self.evidence / 'journal-sink-restored.json').read_text())
        self.assertTrue(restored['contiguous'], restored)
        self.assertEqual(restored['lost'], 0)

    def test_lag_never_surfaces_fails(self):
        self.feed.hides_lag = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('lagging', record['detail'])

    def test_tick_stall_under_lag_fails(self):
        self.feed.stall_on_lag = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('tick', record['detail'])

    def test_late_serving_read_fails(self):
        self.feed.late_reads = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('past the', record['detail'])

    def test_io_growth_under_stall_fails(self):
        self.feed.degrades_io = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('io_health', record['detail'])

    def test_overrun_growth_under_stall_fails(self):
        self.feed.overruns_under_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('scan_overruns', record['detail'])

    def test_sink_failed_state_fails(self):
        self.feed.fails_sink = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('failed state', record['detail'])

    def test_unbounded_depth_fails(self):
        self.feed.overreports_depth = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('declared capacity', record['detail'])

    def test_lost_records_fail(self):
        self.feed.loses_records = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('lost', record['detail'])

    def test_early_journal_answer_is_nondeterministic(self):
        self.feed.serve_without_attest = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('answered while the journal sink stood stalled',
                      record['detail'])

    def test_durable_gap_is_nondeterministic(self):
        self.feed.corrupt_durable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('torn line', record['detail'])

    def test_duplicate_durable_record_is_nondeterministic(self):
        self.feed.duplicate_durable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('contiguous append axis', record['detail'])

    def test_reordered_window_is_nondeterministic(self):
        self.feed.reorder_window = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('disordered', record['detail'])

    def test_retained_window_without_settlements_fails(self):
        self.feed.drop_settlements = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('settlement', record['detail'])

    def test_unanswered_admission_fails(self):
        # The second submission's attest never lands: its record stays
        # queued behind the parked writer and the answer never arrives.
        self.feed.refuse_attest = 2
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('never answered with a receipt', record['detail'])

    def test_released_writer_never_drains_fails(self):
        self.feed.release_ignored = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-sink-isolation-failed', record['detail'])
        self.assertIn('never drained', record['detail'])

    def test_owner_move_fails(self):
        self.feed.move_roles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reconverged', record['detail'])

    def test_untracked_peer_fails(self):
        self.feed.untracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking', record['detail'])

    def test_missing_lever_is_inconclusive(self):
        for seam in ('drain_writers', 'park_drain_writer',
                     'release_drain_writer'):
            record = self.run_scenario(ctx={seam: None})
            self.assertEqual(record['outcome'], 'inconclusive', record)
            self.assertIn(seam, record['detail'])

    def test_unattributable_writer_is_inconclusive(self):
        # The bypass defect: the append runs inside the recording point,
        # so no parked writer can hold a journaled record — the lever
        # admits nothing to attribute and the leg reports inconclusive
        # rather than asserting against a sink it never stalled.
        self.feed.direct_journal_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('mount lever', record['detail'])

    def test_park_refused_is_inconclusive(self):
        # The runner cannot attach the tracer: every candidate is
        # refused, which the leg reports as an absent lever.
        record = self.run_scenario(
            ctx={'park_drain_writer': lambda tid: (_ for _ in ()).throw(
                OSError(1, 'operation not permitted'))})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('mount lever', record['detail'])

    def test_missing_journal_file_is_inconclusive(self):
        (self.dirs['a'] / 'journal.jsonl').unlink(missing_ok=True)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('--journal-file', record['detail'])

    def test_predated_contract_is_inconclusive(self):
        self.feed.no_sink_section = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal_sink', record['detail'])

    def test_torn_baseline_is_inconclusive(self):
        # A corrupt record mid-file, not the torn tail a killed writer
        # leaves: the pre-window audit cannot speak for the file's axis
        # at all, so the leg reports inconclusive.
        (self.dirs['a'] / 'journal.jsonl').write_text(
            '{"entry":{"seq":1}}\nnot a journal record\n'
            '{"entry":{"seq":2}}\n')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('audited', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.b_tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_no_drain_writer_is_inconclusive(self):
        # No `dcs-drain` writer thread to name — the container exposes
        # none, so the leg reports the lever's absence.
        record = self.run_scenario(
            ctx={'drain_writers': lambda name: []})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('dcs-drain writer threads', record['detail'])

    def test_scenario_ctx_carries_the_lever(self):
        # The runner hands the leg the drain-stall triple, bound to the
        # deployed pair's containers.
        cfg = dict(runner.DEFAULT_CONFIG)
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        with patch.object(
                runner, '_drain_stall_lever',
                return_value=(runner.drain_writers,
                              runner.park_drain_writer,
                              runner.release_drain_writer)), \
                patch.object(runner, 'drain_writers',
                             lambda run_id, name, pair='deployed':
                             [21, 22]):
            ctx = runner._scenario_ctx(
                cfg, record, Path('src'), self.run_dir, 'evidence', 0,
                lambda event, detail=None: None)
            self.assertTrue(callable(ctx['drain_writers']))
            self.assertTrue(callable(ctx['park_drain_writer']))
            self.assertTrue(callable(ctx['release_drain_writer']))
            # The ctx's listing is bound to the run's own containers.
            self.assertEqual(ctx['drain_writers']('active'), [21, 22])
            self.assertEqual(ctx['drain_writers']('standby'), [21, 22])

    def test_ctx_without_tracer_carries_no_lever(self):
        cfg = dict(runner.DEFAULT_CONFIG)
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        with patch.object(runner, '_drain_stall_lever',
                          return_value=None):
            ctx = runner._scenario_ctx(
                cfg, record, Path('src'), self.run_dir, 'evidence', 0,
                lambda event, detail=None: None)
        self.assertIsNone(ctx['drain_writers'])
        self.assertIsNone(ctx['park_drain_writer'])
        self.assertIsNone(ctx['release_drain_writer'])

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two passes over the same
        # staged transitions record the same report and evidence bytes
        # — the feed is call-count keyed, never wall-clock.
        runs = []
        for _index in range(2):
            feed = self.fresh_run()
            runs.append((self.run_scenario(feed=feed),
                         {path.name: path.read_bytes()
                          for path in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()