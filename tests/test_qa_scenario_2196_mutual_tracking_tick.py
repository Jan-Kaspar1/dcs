"""The 2196_mutual_tracking_tick leg's scenario unit coverage — the
stubbed pair and TestCase class for scenario_mutual_tracking_tick,
split out per the #940 convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The subject is the deployed pair's bounded tick-domain contract under
mutual standby tracking: demoting the field owner leaves both
controllers standing by and tracking each other, and the regression the
fix closes is a seeded tick_offset that ratchets instead of clearing.
The stub paces each container's own run clock on the wall clock — the
rig's `--scan-ms` shape, so the leg's measured cadence is what the
served ticks actually show — and each doctored flag stages one named
defect or instability the issue calls out.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'MutualTrackingTickTests.test_registered',
    'MutualTrackingTickTests.test_clean_rig_passes_and_validates',
    'MutualTrackingTickTests.test_ratcheting_offset_fails',
    'MutualTrackingTickTests.test_the_rate_clause_fires_on_a_'
    'compounded_landing',
    'MutualTrackingTickTests.test_persisted_discontinuity_fails',
    'MutualTrackingTickTests.test_served_regression_fails',
    'MutualTrackingTickTests.test_seed_never_forms_reports_'
    'nondeterministic',
    'MutualTrackingTickTests.test_seed_never_clears_fails',
    'MutualTrackingTickTests.test_offset_uncleared_fails',
    'MutualTrackingTickTests.test_second_run_boundary_fails',
    'MutualTrackingTickTests.test_source_restarted_fails',
    'MutualTrackingTickTests.test_posture_unsettled_fails',
    'MutualTrackingTickTests.test_roles_unrestored_fails',
    'MutualTrackingTickTests.test_surface_unstamped_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_cadence_unproven_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_watch_starved_reports_'
    'nondeterministic',
    'MutualTrackingTickTests.test_freeze_refused_reports_'
    'nondeterministic',
    'MutualTrackingTickTests.test_journal_unreadable_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_demote_refused_reports_'
    'nondeterministic',
    'MutualTrackingTickTests.test_unreachable_pair_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_single_endpoint_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_missing_pause_action_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_missing_journal_files_reports_'
    'inconclusive',
    'MutualTrackingTickTests.test_unkeyed_pair_reports_inconclusive',
    'MutualTrackingTickTests.test_diverging_digests_report_'
    'nondeterministic',
    'MutualTrackingTickTests.test_silent_judge_reports_unchecked',
    'MutualTrackingTickTests.test_two_runs_produce_identical_evidence',
})


class MutualTrackingTickFeed:
    """A stubbed redundant pair for the tick-domain leg. The launched
    active owns the field with no configured tracking source; the
    standby tracks it through its declared `--standby` pull. Each
    container paces its own run clock on the wall clock — the rig's
    `--scan-ms` shape, so the leg's measured cadence is what the
    served ticks actually show — and a frozen container's clock stands
    still while its monitor socket stays bound, so the survivor's
    scans advance its run tick past the frozen mark.

    The tracking model implements the #693 contract: a pull that
    produced nothing counts a miss and reports `degraded`; a landed
    apply realigns the run on the source's tick with no standing
    offset, so the seed clears on recovery. The `ratchet` flag
    replays the pre-fix shape — the apply lands at `tick + offset` and
    the served sum feeds the peer, so each landing compounds."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    CADENCE = 10.0        # ticks per second the stub paces at
    START = 100

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': self.START, 'b': self.START}
        self.at = {'a': time.monotonic(), 'b': time.monotonic()}
        self.up = {'a': True, 'b': True}
        self.paused = {'a': False, 'b': False}
        self.role = {'a': 'active', 'b': 'standby'}
        self.aligned = {'a': self.START, 'b': self.START}
        self.misses = 0
        self.seq = {'a': 0, 'b': 0}
        self.served = {'a': [], 'b': []}
        self.journals = {}
        # Doctor flags — each named failure the issue calls out.
        self.unreachable = False
        self.demote_refused = False
        self.promote_refused = False
        self.pause_refused = False
        self.drop_role = False
        self.no_seed = False
        self.never_clears = False
        self.offset_standing = False
        self.posture = 'orphaned'
        self.predates_contract = False
        self.promoted = False   # the promotion the leg drives landed
        self.ratchet = 0        # the pre-fix landing, in ticks per apply
        self.mega = 0           # the recorded discontinuity, in ticks
        self.wedged = False     # the standby never reconverges once
                                 # the promotion has landed, so the
                                 # launch roles never come back
        for peer in ('a', 'b'):
            path = self.tmp / ('journal-' + peer + '.jsonl')
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            self.journals[peer] = path

    # ---- the durable journal --------------------------------------

    def _push(self, peer, kind, body, tick=None):
        """One journaled record on `peer` — the append axis the audit
        walks. The `mega` flag stamps the recorded discontinuity the
        pre-fix run left on every tick-ordered artifact."""
        self.seq[peer] += 1
        stamped = self.tick[peer] if tick is None else tick
        stamped += self.mega
        entry = {'seq': self.seq[peer], 'tick': stamped,
                 'event': {kind: body}}
        self.served[peer].append(entry)
        with self.journals[peer].open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    # ---- the wall-clock pacing and the tracking model --------------

    def _pace(self, peer):
        """Advance a container's own run clock with the wall clock —
        the launched peers scan themselves, so a served tick owes the
        cadence it was paced at. A frozen container's clock stands
        still, and `no_seed` holds the survivor too so the frozen
        window never seeds a standing lead."""
        held = not self.up[peer] or self.paused[peer] \
            or (self.no_seed and (self.paused['a'] or self.paused['b']))
        if held:
            return self.tick[peer]
        now = time.monotonic()
        due = int((now - self.at[peer]) * self.CADENCE)
        if due:
            self.tick[peer] += due
            self.at[peer] += due / self.CADENCE
        return self.tick[peer]

    def _pull(self, peer, source):
        """One tracking pull: a produced-nothing pull while the source
        is frozen counts a miss and reports `degraded`; a landed apply
        realigns the run on the source's tick — at `tick + ratchet`
        when the pre-fix defect is staged, which is the compounding
        landing the issue's ratcheting-offset negative replays."""
        if self.paused[source] or not self.up[source]:
            self.misses += 1
            return
        self.misses = 0
        if self.never_clears:
            return
        if self.ratchet:
            self.tick[peer] += self.ratchet
            self.at[peer] = time.monotonic()
            self.aligned[peer] = self.tick[peer]
            return
        self.aligned[peer] = self._pace(source)
        if self.offset_standing:
            # A standing lead the recovered stream never closes — well
            # past the declared clearing bound.
            self.aligned[peer] = self.tick[peer] - 40

    def _settle(self, peer):
        """Role transitions settle at the scan boundary, journaled in
        `seq` order — the durable walk the promotion's audit reads."""
        if self.role[peer] == 'demoting':
            self._push(peer, 'role_changed',
                       {'from': 'demoting', 'to': 'standby'})
            self.role[peer] = 'standby'
        elif self.role[peer] == 'promoting':
            self._push(peer, 'role_changed',
                       {'from': 'promoting', 'to': 'active'})
            self.role[peer] = 'active'

    # ---- the runner-owned lifecycle actions ------------------------

    def pause_controller(self, name):
        if self.pause_refused:
            raise RuntimeError('docker pause failed: refused')
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = True

    def unpause_controller(self, name):
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = False

    # ---- the control plane -----------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _demote(self, peer):
        if self.role[peer] != 'active' or self.demote_refused:
            self._raise(409, 'no_tracking_source')
        self._push(peer, 'role_changed',
                   {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.promote_refused or self.role[peer] in ('active',
                                                       'promoting'):
            self._raise(409, 'not_active')
        self._push(peer, 'role_changed',
                   {'from': self.role[peer], 'to': 'promoting'})
        self.role[peer] = 'promoting'
        self.promoted = True
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch -------------------------------------

    def _report(self, peer):
        self._pace(peer)
        self._settle(peer)
        source = 'b' if peer == 'a' else 'a'
        if self.role[peer] == 'standby':
            self._pull(peer, source)
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] == 'standby':
            if self.wedged and self.promoted \
                    and self.role[source] == 'active':
                report['sync'] = {'degraded': {
                    'detail': 'fetch from ctrl-a:8080: timed out'}}
                return report
            if self.misses:
                report['sync'] = {'degraded': {
                    'detail': 'fetch from ctrl-a:8080: timed out'}}
            elif self.role[source] == 'active':
                report['sync'] = {
                    'tracking': {'aligned': self.aligned[peer]}}
            else:
                report['sync'] = {
                    self.posture: {'aligned': self.aligned[peer]}}
        if peer == 'b' and self.role['b'] == 'standby':
            report['failover'] = {
                'converged': 'tracking' in (report.get('sync') or {}),
                'misses': self.misses, 'budget': 120}
        return report

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        if self.paused[peer]:
            raise urllib.error.URLError('timed out')
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if self.drop_role and peer == 'a' and self.paused['b'] \
                and route == '/role':
            raise urllib.error.URLError('timed out')
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/checkpoint'):
            if self.predates_contract:
                return 200, {'receipts': []}
            return 200, {
                'tick': self._pace(peer),
                'source_owns_field': self.role[peer] == 'active',
                'generation': 1}
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s' % (method, url))


class MutualTrackingTickTests(unittest.TestCase):
    """The mutual-tracking-tick leg against the stubbed pair: a clean
    rig passes with identical digests and evidence — the cadence
    measured, the mutual settle reached, the seed standing, the offset
    cleared, the durable axis ordered, the launch roles restored — each
    doctored contract breach reports
    mutual-tracking-tick-failed, each instability reports
    mutual-tracking-tick-nondeterministic, and an unreachable,
    seam-less, or pre-contract run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = MutualTrackingTickFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'pair_token': 'qa-probe',
               'failover_misses': 120,
               'journal_files': {
                   'active': str(feed.journals['a']),
                   'standby': str(feed.journals['b'])},
               'pause_controller': feed.pause_controller,
               'unpause_controller': feed.unpause_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'MUTUAL_SETTLE', 0.6), \
                patch.object(scenarios, 'MUTUAL_POLL', 0.001), \
                patch.object(scenarios, 'CADENCE_MEASURE', 0.4), \
                patch.object(scenarios, 'WINDOW_HOLD', 0.3), \
                patch.object(scenarios, 'WINDOW_POLL', 0.02):
            return scenarios.scenario_mutual_tracking_tick(
                self.ctx(feed, **overrides))

    def passes(self):
        names = ('mutual-tracking-tick-pass-1.json',
                 'mutual-tracking-tick-pass-2.json')
        for name in names:
            self.assertTrue((self.evidence / name).is_file(), name)
        return [json.loads((self.evidence / name).read_text())
                for name in names]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_tracker_realign_tick_order),
            order.index(scenarios.scenario_mutual_tracking_tick))
        self.assertLess(
            order.index(scenarios.scenario_mutual_tracking_tick),
            order.index(scenarios.scenario_failover))
        self.assertIs(verify.case_function('mutual-tracking-tick'),
                      scenarios.scenario_mutual_tracking_tick)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'surface': 'stamped', 'cadence': 'measured',
             'ticks': 'bounded', 'posture': 'mutual',
             'seed': 'standing', 'offset': 'cleared',
             'axis': 'ordered', 'roles': 'restored'})
        first = passes[0]['record']
        # The cadence is measured from the served ticks, and the pair's
        # mutual settle reached the ownerless verdict with an aligned
        # mark on both peers.
        self.assertGreater(first['cadence']['active'], 0)
        self.assertEqual(first['mutual']['active']['posture'],
                         'standby/orphaned')
        self.assertEqual(first['mutual']['standby']['posture'],
                         'standby/orphaned')
        # The frozen window seeded a standing lead, and the recovery
        # cleared it inside the declared bound on both peers.
        self.assertGreater(first['seeded'], 0)
        self.assertIsNotNone(first['cleared'])
        self.assertTrue(all(abs(offset) <= scenarios.CLEAR_BOUND
                            for offset in first['cleared'].values()))
        # The durable axis is ordered and the promotion continued the
        # demoted peer's run.
        self.assertEqual(first['axis']['active']['boundaries'], 1)
        self.assertEqual(first['axis']['active']['regressions'], [])
        self.assertEqual(first['walk']['owner'][-2:],
                         [['standby', 'promoting'],
                          ['promoting', 'active']])
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_ratcheting_offset_fails(self):
        # The doctored negative the issue names: the seeded offset
        # ratchets instead of clearing — every landing compounds into
        # the run's own clock, the defect's served sum feeding back.
        self.feed.ratchet = 900
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        # The leg can no longer clear the seed, so it never reaches the
        # cadence reading; the run must still fail by name rather than
        # pass the compounded landing.
        self.assertIn('seeded offset', record['detail'])
        report.validate_scenario(record)

    def test_the_rate_clause_fires_on_a_compounded_landing(self):
        # The rate claim is its own clause: a pass whose own run clocks
        # outpaced the measured cadence's bound must fail by name even
        # with every other clause clean.
        original = scenarios._mutual_pass

        def ratcheting(ctx, number, owner, peer, journal_paths):
            record, evidence = original(
                ctx, number, owner, peer, journal_paths)
            record['rates'] = {
                'active': {'rate': 3000.0, 'bound': 40.0},
                'standby': {'rate': 2900.0, 'bound': 40.0}}
            return record, evidence
        with patch.object(scenarios, '_mutual_pass', ratcheting):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        self.assertIn('cadence', record['detail'])
        report.validate_scenario(record)

    def test_persisted_discontinuity_fails(self):
        # The journaled and persisted discontinuity: every recorded
        # artifact stamped off the run's own epoch.
        self.feed.mega = 3000
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        report.validate_scenario(record)

    def test_served_regression_fails(self):
        # A tracking apply never rewinds the run's clock — the crossing
        # half of the defect the seeded offset took the pair past.
        self.feed.ratchet = -40
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        report.validate_scenario(record)

    def test_seed_never_forms_reports_nondeterministic(self):
        # The staged freeze left the survivor's clock on the frozen
        # mark: the seeding regression never happened, so the leg has
        # no verdict to give.
        self.feed.no_seed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        self.assertIn('standing lead', record['detail'])
        report.validate_scenario(record)

    def test_seed_never_clears_fails(self):
        self.feed.never_clears = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        self.assertIn('cleared', record['detail'])
        report.validate_scenario(record)

    def test_offset_uncleared_fails(self):
        # The recovered stream lands the applies but the standing lead
        # never closes — the seed compounding's other face.
        self.feed.offset_standing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        report.validate_scenario(record)

    def test_second_run_boundary_fails(self):
        # The promotion restarting the tick domain instead of
        # continuing it.
        with self.feed.journals['a'].open('a') as handle:
            handle.write(
                json.dumps({'run_boundary': {'run': 2, 'tick': 0}}) + '\n')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        self.assertIn('boundar', record['detail'])
        report.validate_scenario(record)

    def test_source_restarted_fails(self):
        # A source restart the same-generation stream never had.
        original_push = self.feed._push

        def push(peer, kind, body, tick=None):
            original_push(peer, kind, body, tick)
            if peer == 'a' and kind == 'role_changed':
                original_push(peer, 'source_restarted', {'source': 'x'})
        self.feed._push = push
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        report.validate_scenario(record)

    def test_posture_unsettled_fails(self):
        # The mutual settle landing on a verdict the ownerless line
        # does not owe.
        self.feed.posture = 'tracking'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        self.assertIn('orphaned', record['detail'])
        report.validate_scenario(record)

    def test_roles_unrestored_fails(self):
        # The promotion landed but the pair never reconverged onto its
        # launch layout — the restored-posture clause on its own.
        self.feed.wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-failed'), record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_surface_unstamped_reports_inconclusive(self):
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        # The unstamped surface is the staged run's pre-contract shape,
        # which the judge names as an instability, never as a contract
        # violation.
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_cadence_unproven_reports_inconclusive(self):
        self.feed.paused['a'] = True
        self.feed.paused['b'] = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_watch_starved_reports_nondeterministic(self):
        # The frozen window's watch starved: the surviving peer's
        # monitor stopped answering behind the freeze.
        self.feed.drop_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_freeze_refused_reports_nondeterministic(self):
        self.feed.pause_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_journal_unreadable_reports_inconclusive(self):
        self.feed.journals['a'].write_text('not json\n')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('only one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_missing_pause_action_reports_inconclusive(self):
        record = self.run_scenario(pause_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pause_controller', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_pair_reports_inconclusive(self):
        record = self.run_scenario(pair_token=None, probe=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('--pair-token', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        # Two passes whose digests differ: the second pass sees a peer
        # whose cadence the rig never demonstrated.
        original = scenarios._mutual_pass
        calls = {'n': 0}

        def flaky(ctx, number, owner, peer, journal_paths):
            calls['n'] += 1
            record, evidence = original(
                ctx, number, owner, peer, journal_paths)
            if calls['n'] == 2:
                record['cadence'] = {}
            return record, evidence
        with patch.object(scenarios, '_mutual_pass', flaky):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that stopped naming the ratcheting offset would let
        # the leg pass an unexercised contract.
        with patch.object(
                scenarios, '_mutual_tick_self_check',
                return_value=['ratcheting-offset']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'mutual-tracking-tick-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        digests = [json.loads(
            (self.evidence / ('mutual-tracking-tick-pass-%d.json'
                              % number)).read_text())['digest']
            for number in (1, 2)]
        self.assertEqual(first['outcome'], 'passed', first)
        self.assertEqual(digests[0], digests[1])


if __name__ == '__main__':
    unittest.main()