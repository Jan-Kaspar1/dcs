"""The 2175_tracking_source_fallback leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_tracking_source_fallback, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'TrackingFallbackTests.test_registered',
    'TrackingFallbackTests.test_clean_rig_passes_and_validates',
    'TrackingFallbackTests.test_pinned_dead_verdict_fails',
    'TrackingFallbackTests.test_silent_fallback_fails',
    'TrackingFallbackTests.test_dead_pin_strands_fails',
    'TrackingFallbackTests.test_reconverge_never_fails',
    'TrackingFallbackTests.test_restart_needed_fails',
    'TrackingFallbackTests.test_restore_fails',
    'TrackingFallbackTests.test_driven_never_tracks_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_demote_refused_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_island_never_forms_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_driven_promote_refused_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_pin_never_lands_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_promote_refused_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_starved_watch_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_failover_fired_reports_'
    'nondeterministic',
    'TrackingFallbackTests.test_diverging_digests_report_'
    'nondeterministic',
    'TrackingFallbackTests.test_silent_judge_reports_unchecked',
    'TrackingFallbackTests.test_unreachable_pair_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_unconverged_pair_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_swapped_layout_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_unkeyed_run_reports_inconclusive',
    'TrackingFallbackTests.test_unkeyed_deployed_pair_runs_on_the_'
    'probe_pair',
    'TrackingFallbackTests.test_missing_lifecycle_action_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_missing_journal_files_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_single_endpoint_reports_'
    'inconclusive',
    'TrackingFallbackTests.test_predating_rig_reports_inconclusive',
    'TrackingFallbackTests.test_two_runs_produce_identical_'
    'evidence',
})


class FakeClock:
    """The scenario's `time` module swapped for a deterministic
    clock: every `sleep` advances `now` by exactly its argument, so
    the staged windows collect a fixed row count per phase and two
    whole runs emit byte-identical evidence."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class TrackingFallbackFeed:
    """A stubbed keyed pair plus the driven third peer for the
    dead-successor leg: ctrl-a owns the field and configured no
    tracking source — its demotion verifies the announced hints and
    adopts the newest live one — ctrl-b tracks a through its
    configured --standby pull, and ctrl-d launches through the
    run's start_driven action, tracks a, and promotes onto the
    island's ownerless field so both demoted peers' orphan probes
    pin onto it.

    The tracking half models the pin-fallback contract the leg
    pins: each standby pulls its `resolved` or `adopted` learned
    pin ahead of its configured source; a produced-nothing pull on
    a learned pin counts a miss, and PIN_BUDGET consecutive misses
    release the pin and re-run the source resolution in the same
    scan — the claimed live owner first, then the tracked target,
    then the announced hints — journaling the retarget by name.
    A live owner serves owner checkpoints, an ownerless source
    serves ownerless ones (a heartbeat miss plus a once-per-episode
    field_orphaned entry, plus an orphan-resolution probe), and a
    dead or paused source produces nothing at all. The
    `pause_controller`/`unpause_controller` ctx actions freeze and
    thaw ctrl-b; `start_driven`/`stop_driven` launch and remove
    ctrl-d — a removed container answers nothing but keeps no
    hold on the field claim. Every endpoint call on a peer is one
    completed scan; a pull paces its source's scan boundary.
    Doctor flags stage each named defect the issue calls out."""

    ADDRS = {'a': 'probe-a:8080', 'b': 'probe-b:8081',
             'd': 'probe-d:8082'}
    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b', 'ctrl-d:3': 'd'}
    BUDGET = 120
    PIN_BUDGET = 4

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100, 'd': 100}
        self.up = {'a': True, 'b': True, 'd': False}
        self.paused = {'a': False, 'b': False, 'd': False}
        self.role = {'a': 'active', 'b': 'standby', 'd': 'standby'}
        # The configured --standby source each standby pulls; ctrl-a
        # launched with none — its demotion owes the announced
        # adoption.
        self.source = {'a': None, 'b': 'a', 'd': 'a'}
        # The learned pins: `resolved` from orphan-source probing,
        # `adopted` from the announced-source verify.
        self.resolved = {'a': None, 'b': None, 'd': None}
        self.adopted = {'a': None, 'b': None, 'd': None}
        self.pin_miss = {'a': 0, 'b': 0, 'd': 0}
        self.sync = {'a': None, 'b': 'tracking',
                     'd': 'unsynchronized'}
        self.aligned = {'a': None, 'b': 100, 'd': None}
        self.misses = {'a': 0, 'b': 0, 'd': 0}
        self.converged = {'a': False, 'b': True, 'd': False}
        self.announced = {'a': ['b'], 'b': [], 'd': []}
        self.claim = {'owner': 'a', 'yielded': False}
        self.budget = self.BUDGET
        self.killed = False          # the driven successor is down
        self.driven_started = False
        self.seq = {'a': 0, 'b': 0, 'd': 0}
        self.served = {'a': [], 'b': [], 'd': []}
        self.pair_token = 'probe-pair'
        self.a_promotes = 0
        self.starving = False
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_d = self.tmp / 'journal-d.jsonl'
        for path in (self.journal_a, self.journal_b, self.journal_d):
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_tracking = False      # the standby never converges
        self.swapped = False          # ctrl-b already owns the field
        self.stamps_absent = False    # the served checkpoint drops
                                      # the ownership stamps — a run
                                      # predating the contract
        self.demote_refused = False   # the island-inducing demote
                                      # answers no_tracking_source
        self.never_orphaned = False   # peers keep reporting tracking
                                      # on an ownerless line
        self.driven_never_tracks = False  # the driven peer never
                                          # converges
        self.driven_promote_refused = False  # the successor's claim
                                             # answers refused
        self.pin_never_lands = False  # the orphan probe never pins
                                      # the successor
        self.promote_refused = False  # the launch owner's re-claim
                                      # is refused
        self.no_release = False       # the pin misses never release
                                      # the learned pin
        self.silent_fallback = False  # the retarget onto the
                                      # configured source never
                                      # journals
        self.pinned_dead_tracking = False  # the peer asserts tracking
                                           # while pinned on the dead
                                           # source — the doctored
                                           # negative the issue names
        self.fallback_never_tracks = False  # the retargeted pulls
                                            # never reconverge
        self.restarted = False        # a run boundary opens across
                                      # the fallback — the
                                      # reconvergence cost a restart
        self.owner_slips = False      # the restored owner slips off
                                      # the field again
        self.starve = False           # the peer's monitor answers
                                      # nothing through the fallback
                                      # window
        self.claim_free_after_kill = False  # the field claim drops
                                            # with the killed
                                            # successor — the armed
                                            # failover can fire

    # ---- the served surface --------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _journal(self, peer, kind, body):
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick[peer],
                 'event': {kind: body}}
        self.served[peer].append(entry)
        path = {'a': self.journal_a, 'b': self.journal_b,
                'd': self.journal_d}[peer]
        with path.open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _boundary(self, peer, run):
        path = {'a': self.journal_a, 'b': self.journal_b,
                'd': self.journal_d}[peer]
        with path.open('a') as handle:
            handle.write(json.dumps(
                {'run_boundary': {'run': run,
                                  'tick': self.tick[peer]}}) + '\n')

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] != 'active':
            sync = self.sync[peer]
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.aligned[peer]}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {
                    'detail': 'checkpoint pull produced nothing'}}
            else:
                report['sync'] = 'unsynchronized'
            report['failover'] = {
                'converged': self.converged[peer],
                'misses': self.misses[peer],
                'budget': self.budget}
            report['field_claim'] = 'held' \
                if self.claim['owner'] == peer else None
        return report

    # ---- the tracking model ---------------------------------------

    def _pace(self, peer):
        """The peer's autonomous scan boundary — role transitions
        settle and the run's own tick advances."""
        self.tick[peer] += 1
        if self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
        elif self.role[peer] == 'promoting':
            self.role[peer] = 'active'

    def _live(self, peer):
        return self.up[peer] and not self.paused[peer]

    def _target(self, peer):
        """The pull's current source: a learned pin outranks the
        configured --standby source while it stands."""
        return self.resolved[peer] or self.adopted[peer] \
            or self.source[peer]

    def _resolve(self, peer):
        """The tracking-source resolution the release and the orphan
        apply both run: the claimed live owner first, then the
        tracked target, then the announced hints — the first live
        peer serving the line as its owner wins the pin, journaled
        by name."""
        if self.pin_never_lands:
            return
        candidates = []
        owner = self.claim.get('owner')
        if owner:
            candidates.append(owner)
        tracked = self._target(peer)
        if tracked:
            candidates.append(tracked)
        candidates.extend(self.announced[peer])
        for cand in candidates:
            if cand == peer or not self._live(cand) \
                    or self.role[cand] != 'active':
                continue
            self.resolved[peer] = cand
            if not (self.silent_fallback and cand == 'a'
                    and self.killed):
                self._journal(peer, 'tracking_source_adopted',
                              {'source': self.ADDRS[cand]})
            if self.restarted and peer == 'b' and cand == 'a' \
                    and self.killed:
                self._boundary('b', 2)
            return
        self.resolved[peer] = None

    def _failover(self, peer):
        """The armed heartbeat gate: the miss budget reached with the
        convergence proof standing and the field claim free to take
        promotes the standby in place."""
        if self.misses[peer] > self.budget:
            self.converged[peer] = False
        held = self.claim and not self.claim['yielded'] \
            and self.claim['owner'] != peer \
            and self._live(self.claim['owner'])
        if self.role[peer] == 'standby' \
                and self.misses[peer] >= self.budget \
                and (self.converged[peer]
                     or self.misses[peer] == self.budget) \
                and not held \
                and not (self.owner_slips and peer == 'a'):
            self.role[peer] = 'promoting'
            self.claim = {'owner': peer, 'yielded': False}

    def _missed(self, peer):
        """A produced-nothing pull: the in-flight miss counts toward
        the failover budget; the standing verdict rides it out —
        orphaned and diverged stand, everything else degrades. The
        same miss counts against a learned pin; PIN_BUDGET
        consecutive misses release it and re-run the resolution in
        the same scan."""
        self.misses[peer] += 1
        target = self._target(peer)
        if self.pinned_dead_tracking and peer == 'b' and self.killed:
            self.sync[peer] = 'tracking'
        elif self.sync[peer] not in ('orphaned', 'diverged'):
            self.sync[peer] = 'degraded'
        pinned = target is not None and (
            target == self.resolved[peer]
            or target == self.adopted[peer])
        if pinned:
            self.pin_miss[peer] += 1
            release = not (self.no_release or self.pinned_dead_tracking)
            if self.pin_miss[peer] >= self.PIN_BUDGET and release:
                self.pin_miss[peer] = 0
                if self.resolved[peer] == target:
                    self.resolved[peer] = None
                if self.adopted[peer] == target:
                    self.adopted[peer] = None
                self._resolve(peer)
        self._failover(peer)

    def _pull(self, peer):
        """One tracking pull of the peer's current source: announce
        on the source, apply its served verdict — owner
        checkpoints converge tracking, ownerless ones count the
        miss, journal the orphan transition once, and re-run the
        source probe — then the armed failover gate reads the miss
        run."""
        target = self._target(peer)
        if target is None:
            return
        if self.no_tracking and peer == 'b':
            self.sync[peer] = 'unsynchronized'
            return
        if not self._live(target):
            self._missed(peer)
            return
        ann = self.announced[target]
        if peer in ann:
            ann.remove(peer)
        ann.insert(0, peer)
        # The source's own scan boundary paces on the pull — its
        # served document advances while it runs.
        self._pace(target)
        self.pin_miss[peer] = 0
        doc_tick = self.tick[target]
        if self.role[target] == 'active':
            self.misses[peer] = 0
            self.converged[peer] = True
            if self.driven_never_tracks and peer == 'd':
                self.sync[peer] = 'unsynchronized'
            elif self.fallback_never_tracks and peer == 'b' \
                    and self.killed and self.resolved[peer] == 'a':
                self.sync[peer] = 'degraded'
            else:
                self.sync[peer] = 'tracking'
            self.aligned[peer] = doc_tick
        elif self.never_orphaned:
            self.sync[peer] = 'tracking'
            self.aligned[peer] = doc_tick
        else:
            # The ownerless apply: no live field owner is proven, so
            # the heartbeat counts it as the miss it is — and the
            # orphan-resolution probe re-runs the source search.
            self.misses[peer] += 1
            self.converged[peer] = True
            transition = self.sync[peer] != 'orphaned'
            if transition:
                self._journal(peer, 'field_orphaned',
                              {'aligned': doc_tick})
            self.sync[peer] = 'orphaned'
            self.aligned[peer] = doc_tick
            self._resolve(peer)
        if self.owner_slips and peer == 'b' and self.killed \
                and self.sync[peer] == 'tracking' \
                and self.resolved[peer] == 'a':
            # The restored owner slips off the field again — the
            # launch roles never settle back.
            self.role['a'] = 'standby'
        self._failover(peer)

    def _scan(self, peer):
        """One completed scan: role transitions settle at the
        boundary and a standby pulls its tracked source."""
        if not self.up[peer] or self.paused[peer]:
            return
        self._pace(peer)
        if self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the runner-owned lifecycle actions ------------------------

    def start_driven(self, name):
        """The runner's third-controller launch: a fresh standby
        tracking the named peer's field owner."""
        if name != 'active':
            raise RuntimeError('driven launch needs the field owner')
        self.driven_started = True
        self.up['d'] = True
        self.role['d'] = 'standby'
        self.sync['d'] = 'unsynchronized'
        self.source['d'] = 'a'
        return {'container': 'probe-d', 'port': 8082}

    def stop_driven(self):
        """The runner's container removal: the pinned successor
        answers nothing from here on."""
        self.up['d'] = False
        self.killed = True
        if self.starve:
            self.starving = True
        if self.claim_free_after_kill:
            self.claim = {'owner': 'a', 'yielded': True}

    def pause(self, name):
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = True

    def unpause(self, name):
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = False

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        if peer == 'a':
            if self.demote_refused:
                self._raise(409, 'no_tracking_source')
            # The announced-source verify: the newest live hint is
            # the provisional adoption the demotion owes.
            adopted = next((hint for hint in self.announced['a']
                            if self._live(hint)), None)
            if adopted is None:
                self._raise(409, 'no_tracking_source')
            self._journal('a', 'tracking_source_adopted',
                          {'source': self.ADDRS[adopted]})
            self.adopted['a'] = adopted
        else:
            # A configured --standby source covers the demotion — no
            # verify, no adoption.
            self.source[peer] = 'a'
        self._journal(peer, 'role_changed',
                      {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        self.sync[peer] = 'unsynchronized'
        self.aligned[peer] = None
        self.misses[peer] = 0
        self.converged[peer] = False
        self.resolved[peer] = None
        # The keep-claim release: the claim stands, marked yielded.
        self.claim = {'owner': peer, 'yielded': True}
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.role[peer] != 'standby' \
                or self.sync[peer] not in ('tracking', 'orphaned',
                                           'reinitialized'):
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        if peer == 'a' and self.promote_refused:
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer]}})
        if peer == 'd' and self.driven_promote_refused:
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer]}})
        claim_live_other = self.claim \
            and not self.claim['yielded'] \
            and self.claim['owner'] != peer \
            and self._live(self.claim['owner'])
        if self.sync[peer] == 'orphaned' and claim_live_other:
            self._raise(409, {'field_claim_failed': {
                'detail': 'writer claim held by '
                          + self.ADDRS.get(self.claim['owner'],
                                           self.claim['owner'])}})
        self.claim = {'owner': peer, 'yielded': False}
        self.role[peer] = 'promoting'
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch -------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer] or self.paused[peer]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if query else 0
            self._scan(peer)
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/role'):
            if self.starving and peer == 'b':
                raise urllib.error.URLError('monitor starved')
            self._scan(peer)
            return 200, self._report(peer)
        if (method, route) == ('GET', '/checkpoint'):
            self._scan(peer)
            if self.stamps_absent:
                return 200, {'tick': self.tick[peer]}
            return 200, {
                'source_owns_field': self.role[peer] == 'active',
                'line_owner': self.ADDRS[self.claim['owner']],
                'tick': self.tick[peer]}
        if (method, route) == ('POST', '/demote'):
            self._scan(peer)
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            self._scan(peer)
            return self._promote(peer)
        if (method, route) == ('POST', '/scan'):
            scans = int((body or {}).get('scans') or 1)
            for _ in range(scans):
                self._scan(peer)
            return 200, {'scans': scans}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class TrackingFallbackTests(unittest.TestCase):
    """The tracking-source-fallback leg against the stubbed keyed
    pair: a clean rig passes with identical digests — the driven
    peer converging, the island demote opening the orphaned window,
    the successor's claim pinning the standby through the journaled
    tracking_source_adopted naming :8082, the frozen kill window,
    the produced-nothing misses releasing the pin inside the
    budget, the named retarget onto the configured :8080 endpoint,
    the reconverged tracking verdict, no run boundary, and the
    restored launch roles — each doctored contract breach reports
    source-fallback-failed, each instability reports
    source-fallback-nondeterministic, and an unreachable,
    unconverged, unkeyed, pre-contract, or seam-less run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = TrackingFallbackFeed(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'journal_files': {
                   'active': str(feed.journal_a),
                   'standby': str(feed.journal_b),
                   'driven': str(feed.journal_d)},
               'start_driven': feed.start_driven,
               'stop_driven': feed.stop_driven,
               'pause_controller': feed.pause,
               'unpause_controller': feed.unpause}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'time', self.clock), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.01), \
                patch.object(scenarios, 'FALLBACK_SETTLE', 2.0), \
                patch.object(scenarios, 'FALLBACK_FORM', 1.0), \
                patch.object(scenarios, 'FALLBACK_POLL', 0.01), \
                patch.object(scenarios, 'DRIVEN_SCANS', 4):
            return scenarios.scenario_tracking_source_fallback(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The fallback leg's window: behind the orphan-episode leg
        # whose keyed island staging it extends, before the failover
        # case whose launch roles it restores.
        self.assertLess(
            order.index(scenarios.scenario_orphan_episode_bound),
            order.index(scenarios.scenario_tracking_source_fallback))
        self.assertLess(
            order.index(scenarios.scenario_tracking_source_fallback),
            order.index(scenarios.scenario_failover))
        self.assertIs(
            verify.case_function('tracking-source-fallback'),
            scenarios.scenario_tracking_source_fallback)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = []
        for name in ('tracking-source-fallback-pass-1.json',
                     'tracking-source-fallback-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            passes.append(json.loads(path.read_text()))
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'pinned': 'successor', 'fallback': 'journaled',
             'reconverged': 'tracking', 'restart': 'none',
             'roles': 'restored'})
        first = passes[0]['record']
        # The learned pin journaled an adoption naming the driven
        # successor's :8082 listen port, ahead of the frozen kill.
        pinned = [event for event in first['pinned']['journal']]
        self.assertTrue(
            all(str(event.get('source')).endswith(':8082')
                for event in pinned), pinned)
        # The fallback journaled an adoption naming the configured
        # endpoint's :8080 listen port — the named retarget evidence.
        fallback = first['fallback_journal']
        self.assertTrue(
            all(str(event.get('source')).endswith(':8080')
                for event in fallback), fallback)
        self.assertEqual(
            first['reconverged']['role'], 'standby')
        self.assertIn('tracking',
                      first['reconverged']['sync'])
        self.assertEqual(first['boundaries'],
                         {'before': 1, 'after': 1})
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_pinned_dead_verdict_fails(self):
        # The doctored negative the issue names first: the peer
        # asserted as reconverged while the journal shows the pin
        # never retargeted off the dead source.
        self.feed.pinned_dead_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        self.assertIn('pinned', record['detail'])
        report.validate_scenario(record)

    def test_silent_fallback_fails(self):
        # The retarget onto the configured source happens but never
        # journals its named evidence.
        self.feed.silent_fallback = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        report.validate_scenario(record)

    def test_dead_pin_strands_fails(self):
        # The produced-nothing misses never release the learned pin —
        # the peer strands on the dead source.
        self.feed.no_release = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        self.assertIn('stranded', record['detail'])
        report.validate_scenario(record)

    def test_reconverge_never_fails(self):
        # The named retarget journals but the tracking verdict never
        # reconverges.
        self.feed.fallback_never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_restart_needed_fails(self):
        # A run boundary opens across the fallback — the
        # reconvergence cost the restart the contract forbids.
        self.feed.restarted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        self.assertIn('run boundary', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.owner_slips = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-failed'), record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_driven_never_tracks_reports_nondeterministic(self):
        self.feed.driven_never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('driven', record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_island_never_forms_reports_nondeterministic(self):
        self.feed.never_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('island', record['detail'])
        report.validate_scenario(record)

    def test_driven_promote_refused_reports_nondeterministic(self):
        self.feed.driven_promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('driven', record['detail'])
        report.validate_scenario(record)

    def test_pin_never_lands_reports_nondeterministic(self):
        self.feed.pin_never_lands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('pin', record['detail'])
        report.validate_scenario(record)

    def test_promote_refused_reports_nondeterministic(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_starved_watch_reports_nondeterministic(self):
        self.feed.starve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_failover_fired_reports_nondeterministic(self):
        # The armed failover's miss budget sits inside the release
        # budget and the field claim drops with the killed successor:
        # the peer promotes out from under the leg.
        self.feed.budget = 3
        self.feed.claim_free_after_kill = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('promoted out', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-nondeterministic'), record['detail'])
        self.assertIn('digests', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_fallback',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-fallback-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_swapped_layout_reports_inconclusive(self):
        # ctrl-b already owns the field and ctrl-a never converged:
        # the unconfigured peer the island induction needs cannot be
        # restored onto it.
        self.feed.swapped = True
        self.feed.role = {'a': 'standby', 'b': 'active', 'd': 'standby'}
        self.feed.sync = {'a': 'unsynchronized', 'b': None,
                          'd': 'unsynchronized'}
        self.feed.source = {'a': None, 'b': 'a', 'd': 'a'}
        self.feed.claim = {'owner': 'b', 'yielded': False}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_run_reports_inconclusive(self):
        self.feed.pair_token = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pair-token', record['detail'])
        report.validate_scenario(record)

    def test_unkeyed_deployed_pair_runs_on_the_probe_pair(self):
        # The deployed pair carries no --pair-token; the lane-staged
        # probe pair the ctx['probe'] subject names is keyed — the
        # leg exercises the contract on it and reports a real
        # verdict instead of a capability skip (#1058).
        record = self.run_scenario(pair_token=None,
                                   probe=self.ctx())
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertIn('probe pair',
                      ' '.join(record['observations']))
        report.validate_scenario(record)

    def test_missing_lifecycle_action_reports_inconclusive(self):
        record = self.run_scenario(start_driven=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start_driven', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_predating_rig_reports_inconclusive(self):
        # A run predating the contract serves checkpoints without the
        # ownership stamps the pin machinery reads.
        self.feed.stamps_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: the fake clock fixes
        # every wait's iteration count, so two whole runs emit
        # identical reports and identical evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = TrackingFallbackFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
