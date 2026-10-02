"""The 1020_attributed_switch_isolation leg's scenario unit coverage —
the feed fakes and TestCase class for
scenario_attributed_switch_isolation, one test module per leg of the
split schedule (tests/test_qa_scenario_modules.py pins the pairing).
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check.
"""
import importlib
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'AttributedSwitchIsolationTests.test_registered_in_scenarios',
    'AttributedSwitchIsolationTests.test_isolated_pair_passes_and_validates',
    'AttributedSwitchIsolationTests.test_two_runs_produce_identical_evidence',
    'AttributedSwitchIsolationTests.test_queued_attributed_switch_reports_failed',
    'AttributedSwitchIsolationTests.test_late_attributed_switch_reports_failed',
    'AttributedSwitchIsolationTests.test_wrong_verdict_reports_failed',
    'AttributedSwitchIsolationTests.test_starved_baseline_reports_failed',
    'AttributedSwitchIsolationTests.test_unpinned_lane_reports_nondeterministic',
    'AttributedSwitchIsolationTests.test_unopened_clients_report_nondeterministic',
    'AttributedSwitchIsolationTests.test_unrecovered_lane_reports_failed',
    'AttributedSwitchIsolationTests.test_unrestored_pair_reports_failed',
    'AttributedSwitchIsolationTests.test_moved_pair_reports_nondeterministic',
    'AttributedSwitchIsolationTests.test_precontract_routing_reports_inconclusive',
    'AttributedSwitchIsolationTests.test_diverged_digests_report_nondeterministic',
    'AttributedSwitchIsolationTests.test_self_check_covers_its_negatives',
    'AttributedSwitchIsolationTests.test_slipping_self_check_reports_unchecked',
    'AttributedSwitchIsolationTests.test_missing_seams_report_inconclusive',
    'AttributedSwitchIsolationTests.test_unconverged_pair_reports_inconclusive',
})


LEG = 'qa_lane.scenarios.1020_attributed_switch_isolation'

# The window's own timing fields — the measured seconds a clean run and
# its twin never agree on. Everything else in a pass window is the
# normalized verdict record the two-pass digest is computed over.
TIMING_FIELDS = frozenset({'elapsed', 'held_for'})


def _scrub(value):
    """`value` with every timing field dropped — the shape two clean
    runs of the same rig must agree on."""
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()
                if key not in TIMING_FIELDS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


