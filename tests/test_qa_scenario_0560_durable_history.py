"""The 0560_durable_history leg's scenario unit coverage — the feed
fake and TestCase classes for scenario_durable_history, following the
module-per-leg convention (#928, #940). The shared fakes and helpers
live in tests/qa_scenario_support.py.
"""
import importlib
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class DurableHistoryFeed:
    """A stubbed pair for the durable-history scenario. ctrl-a owns the
    field; every answered monitor request on it is one scan, pushing
    each declared-duty point's post-scan sample onto the durable
    stream — the --history-file's append axis — through the bounded
    drain queue the feed models: pushes land in `pending`, the writer
    drains them to the file unless its `dcs-drain` thread stands
    parked, and `publication.history_sink` reports the queue's
    standing health. The served window is the bounded tail plus the
    pinned markers eviction migrates aside; a restart replays the
    file, continues the seq axis, and lands the new lifetime's
    anchored run-boundary marker. Fault flags stage the leg's named
    defects."""

    POINTS = (10, 11, 12, 13)
    CAPACITY = 1024

    def __init__(self, history_a, history_b):
        self.a_tick = 400
        self.b_tick = 400
        self.a_up = True
        self.a_down = 0          # refused-request window while down
        self.run = 1             # the process's real lifetime ordinal
        self.next_seq = 1        # the file's append axis
        self.tail = []           # the bounded served window
        self.pinned = []         # evicted markers, still served
        self.cap = 8             # the retained tail's bound
        self.file = Path(history_a)
        self.accepted = 0        # records handed to the drain queue
        self.drained = 0         # records the writer appended
        self.pending = []        # queued, not yet drained
        self.high_water = 0
        self.writers = [21, 22, 23]   # the container's dcs-drain tids
        self.history_tid = 22         # the history sink's writer
        self.parked = set()
        self.restarts = []
        # Fault flags staging the named failures and inconclusives.
        self.down = False            # every request refused
        self.predates = False        # no durable surface at all
        self.no_duty = False         # no point declares recording duty
        self.gap_silent = False      # a stale cursor reads fabricated
                                   # continuity
        self.stretch_cap = False     # the retained window's bound slips
        self.drops_marker = False    # the restart's boundary entry
                                     # never lands
        self.unanchored = False      # markers carry no civil-time
                                     # anchor
        self.resumes_cold = False    # the restart restarts the seq axis
        self.stall_silent = False    # the stalled sink never reports
                                     # lagging
        self.scan_stalls = False     # the parked writer lengthens scans
        self.never_drains = False    # the released writer never drains
        self.released_once = False
        self.corrupt = False         # a corrupt line lands mid-file
        self.promotes = False        # ctrl-b promotes inside the gap
        self.returns = True          # False: the restart action fails
        self.serves = True           # False: the monitor never returns
        self._write({'run_boundary': {'run': 1, 'tick': 0,
                                      'anchor': {'epoch_ms': 1000}}})
        # The first lifetime's boundary is a served durable entry too
        # — seq 1 on the stream — migrating to the pinned set when the
        # tail's churn evicts it.
        self._push({'seq': self.next_seq, 'tick': 0,
                    'event': {'run_boundary': {'run': 1,
                                               'anchor':
                                               {'epoch_ms': 1000}}}})

    # ---- the --history-file ----

    def _write(self, record):
        with self.file.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    # ---- the recorder's post-scan push ----

    def _kind(self, entry):
        return next(iter(entry['event']))

    def _push(self, entry):
        self.tail.append(entry)
        self.next_seq += 1
        while len(self.tail) > self.cap:
            evicted = self.tail.pop(0)
            if self._kind(evicted) != 'sampled':
                self.pinned.append(evicted)
            if self.stretch_cap:
                # The defect: the bound slips a slot per eviction —
                # the front still rolls, but the window keeps
                # growing past its declared bound.
                self.cap += 1
        self.pending.append(entry)
        self.accepted += 1
        self.high_water = max(self.high_water, len(self.pending))

    def _drain(self):
        """The writer's beat: queued records append to the file — unless
        the history drain's thread stands parked, or the released
        writer never resumed."""
        if self.history_tid in self.parked:
            return
        if self.never_drains and self.released_once:
            return
        for entry in self.pending:
            self._write({'entry': entry})
        self.drained += len(self.pending)
        self.pending = []

    def _scan(self):
        """One completed scan on the field owner: the declared-duty
        census's post-scan samples record, then the writer's beat. A
        stalled sink must never lengthen the scan — the defect flag
        freezes the tick while the queue keeps taking records."""
        stalled = self.scan_stalls and self.history_tid in self.parked
        if not stalled:
            self.a_tick += 1
        if not self.no_duty:
            for point in self.POINTS:
                self._push({'seq': self.next_seq, 'tick': self.a_tick,
                            'event': {'sampled': {
                                'point': point,
                                'sample': {'value': {'float':
                                                     float(self.a_tick)},
                                           'quality': {'quality':
                                                       'good'}}}}})
        self._drain()

    # ---- the runner-owned actions' simulated halves ----

    def restart(self, name):
        """The warm restart: the container cycles, the file gains one
        run_boundary record, the served window replays the file's
        retained tail, the seq axis continues, and the new lifetime's
        boundary marker lands as a durable entry."""
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        assert name == 'active'
        self.a_up = False
        self.a_down = 2
        self.run += 1
        self.pending = []        # the killed process's queue dies with it
        # The file's replay: the retained tail re-serves with the same
        # pinning rule the live ring ran.
        self.tail, self.pinned = [], []
        for line in self.file.read_text().splitlines():
            record = json.loads(line)
            if 'entry' in record:
                entry = record['entry']
                self.tail.append(entry)
                while len(self.tail) > self.cap:
                    evicted = self.tail.pop(0)
                    if self._kind(evicted) != 'sampled':
                        self.pinned.append(evicted)
                self.next_seq = entry['seq'] + 1
        if self.resumes_cold:
            # The defect: the file's axis is abandoned — the new run
            # restarts numbering on an empty window.
            self.next_seq = 1
            self.tail, self.pinned = [], []
        # The new lifetime's file marker — written synchronously at
        # open — then its served entry through the drain queue.
        anchor = None if self.unanchored \
            else {'epoch_ms': 1700000000000 + self.a_tick}
        mark = {'run': self.run, 'tick': self.a_tick}
        if anchor:
            mark['anchor'] = anchor
        if self.corrupt and self.run == 2:
            self._write_raw('{"entry": torn')
        self._write({'run_boundary': mark})
        if not self.drops_marker:
            event = {'run': self.run}
            if anchor:
                event['anchor'] = anchor
            self._push({'seq': self.next_seq, 'tick': self.a_tick,
                        'event': {'run_boundary': event}})

    def _write_raw(self, line):
        with self.file.open('a') as stream:
            stream.write(line + '\n')

    # ---- the drain-stall lever's simulated halves ----

    def drain_writers(self, name):
        assert name == 'active'
        return list(self.writers)

    def park(self, tid):
        self.parked.add(tid)

    def release(self, tid):
        self.parked.discard(tid)
        self.released_once = True
        self._drain()

    # ---- the served wire ----

    def _sink(self):
        depth = len(self.pending)
        state = 'lagging' if depth else 'healthy'
        if self.stall_silent:
            state = 'healthy'
        return {'state': state, 'accepted': self.accepted,
                'drained': self.drained, 'lost': 0, 'depth': depth,
                'high_water': self.high_water,
                'capacity': self.CAPACITY}

    def _durable(self, params):
        since = int(params.get('since', '0'))
        point = params.get('point')
        # The durability-attesting wait: a parked writer stands the
        # read until the queue's release — bounded like the wire's.
        deadline = time.monotonic() + 10
        while self.history_tid in self.parked \
                and time.monotonic() < deadline:
            time.sleep(0.005)
        entries = [entry for entry in self.pinned + self.tail
                   if entry['seq'] > since]
        if self.gap_silent and entries and since \
                and entries[0]['seq'] > since + 1:
            # The defect: the evicted stretch is filled in with
            # fabricated entries — the cursor reads seamless
            # continuity.
            entries = [{'seq': seq, 'tick': self.a_tick,
                        'event': {'sampled': {
                            'point': self.POINTS[0],
                            'sample': {'value': {'float': 0.0},
                                       'quality': {'quality':
                                                   'good'}}}}}
                       for seq in range(since + 1, entries[0]['seq'])] \
                + entries
        if point is not None:
            point = int(point)
            entries = [entry for entry in entries
                       if self._kind(entry) != 'sampled'
                       or entry['event']['sampled']['point'] == point]
        return 200, entries

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        params = dict(pair.split('=', 1)
                      for pair in query.split('&') if pair)
        if self.down:
            raise urllib.error.URLError('connection refused')
        side = 'b' if host == 'ctrl-b:2' else 'a'
        if side == 'b':
            if (method, route) == ('GET', '/role'):
                self.b_tick += 1
                role = 'promoting' \
                    if self.promotes and not self.a_up else 'standby'
                return 200, {'role': role, 'tick': self.b_tick,
                             'sync': {'tracking':
                                      {'aligned': self.a_tick}}}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.a_up:
            self.a_down -= 1
            if self.a_down <= 0 and self.serves:
                self.a_up = True
            else:
                raise urllib.error.URLError('connection refused')
        self._scan()
        if (method, route) == ('GET', '/history/durable'):
            if self.predates:
                raise urllib.error.HTTPError(
                    url, 404, 'not found', {}, None)
            return self._durable(params)
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.a_tick,
                         'sync': None}
        if (method, route) == ('GET', '/snapshot'):
            publication = {'published': self.a_tick}
            if not self.predates:
                publication['history_sink'] = self._sink()
            return 200, {'tick': self.a_tick,
                         'publication': publication}
        raise AssertionError('unexpected request %s %s' % (method, url))


