"""The 2430_ownerless_remote_backoff leg's scenario unit coverage —
the feed fake and TestCase class for
scenario_ownerless_remote_backoff, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py.


The feed stages the leg's shape: ctrl-a owns the deployed pair's
field while ctrl-b tracks; the born levers stage the scratch field
and its container pause — 'paused' leaves the seat's monitor
serving but the field itself never answers — and the labeled
born-active launch stands pending and ownerless on it. The seat's
monitor channel serves /role, /health, and the executor-lock read
/checkpoint; the pending tick advances one per served read unless a
doctor freezes it. Every transition keys off the leg's lever calls
so two passes emit identical digests; the fault flags stage each
named defect, each nondeterministic surface, and each pre-contract
shape the leg inconcludes on."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class BackoffFeed:
    """A stubbed rig for the ownerless remote-attachment backoff leg.

    The born field stands 'serving' until pause_field() freezes it —
    the seat's monitor then still answers (the monitor is the run's
    own surface; the frozen member is the field it is remote from)
    while the pending tick keeps advancing at the fix's bounded
    cadence. unpause_field() lands the deferred conditional grant —
    the seat walks standby → promoting → active with the claim held
    and the journal carries the transitions. The fault flags doctor
    each named shape: the defect signature itself (tick frozen at 0
    with the lock read starved or the scan age unbounded — the
    pre-contract revision's inconclusive shape), the same frozen tick
    on a bounded surface (the issue's doctored negative — a failure
    the judge names), the dishonest pending surfaces, the starved or
    unbounded reads, the grant that never lands, and the pair
    instabilities."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': 'driven'}
    REMOTE = 'dcs-hw-qa-1-born-plant:9003'

    def __init__(self, journal):
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.atick = 900         # the deployed owner's scan tick
        self.btick = 900         # the deployed standby's scan tick
        self.field = None        # None | 'serving' | 'paused'
        self.seat = None         # the born launch's run state
        self.moved = False       # the pair's post-thaw disturbance
        self.role_reads = 0      # /role reads served on the seat
        self.calls = []
        # Staging failures.
        self.stage_fails = False        # start_born_field raises
        self.pause_fails = False        # pause_born_field raises
        self.unpause_fails = False      # unpause_born_field raises
        self.launch_fails = False       # start_born_controller raises
        self.state_fails = False        # born_controller_state raises
        self.silent_rig = False         # every endpoint refuses
        self.unsettled_pair = False     # the standby never tracks
        self.watch_starves_seat = False  # the seat monitor never answers
        # The pre-contract shapes the leg inconcludes on.
        self.predates_pending = False   # the launch exits on the freeze
        self.departs_mid_window = False # the pending seat dies mid-watch
        self.defect_signature = False   # frozen tick + starved lock
                                        # read + unbounded scan age
        self.health_unshaped = False    # /health predates the bounded
                                        # liveness report
        # The contract defect doctors.
        self.frozen_bounded = False     # tick 0 on a bounded surface —
                                        # the issue's doctored negative
        self.below_floor = False        # the cadence under-fills
        self.surface_active = False     # pending reports role=active
        self.claims_held = False        # pending reports the claim held
        self.dishonest_sync = False     # pending reports tracking
        self.mirror_stall = False       # a mid-window mirror read stalls
        self.lock_stall = False         # the executor-lock read starves
        self.age_unbounded = False      # the served scan age overflows
        self.recovery_never = False     # the thaw never lands the grant
        self.grant_unjournaled = False  # the grant lands unjournaled
        # The deployed pair's instabilities.
        self.pair_moves = False         # the standby reports active
        self.pair_wedged = False        # the owner's tick freezes

    # --- the runner's born levers, faked ---------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self.field = 'serving'
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': self.REMOTE, 'mode': mode}

    def pause_field(self):
        self.calls.append(('pause_born_field',))
        if self.pause_fails:
            raise RuntimeError('docker pause failed')
        self.field = 'paused'

    def unpause_field(self):
        self.calls.append(('unpause_born_field',))
        if self.unpause_fails:
            raise RuntimeError('docker unpause failed')
        if self.field == 'paused':
            self.field = 'serving'
            if self.seat and self.seat['pending'] \
                    and not self.recovery_never:
                self.seat['pending'] = False
                self.seat['owns'] = True
                if not self.grant_unjournaled:
                    self._journal({'role_changed': {
                        'from': 'standby', 'to': 'promoting'}})
                    self._journal({'role_changed': {
                        'from': 'promoting', 'to': 'active'}})
            if self.pair_moves:
                self.moved = True

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        self.field = None

    def start_controller(self, seat, remote, peer=None, standby=None):
        self.calls.append(('start_born_controller', seat, remote))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        self.seat = {'seat': seat, 'remote': remote, 'pending': True,
                     'owns': False, 'tick': 0,
                     'exited': self.predates_pending,
                     'exit': 1 if self.predates_pending else None}
        self._boundary()
        return {'container': 'dcs-hw-qa-1-' + seat}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seat = None

    def state(self, seat):
        if self.state_fails:
            raise RuntimeError('docker inspect failed')
        if self.departs_mid_window and self.seat is not None \
                and self.role_reads > 4:
            self.seat['exited'] = True
            self.seat['exit'] = 1
        if self.seat is None:
            return {'container': 'dcs-hw-qa-1-' + seat,
                    'running': False, 'exit': None, 'logs': '',
                    'absent': True}
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': not self.seat['exited'],
                'exit': self.seat['exit'], 'logs': '',
                'absent': False}

    # --- the born seat's durable journal ----------------------------

    def _write(self, record):
        with self.journal.open('a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _boundary(self):
        self._write({'run_boundary': {'run': 1, 'tick': 0}})

    def _journal(self, event):
        self._write({'entry': {'seq': 0, 'tick': self.seat['tick'],
                               'event': event}})

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _advance(self):
        seat = self.seat
        if seat is None or seat['exited'] or not seat['pending']:
            return
        if self.defect_signature or self.frozen_bounded:
            return
        seat['tick'] = min(seat['tick'] + 1, 2) \
            if self.below_floor else seat['tick'] + 1

    def _pair_role(self, host):
        if host == 'ctrl-a:1':
            tick = self.atick
            if not self.pair_wedged:
                self.atick += 1
            return {'role': 'active', 'tick': tick,
                    'field_claim': 'held'}
        tick = self.btick
        self.btick += 1
        if self.moved:
            return {'role': 'active', 'tick': tick,
                    'field_claim': 'held'}
        report = {'role': 'standby', 'tick': tick}
        report['sync'] = {'degraded': {'reason': 'unsettled'}} \
            if self.unsettled_pair \
            else {'tracking': {'aligned': tick}}
        return report

    def _seat_role(self):
        seat = self.seat
        self.role_reads += 1
        if self.mirror_stall and self.role_reads == 8:
            raise urllib.error.URLError('timed out')
        role = 'active' if seat['owns'] else 'standby'
        if self.surface_active and seat['pending']:
            role = 'active'
        sync = 'unsynchronized'
        if self.dishonest_sync and seat['pending']:
            sync = {'tracking': {'aligned': seat['tick']}}
        claim = 'held' if seat['owns'] else None
        if self.claims_held and seat['pending']:
            claim = 'held'
        return {'role': role, 'tick': seat['tick'],
                'field_claim': claim, 'sync': sync}

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if host in ('ctrl-a:1', 'ctrl-b:2'):
            if (method, route) == ('GET', '/role'):
                return 200, self._pair_role(host)
            if (method, route) == ('GET', '/health'):
                return 200, {'live': True, 'tick': self.atick,
                             'last_scan_age_ms': 20}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if self.HOSTS.get(host) != 'driven' or self.seat is None \
                or self.seat['exited'] or self.watch_starves_seat:
            raise urllib.error.URLError('connection refused')
        self._advance()
        seat = self.seat
        if (method, route) == ('GET', '/role'):
            return 200, self._seat_role()
        if (method, route) == ('GET', '/health'):
            if self.health_unshaped:
                return 200, {'live': True, 'role': 'standby',
                             'tick': seat['tick']}
            age = 90000 if (self.defect_signature
                            or self.age_unbounded) else 120
            return 200, {
                'live': True,
                'role': 'active' if seat['owns'] else 'standby',
                'tick': seat['tick'], 'last_scan_age_ms': age}
        if (method, route) == ('GET', '/checkpoint'):
            if self.defect_signature or self.lock_stall:
                raise urllib.error.URLError('timed out')
            return 200, {'tick': seat['tick']}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class OwnerlessBackoffTests(unittest.TestCase):
    """scenario_ownerless_remote_backoff against the stubbed rig: the
    labeled born-active stands pending and ownerless on the frozen
    field, its monitor keeps a bounded degraded cadence with /role
    and /health answering, the thaw lands the deferred grant, and the
    deployed pair never moves — two passes, identical digests. Each
    fault flag stages a named failure, a nondeterministic surface, or
    a pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'driven-journal.jsonl'
        self.feed = BackoffFeed(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'driven': 'http://ctrl-d:5',
                'evidence_dir': str(self.evidence),
                'start_born_field': feed.start_field,
                'pause_born_field': feed.pause_field,
                'unpause_born_field': feed.unpause_field,
                'stop_born_field': feed.stop_field,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state,
                'journal_files': {'driven': str(self.journal)}}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'MIRROR_BOUND', 0.02), \
                patch.object(scenarios, 'LOCK_BOUND', 0.02), \
                patch.object(scenarios, 'FROZEN_WINDOW', 0.08), \
                patch.object(scenarios, 'BACKOFF_POLL', 0.001), \
                patch.object(scenarios, 'BACKOFF_SETTLE', 0.05):
            return scenarios.scenario_ownerless_remote_backoff(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('ownerless-remote-backoff-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_ownerless_remote_backoff,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('ownerless-remote-backoff'),
            scenarios.scenario_ownerless_remote_backoff)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_remote_foreign_model),
            order.index(
                scenarios.scenario_ownerless_remote_backoff))
        self.assertLess(
            order.index(
                scenarios.scenario_ownerless_remote_backoff),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/ownerless-remote-backoff-pass-1.json',
             'evidence/ownerless-remote-backoff-pass-2.json'])
        passed = self._pass(1)
        self.assertEqual(passed['violations'], {})
        frozen = passed['frozen']
        ticks = [tick for tick in frozen['ticks']
                 if isinstance(tick, int)]
        self.assertGreater(ticks[-1] - ticks[0], 0)
        self.assertTrue(passed['recovery']['settled'])
        self.assertTrue(passed['recovery']['grant_journaled'])
        self.assertEqual(
            passed['digest'],
            {'pending': 'held', 'cadence': 'bounded',
             'monitor': 'bounded', 'recovery': 'claimed',
             'pair': 'undisturbed'})
        self.assertEqual(passed['digest'], self._pass(2)['digest'])
        # Every pass ends torn down — the seat and the scratch field
        # removed, the launch configuration restored.
        self.assertIsNone(self.feed.seat)
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_born_field'), 2)
        self.assertEqual(kinds.count('stop_born_field'), 2)
        self.assertGreaterEqual(kinds.count('unpause_born_field'),
                                kinds.count('pause_born_field'))
        self.assertIn(('stop_born_controller', 'driven'),
                      self.feed.calls)

    def test_two_runs_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = self._pass(1)['digest']
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        evidence2 = Path(tmp2.name) / 'evidence'
        evidence2.mkdir()
        journal2 = Path(tmp2.name) / 'driven-journal.jsonl'
        feed2 = BackoffFeed(journal2)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['journal_files'] = {'driven': str(journal2)}
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(first, json.loads(
            (evidence2 / 'ownerless-remote-backoff-pass-1.json')
            .read_text())['digest'])

    # The doctored negatives — each named defect must fail the run by
    # the named diagnostic.

    def test_tick_frozen_with_bounded_surface_fails(self):
        # The issue's own doctored negative: bounded degradation
        # asserted while the pending seat's tick stays frozen at 0
        # across the window — the run fails by name, never
        # inconclusive and never passed.
        self.feed.frozen_bounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_cadence_below_floor_fails(self):
        self.feed.below_floor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_reports_active_fails(self):
        self.feed.surface_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_claims_held_fails(self):
        self.feed.claims_held = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dishonest_sync_fails(self):
        self.feed.dishonest_sync = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mirror_read_starved_fails(self):
        # A /role read that reaches the serving monitor but never
        # answers inside the bound — the defect's stall leaking past
        # the mirror's contract.
        self.feed.mirror_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_lock_read_starved_fails(self):
        # The executor-lock read queuing past its bound while the
        # cadence held — the serialized-stall defect's read shape.
        self.feed.lock_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_scan_age_unbounded_fails(self):
        self.feed.age_unbounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovery_never_lands_fails(self):
        self.feed.recovery_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_grant_unjournaled_fails(self):
        self.feed.grant_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    # The pre-contract and unreadable surfaces must report
    # inconclusive.

    def test_pending_exit_is_inconclusive(self):
        # The pre-contract shape: the labeled launch exits on the
        # frozen field rather than standing pending.
        self.feed.predates_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_health_unshaped_is_inconclusive(self):
        self.feed.health_unshaped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unpause_failure_is_inconclusive(self):
        self.feed.unpause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unpause', record.get('detail', ''))
        report.validate_scenario(record)

    def test_departed_mid_window_is_inconclusive(self):
        # A pending run that dies mid-watch is the pre-contract
        # shape, not a frozen-window failure.
        self.feed.departs_mid_window = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_defect_signature_is_inconclusive(self):
        # The recorded defect itself — the pending seat's tick frozen
        # at 0 while its executor stall starves the lock read and the
        # served scan age runs unbounded — is a revision predating
        # the contract, not a failure of this leg's judge.
        self.feed.defect_signature = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    # The instability the contract does not answer for must report
    # nondeterministic.

    def test_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pause_failure_is_nondeterministic(self):
        self.feed.pause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_watch_starves_is_nondeterministic(self):
        self.feed.watch_starves_seat = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_fails_is_nondeterministic(self):
        self.feed.state_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_moves_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedged_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        calls = []
        real = scenarios._backoff_digest

        def diverging(record, violations):
            calls.append(1)
            digest = dict(real(record, violations))
            if len(calls) > 1:
                digest['pair'] = 'wiggled'
            return digest

        with patch.object(scenarios, '_backoff_digest', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_born_field', 'pause_born_field',
                    'unpause_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('born-active staging', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled_pair = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    # The leg's own auditors must catch their planted negatives.

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_judge_backoff',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ownerless-backoff-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._backoff_self_check(), [])


if __name__ == '__main__':
    unittest.main()
