"""The 2440_pending_serving_bound leg's scenario unit coverage —
the feed fake and TestCase class for
scenario_pending_serving_bound, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape: ctrl-a owns the deployed pair's
field while ctrl-b tracks; the born levers stage the scratch field
and its container pause — 'paused' leaves the seat's monitor
serving but the field itself never answers — and the labeled
born-active launch stands pending on it. The seat's monitor
channel serves the lock-free /role and /health mirrors plus the
lock-taking serving set: /checkpoint, /promote answering the named
not_converged refusal, /demote answering not_active, and /command
answering the receipted not_active rejection. The pending tick
advances one per served read unless a doctor freezes it; a queued
scan sleeps a lock call past its bound — the ~N_channels-timeout
serialization the issue's doctored negative names. The contrast
control re-stages the seat on a 'silent' field — the address
resolvable but nothing listening — where the same set must answer
inside the instant bound. Every transition keys off the leg's
lever calls so two passes emit identical digests; the fault flags
stage each named defect, each nondeterministic surface, and each
pre-contract shape the leg inconcludes on — including the served
null last_scan_age_ms a run reports before its first completed scan,
which the leg must read as the run's own start-up window rather than
a revision predating the liveness contract."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PendingServingBoundTests.test_registered',
    'PendingServingBoundTests.test_clean_passes_validates_and_tears_down',
    'PendingServingBoundTests.test_two_runs_produce_identical_digests',
    'PendingServingBoundTests.test_lock_call_queued_behind_scan_fails',
    'PendingServingBoundTests.test_lock_call_starved_fails',
    'PendingServingBoundTests.test_promote_refusal_wrong_fails',
    'PendingServingBoundTests.test_promote_admitted_fails',
    'PendingServingBoundTests.test_demote_refusal_wrong_fails',
    'PendingServingBoundTests.test_command_admitted_fails',
    'PendingServingBoundTests.test_checkpoint_claims_field_fails',
    'PendingServingBoundTests.test_mirror_read_starved_fails',
    'PendingServingBoundTests.test_tick_frozen_with_bounded_surface_fails',
    'PendingServingBoundTests.test_cadence_below_floor_fails',
    'PendingServingBoundTests.test_scan_age_unbounded_fails',
    'PendingServingBoundTests.test_pending_reports_active_fails',
    'PendingServingBoundTests.test_pending_claims_held_fails',
    'PendingServingBoundTests.test_dishonest_sync_fails',
    'PendingServingBoundTests.test_recovery_never_lands_fails',
    'PendingServingBoundTests.test_grant_unjournaled_fails',
    'PendingServingBoundTests.test_refused_contrast_stalls_fails',
    'PendingServingBoundTests.test_pending_exit_is_inconclusive',
    'PendingServingBoundTests.test_health_unshaped_is_inconclusive',
    'PendingServingBoundTests.test_pre_first_scan_stamp_still_passes',
    'PendingServingBoundTests.test_unpause_failure_is_inconclusive',
    'PendingServingBoundTests.test_departed_mid_window_is_inconclusive',
    'PendingServingBoundTests.test_refused_departure_is_inconclusive',
    'PendingServingBoundTests.test_defect_signature_is_inconclusive',
    'PendingServingBoundTests.test_stage_failure_is_nondeterministic',
    'PendingServingBoundTests.test_pause_failure_is_nondeterministic',
    'PendingServingBoundTests.test_launch_failure_is_nondeterministic',
    'PendingServingBoundTests.test_refused_stage_failure_is_nondeterministic',
    'PendingServingBoundTests.test_watch_starves_is_nondeterministic',
    'PendingServingBoundTests.test_state_fails_is_nondeterministic',
    'PendingServingBoundTests.test_pair_moves_is_nondeterministic',
    'PendingServingBoundTests.test_pair_wedged_is_nondeterministic',
    'PendingServingBoundTests.test_divergent_digests_are_nondeterministic',
    'PendingServingBoundTests.test_missing_seams_are_inconclusive',
    'PendingServingBoundTests.test_unreachable_rig_is_inconclusive',
    'PendingServingBoundTests.test_unsettled_pair_is_inconclusive',
    'PendingServingBoundTests.test_unchecked_self_check_fails',
    'PendingServingBoundTests.test_self_check_is_complete',
})


class ServingFeed:
    """A stubbed rig for the pending bounded-serving leg.

    The born field stands 'serving' until pause_field() freezes it —
    the seat's monitor then still answers (the monitor is the run's
    own surface; the frozen member is the field it is remote from)
    while the pending tick keeps advancing at the fix's bounded
    cadence. The lock-taking set answers its named pending verdicts:
    /checkpoint's document stamps source_owns_field false, /promote
    refuses 409 not_converged, /demote refuses 409 not_active, and
    /command settles a receipted rejected:not_active. unpause_field()
    lands the deferred conditional grant — the seat walks
    standby → promoting → active with the claim held and the journal
    carries the transitions. The 'silent' field restage stands the
    contrast control's connection-refused shape — the seat re-launches
    pending on an address nothing listens on, and the same serving set
    answers instantly. The fault flags doctor each named shape: the
    defect signature itself (tick frozen at 0 with the lock reads
    starved — the pre-contract revision's inconclusive shape), a lock
    call queuing ~N_channels timeouts behind the wedged scan or
    starving outright (the issue's doctored negative — failures the
    judge names), the frozen tick on a bounded surface, the wrong or
    admitted refusals, the dishonest pending surfaces, the grant that
    never lands, the refused-field contrast stalling, and the pair
    instabilities."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': 'driven'}
    REMOTE = 'dcs-hw-qa-1-born-plant:9003'

    def __init__(self, journal):
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.atick = 900         # the deployed owner's scan tick
        self.btick = 900         # the deployed standby's scan tick
        self.field = None        # None | 'serving' | 'paused' | 'silent'
        self.seat = None         # the born launch's run state
        self.moved = False       # the pair's post-thaw disturbance
        self.role_reads = 0      # /role reads served on the seat
        self.health_reads = 0    # /health reads served on the seat
        self.calls = []
        # Staging failures.
        self.stage_fails = False        # start_born_field raises
        self.pause_fails = False        # pause_born_field raises
        self.unpause_fails = False      # unpause_born_field raises
        self.launch_fails = False       # start_born_controller raises
        self.refused_stage_fails = False  # the 'silent' restage raises
        self.state_fails = False        # born_controller_state raises
        self.silent_rig = False         # every endpoint refuses
        self.unsettled_pair = False     # the standby never tracks
        self.watch_starves_seat = False  # the seat monitor never answers
        # The pre-contract shapes the leg inconcludes on.
        self.predates_pending = False   # the launch exits on the freeze
        self.departs_mid_window = False  # the pending seat dies mid-watch
        self.departs_refused = False    # the refused-field launch exits
        self.defect_signature = False   # frozen tick + starved lock
                                        # reads + unbounded scan age
        self.health_unshaped = False    # /health predates the bounded
                                        # liveness report
        self.pre_scan_stamp = False     # /health answers null until the
                                        # seat's first completed scan
        # The contract defect doctors.
        self.queued_scan = False        # lock calls queue ~N_channels
                                        # timeouts behind the wedged
                                        # scan — the issue's doctored
                                        # negative
        self.lock_stall = False         # the lock-taking reads starve
        self.frozen_bounded = False     # tick 0 on a bounded surface
        self.below_floor = False        # the cadence under-fills
        self.surface_active = False     # pending reports role=active
        self.claims_held = False        # pending reports the claim held
        self.dishonest_sync = False     # pending reports tracking
        self.checkpoint_claims = False  # the checkpoint stamps
                                        # source_owns_field true
        self.mirror_stall = False       # a mid-window mirror read stalls
        self.age_unbounded = False      # the served scan age overflows
        self.promote_mislabeled = False  # /promote answers a wrong refusal
        self.promote_admits = False     # /promote answers 200
        self.demote_mislabeled = False  # /demote answers a wrong refusal
        self.command_admitted = False   # /command accepts the write
        self.recovery_never = False     # the thaw never lands the grant
        self.grant_unjournaled = False  # the grant lands unjournaled
        self.refused_stall = False      # the refused-field contrast's
                                        # lock reads starve
        # The deployed pair's instabilities.
        self.pair_moves = False         # the standby reports active
        self.pair_wedged = False        # the owner's tick freezes

    # --- the runner's born levers, faked ---------------------------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.stage_fails \
                or (mode == 'silent' and self.refused_stage_fails):
            raise RuntimeError('docker run failed: name in use')
        self.field = mode
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
        departed = self.predates_pending \
            or (self.departs_refused and self.field == 'silent')
        self.seat = {'seat': seat, 'remote': remote, 'pending': True,
                     'owns': False, 'tick': 0,
                     'exited': departed,
                     'exit': 1 if departed else None}
        # The launch's --journal-file resets at launch — the runner
        # owns that boundary.
        self.journal.write_text('')
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

    def _lock_stalled(self):
        if self.defect_signature or self.lock_stall:
            return True
        return self.field == 'silent' and self.refused_stall

    def _conflict(self, payload):
        raise urllib.error.HTTPError(
            'http://ctrl-d:5', 409, 'Conflict', None,
            io.BytesIO(json.dumps(payload).encode()))

    def _queued(self):
        # A lock-taking call arriving while the wedged scan holds the
        # executor mutex — the ~N_channels-timeout serialization the
        # finding measured (~72s here scaled to a beat past the leg's
        # patched bound).
        if self.queued_scan and self.field != 'silent':
            time.sleep(0.05)

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
            self.health_reads += 1
            if self.health_unshaped:
                return 200, {'live': True, 'role': 'standby',
                             'tick': seat['tick']}
            age = 90000 if (self.defect_signature
                            or self.age_unbounded) else 120
            # A run that has not completed a scan yet serves the
            # stamp null — the documented shape before the first
            # completion, never a pre-contract revision.
            if self.pre_scan_stamp and self.health_reads < 3:
                age = None
            return 200, {
                'live': True,
                'role': 'active' if seat['owns'] else 'standby',
                'tick': seat['tick'], 'last_scan_age_ms': age}
        if (method, route) == ('GET', '/checkpoint'):
            if self._lock_stalled():
                raise urllib.error.URLError('timed out')
            self._queued()
            owns = seat['owns'] or \
                (self.checkpoint_claims and seat['pending'])
            return 200, {'tick': seat['tick'],
                         'source_owns_field': owns}
        if (method, route) == ('POST', '/promote'):
            if self._lock_stalled():
                raise urllib.error.URLError('timed out')
            self._queued()
            if self.promote_admits:
                return 200, {'role': 'active', 'tick': seat['tick'],
                             'field_claim': 'held'}
            if self.promote_mislabeled:
                self._conflict({'field_claim_failed': {
                    'detail': 'the field refused the claim'}})
            self._conflict({'not_converged': 'unsynchronized'})
        if (method, route) == ('POST', '/demote'):
            if self._lock_stalled():
                raise urllib.error.URLError('timed out')
            self._queued()
            if self.demote_mislabeled:
                self._conflict({'no_tracking_source': 'no source'})
            self._conflict('not_active')
        if (method, route) == ('POST', '/command'):
            if self._lock_stalled():
                raise urllib.error.URLError('timed out')
            self._queued()
            outcome = {'accepted': {'apply_tick': seat['tick'] + 1}} \
                if self.command_admitted \
                else {'rejected': {'reason': {
                    'not_active': {'role': 'standby'}}}}
            return 200, {'command': (body or {}).get('command'),
                         'outcome': outcome,
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class PendingServingBoundTests(unittest.TestCase):
    """scenario_pending_serving_bound against the stubbed rig: the
    labeled born-active stands pending on the frozen field, its
    monitor keeps /role and /health instant, every lock-taking call —
    the checkpoint pull, the promote/demote switchovers, the receipted
    command — answers inside its bound with the named verdict, the
    tick holds its bounded degraded cadence, the refused-field
    contrast answers instantly, the thaw lands the deferred grant, and
    the deployed pair never moves — two passes, identical digests.
    Each fault flag stages a named failure, a nondeterministic
    surface, or a pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'driven-journal.jsonl'
        self.feed = ServingFeed(self.journal)

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
                patch.object(scenarios, 'REFUSED_BOUND', 0.02), \
                patch.object(scenarios, 'FROZEN_WINDOW', 0.08), \
                patch.object(scenarios, 'SERVING_POLL', 0.001), \
                patch.object(scenarios, 'SERVING_SETTLE', 0.05):
            return scenarios.scenario_pending_serving_bound(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('pending-serving-bound-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_pending_serving_bound,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('pending-serving-bound'),
            scenarios.scenario_pending_serving_bound)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(
                scenarios.scenario_ownerless_remote_backoff),
            order.index(
                scenarios.scenario_pending_serving_bound))
        self.assertLess(
            order.index(
                scenarios.scenario_pending_serving_bound),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/pending-serving-bound-pass-1.json',
             'evidence/pending-serving-bound-pass-2.json'])
        passed = self._pass(1)
        self.assertEqual(passed['violations'], {})
        frozen = passed['frozen']
        ticks = [tick for tick in frozen['ticks']
                 if isinstance(tick, int)]
        self.assertGreater(ticks[-1] - ticks[0], 0)
        # Every lock-taking endpoint got its say inside the window.
        probed = {read['path'] for read in frozen['reads']
                  if read['kind'] == 'lock'}
        self.assertEqual(probed, {'/checkpoint', '/promote',
                                  '/demote', '/command'})
        self.assertTrue(passed['recovery']['settled'])
        self.assertTrue(passed['recovery']['grant_journaled'])
        self.assertTrue(passed['refused']['served'])
        self.assertEqual(
            passed['digest'],
            {'pending': 'held', 'cadence': 'bounded',
             'serving': 'bounded', 'refused': 'instant',
             'recovery': 'claimed', 'pair': 'undisturbed'})
        self.assertEqual(passed['digest'], self._pass(2)['digest'])
        # Every pass ends torn down — the seat and the scratch field
        # removed, the launch configuration restored.
        self.assertIsNone(self.feed.seat)
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        # Two frozen-field stagings and two refused-field restages.
        self.assertEqual(kinds.count('start_born_field'), 4)
        self.assertEqual(kinds.count('stop_born_field'), 4)
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
        feed2 = ServingFeed(journal2)
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['journal_files'] = {'driven': str(journal2)}
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(first, json.loads(
            (evidence2 / 'pending-serving-bound-pass-1.json')
            .read_text())['digest'])

    # The doctored negatives — each named defect must fail the run by
    # the named diagnostic.

    def test_lock_call_queued_behind_scan_fails(self):
        # The issue's own doctored negative: bounded serving asserted
        # while a lock-taking endpoint queues ~N_channels timeouts
        # behind the wedged pending scan — the call answers its named
        # refusal but far past the declared bound.
        self.feed.queued_scan = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_lock_call_starved_fails(self):
        # The executor-lock reads never answering while the cadence
        # held — the serialized-stall defect's read shape minus its
        # frozen-tick signature.
        self.feed.lock_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promote_refusal_wrong_fails(self):
        self.feed.promote_mislabeled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promote_admitted_fails(self):
        self.feed.promote_admits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_demote_refusal_wrong_fails(self):
        self.feed.demote_mislabeled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_command_admitted_fails(self):
        self.feed.command_admitted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_checkpoint_claims_field_fails(self):
        # A pending run's checkpoint stamping source_owns_field true —
        # the pending surface claiming the field it never answered.
        self.feed.checkpoint_claims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mirror_read_starved_fails(self):
        # A /role read that reaches the serving monitor but never
        # answers inside the bound — the stall leaking past the
        # lock-free mirror's contract.
        self.feed.mirror_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_tick_frozen_with_bounded_surface_fails(self):
        # Bounded serving asserted while the pending seat's tick stays
        # frozen at 0 across the window — the run fails by name,
        # never inconclusive and never passed.
        self.feed.frozen_bounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_cadence_below_floor_fails(self):
        self.feed.below_floor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_scan_age_unbounded_fails(self):
        self.feed.age_unbounded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_reports_active_fails(self):
        self.feed.surface_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pending_claims_held_fails(self):
        self.feed.claims_held = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dishonest_sync_fails(self):
        self.feed.dishonest_sync = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovery_never_lands_fails(self):
        self.feed.recovery_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_grant_unjournaled_fails(self):
        self.feed.grant_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_contrast_stalls_fails(self):
        # The contrast control: on the refused field the same lock
        # reads must answer instantly — a stall there is a contract
        # failure, not the defect's accepted-but-silent shape.
        self.feed.refused_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-failed',
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

    def test_pre_first_scan_stamp_still_passes(self):
        # A run that has not completed a scan yet answers /health with a
        # null last_scan_age_ms — the served shape before the first
        # completion. The leg must read that as the run's own start-up
        # window, never as a revision predating the liveness contract:
        # only a run whose /health answers never carry the stamp at all
        # inconcludes.
        self.feed.pre_scan_stamp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        ages = [read.get('last_scan_age_ms') for read in
                self._pass(1)['frozen']['reads']
                if read['path'] == '/health']
        self.assertIn(None, ages)
        self.assertTrue(any(isinstance(age, int) for age in ages))

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

    def test_refused_departure_is_inconclusive(self):
        # The contrast control's pending launch exits on the refused
        # field — the same pre-contract shape.
        self.feed.departs_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_defect_signature_is_inconclusive(self):
        # The recorded defect itself — the pending seat's tick frozen
        # at 0 while the executor stall starves the lock-taking reads
        # and the served scan age runs unbounded — is a revision
        # predating the contract, not a failure of this leg's judge.
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
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pause_failure_is_nondeterministic(self):
        self.feed.pause_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_stage_failure_is_nondeterministic(self):
        self.feed.refused_stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_watch_starves_is_nondeterministic(self):
        self.feed.watch_starves_seat = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_fails_is_nondeterministic(self):
        self.feed.state_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_moves_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedged_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        calls = []
        real = scenarios._serving_digest

        def diverging(record, violations):
            calls.append(1)
            digest = dict(real(record, violations))
            if len(calls) > 1:
                digest['pair'] = 'wiggled'
            return digest

        with patch.object(scenarios, '_serving_digest', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-nondeterministic',
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
        with patch.object(scenarios, '_judge_serving',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('pending-serving-bound-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._serving_self_check(), [])


if __name__ == '__main__':
    unittest.main()