class DurableHistoryTests(unittest.TestCase):
    """scenario_durable_history against the stubbed pair: the served
    durable stream's declared-duty samples and point filter, the
    bounded window's eviction gap, the field owner's restart
    re-serving the file's axis behind a new anchored run-boundary
    marker, the parked drain writer's lagging sink that lengthens no
    scan, the released writer's drain, the file's contiguous
    append-axis audit, and the launch roles restored — twice, with
    identical digests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{"tick": 400}')
            self.dirs[name] = directory
        self.history_a = self.dirs['a'] / 'history.jsonl'
        self.history_b = self.dirs['b'] / 'history.jsonl'
        self.feed = DurableHistoryFeed(self.history_a, self.history_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, ctx=None):
        feed = feed if feed is not None else self.feed
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return None

        def restart(name):
            runner.restart_controller(
                'qa-1', name,
                lambda event, detail=None: events.append(event))
            feed.restart(name)

        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'evidence_dir': str(self.evidence),
                'restart_controller': restart,
                'failover_misses': 120,
                'history_files': {'active': str(self.history_a),
                                  'standby': str(self.history_b)},
                'drain_writers': feed.drain_writers,
                'park_drain_writer': feed.park,
                'release_drain_writer': feed.release}
        if ctx is not None:
            base.update(ctx)
        patches = {'POLL_INTERVAL': 0.001, 'DURABLE_SETTLE': 0.5,
                   'DURABLE_RETURN': 0.5, 'DURABLE_POLL': 0.001,
                   'DURABLE_STIR_POLL': 0.002, 'DURABLE_DEADLINE': 1.0,
                   'DURABLE_RETAIN': 1.0, 'DURABLE_STIR': 2,
                   'DURABLE_PROBE': 0.3, 'DURABLE_TICKS': 2,
                   'DURABLE_HOLD': 0.05, 'DURABLE_ANSWER': 5.0}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(runner, 'docker', fake_docker):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_durable_history(base)
        return record, calls, events

    def _pass_evidence(self, number):
        name = 'durable-history-pass-' + str(number) + '.json'
        return json.loads((self.evidence / name).read_text())

    def _file_records(self):
        return [json.loads(line)
                for line in self.history_a.read_text().splitlines()
                if line.strip()]

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_durable_history,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record, calls, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # The real runner restart action ran once per pass on the
        # field owner's container.
        self.assertEqual(self.feed.restarts, ['active', 'active'])
        self.assertEqual(calls.count(('start', 'dcs-hw-qa-1-a')), 2)
        self.assertIn('controller-restarted', events)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Three lifetimes on the file, one run_boundary record each.
        marks = [item['run_boundary'] for item in self._file_records()
                 if 'run_boundary' in item]
        self.assertEqual([mark['run'] for mark in marks], [1, 2, 3])
        self.assertTrue(all('epoch_ms' in (mark.get('anchor') or {})
                            for mark in marks))
        # The append axis: contiguous entry seqs, never reused.
        seqs = [item['entry']['seq'] for item in self._file_records()
                if 'entry' in item]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        digests = [self._pass_evidence(n)['digest'] for n in (1, 2)]
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[0], {
            'duty': 'recording', 'filter': 'point-scoped',
            'retention': 'gap-honest', 'resume': 'continued',
            'anchor': 'stamped', 'stall': 'lagging-bounded',
            'drain': 'caught-up', 'file': 'audited',
            'roles': 'restored'})

    def test_restart_reserves_the_window_attributed(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for number in (1, 2):
            restart = self._pass_evidence(number)['restart']
            # The new lifetime's boundary lands served — the retained
            # tail below it re-serves across the seam — and fresh
            # samples continue above the held cursor.
            self.assertIn(number + 1, restart['resumed']
                          ['boundary_runs'])
            # The boundary's seq continues the file's axis — it lands
            # above 1 rather than restarting the window's numbering.
            self.assertGreater(restart['window']['boundary_seq'], 1)
            self.assertLessEqual(restart['window']['front'][0],
                                 restart['window']['boundary_seq'])
            self.assertTrue(restart['resumed']['post'])
            self.assertTrue(all(
                seq > restart['before']['cursor']
                for seq in restart['resumed']['post']))

    def test_retention_evicts_with_honest_gap(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for number in (1, 2):
            leg = self._pass_evidence(number)['retention']
            self.assertGreater(leg['retained']['front'], 1)
            probe = leg['gap_probe']
            self.assertGreater(probe['head'][0], probe['since'] + 1)
            self.assertLessEqual(leg['bound']['retained1'],
                                 leg['bound']['retained0'])

    def test_stall_lags_the_sink_not_the_scan(self):
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for number in (1, 2):
            stall = self._pass_evidence(number)['stall']
            self.assertEqual(stall['held'], self.feed.history_tid)
            self.assertTrue(any(sample['state'] == 'lagging'
                                for sample in stall['held_samples']))
            self.assertTrue(stall['standing'])
            self.assertEqual(stall['answer']['status'], 200)
            self.assertGreaterEqual(
                stall['ticks']['after'] - stall['ticks']['before'], 2)
            self.assertEqual(stall['restored_sink']['state'],
                             'healthy')

    def test_silent_eviction_gap_fails(self):
        self.feed.gap_silent = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        self.assertIn('cursor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unbounded_window_fails(self):
        self.feed.stretch_cap = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        self.assertIn('bound', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_restart_fails(self):
        self.feed.drops_marker = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        self.assertIn('run_boundary', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unanchored_marker_fails(self):
        self.feed.unanchored = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        self.assertIn('anchor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restarted_axis_fails(self):
        self.feed.resumes_cold = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_stall_fails(self):
        self.feed.stall_silent = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        self.assertIn('lagging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_lengthened_scan_fails(self):
        self.feed.scan_stalls = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_undrained_release_fails(self):
        self.feed.never_drains = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_corrupt_file_fails(self):
        self.feed.corrupt = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_spurious_promotion_fails(self):
        self.feed.promotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promot', record.get('detail', ''))
        report.validate_scenario(record)

    def test_predating_rig_is_inconclusive(self):
        self.feed.predates = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('durable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_duty_is_inconclusive(self):
        self.feed.no_duty = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_no_lever_is_inconclusive(self):
        record, _, _ = self.run_scenario(
            ctx={'drain_writers': None, 'park_drain_writer': None,
                 'release_drain_writer': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('drain_writers', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_restart_is_inconclusive(self):
        self.feed.returns = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        # The named diagnostic's other half: two clean passes whose
        # normalized verdicts disagree.
        leg = importlib.import_module(
            'qa_lane.scenarios.0560_durable_history')
        real = leg._durable_pass

        def wrapper(ctx, number, owner, peer):
            digest, violations, evidence = real(ctx, number, owner,
                                                peer)
            if number == 2 and digest is not None:
                digest = dict(digest, drain='skipped')
            return digest, violations, evidence

        with patch.object(scenarios, '_durable_pass', wrapper):
            record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('durable-history-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        record1, _, _ = self.run_scenario()
        evidence1 = [self._pass_evidence(n) for n in (1, 2)]
        for number in (1, 2):
            (self.evidence
             / ('durable-history-pass-' + str(number) + '.json')
             ).unlink()
        record2, _, _ = self.run_scenario()
        evidence2 = [self._pass_evidence(n) for n in (1, 2)]
        self.assertEqual(record1['outcome'], 'passed')
        self.assertEqual(record2['outcome'], 'passed')
        for first, second in zip(evidence1, evidence2):
            self.assertEqual(first['digest'], second['digest'])


if __name__ == '__main__':
    unittest.main()
