"""The 3650_state_file_isolation leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_state_file_isolation, split out of the test_qa_scenarios
monolith (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import collections
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'StateFileIsolationTests.test_registered_in_scenarios',
    'StateFileIsolationTests.test_clean_rig_passes_and_validates',
    'StateFileIsolationTests.test_lever_stages_and_releases_the_fifo',
    'StateFileIsolationTests.test_lag_never_surfaces_fails',
    'StateFileIsolationTests.test_tick_stall_under_lag_fails',
    'StateFileIsolationTests.test_io_growth_under_stall_fails',
    'StateFileIsolationTests.test_overrun_growth_under_stall_fails',
    'StateFileIsolationTests.test_sink_failed_state_fails',
    'StateFileIsolationTests.test_early_answer_is_nondeterministic',
    'StateFileIsolationTests.test_tick_regress_is_nondeterministic',
    'StateFileIsolationTests.test_counter_regress_is_nondeterministic',
    'StateFileIsolationTests.test_lag_flicker_is_nondeterministic',
    'StateFileIsolationTests.test_stale_durable_file_is_nondeterministic',
    'StateFileIsolationTests.test_unanswered_admission_fails',
    'StateFileIsolationTests.test_rejected_admission_fails',
    'StateFileIsolationTests.test_lost_captures_fail',
    'StateFileIsolationTests.test_owner_move_fails',
    'StateFileIsolationTests.test_untracked_peer_fails',
    'StateFileIsolationTests.test_missing_lever_is_inconclusive',
    'StateFileIsolationTests.test_undeclared_endpoint_is_inconclusive',
    'StateFileIsolationTests.test_predated_contract_is_inconclusive',
    'StateFileIsolationTests.test_unreachable_rig_is_inconclusive',
    'StateFileIsolationTests.test_unsettled_pair_is_inconclusive',
    'StateFileIsolationTests.test_failed_impede_is_inconclusive',
    'StateFileIsolationTests.test_failed_restore_is_inconclusive',
    'StateFileIsolationTests.test_scenario_ctx_carries_the_lever',
    'StateFileIsolationTests.test_undeclared_config_carries_no_lever',
    'StateFileIsolationTests.test_misconfigured_mounts_fail_loudly',
    'StateFileIsolationTests.test_two_runs_produce_identical_evidence',
})


class _FakeSink:
    """One controller's --state-file drain, emulated with the real
    mechanism the lever acts on: a dedicated writer thread serializes
    each queued capture through write('state.json.tmp')+rename inside
    the controller's runner-owned dir — the open the lever's staged
    FIFO blocks — while the scan-side offer() piles captures into a
    bounded queue and the admission path's attest() waits on the
    drained ordinal off the scan's lock."""

    def __init__(self, directory, feed):
        self.directory = Path(directory)
        self.feed = feed
        self.accepted = 0
        self.drained = 0
        self.lost = 0
        self.high_water = 0
        self.capacity = 64
        self.failed = False
        self._pending = collections.deque()
        self._cv = threading.Condition()
        self._stop = False
        self._writer = threading.Thread(target=self._drain,
                                        daemon=True)
        self._writer.start()

    def offer(self, payload):
        """The nonblocking capture handoff — the returned ordinal is
        what attest() waits on; a full or failed queue refuses by
        name (the contract's fatal edge)."""
        if self.feed.direct_state_writes:
            # The bypass defect: the sink writes the state file
            # synchronously and never queues — the staged stall never
            # bites, so the named lagging state never surfaces.
            self.directory.joinpath('state.json').write_text(payload)
            with self._cv:
                self.accepted += 1
                self.drained += 1
                self._cv.notify_all()
                return self.accepted
        with self._cv:
            if self.failed or len(self._pending) >= self.capacity:
                self.lost += 1
                self.failed = True
                return None
            self._pending.append(payload)
            self.accepted += 1
            self.high_water = max(self.high_water,
                                  len(self._pending))
            self._cv.notify()
            return self.accepted

    def stop(self):
        """End the drain loop without writing. A writer parked inside
        the staged FIFO's open() is released by pairing it with a
        momentary reader so the thread exits instead of leaking past
        teardown."""
        with self._cv:
            self._stop = True
            self._pending.clear()
            self._cv.notify_all()
        tmp = self.directory / 'state.json.tmp'
        fd = None
        if runner._is_fifo(tmp):
            try:
                fd = os.open(tmp, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                fd = None
        self._writer.join(2)
        if fd is not None:
            os.close(fd)

    def _drain(self):
        while True:
            with self._cv:
                while not self._pending and not self._stop:
                    self._cv.wait(0.02)
                if self._stop:
                    return
                payload = self._pending.popleft()
            tmp = self.directory / 'state.json.tmp'
            if self.feed.fail_sink and runner._is_fifo(tmp):
                # The mount error the contract names: the drain's
                # next write to the staged mount fails and the sink
                # reports its failed state.
                with self._cv:
                    self.failed = True
                    self._pending.clear()
                    self._cv.notify_all()
                continue
            try:
                tmp.write_text(payload)   # blocks on the staged FIFO
                os.replace(tmp, self.directory / 'state.json')
            except OSError:
                # Teardown unlinked the run dir out from under a
                # writer still parked in the staged open — the
                # capture is abandoned, not drained.
                continue
            with self._cv:
                self.drained += 1
                self._cv.notify_all()

    def attest(self, ordinal, bound=30):
        """The request worker's own bounded wait: the receipted
        command's answer only once the drain covered the admission's
        capture."""
        if ordinal is None:
            return False
        end = time.monotonic() + bound
        with self._cv:
            while self.drained < ordinal and not self.failed:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(min(left, 0.05))
            return self.drained >= ordinal and not self.failed

    def health(self):
        """The publication.state_sink section the monitor serves."""
        with self._cv:
            depth = len(self._pending)
            state = ('failed' if self.failed else
                     ('lagging' if depth else 'healthy'))
            if self.feed.lag_flickers and self.feed.stalled \
                    and state == 'lagging' \
                    and self.feed.flickered:
                state = 'healthy'
            elif self.feed.lag_flickers and self.feed.stalled \
                    and state == 'lagging':
                self.feed.flickered = True
            return {'state': state, 'accepted': self.accepted,
                    'drained': self.drained, 'lost': self.lost,
                    'depth': depth, 'high_water': self.high_water,
                    'capacity': self.capacity}


class StateFileFeed:
    """A stubbed pair for the state-file-isolation scenario. ctrl-a
    owns the field — every request on it is one completed scan:
    tick advance, receipt application, and a checkpoint capture the
    sink writer drains through write(tmp)+rename on the real
    runner-owned mount, the open the lever's staged FIFO blocks.
    ctrl-b tracks its stream. The feed's fault flags stage each
    named defect the leg's diagnostics cover."""

    def __init__(self, dirs):
        self.dirs = {key: Path(value) for key, value in dirs.items()}
        self.sinks = {key: _FakeSink(value, self)
                      for key, value in dirs.items()}
        self.a_tick = 400
        self.b_tick = 400
        self.aligned = 400
        self.a_role = 'active'
        self.b_role = 'standby'
        self.b_tracking = True
        self.attempts = 0
        self.receipts = []
        self.point = False
        self.stalled = False      # set by the ctx wrapper beside the
                                  # real runner action
        self.flickered = False
        self.restore_raises = False    # the lever's restore half errs
        self.untracks = False          # the standby drops tracking on
                                       # restore
        self._was_stalled = False
        # Fault flags staging the named failures.
        self.down = False              # the rig is unreachable
        self.no_sink_section = False   # predates the served contract
        self.answer_immediately = False  # the pre-#982 ordering defect
        self.never_attests = False     # the answer never lands
        self.direct_state_writes = False  # the stall never bites
        self.stall_on_lag = False      # scans lengthen while queued
        self.fail_sink = False         # the drain's next write fails
        self.degrades_io = False       # boundary counters grow stalled
        self.overruns_under_stall = False  # scan_overruns grows stalled
        self.regress_counters = False  # io_health counters shrink
        self.regress_tick = False      # the served tick rewinds
        self.lag_flickers = False      # lagging clears unrestored
        self.audit_lies = False        # durable captures predate the
                                       # admission they attest
        self.move_roles = False        # the stall fails the pair over
        self.reject_command = False    # the window write is refused
        self._lock = threading.Lock()

    def _capture(self):
        """The checkpoint body a drain write persists — the file-side
        admission counter the durability audit correlates."""
        attempts = self.attempts - (1 if self.audit_lies else 0)
        return json.dumps({'tick': self.a_tick,
                           'command_admission': {
                               'attempts': attempts}})

    def _scan(self, peer):
        """One completed scan cycle: the tick advance and the capture
        offer; a refused push is the contract's fatal edge."""
        self._was_stalled = self._was_stalled or self.stalled
        sink = self.sinks[peer]
        if peer == 'a':
            if self.stall_on_lag and sink.health()['depth'] > 0:
                # The lengthened scan the contract forbids — no tick,
                # no capture.
                return False
            if self.regress_tick and sink.health()['depth'] > 0:
                self.a_tick -= 1
                # The served tick rewinds under the stall — still no
                # capture; the regression is the defect.
                return False
            self.a_tick += 1
            if self.move_roles and self._was_stalled \
                    and not self.stalled:
                # The stall failed the pair over — the field owner
                # does not come back on restore.
                self.a_role = 'standby'
                self.b_role = 'active'
                self.b_tracking = False
            for receipt in self.receipts:
                accepted = receipt['outcome'].get('accepted')
                if accepted and self.a_tick >= accepted['apply_tick']:
                    write = receipt['command']['write_value']
                    self.point = write['value']['bool']
                    receipt['outcome'] = {'applied': {
                        'tick': self.a_tick}}
        else:
            self.b_tick += 1
            if self.untracks and self._was_stalled \
                    and not self.stalled:
                # The standby dropped out of tracking across the
                # stall and never regained it.
                self.b_tracking = False
            if self.b_tracking and self.b_role == 'standby':
                self.aligned = self.a_tick
        return True

    def _io_health(self):
        """ctrl-a's io_health section — flat counters while the stall
        stands, growing only under the staged defects."""
        failures = overruns = 0
        if self.regress_counters:
            failures = 0 if self.stalled else 3
        elif self.degrades_io and self.stalled:
            failures = self.a_tick - 400
        if self.overruns_under_stall and self.stalled:
            overruns = self.a_tick - 400
        return {'failed_reads': failures, 'failed_writes': 0,
                'failed_exchanges': 0,
                'consecutive_failures': failures,
                'scan_overruns': overruns, 'last_error': None}

    def _admit(self, body):
        """POST /command: validation, admission, the capture push,
        and the durability-attesting answer the contract orders."""
        self.attempts += 1
        write = body['command']['write_value']
        if self.reject_command:
            # A refused submission still receipts — it just never
            # promises durability, so it may answer at once.
            receipt = {'command': body['command'],
                       'outcome': {'rejected': {'reason': {
                           'not_writable': {'point': write['point']}}}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, copy.deepcopy(receipt)
        receipt = {'command': body['command'],
                   'outcome': {'accepted': {
                       'apply_tick': self.a_tick + 1}},
                   'actor': body.get('actor')}
        self.receipts.append(receipt)
        ordinal = self.sinks['a'].offer(self._capture())
        if self.answer_immediately:
            # The pre-#982 defect: the 200 answers before the file
            # could have covered the admission.
            return 200, copy.deepcopy(receipt)
        if self.never_attests \
                or not self.sinks['a'].attest(ordinal):
            raise urllib.error.URLError('the sink never attested '
                                        'the admission')
        # The wire body is the admission's serialization, not the log
        # entry's live view — a later settle must not rewrite what the
        # 200 attested.
        return 200, copy.deepcopy(receipt)

    def close(self):
        for sink in self.sinks.values():
            sink.stop()

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if self.down:
            raise urllib.error.URLError('connection refused')
        if host == 'ctrl-b:2':
            self._scan('b')
            if (method, route) == ('GET', '/role'):
                sync = ({'tracking': {'aligned': self.aligned}}
                        if self.b_tracking and self.b_role == 'standby'
                        else None)
                return 200, {'role': self.b_role,
                             'tick': self.b_tick, 'sync': sync}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('POST', '/command'):
            # Admission and the capture push hold the lock; the
            # attest wait is the request worker's own, off it — the
            # serving lane keeps answering while the answer parks.
            with self._lock:
                self._scan('a')
            return self._admit(body)
        with self._lock:
            scanned = self._scan('a')
            if (method, route) == ('GET', '/role'):
                response = {'role': self.a_role, 'tick': self.a_tick,
                            'sync': None}
            elif (method, route) == ('GET', '/snapshot'):
                publication = {'published': self.a_tick,
                               'coalesced': 0, 'depth': 0,
                               'window': 4}
                if not self.no_sink_section:
                    publication['state_sink'] = \
                        self.sinks['a'].health()
                response = {
                    'tick': self.a_tick,
                    'points': [{'point': 10, 'sample': {
                        'value': {'bool': self.point},
                        'quality': {'quality': 'good'}}}],
                    'publication': publication,
                    'io_health': self._io_health()}
            elif (method, route) == ('GET', '/signals'):
                response = {'points': [
                    {'point': 10, 'signal': None, 'name': 'p101-oos',
                     'direction': 'in', 'value_type': 'bool',
                     'writable': True}], 'components': []}
            elif (method, route) == ('GET', '/receipts'):
                response = list(self.receipts)
            elif (method, route) == ('GET', '/checkpoint'):
                response = {
                    'receipts': list(self.receipts),
                    'command_admission': {
                        'attempts': self.attempts}}
            else:
                raise AssertionError('unexpected request %s %s'
                                     % (method, url))
            # The completed scan's checkpoint capture hands to the
            # sink after the read — the served surface reports the
            # backlog the drain already holds, never the offer in
            # flight, so a drained queue reads depth 0.
            if scanned:
                self.sinks['a'].offer(self._capture())
            return 200, response


class StateFileIsolationTests(unittest.TestCase):
    """scenario_state_file_isolation against the stubbed pair: the
    real runner lever stages a reader-less FIFO at the sink's
    write-then-rename temporary inside the controller's runner-owned
    dir — the feed's drain writer genuinely blocks in the open until
    restore pairs a host reader — while the served surface must keep
    cadence, report the named lagging state, and hold the window
    admission's answer until the file caught up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text(json.dumps(
                {'tick': 400,
                 'command_admission': {'attempts': 0}}))
            self.dirs[name] = directory
        self.feed = StateFileFeed(
            {'a': self.dirs['a'], 'b': self.dirs['b']})

    def tearDown(self):
        self.feed.close()
        self.tmp.cleanup()

    def reset_dirs(self):
        for directory in self.dirs.values():
            tmp = directory / 'state.json.tmp'
            if tmp.exists():
                tmp.unlink()
            (directory / 'state.json').write_text(json.dumps(
                {'tick': 400,
                 'command_admission': {'attempts': 0}}))

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        self.addCleanup(feed.close)
        evidence = evidence if evidence is not None else self.evidence
        events = []
        mounts = {'active': 'fifo', 'standby': 'fifo'}

        def impede(name):
            # The real runner action — the staged FIFO under the
            # bounded run dir — then the feed's stalled marker.
            runner.impede_state_file(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event),
                mounts)
            feed.stalled = True

        def restore(name):
            if feed.restore_raises:
                raise RuntimeError('reader attach failed')
            runner.restore_state_file(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event))
            feed.stalled = False

        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': '127.0.0.1:9999',
                'evidence_dir': str(evidence),
                'impede_state_file': impede,
                'restore_state_file': restore,
                'state_files': {
                    'active': str(self.dirs['a'] / 'state.json'),
                    'standby': str(self.dirs['b'] / 'state.json')}}
        if ctx is not None:
            base.update(ctx)
        patches = {'SINK_POLL': 0.005, 'SINK_DEADLINE': 5,
                   'SINK_SETTLE': 5, 'SINK_ANSWER_DEADLINE': 10}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(runner, 'STATE_FILE_ATTACH_GRACE', 0.5), \
                patch.object(runner, 'STATE_FILE_BOUND', 3):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_state_file_isolation(base)
        return record, events

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_state_file_isolation,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # The real lever ran: the staged FIFO is gone, the state file
        # is a regular checkpoint again, and the drain covered the
        # window admission the answered receipt attested.
        self.assertIn('state-file-impeded', events)
        self.assertIn('state-file-restored', events)
        # The drain writer may still be covering the restored
        # window's last captures — a regular tmp legitimately stands
        # between write and rename until the queue is covered.
        self.assertTrue(self.feed.sinks['a'].attest(
            self.feed.sinks['a'].accepted))
        self.assertFalse(
            (self.dirs['a'] / 'state.json.tmp').exists())
        state = self.dirs['a'] / 'state.json'
        self.assertTrue(state.is_file())
        persisted = json.loads(state.read_text())
        self.assertGreaterEqual(
            persisted['command_admission']['attempts'], 1)
        self.assertEqual(self.feed.sinks['a'].lost, 0)
        # Both halves of the ordering evidence: the receipted command
        # settled applied and the pair stands a tracking standby.
        self.assertTrue(self.feed.point)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/state-file-baseline.json',
            'evidence/state-file-command.json',
            'evidence/state-file-window.json',
            'evidence/state-file-restored.json'})
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_lever_stages_and_releases_the_fifo(self):
        # The runner actions alone: a real writer blocks on the
        # staged FIFO, restore pairs a reader and the write completes
        # as state.json, then the next capture's regular write ends
        # the mount's regularity wait.
        events = []
        timeline = lambda event, detail=None: events.append(event)
        mounts = {'active': 'fifo'}
        tmp = self.dirs['a'] / 'state.json.tmp'
        runner.impede_state_file('qa-1', self.run_dir, 'active',
                                 timeline, mounts)
        self.assertTrue(runner._is_fifo(tmp))
        done, restored = [], []

        def writer():
            tmp.write_text('{"tick": 401}')
            os.replace(tmp, self.dirs['a'] / 'state.json')
            done.append(True)

        def restorer():
            runner.restore_state_file('qa-1', self.run_dir, 'active',
                                      timeline)
            restored.append(True)

        thread = threading.Thread(target=writer)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(done)          # the stalled open blocks
        with patch.object(runner, 'STATE_FILE_ATTACH_GRACE', 0.5):
            restorer_t = threading.Thread(target=restorer)
            restorer_t.start()
            thread.join(5)
            self.assertEqual(done, [True])
            # The next scan's capture: a regular write-then-rename
            # makes state.json an ordinary file again.
            tmp.write_text('{"tick": 402}')
            os.replace(tmp, self.dirs['a'] / 'state.json')
            restorer_t.join(5)
        self.assertEqual(restored, [True])
        self.assertEqual(
            json.loads((self.dirs['a'] / 'state.json').read_text())
            ['tick'], 402)
        self.assertEqual(events, ['state-file-impede',
                                  'state-file-impeded',
                                  'state-file-restore',
                                  'state-file-restored'])
        # An endpoint the config never declared has no lever.
        with self.assertRaises(RuntimeError):
            runner.impede_state_file('qa-1', self.run_dir, 'standby',
                                     timeline, mounts)
        # And restore with nothing staged fails loudly.
        with self.assertRaises(RuntimeError):
            runner.restore_state_file('qa-1', self.run_dir, 'active',
                                      timeline)

    def test_lag_never_surfaces_fails(self):
        self.feed.direct_state_writes = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('lagging', record['detail'])

    def test_tick_stall_under_lag_fails(self):
        self.feed.stall_on_lag = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('tick', record['detail'])

    def test_io_growth_under_stall_fails(self):
        self.feed.degrades_io = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('io_health.failed_reads', record['detail'])

    def test_overrun_growth_under_stall_fails(self):
        self.feed.overruns_under_stall = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('io_health.scan_overruns', record['detail'])

    def test_sink_failed_state_fails(self):
        self.feed.fail_sink = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('failed', record['detail'])

    def test_early_answer_is_nondeterministic(self):
        self.feed.answer_immediately = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('answered while the file', record['detail'])

    def test_tick_regress_is_nondeterministic(self):
        self.feed.regress_tick = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-nondeterministic',
                      record['detail'])

    def test_counter_regress_is_nondeterministic(self):
        self.feed.regress_counters = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('io_health', record['detail'])

    def test_lag_flicker_is_nondeterministic(self):
        self.feed.lag_flickers = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('lagging', record['detail'])

    def test_stale_durable_file_is_nondeterministic(self):
        self.feed.audit_lies = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-nondeterministic',
                      record['detail'])
        self.assertIn('admission', record['detail'])

    def test_unanswered_admission_fails(self):
        self.feed.never_attests = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])

    def test_rejected_admission_fails(self):
        self.feed.reject_command = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('rejected', record['detail'])

    def test_lost_captures_fail(self):
        feed = self.feed

        class LostSink(_FakeSink):
            def offer(self, payload):
                self.lost += 1 if self.feed.stalled else 0
                return super().offer(payload)

        feed.sinks['a'] = LostSink(self.dirs['a'], feed)
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('lost', record['detail'])

    def test_owner_move_fails(self):
        self.feed.move_roles = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('reconverge', record['detail'])

    def test_untracked_peer_fails(self):
        # The stall leaves the standby degraded past the restore —
        # the pair never reconverges to active + tracking.
        self.feed.untracks = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('state-file-isolation-failed',
                      record['detail'])
        self.assertIn('tracking', record['detail'])

    def test_missing_lever_is_inconclusive(self):
        record, _ = self.run_scenario(ctx={'impede_state_file': None,
                                           'restore_state_file': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('mount lever', record['detail'])

    def test_undeclared_endpoint_is_inconclusive(self):
        # The config declares no lever for the field owner — the
        # action refuses and the leg reports inconclusive.
        feed = self.feed
        events = []

        def impede(name):
            runner.impede_state_file(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event),
                {'standby': 'fifo'})
            feed.stalled = True

        record, _ = self.run_scenario(
            ctx={'impede_state_file': impede})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('impede', record['detail'])

    def test_predated_contract_is_inconclusive(self):
        self.feed.no_sink_section = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('state_sink', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.down = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.b_tracking = False
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_failed_impede_is_inconclusive(self):
        def impede(name):
            raise RuntimeError('mkfifo failed')

        record, _ = self.run_scenario(
            ctx={'impede_state_file': impede})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('impede', record['detail'])

    def test_failed_restore_is_inconclusive(self):
        self.feed.restore_raises = True
        record, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restore', record['detail'])

    def test_scenario_ctx_carries_the_lever(self):
        calls, events = [], []
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG), record, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: events.append(event))
        self.assertTrue(callable(ctx['impede_state_file']))
        self.assertTrue(callable(ctx['restore_state_file']))
        ctx['impede_state_file']('standby')
        tmp = self.dirs['b'] / 'state.json.tmp'
        self.assertTrue(runner._is_fifo(tmp))
        # And the staged stall releases on restore — no writer ever
        # attaches, so the grace removes the orphaned node.
        ctx['restore_state_file']('standby')
        self.assertFalse(tmp.exists())

    def test_undeclared_config_carries_no_lever(self):
        cfg = dict(runner.DEFAULT_CONFIG)
        cfg['state_file_mounts'] = {}
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            cfg, record, Path('src'), self.run_dir, 'evidence', 0,
            lambda event, detail=None: None)
        self.assertIsNone(ctx['impede_state_file'])
        self.assertIsNone(ctx['restore_state_file'])

    def test_misconfigured_mounts_fail_loudly(self):
        cfg = dict(runner.DEFAULT_CONFIG)
        for bad in ({'active': 'throttle'}, {'forge': 'fifo'},
                    'fifo'):
            cfg['state_file_mounts'] = bad
            with self.assertRaises(RuntimeError, msg=repr(bad)):
                runner._state_file_mounts(cfg)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two passes over the same
        # staged transitions record the same report and evidence
        # bytes — the feed is call-count keyed, never wall-clock.
        runs = []
        for _index in range(2):
            self.reset_dirs()
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = StateFileFeed(
                {'a': self.dirs['a'], 'b': self.dirs['b']})
            record, _ = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
