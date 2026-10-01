"""The 3690_scan_batch_bound leg's scenario unit coverage — the feed
fake for the driven /scan batch contract, the raw-wire monitor face
the throwaway severed client posts to, and the TestCase class for
scenario_scan_batch_bound, per the module-per-leg test convention
(#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ScanBatchBoundTests.test_registered_in_scenarios',
    'ScanBatchBoundTests.test_clean_rig_passes_and_validates',
    'ScanBatchBoundTests.test_two_passes_share_one_digest',
    'ScanBatchBoundTests.test_two_runs_produce_identical_records',
    'ScanBatchBoundTests.test_scenario_ctx_carries_the_driven_actions',
    'ScanBatchBoundTests.test_missing_actions_are_inconclusive',
    'ScanBatchBoundTests.test_unreachable_rig_is_inconclusive',
    'ScanBatchBoundTests.test_unsettled_pair_is_inconclusive',
    'ScanBatchBoundTests.test_no_active_is_failed',
    'ScanBatchBoundTests.test_unkeyed_pair_is_inconclusive',
    'ScanBatchBoundTests.test_probe_subject_rebinds',
    'ScanBatchBoundTests.test_driven_launch_failure_is_inconclusive',
    'ScanBatchBoundTests.test_driven_never_serves_is_inconclusive',
    'ScanBatchBoundTests.test_predated_contract_is_inconclusive',
    'ScanBatchBoundTests.test_over_bound_accepted_fails',
    'ScanBatchBoundTests.test_over_bound_running_unanswered_fails',
    'ScanBatchBoundTests.test_wrong_verdict_fails',
    'ScanBatchBoundTests.test_unnamed_refusal_fails',
    'ScanBatchBoundTests.test_refused_batch_ran_fails',
    'ScanBatchBoundTests.test_refused_demote_fails',
    'ScanBatchBoundTests.test_refused_driven_promote_fails',
    'ScanBatchBoundTests.test_driven_never_active_fails',
    'ScanBatchBoundTests.test_fast_batch_outruns_staging_inconclusive',
    'ScanBatchBoundTests.test_never_terminating_batch_fails',
    'ScanBatchBoundTests.test_racing_batch_fails',
    'ScanBatchBoundTests.test_leaking_plant_fails',
    'ScanBatchBoundTests.test_skipped_plant_steps_fail',
    'ScanBatchBoundTests.test_pinned_worker_fails',
    'ScanBatchBoundTests.test_restore_demote_refused_fails',
    'ScanBatchBoundTests.test_restore_promote_refused_fails',
    'ScanBatchBoundTests.test_moved_roles_restore_and_fail',
    'ScanBatchBoundTests.test_plant_never_resumes_fails',
    'ScanBatchBoundTests.test_swapped_launch_layout_restores',
    'ScanBatchBoundTests.test_diverging_digests_report_nondeterministic',
    'ScanBatchBoundTests.test_silenced_audits_report_unchecked',
})


class ScanBoundPlant(ClaimPlantPeer):
    """The scan-batch leg's plant half: the shared field whose step
    counter the disconnect leg watches. `ping` answers the counter —
    every field-owning driven scan the feed runs advances it exactly
    once."""

    def _respond(self, conn, request):
        if request.get('op') == 'ping':
            with self.lock:
                self.requests.append(request.get('op'))
                return {'result': 'alive', 'tick': self.plant_tick}
        return super()._respond(conn, request)


class _DrivenWirePeer:
    """The driven monitor's raw HTTP/1.1 face — the piece the
    throwaway severed client needs: accept a connection, read the
    POST /scan through its Content-Length, start the feed's batch,
    and answer once it is staged — a mid-flight client close meets a
    listener that never notices, exactly the blind spot the declared
    bound exists to cover."""

    def __init__(self, feed):
        self.feed = feed
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET,
                                 socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(0.5)
        self.address = ('127.0.0.1:'
                        + str(self.listener.getsockname()[1]))
        self.closed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.closed:
            try:
                conn, _addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(30)
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, body = buf.partition(b'\r\n\r\n')
            length = 0
            for line in head.split(b'\r\n'):
                if line.lower().startswith(b'content-length:'):
                    length = int(line.split(b':', 1)[1].strip())
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            scans = json.loads(body[:length] or b'{}').get('scans')
            status, answer = self.feed.wire_scan(scans)
            payload = json.dumps(answer).encode()
            conn.sendall(
                ('HTTP/1.1 ' + str(status) + ' X\r\nContent-Length: '
                 + str(len(payload))
                 + '\r\nConnection: close\r\n\r\n').encode() + payload)
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def close(self):
        self.closed = True
        try:
            self.listener.close()
        except OSError:
            pass


class ScanBoundFeed:
    """A stubbed pair plus driven third peer for the scan-batch-bound
    scenario: ctrl-a owns the field, ctrl-b tracks it, ctrl-d runs
    --standby --driven so every one of its scans lands inside a
    POST /scan — the feed's `served` counter is that monitor's tick,
    the shared plant's `plant_tick` the field's own step counter. A
    background thread plays the owning pair member's autonomous scan
    loop — stepping the shared plant while ctrl-a or ctrl-b is
    active, never while the driven peer holds the field — so the
    leg's baseline, severed-batch, and restore witnesses read the
    same counter the deployed rig's would. /scan on the driven peer
    refuses any count past the leg's declared bound with the named
    'refused: scans <n> exceeds the per-request bound of <bound>' —
    the deployed monitor's wording — and runs requested scans one
    served tick apiece, stepping the plant only while the driven
    peer owns the field. Doctor flags stage each named defect the
    leg's diagnostics cover."""

    def __init__(self, plant, wire):
        self.plant = plant
        self.wire = wire             # _DrivenWirePeer — started below
        self.served = 0              # the driven monitor's tick
        self.a_role = 'active'
        self.b_role = 'standby'
        self.d_role = 'standby'
        self.a_tracking = False
        self.b_tracking = True
        self.d_tracking = False
        self.d_up = False            # the driven monitor's presence
        self.launched = None         # owner argument start_driven saw
        self.stopped = 0             # stop_driven call count
        self.batches = 0             # async batches ever started
        self.closed = False
        self.auto_step = True        # the owning member's scan loop
        self.scan_interval = 0.01    # per-scan pacing of a batch
        self.pair_token = 'scan-bound-pair'
        # Doctor flags for the named-failure cases.
        self.pair_down = False           # the rig is unreachable
        self.launch_fails = False        # start_driven raises
        self.driven_down = False         # the driven monitor is absent
        self.predates = False            # /scan is not a route
        self.accepts_over_bound = False  # over-bound batches run
        self.hangs_over_bound = False    # over-bound probes hang
        self.verdict_other = False       # over-bound answers 503
        self.unnamed_refusal = False     # the bound goes unnamed
        self.refused_runs = False        # a refused batch still runs
        self.demote_refused = False      # the owner's demote refuses
        self.driven_promote_refused = False
        self.driven_never_active = False  # promote answers, no role
        self.fast_batch = False          # batches finish pre-sever
        self.stalls_at = None            # the batch hangs mid-run
        self.races = False               # the batch runs past bound
        self.leaks_plant = False         # steps continue post-batch
        self.skips_steps = False         # field steps drop mid-batch
        self.pin_after_batch = False     # the worker never frees
        self.demote_restore_refused = False
        self.promote_restore_refused = False
        self.b_rogue = False             # a peer's role moves under
                                         # the hand-back
        self.no_resume = False           # the field stays still
        self.thread = threading.Thread(target=self._auto_loop,
                                       daemon=True)
        self.thread.start()

    def swap(self):
        """The swapped launch layout: ctrl-b owns the field, ctrl-a
        tracks it — the restore owes the entry roles, not a fixed
        letter."""
        self.a_role, self.b_role = 'standby', 'active'
        self.a_tracking, self.b_tracking = True, False

    def _auto_loop(self):
        """The pair member that owns the field steps it on its own
        scan loop — one step per cadence while ctrl-a or ctrl-b is
        active, nothing while the driven peer holds the field."""
        while not self.closed:
            if self.auto_step and not self.no_resume \
                    and (self.a_role == 'active'
                         or self.b_role == 'active'):
                with self.plant.lock:
                    self.plant.plant_tick += 1
            time.sleep(0.002)

    def close(self):
        self.closed = True

    def _refusal(self, scans):
        return {'error': 'refused: scans ' + str(scans)
                + ' exceeds the per-request bound of '
                + str(scenarios.SCAN_BATCH_BOUND)}

    def _step_batch_once(self):
        """One driven scan: the served tick always advances; the
        shared plant advances only while the driven peer owns the
        field — the tracking standby's scans step nothing."""
        self.served += 1
        if self.d_role == 'active':
            self.plant.plant_tick += 1

    def _scan_now(self, scans):
        """A synchronous request's scans — probes, convergence prods,
        settle pokes — each one a served tick, the plant moving only
        for the field owner."""
        for _index in range(scans):
            with self.plant.lock:
                self._step_batch_once()
        if self.d_role == 'standby' and scans:
            self.d_tracking = True

    def _batch(self, scans):
        """The severed-client batch: paced scan-by-scan so a
        mid-flight watch can catch it; doctor flags reshape the
        run — a fast batch completes inside one lock hold so the
        watch can only ever see pre or post, a stalled batch hangs
        mid-run, a racing batch ignores the bound, a leaking batch
        leaves a stepping remnant, a skipping batch drops field
        steps."""
        self.batches += 1
        bound = scenarios.SCAN_BATCH_BOUND
        if self.fast_batch:
            with self.plant.lock:
                for _index in range(scans):
                    self._step_batch_once()
            return
        limit = scans + (40 if self.races else 0)
        for index in range(limit):
            if self.stalls_at is not None and index == self.stalls_at:
                time.sleep(20)
            with self.plant.lock:
                self.served += 1
                if self.d_role == 'active' \
                        and (not self.skips_steps or index % 2 == 0):
                    self.plant.plant_tick += 1
            if self.scan_interval:
                time.sleep(self.scan_interval)
        if self.leaks_plant:
            self._leak()

    def _leak(self):
        """The uncancellable remnant: the shared plant keeps stepping
        after the batch's own served counter has stopped — a dead
        client's work still diverging the field's timeline."""
        def run():
            while not self.closed:
                with self.plant.lock:
                    self.plant.plant_tick += 1
                time.sleep(0.004)
        threading.Thread(target=run, daemon=True).start()

    def _start_batch(self, scans):
        bound = scenarios.SCAN_BATCH_BOUND
        capped = min(scans, bound + 120) \
            if scans is not None and scans > bound else scans
        threading.Thread(target=self._batch, args=(capped,),
                         daemon=True).start()

    def _scan_request(self, scans):
        """POST /scan on the driven peer: the named refusal for a
        count past the bound — answered before the first scan — the
        synchronous run for anything within it. Doctor flags stage
        the contract's failure shapes."""
        if self.predates:
            return 404, {'error': 'no such route'}
        if self.pin_after_batch and self.batches:
            raise urllib.error.URLError('the worker never freed')
        bound = scenarios.SCAN_BATCH_BOUND
        if not isinstance(scans, int) or scans > bound:
            if self.accepts_over_bound:
                self._start_batch(scans)
                return 200, {'scans': scans}
            if self.hangs_over_bound:
                self._start_batch(scans)
                raise urllib.error.URLError('timed out')
            if self.verdict_other:
                return 503, {'error': 'overloaded'}
            if self.refused_runs and isinstance(scans, int):
                self._scan_now(min(scans, bound))
            if self.unnamed_refusal:
                return 400, {'error': 'refused: too many scans'}
            return 400, self._refusal(scans)
        self._scan_now(scans)
        return 200, {'scans': scans, 'served': self.served}

    def wire_scan(self, scans):
        """The raw-wire POST /scan: the bound batch a throwaway
        client severs mid-flight — staged async so the mid-flight
        watch can observe its per-scan steps."""
        if self.predates:
            return 404, {'error': 'no such route'}
        bound = scenarios.SCAN_BATCH_BOUND
        if not isinstance(scans, int) or scans > bound:
            return 400, self._refusal(scans)
        self._start_batch(scans)
        return 200, {'scans': scans}

    def _report(self, peer):
        if peer == 'd':
            role, tracking, tick = (self.d_role, self.d_tracking,
                                    self.served)
        elif peer == 'a':
            role, tracking, tick = (self.a_role, self.a_tracking,
                                    self.plant.plant_tick)
        else:
            role, tracking, tick = (self.b_role, self.b_tracking,
                                    self.plant.plant_tick)
        sync = {'tracking': {'aligned': tick}} \
            if role == 'standby' and tracking \
            else {'unsynchronized': {}}
        return {'role': role, 'tick': tick, 'sync': sync}

    def _demote(self, peer):
        if peer == 'a' and self.demote_refused:
            return 409, {'error': 'refused: a scan holds the role'}
        if peer == 'd' and self.demote_restore_refused:
            return 409, {'error': 'refused: a scan holds the role'}
        if peer == 'a':
            self.a_role, self.a_tracking = 'standby', True
        elif peer == 'b':
            self.b_role, self.b_tracking = 'standby', True
        else:
            self.d_role, self.d_tracking = 'standby', True
            if self.b_rogue:
                # The hand-back move that restores wrongly: a peer's
                # role moves while the driven owner steps down.
                self.b_role, self.b_tracking = 'active', False
        return 200, self._report(peer)

    def _promote(self, peer):
        if peer == 'd' and self.driven_promote_refused:
            return 409, {'error': 'refused: the field stays yielded'}
        if peer == 'd' and not self.d_tracking:
            return 409, {'error': 'refused: not synchronized'}
        if peer == 'a' and self.promote_restore_refused:
            return 409, {'error': 'refused: the claim stands'}
        if peer == 'd':
            if not self.driven_never_active:
                self.d_role, self.d_tracking = 'active', False
        elif peer == 'a':
            self.a_role, self.a_tracking = 'active', False
        else:
            self.b_role, self.b_tracking = 'active', False
        return 200, self._report(peer)

    def http_json(self, method, url, body=None, timeout=10):
        """The measurement channel — replaces scenarios.http_json."""
        if self.pair_down:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        if host == 'ctrl-a:1':
            peer = 'a'
        elif host == 'ctrl-b:2':
            peer = 'b'
        elif host == self.wire.address:
            peer = 'd'
        else:
            raise AssertionError('unexpected host ' + host)
        if peer == 'd' and not self.d_up:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('POST', '/scan'):
            if peer != 'd':
                raise AssertionError('/scan posted to ' + peer)
            return self._scan_request((body or {}).get('scans'))
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.served if peer == 'd'
                         else self.plant.plant_tick}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))

    def start_driven(self, owner):
        """The ctx['start_driven'] action — the run's third
        controller, launched --standby <owner> --driven."""
        self.launched = owner
        if self.launch_fails:
            raise RuntimeError('the driven container never started')
        if not self.driven_down:
            self.d_up = True
        return {'container': 'dcs-ctrl-driven-qa'}

    def stop_driven(self):
        """The ctx['stop_driven'] action — the driven container
        removed, its monitor gone."""
        self.stopped += 1
        self.d_up = False