class IsolationFeed:
    """A stubbed rig for the attributed-switch-isolation leg.

    The deployed pair settles in its launch roles — ctrl-a active,
    ctrl-b tracking — and the run's driven standby answers on ctrl-d
    with the two refusals its unconverged posture earns:
    `not_active` from the non-owner's demote, `not_converged` from the
    promotion whose final-sync pull just failed against the silent
    source. Every raw connection the leg opens is a pinning client or
    a severed `POST /scan` batch, and the submission lane answers a
    one-scan submission only while fewer than both of its workers are
    occupied — the pin the leg must prove — and only until a severed
    batch's own bounded span is over. The driven peer's run tick never
    advances (its source is silent, so no scan completes inside the
    capture window), so an attributed answer's queue-order evidence
    reads zero: it rode the control lane.

    The doctor flags stage each named failure the leg reports.
    """

    def __init__(self):
        self.a_tick = 40
        self.b_aligned = 40
        self.d_tick = 4
        self.sockets = []
        self.paused = False
        self.thawed = 0
        self.latched = False
        self.launched = []
        self.torn_down = 0
        self.batch_span = 0.25    # a severed batch's own bounded span
        self.calls = []           # (phase, path, actor) per actuation
        # Doctors.
        self.silence = set()      # (phase, path) the lane never answers
        self.late = set()         # (phase, path) answered past the bound
        self.wrong = set()        # (phase, path) answered wrongly
        self.starve = set()       # paths that never answer
        self.never_pins = False   # the submission lane never pinned
        self.never_drain = False  # the lane never serves a scan again
        self.failover = False     # the armed budget fires mid-window
        self.refuse_open = False  # a pinning client never opens
        self.stick_degraded = False   # the pair never reconverges

    # --- the runner-owned seams ---

    def start_driven(self, owner):
        self.launched.append(owner)
        return {'container': 'dcs-hw-run-d'}

    def stop_driven(self):
        self.torn_down += 1

    def pause_controller(self, name):
        self.paused = True

    def unpause_controller(self, name):
        self.paused = False
        self.thawed += 1
        if self.failover:
            # The armed budget fired while the source was paused; the
            # peer has left its standby role by the time it is thawed.
            self.latched = True

    def connect(self, base, timeout=5):
        if self.refuse_open:
            raise OSError('the pinning client never opened')
        stream = FakeSocket()
        stream.opened_at = time.monotonic()
        stream.kind = None
        self.sockets.append(stream)
        return stream

    # --- the submission lane the leg congests ---

    def _classify(self, stream):
        """Which shape a raw client belongs to: a holder declares the
        long undelivered body, a severed batch a whole small one."""
        if stream.kind is None:
            head = stream.sent.split(b'\r\n\r\n', 1)[0]
            stream.kind = ('holders'
                           if str(scenarios.HOLDER_LENGTH).encode()
                           in head else
                           'batches' if b'/scan' in head else 'other')
        return stream.kind

    def _occupied(self, kind):
        """The submission workers the leg's `kind` clients occupy: a
        holder for as long as its socket stands, a severed batch for
        its own bounded span."""
        workers = 0
        for stream in self.sockets:
            if stream.closed or self._classify(stream) != kind:
                continue
            if kind == 'batches' and time.monotonic() \
                    - stream.opened_at > self.batch_span:
                continue
            workers += 1
        return workers

    def pinned(self):
        """Whether the submission lane is pinned — the leg's own probe
        reads exactly this."""
        if self.never_pins:
            return False
        return (self._occupied('holders') + self._occupied('batches')) \
            >= scenarios.SUBMIT_WORKERS

    def phase(self):
        """The shape standing on the submission lane right now — the
        one a leg call lands inside; None once the stage is released."""
        for kind in ('holders', 'batches'):
            if self._occupied(kind):
                return kind
        return None

    def request_status(self, method, url, body=None, timeout=10):
        # The lane's only submission: refused while pinned (the pin
        # proof reads it that way) and served again only once the
        # congestion is gone.
        if method == 'POST' and url.endswith('/scan'):
            if self.never_drain or self.pinned():
                raise urllib.error.URLError('timed out')
            return 200, b'{"tick": 5}'
        raise urllib.error.URLError('no route ' + url)

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=10):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.active(method, path)
        if host.startswith('ctrl-b'):
            return self.standby(method, path)
        if host.startswith('ctrl-d'):
            return self.driven(method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def active(self, method, path):
        if path == '/role':
            return 200, {'role': 'active', 'tick': self.a_tick}
        raise urllib.error.URLError('no route ' + path)

    def standby(self, method, path):
        if path != '/role':
            raise urllib.error.URLError('no route ' + path)
        self.b_aligned += 1
        if self.latched:
            return 200, {'role': 'promoting', 'tick': self.b_aligned,
                         'sync': {'tracking': {'aligned': self.b_aligned}}}
        degraded = self.stick_degraded and self.thawed > 0
        sync = {'degraded': {'detail': 'fetch failed: timed out'}} \
            if degraded else {'tracking': {'aligned': self.b_aligned}}
        return 200, {'role': 'standby', 'tick': self.b_aligned,
                     'sync': sync}

    def driven(self, method, path, body):
        if path == '/role':
            sync = {'unsynchronized': {}} if not self.paused \
                else {'degraded': {'detail': 'fetch failed: timed out'}}
            return 200, {'role': 'standby', 'tick': self.d_tick,
                         'sync': sync}
        if path == '/health':
            if path in self.starve:
                raise urllib.error.URLError('timed out')
            return 200, {'live': True, 'role': 'standby',
                         'tick': self.d_tick, 'last_scan_age_ms': 120}
        if path in scenarios.ISOLATION_PATHS and method == 'POST':
            return self.actuate(path, body)
        raise urllib.error.URLError('no route ' + path)

    def actuate(self, path, body):
        """One actuation's served verdict: the named refusal the driven
        peer's unconverged posture earns, unless a doctor stages
        another. The doctors stage the *attributed* switch — the bare
        control-lane probe beside it keeps its lane."""
        actor = (body or {}).get('actor')
        phase = self.phase()
        self.calls.append((phase, path, actor))
        if path in self.starve and actor is None:
            raise urllib.error.URLError('timed out')
        if actor is None:
            return 409, {'not_active': {}}
        key = (phase, path)
        if key in self.silence:
            raise urllib.error.URLError('timed out')
        if key in self.late:
            time.sleep(scenarios.SWITCH_BOUND + 0.1)
        if key in self.wrong:
            return 409, {'already_active': {}}
        return 409, ({'/demote': {'not_active': {}},
                      '/promote': {'not_converged': {
                          'sync': {'unsynchronized': {}}}}}[path])


class AttributedSwitchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = IsolationFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3', 'failover_misses': 120,
               'evidence_dir': str(self.evidence),
               'start_driven': feed.start_driven,
               'stop_driven': feed.stop_driven,
               'pause_controller': feed.pause_controller,
               'unpause_controller': feed.unpause_controller}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None, **patches):
        feed = feed or self.feed
        constants = {'PIN_SETTLE': 0.001, 'ISOLATION_POLL': 0.001,
                     'HOLDER_HOLD': 0.01, 'SETTLE': 1.0,
                     'RELEASE_BOUND': 2.0, 'SWITCH_BOUND': 0.05,
                     'BASELINE_BOUND': 0.05, 'PIN_PROBE_BOUND': 0.05}
        constants.update(patches)
        with patch.multiple(scenarios, **constants), \
                patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_connect', feed.connect), \
                patch.object(scenarios, '_request_status',
                             feed.request_status):
            return scenarios.scenario_attributed_switch_isolation(
                ctx or self._ctx(feed))

    def _window(self, number):
        return json.loads(
            (self.evidence
             / ('attributed-switch-isolation-pass-' + str(number)
                + '.json')).read_text())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_attributed_switch_isolation,
                      order)
        # The leg shares the armed window the monitor-starvation flood
        # leaves standing, and restores it for the duty rotation
        # behind it.
        self.assertLess(
            order.index(scenarios.scenario_monitor_starvation),
            order.index(scenarios.scenario_attributed_switch_isolation))
        self.assertLess(
            order.index(scenarios.scenario_attributed_switch_isolation),
            order.index(scenarios.scenario_duty_rotation))
        self.assertIs(verify.case_function('attributed-switch-isolation'),
                      scenarios.scenario_attributed_switch_isolation)

    def test_isolated_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertIn('two isolation passes, identical digests',
                      ' '.join(record['observations']))
        for number in (1, 2):
            window = self._window(number)
            self.assertEqual(window['source'], 'paused')
            self.assertEqual(window['owner'], 'active')
            for shape in ('holders', 'batches'):
                entry = window['shapes'][shape]
                self.assertTrue(entry['opened'], shape)
                self.assertTrue(entry['pinned'], shape)
                self.assertTrue(entry['pin_error'], shape)
                self.assertEqual(sorted(entry['switches']),
                                 ['/demote', '/promote'])
                for path in ('/demote', '/promote'):
                    act = entry['switches'][path]
                    self.assertTrue(act['answered'], (shape, path))
                    self.assertTrue(act['bounded'], (shape, path))
                    self.assertTrue(act['correct'], (shape, path))
                    self.assertEqual(act['verdict'],
                                     'not_active' if path == '/demote'
                                     else 'not_converged',
                                     (shape, path))
                    self.assertEqual(act['ticks_behind'], 0,
                                     (shape, path))
                self.assertEqual(sorted(entry['baseline']),
                                 ['/demote', '/health', '/role'])
                for path in entry['baseline']:
                    self.assertTrue(entry['baseline'][path]['answered'],
                                    (shape, path))
                    self.assertTrue(entry['baseline'][path]['bounded'],
                                    (shape, path))
            self.assertTrue(window['shapes']['holders']['held_for'] >= 0.0)
            self.assertTrue(
                window['shapes']['holders']['held_long_enough'])
            self.assertEqual(window['recovery']['drained'], True)
            self.assertEqual(window['recovery']['reconverged'], True)
            self.assertEqual(window['recovery']['moved'], False)
            self.assertEqual(window['recovery']['roles'],
                             {'active': 'active', 'standby': 'standby'})
            self.assertEqual(window['violations'], {})
        digest = json.dumps(scenarios._digest_isolation({}), sort_keys=True)
        for field in ('"gate": "isolated"', '"batches": "isolated"',
                      '"baseline": "held"', '"released": "drained"',
                      '"reconverged": "reconverged"',
                      '"roles": "restored"'):
            self.assertIn(field, digest)
        self.assertTrue(self.feed.sockets)
        self.assertTrue(all(stream.closed
                            for stream in self.feed.sockets))
        self.assertEqual(self.feed.launched, ['active'])
        self.assertEqual(self.feed.torn_down, 1)
        self.assertFalse(self.feed.paused)
        # Every attributed actuation carried the declared actor; the
        # baseline bodiless demote beside them carried none.
        attributed = [call for call in self.feed.calls
                      if call[2] == scenarios.ISOLATION_ACTOR]
        bare = [call for call in self.feed.calls if call[2] is None]
        self.assertEqual(len(attributed), 8)
        self.assertEqual(len(bare), 4)
        self.assertEqual({call[0] for call in attributed},
                         {'holders', 'batches'})
        self.assertEqual({call[1] for call in bare}, {'/demote'})
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            feed = IsolationFeed()
            evidence = Path(self.tmp.name) / ('run' + str(index))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed, ctx=self._ctx(feed))
            windows = [_scrub(self._window(number))
                       for number in (1, 2)]
            runs.append((record['outcome'], record['observations'],
                         windows))
        self.assertEqual(runs[0], runs[1])

    def test_queued_attributed_switch_reports_failed(self):
        # The doctored negative the issue names: isolation asserted
        # held while the attributed actuations sit queued behind the
        # staged congestion. The holding shape answered, so this is
        # the contract failing and not a revision predating it.
        self.feed.silence = {('batches', '/demote'),
                             ('batches', '/promote')}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('never answered inside the declared',
                      record['detail'])
        report.validate_scenario(record)

    def test_late_attributed_switch_reports_failed(self):
        self.feed.late = {('batches', '/demote')}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('past the declared', record['detail'])
        report.validate_scenario(record)

    def test_wrong_verdict_reports_failed(self):
        self.feed.wrong = {('batches', '/promote')}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('not the verdict that posture earns',
                      record['detail'])
        report.validate_scenario(record)

    def test_starved_baseline_reports_failed(self):
        self.feed.starve = {'/health'}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('bare control lane /health never answered',
                      record['detail'])
        report.validate_scenario(record)

    def test_unpinned_lane_reports_nondeterministic(self):
        self.feed.never_pins = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-nondeterministic'),
            record['detail'])
        self.assertIn('was never pinned', record['detail'])
        report.validate_scenario(record)

    def test_unopened_clients_report_nondeterministic(self):
        self.feed.refuse_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-nondeterministic'),
            record['detail'])
        self.assertIn('never opened', record['detail'])
        report.validate_scenario(record)

    def test_unrecovered_lane_reports_failed(self):
        self.feed.never_drain = True
        record = self.run_scenario(RELEASE_BOUND=0.1)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('never served a scan again', record['detail'])
        report.validate_scenario(record)

    def test_unrestored_pair_reports_failed(self):
        # The pair converges on entry and never again once the source
        # is thawed — the restore clause the leg audits.
        self.feed.stick_degraded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-failed'), record['detail'])
        self.assertIn('did not reconverge with its launch roles',
                      record['detail'])
        report.validate_scenario(record)

    def test_moved_pair_reports_nondeterministic(self):
        self.feed.failover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-'), record['detail'])
        self.assertIn('armed failover budget fired', record['detail'])
        report.validate_scenario(record)

    def test_precontract_routing_reports_inconclusive(self):
        # The holding set's clients own both submission workers and an
        # attributed switch shares their wait: the routing the fix
        # replaced, which every staged build predates until it lands.
        self.feed.silence = {('holders', '/demote'),
                             ('holders', '/promote')}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the attributed-switch control-lane',
                      record['detail'])
        gate = self._window(1)['shapes']['holders']['switches']
        self.assertFalse(gate['/demote']['answered'])
        self.assertTrue(gate['/demote']['released_late'])
        self.assertEqual(gate['/demote']['retry_verdict'], 'not_active')
        report.validate_scenario(record)

    def test_diverged_digests_report_nondeterministic(self):
        first = {'gate': 'isolated', 'batches': 'isolated',
                 'baseline': 'held', 'released': 'drained',
                 'reconverged': 'reconverged', 'roles': 'restored'}
        with patch.object(scenarios, '_digest_isolation',
                          side_effect=[first, dict(first, gate='queued')]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_self_check_covers_its_negatives(self):
        # The leg's own self-check replays its judge over every
        # planted negative and reports none slipping ...
        self.assertEqual(scenarios._self_check_isolation(), [])
        # ... and the judge is what catches them: a window whose
        # isolation is asserted while the attributed demote sat queued
        # behind the staged batches names the contract failure by key.
        leg = importlib.import_module(LEG)
        record = leg._clean_record()
        record['shapes']['batches']['switches']['/demote'].update(
            {'answered': False, 'bounded': False, 'correct': False,
             'status': None, 'verdict': 'status:None',
             'error': 'timed out'})
        found = {}
        leg._judge_pass(record,
                        lambda key, diagnostic, detail:
                        found.setdefault(key, diagnostic))
        self.assertEqual(found['batch-/demote'],
                         'attributed-switch-isolation-failed')
        self.assertNotIn('batch-/promote', found)

    def test_slipping_self_check_reports_unchecked(self):
        with patch.object(scenarios, '_self_check_isolation',
                          return_value=['attributed-queued-behind-batches'
                                        ]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'attributed-switch-isolation-unchecked'), record['detail'])
        self.assertIn('slipped', record['detail'])
        report.validate_scenario(record)

    def test_missing_seams_report_inconclusive(self):
        ctx = self._ctx()
        del ctx['pause_controller']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pause_controller', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.stick_degraded = True
        self.feed.thawed = 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('the pair is not converged', record['detail'])
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
