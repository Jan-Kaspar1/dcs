"""The 3675_bounded_liveness leg's scenario unit coverage — the feed
fake and TestCase class for scenario_bounded_liveness, per the
module-per-leg test convention (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py.
"""
from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class LivenessFeed:
    """A stubbed pair for the bounded-liveness scenario. ctrl-a owns
    the field, ctrl-b tracks it. Every read while the plant runs is
    one completed scan — the served liveness mirror re-stamps — while
    the ctx lever's paused flag wedges every scan: reads keep
    answering off the published copies exactly like the bounded
    liveness mirror intends, but the served last_scan_age_ms grows
    per wedged read — call-counted, never wall-clock, so the digests
    stay deterministic. The fault flags stage each named defect the
    leg's diagnostics cover."""

    AGE_STEP = 200        # the scan-age growth one wedged read adds —
                          # call counts, so a pause never waits on
                          # wall-clock in the unit fake
    BASE_AGE = 60         # a healthy scan cadence's reported age

    def __init__(self):
        self.paused = False          # set/cleared by the ctx levers
        self.a_role = 'active'
        self.b_role = 'standby'
        self.a_tracking = False
        self.b_tracking = True
        self.a_tick = 400
        self.b_tick = 400
        self.aligned = 400
        self.stall = {'a': 0, 'b': 0}   # wedged reads per peer
        # Fault flags staging the named failures.
        self.down = False            # the rig is unreachable
        self.no_health = False       # /health 404s — predates the
                                     # bounded liveness contract
        self.unshaped_health = False  # /health answers an unshaped
                                      # body — same predated verdict
        self.no_age = False          # no completed scan ever stamps
        self.drop_age = False        # the age vanishes under the wedge
        self.flat_age = False        # the reported age never grows
        self.stall_live = 0.0        # seconds a wedged liveness read
                                     # sleeps — the doctored pre-#1147
                                     # defect: answered, but past the
                                     # bound
        self.drop_live = False       # wedged liveness reads time out
        self.stall_copies = 0.0      # published-copy reads stall too
        self.move_roles = False      # a peer's role moves mid-wedge
        self.never_recovers = False  # the wedge outlives the unpause
        self.pause_raises = False
        self.unpause_raises = False

    def swap(self):
        """The swapped launch layout: ctrl-b owns the field, ctrl-a
        tracks it — the restore owes the entry roles, not a fixed
        letter."""
        self.a_role, self.b_role = 'standby', 'active'
        self.a_tracking, self.b_tracking = True, False

    def _role(self, peer):
        role = self.a_role if peer == 'a' else self.b_role
        tick = self.a_tick if peer == 'a' else self.b_tick
        tracking = self.a_tracking if peer == 'a' else self.b_tracking
        sync = {'tracking': {'aligned': self.aligned}} \
            if role == 'standby' and tracking else None
        return {'role': role, 'tick': tick, 'sync': sync}

    def _age(self, peer, wedged):
        if self.no_age or (self.drop_age and wedged):
            return None
        if not wedged:
            return self.BASE_AGE
        if self.flat_age:
            return self.BASE_AGE
        return self.BASE_AGE + self.stall[peer] * self.AGE_STEP

    def http_json(self, method, url, body=None, timeout=10):
        """The measurement channel — replaces scenarios.http_json."""
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if self.down:
            raise urllib.error.URLError('connection refused')
        peer = 'a' if host == 'ctrl-a:1' else 'b'
        tick = self.a_tick if peer == 'a' else self.b_tick
        if (method, route) == ('POST', '/demote'):
            if peer == 'a':
                self.a_role, self.a_tracking = 'standby', True
            else:
                self.b_role, self.b_tracking = 'standby', True
            return 200, self._role(peer)
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                self.a_role, self.a_tracking = 'active', False
            else:
                self.b_role, self.b_tracking = 'active', False
            return 200, self._role(peer)
        if method != 'GET':
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        wedged = self.paused or self.never_recovers
        if wedged:
            # The wedge: no scan completes, so the served scan age
            # grows by the read count — while the liveness mirror and
            # the published copies keep answering at once.
            self.stall[peer] += 1
            if self.move_roles:
                self.a_role, self.b_role = 'standby', 'active'
                self.a_tracking = self.b_tracking = False
            if route in ('/health', '/role'):
                if self.drop_live:
                    raise urllib.error.URLError('timed out')
                if self.stall_live:
                    time.sleep(self.stall_live)
            elif self.stall_copies:
                time.sleep(self.stall_copies)
        else:
            self.stall[peer] = 0
            if peer == 'a':
                self.a_tick += 1
            else:
                self.b_tick += 1
                if self.b_tracking and self.b_role == 'standby':
                    self.aligned = self.a_tick
        if route == '/health':
            if self.no_health:
                raise urllib.error.HTTPError(
                    url, 404, 'not found', {}, io.BytesIO(b''))
            if self.unshaped_health:
                return 200, {'pong': True}
            return 200, {'live': True,
                         'role': self._role(peer)['role'],
                         'tick': tick,
                         'last_scan_age_ms': self._age(peer, wedged)}
        if route == '/role':
            return 200, self._role(peer)
        if route == '/snapshot':
            return 200, {'tick': tick, 'points': []}
        if route == '/journal':
            return 200, []
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class BoundedLivenessTests(unittest.TestCase):
    """scenario_bounded_liveness against the stubbed pair: the ctx
    lever's paused flag stands the wedge a frozen plant leaves —
    sockets held, scans never completing, the served scan age
    climbing — while every read answers off the published copies
    inside the bound, until the lever's release re-stamps the
    mirror."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.feed = LivenessFeed()
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, ctx=None):
        feed = feed if feed is not None else self.feed

        def pause():
            if feed.pause_raises:
                raise RuntimeError('docker pause failed')
            feed.paused = True
            self.events.append('plant-paused')

        def unpause():
            if feed.unpause_raises:
                raise RuntimeError('docker unpause failed')
            if not feed.paused:
                # The real `docker unpause` refuses a running
                # container — the leg's best-effort exit release
                # swallows it exactly like the runner's would.
                raise RuntimeError('container is not paused')
            feed.paused = False
            self.events.append('plant-unpaused')

        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'evidence_dir': str(self.evidence),
                'pause_plant': pause,
                'unpause_plant': unpause}
        if ctx is not None:
            base.update(ctx)
        patches = {'WEDGE_OBSERVE': 0.15, 'WEDGE_HOLD': 0.2,
                   'LIVE_POLL': 0.01, 'LIVE_SETTLE': 1,
                   'LIVE_RECOVER': 0.5, 'LIVE_BOUND': 0.2,
                   'LATENCY_BOUND': 0.2}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_bounded_liveness(base)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_bounded_liveness,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.events,
                         ['plant-paused', 'plant-unpaused',
                          'plant-paused', 'plant-unpaused'])
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {'evidence/bounded-liveness-pass-1.json',
                                'evidence/bounded-liveness-pass-2.json'})
        for entry in record['evidence']:
            self.assertTrue(
                (self.evidence.parent / entry['ref']).exists(), entry)
        payload = json.loads(
            (self.evidence / 'bounded-liveness-pass-1.json')
            .read_text())
        self.assertEqual(payload['digest'], {
            'liveness': 'bounded', 'scan_age': 'grew',
            'published': 'baseline', 'recovery': 'resumed',
            'roles': 'held'})
        # The wedge window held both peers' bounded reads — and the
        # served scan age grew across it.
        self.assertTrue(payload['wedge'])
        for ages in payload['ages'].values():
            self.assertGreater(ages[-1], ages[0])
        # The launch layout stands untouched.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')

    def test_two_passes_share_one_digest(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = [json.loads(
            (self.evidence / name).read_text())['digest']
            for name in ('bounded-liveness-pass-1.json',
                         'bounded-liveness-pass-2.json')]
        self.assertEqual(passes[0], passes[1])

    def test_two_runs_produce_identical_records(self):
        # The deterministic-rerun contract: two passes over the same
        # wedge record the same verdict labels — the feed is
        # call-count keyed, never wall-clock.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            runs.append(self.run_scenario(feed=LivenessFeed()))
        self.assertEqual(runs[0]['outcome'], 'passed', runs[0])
        self.assertEqual(runs[0], runs[1])

    def test_scenario_ctx_carries_the_levers(self):
        calls = []
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG), record, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: self.events.append(event))
        self.assertTrue(callable(ctx['pause_plant']))
        self.assertTrue(callable(ctx['unpause_plant']))
        with patch.object(runner, 'docker',
                          lambda *a, **kw: calls.append(a)):
            ctx['pause_plant']()
            ctx['unpause_plant']()
        self.assertEqual(calls, [('pause', 'dcs-hw-qa-1-plant'),
                                 ('unpause', 'dcs-hw-qa-1-plant')])
        self.assertEqual(self.events, ['plant-pause', 'plant-paused',
                                       'plant-unpause',
                                       'plant-unpaused'])

    def test_missing_levers_are_inconclusive(self):
        record = self.run_scenario(ctx={'pause_plant': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pause/unpause', record['detail'])
        record = self.run_scenario(ctx={'unpause_plant': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)

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

    def test_predated_contract_is_inconclusive(self):
        self.feed.no_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        self.feed.no_health = False
        self.feed.unshaped_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_unstamped_report_is_inconclusive(self):
        self.feed.no_age = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_failed_pause_is_inconclusive(self):
        self.feed.pause_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pause', record['detail'])

    def test_failed_unpause_is_inconclusive(self):
        self.feed.unpause_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unpause', record['detail'])

    def test_stalled_liveness_read_fails(self):
        # The doctored negative the issue names: the liveness read
        # answers — but past the declared bound, the pre-#1147 stall
        # behind the wedged scan's lock hold.
        self.feed.stall_live = 0.35
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('past the', record['detail'])

    def test_dropped_liveness_read_fails(self):
        self.feed.drop_live = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('never answered', record['detail'])

    def test_flat_scan_age_fails(self):
        self.feed.flat_age = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('never grew', record['detail'])

    def test_dropped_scan_age_fails(self):
        self.feed.drop_age = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('never carried', record['detail'])

    def test_stalled_copy_read_fails(self):
        self.feed.stall_copies = 0.35
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('past the', record['detail'])

    def test_wedged_recovery_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-failed', record['detail'])
        self.assertIn('never recovered', record['detail'])

    def test_moved_roles_restore_and_fail(self):
        self.feed.move_roles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-nondeterministic',
                      record['detail'])
        # The exit owes the launch roles even on a failed pass.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertTrue(self.feed.b_tracking)

    def test_swapped_launch_layout_restores(self):
        # The leg restores the entry layout, not a fixed letter:
        # ctrl-b owning the field at entry is still the layout the
        # pair stands at when the leg leaves.
        self.feed.swap()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.b_role, 'active')
        self.assertEqual(self.feed.a_role, 'standby')

    def test_silenced_judge_reports_unchecked(self):
        with patch.object(scenarios, '_judge_wedge',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bounded-liveness-unchecked', record['detail'])


if __name__ == '__main__':
    unittest.main()