class ScanBatchBoundTests(unittest.TestCase):
    """scenario_scan_batch_bound against the stubbed rig: the wired
    driven monitor refuses over-bound probes by name, severs
    mid-flight batches that still terminate at the bound, and hands
    the launch layout back — until a doctor flag stages a named
    defect or an inconclusive rig."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = ScanBoundPlant()
        self.feed = ScanBoundFeed(self.plant, None)
        self.wire = _DrivenWirePeer(self.feed)
        self.feed.wire = self.wire

    def tearDown(self):
        self.feed.close()
        self.wire.close()
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'driven': 'http://' + feed.wire.address,
                'plant': feed.plant.address,
                'pair_token': feed.pair_token,
                'start_driven': feed.start_driven,
                'stop_driven': feed.stop_driven,
                'evidence_dir': str(self.evidence)}
        base.update(extra)
        return base

    def run_scenario(self, feed=None, ctx=None, **tunables):
        feed = feed or self.feed
        patches = {'SCAN_BATCH_BOUND': 32, 'BOUND_SETTLE': 3.0,
                   'BOUND_POLL': 0.005, 'CONVERGE_SCANS': 2,
                   'REFUSAL_TIMEOUT': 1.0, 'SEVER_WATCH': 2.0,
                   'SEVER_POLL': 0.001, 'TERMINATE_DEADLINE': 3.0,
                   'PLATEAU_HOLD': 0.05, 'PROBE_SCANS': 1}
        patches.update(tunables)
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_scan_batch_bound(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_scan_batch_bound, order)
        self.assertLess(
            order.index(scenarios.scenario_failover),
            order.index(scenarios.scenario_scan_batch_bound))

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for name in ('scan-batch-bound-pass-1.json',
                     'scan-batch-bound-pass-2.json'):
            self.assertTrue((self.evidence / name).exists(), name)
        payload = json.loads(
            (self.evidence / 'scan-batch-bound-pass-1.json')
            .read_text())
        self.assertEqual(payload['digest'], {
            'refusal': 'named', 'severed': 'mid-flight',
            'terminated': 'at-bound', 'plant': 'froze',
            'worker': 'freed', 'restored': 'restored'})
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertTrue(self.feed.b_tracking)
        self.assertFalse(self.feed.d_up)

    def test_two_passes_share_one_digest(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = [json.loads(
            (self.evidence / name).read_text())['digest']
            for name in ('scan-batch-bound-pass-1.json',
                         'scan-batch-bound-pass-2.json')]
        self.assertEqual(passes[0], passes[1])

    def test_two_runs_produce_identical_records(self):
        first = self.run_scenario()
        second = self.run_scenario()
        self.assertEqual('passed', first['outcome'])
        self.assertEqual('passed', second['outcome'])
        self.assertEqual(first['detail'], second['detail'])

    def test_scenario_ctx_carries_the_driven_actions(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.launched, 'active')
        self.assertEqual(self.feed.stopped, 1)

    def test_missing_actions_are_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(start_driven=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_driven', record['detail'])
        record = self.run_scenario(ctx=self._ctx(driven=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('driven', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.pair_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.b_tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_no_active_is_failed(self):
        self.feed.a_role = 'standby'
        self.feed.a_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record['detail'])

    def test_unkeyed_pair_is_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(pair_token=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pair-token', record['detail'])

    def test_probe_subject_rebinds(self):
        record = self.run_scenario(ctx=self._ctx(
            pair_token=None,
            probe=self._ctx(pair_token='probe-pair')))
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)

    def test_driven_launch_failure_is_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch', record['detail'])

    def test_driven_never_serves_is_inconclusive(self):
        self.feed.driven_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('/role', record['detail'])

    def test_predated_contract_is_inconclusive(self):
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_over_bound_accepted_fails(self):
        self.feed.accepts_over_bound = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('accepted', record['detail'])

    def test_over_bound_running_unanswered_fails(self):
        self.feed.hangs_over_bound = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('never answered', record['detail'])

    def test_wrong_verdict_fails(self):
        self.feed.verdict_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('503', record['detail'])

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('never named', record['detail'])

    def test_refused_batch_ran_fails(self):
        self.feed.refused_runs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('still ran', record['detail'])

    def test_refused_demote_fails(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('/demote', record['detail'])

    def test_refused_driven_promote_fails(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('/promote', record['detail'])

    def test_driven_never_active_fails(self):
        self.feed.driven_never_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('role=active', record['detail'])

    def test_fast_batch_outruns_staging_inconclusive(self):
        self.feed.fast_batch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('outruns the mid-flight staging',
                      record['detail'])

    def test_never_terminating_batch_fails(self):
        self.feed.stalls_at = 8
        record = self.run_scenario(TERMINATE_DEADLINE=0.3)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('never terminated', record['detail'])

    def test_racing_batch_fails(self):
        self.feed.races = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('dead client', record['detail'])

    def test_leaking_plant_fails(self):
        self.feed.leaks_plant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('kept stepping', record['detail'])

    def test_skipped_plant_steps_fail(self):
        self.feed.skips_steps = True
        record = self.run_scenario(TERMINATE_DEADLINE=1.0)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('diverged', record['detail'])

    def test_pinned_worker_fails(self):
        self.feed.pin_after_batch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('worker', record['detail'])

    def test_restore_demote_refused_fails(self):
        self.feed.demote_restore_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('hand-back refused', record['detail'])

    def test_restore_promote_refused_fails(self):
        self.feed.promote_restore_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('re-took the field', record['detail'])

    def test_moved_roles_restore_and_fail(self):
        self.feed.b_rogue = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-nondeterministic',
                      record['detail'])
        # The exit owes the launch roles even on a failed pass.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertTrue(self.feed.b_tracking)

    def test_plant_never_resumes_fails(self):
        self.feed.no_resume = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-failed', record['detail'])
        self.assertIn('never stepped again', record['detail'])

    def test_swapped_launch_layout_restores(self):
        self.feed.swap()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.b_role, 'active')
        self.assertEqual(self.feed.a_role, 'standby')
        self.assertTrue(self.feed.a_tracking)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'refusal': 'named'}, {}, {'pass': 1}),
                       ({'refusal': 'accepted'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_batch_pass',
                          lambda *args: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-nondeterministic',
                      record['detail'])
        self.assertIn('digests diverged', record['detail'])

    def test_silenced_audits_report_unchecked(self):
        with patch.object(scenarios, '_judge_batch_refusal',
                          lambda record, note: None), \
                patch.object(scenarios, '_judge_batch_disconnect',
                             lambda record, note: None), \
                patch.object(scenarios, '_judge_batch_restore',
                             lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('scan-batch-bound-unchecked', record['detail'])


if __name__ == '__main__':
    unittest.main()
