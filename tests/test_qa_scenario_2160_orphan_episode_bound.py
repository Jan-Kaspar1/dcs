"""The 2160_orphan_episode_bound leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_orphan_episode_bound, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'OrphanBoundTests.test_registered',
    'OrphanBoundTests.test_clean_rig_passes_and_validates',
    'OrphanBoundTests.test_journal_flooded_fails',
    'OrphanBoundTests.test_verdict_flickered_fails',
    'OrphanBoundTests.test_misses_stalled_fails',
    'OrphanBoundTests.test_island_never_forms_fails',
    'OrphanBoundTests.test_orphan_journal_absent_fails',
    'OrphanBoundTests.test_episode_unended_fails',
    'OrphanBoundTests.test_second_episode_silent_fails',
    'OrphanBoundTests.test_restore_fails',
    'OrphanBoundTests.test_demote_refused_reports_nondeterministic',
    'OrphanBoundTests.test_silent_adoption_reports_nondeterministic',
    'OrphanBoundTests.test_starved_watch_reports_nondeterministic',
    'OrphanBoundTests.test_failover_fired_reports_nondeterministic',
    'OrphanBoundTests.test_served_read_dropped_reports_'
    'nondeterministic',
    'OrphanBoundTests.test_pulls_never_landed_reports_'
    'nondeterministic',
    'OrphanBoundTests.test_redemote_refused_reports_'
    'nondeterministic',
    'OrphanBoundTests.test_diverging_digests_report_nondeterministic',
    'OrphanBoundTests.test_silent_judge_reports_unchecked',
    'OrphanBoundTests.test_unreachable_pair_reports_inconclusive',
    'OrphanBoundTests.test_unconverged_pair_reports_inconclusive',
    'OrphanBoundTests.test_swapped_layout_reports_inconclusive',
    'OrphanBoundTests.test_unkeyed_run_reports_inconclusive',
    'OrphanBoundTests.test_unkeyed_deployed_pair_runs_on_the_'
    'probe_pair',
    'OrphanBoundTests.test_missing_pause_action_reports_'
    'inconclusive',
    'OrphanBoundTests.test_missing_journal_files_reports_'
    'inconclusive',
    'OrphanBoundTests.test_single_endpoint_reports_inconclusive',
    'OrphanBoundTests.test_two_runs_produce_identical_evidence',
})


class FakeClock:
    """The scenario's `time` module swapped for a deterministic
    clock: every `sleep` advances `now` by exactly its argument, so
    the held-window watch collects a fixed row count per phase and
    two whole runs emit byte-identical evidence."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


class OrphanBoundFeed:
    """A stubbed keyed pair for the orphan-episode leg: ctrl-a owns
    the field and configured no tracking source — its demotion
    verifies the announced hints and adopts the newest, the sibling
    standby — and ctrl-b tracks a through its configured --standby
    pull. The pair is armed for failover: each standby's /role
    carries the proof, the consecutive-miss count, and the budget.

    The tracking half models the #1041 contract the leg pins: a
    completed pull on an ownerless source applies the held document
    and counts as one heartbeat miss — no live field owner is
    proven — while a produced-nothing pull (the source frozen past
    the fetch timeout) counts the in-flight miss and leaves the
    standing verdict untouched: an orphaned peer rides the miss out
    orphaned, journaled once per episode. The `pause_controller`/
    `unpause_controller` ctx actions freeze and thaw ctrl-a: a
    paused peer's monitor accepts nothing and a tracking pull on it
    produces nothing. Every endpoint call on a peer is one
    completed scan; a pull paces its source's scan boundary, so the
    pulled document's tick advances while the source runs. Doctor
    flags stage each named defect the issue calls out."""

    ADDRS = {'a': 'probe-a:8080', 'b': 'probe-b:8081',
             'f': 'probe-f:8090'}
    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    BUDGET = 120

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.paused = {'a': False, 'b': False}
        self.role = {'a': 'active', 'b': 'standby'}
        # The checkpoint source each standby pulls — ctrl-a launched
        # with none; its demotion owes the announced adoption.
        self.source = {'a': None, 'b': 'a'}
        self.sync = {'a': None, 'b': 'tracking'}
        self.aligned = {'a': None, 'b': 100}
        self.misses = {'a': 0, 'b': 0}
        self.converged = {'a': False, 'b': True}
        self.orphans = {'a': 0, 'b': 0}
        self.announced = {'a': ['b'], 'b': []}
        self.claim = {'owner': 'a', 'yielded': False}
        # The frozen-pulls doctor's pinned document tick — the served
        # checkpoint never advances once the island is staged.
        self.frozen_at = 100
        # The armed failover budget — writable for the low-budget
        # doctor that parks it inside the held window.
        self.budget = self.BUDGET
        self.seq = {'a': 0, 'b': 0}
        self.served = {'a': [], 'b': []}
        self.pair_token = 'probe-pair'
        self.a_demotes = 0
        self.a_promotes = 0
        self.starving = False
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        self.journal_a.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        self.journal_b.write_text(json.dumps(
            {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_tracking = False      # the standby never converges
        self.swapped = False          # ctrl-b already owns the field
        self.demote_refused = False   # the island-inducing demote
                                      # answers no_tracking_source
        self.silent_adoption = False  # the adoption never journals
        self.foreign_adoption = False  # the journaled adoption names
                                       # a forged endpoint, not the
                                       # sibling's :8081
        self.never_orphaned = False   # peers keep reporting tracking
                                      # on an ownerless line
        self.orphan_silent = False    # the orphaned verdict never
                                      # journals
        self.journal_flood = False    # every ownerless apply
                                      # re-journals the episode — the
                                      # pre-fix flood
        self.flicker = False          # produced-nothing misses drop
                                      # the standing verdict to
                                      # degraded — the pre-fix
                                      # flicker
        self.stall_misses = False     # produced-nothing pulls never
                                      # reach the miss accounting
        self.frozen_pulls = False     # the thawed source's document
                                      # never advances — no pull
                                      # lands observably
        self.starve = False           # the peer's monitor answers
                                      # nothing through the held
                                      # window
        self.served_drops = False     # the served /journal read drops
                                      # once the episode opened
        self.promote_refused = False  # the episode-ending promote is
                                      # refused
        self.redemote_refused = False  # the episode-reopening demote
                                       # is refused
        self.stick_orphaned = False   # the peer keeps reporting
                                      # orphaned on a live owner —
                                      # the episode never ends
        self.no_second = False        # the second episode's entry
                                      # never journals
        self.restore_refused = False  # the launch-role restore's
                                      # promote is refused

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
        path = self.journal_a if peer == 'a' else self.journal_b
        with path.open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')
        if kind == 'field_orphaned':
            self.orphans[peer] += 1
            if peer == 'b' and self.starve:
                # The starved-watch doctor: the peer's monitor goes
                # silent once the held episode opens.
                self.starving = True

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] != 'active':
            sync = self.sync[peer]
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.aligned[peer]}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {
                    'detail': 'checkpoint pull still in flight'}}
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

    def _missed(self, peer):
        """A produced-nothing pull: the in-flight miss counts toward
        the budget; the standing verdict rides it out — orphaned
        and diverged stand, everything else degrades."""
        if not self.stall_misses:
            self.misses[peer] += 1
        if self.sync[peer] == 'orphaned':
            if self.flicker:
                self.sync[peer] = 'degraded'
        elif self.sync[peer] != 'diverged':
            self.sync[peer] = 'degraded'

    def _pull(self, peer):
        """One tracking pull of the peer's current source: announce
        on the source, apply its served verdict — ownerless
        documents count a heartbeat miss, journal the orphan
        transition once per episode, and leave the verdict orphaned
        — then the armed failover gate reads the miss run."""
        source = self.source[peer]
        if source is None:
            return
        if self.no_tracking and peer == 'b':
            self.sync[peer] = 'unsynchronized'
            return
        if not self.up[source] or self.paused[source]:
            self._missed(peer)
        else:
            ann = self.announced[source]
            if peer in ann:
                ann.remove(peer)
            ann.insert(0, peer)
            # The source's own scan boundary paces on the pull — its
            # served document advances while it runs.
            self._pace(source)
            owns = self.role[source] == 'active'
            if self.stick_orphaned and peer == 'b' \
                    and self.orphans['b'] >= 1:
                owns = False
            if self.frozen_pulls and source == 'a':
                doc_tick = self.frozen_at
            else:
                doc_tick = self.tick[source]
            if owns:
                self.misses[peer] = 0
                self.converged[peer] = True
                self.sync[peer] = 'tracking'
                self.aligned[peer] = doc_tick
            elif self.never_orphaned:
                self.sync[peer] = 'tracking'
                self.aligned[peer] = doc_tick
            else:
                # The ownerless apply: no live field owner is
                # proven, so the heartbeat counts it as the miss it
                # is rather than resetting — but the landed apply
                # still re-proves the run's convergence.
                self.misses[peer] += 1
                self.converged[peer] = True
                transition = self.sync[peer] != 'orphaned'
                journals = transition or self.journal_flood
                if transition and self.no_second \
                        and self.orphans[peer] >= 1:
                    journals = self.journal_flood
                if journals and not self.orphan_silent:
                    self._journal(peer, 'field_orphaned',
                                  {'aligned': doc_tick})
                self.sync[peer] = 'orphaned'
                self.aligned[peer] = doc_tick
        if self.misses[peer] > self.budget:
            self.converged[peer] = False
        held = self.claim and not self.claim['yielded'] \
            and self.claim['owner'] != peer
        if self.role[peer] == 'standby' \
                and self.misses[peer] >= self.budget \
                and (self.converged[peer]
                     or self.misses[peer] == self.budget) \
                and not held \
                and not (self.restore_refused and peer == 'a'):
            # The armed failover gate fires at the miss boundary —
            # the conditional orphan claim lands over the yielded
            # claim the island left.
            self.role[peer] = 'promoting'
            self.claim = {'owner': peer, 'yielded': False}

    def _scan(self, peer):
        """One completed scan: role transitions settle at the
        boundary and a standby pulls its tracked source."""
        self._pace(peer)
        if self.role[peer] == 'standby':
            self._pull(peer)

    # ---- the runner-owned lifecycle actions ------------------------

    def pause(self, name):
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = True

    def unpause(self, name):
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = False

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        if peer == 'a':
            self.a_demotes += 1
            if self.demote_refused \
                    or (self.redemote_refused and self.a_demotes >= 2) \
                    or not self.announced['a']:
                self._raise(409, 'no_tracking_source')
            # The announced-source verify: the newest hint is the
            # sibling standby's — its checkpoint merely replays the
            # demoted line, so it is the provisional adoption.
            adopted = self.announced['a'][0]
            if self.frozen_pulls:
                self.frozen_at = self.tick['a']
            if not self.silent_adoption:
                self._journal('a', 'tracking_source_adopted', {
                    'source': self.ADDRS['f']
                    if self.foreign_adoption else self.ADDRS[adopted]})
            self.source['a'] = adopted
        else:
            # A configured --standby source covers the demotion — no
            # verify, no adoption.
            self.source[peer] = 'a'
        self._journal(peer, 'role_changed',
                      {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        # The tracking session resets on demotion — the adoption
        # starts a fresh heartbeat: no standing sync, no alignment,
        # no miss run, no convergence proof.
        self.sync[peer] = 'unsynchronized'
        self.aligned[peer] = None
        self.misses[peer] = 0
        self.converged[peer] = False
        # The keep-claim release: the claim stands, marked yielded.
        self.claim = {'owner': peer, 'yielded': True}
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.role[peer] != 'standby' \
                or self.sync[peer] in (None, 'unsynchronized'):
            self._raise(409, {'not_converged': {
                'sync': self.sync[peer] or 'unsynchronized'}})
        if peer == 'a':
            self.a_promotes += 1
            if self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': self.sync[peer]}})
            if self.restore_refused and self.a_promotes >= 2:
                self._raise(409, {'not_converged': {
                    'sync': self.sync[peer]}})
            # The starved watch ends when the control plane moves.
            self.starving = False
        claim = self.claim
        if self.sync[peer] == 'orphaned' and claim \
                and not claim['yielded'] \
                and claim['owner'] != peer:
            self._raise(409, {'field_claim_failed': {
                'detail': 'writer claim held by '
                          + self.ADDRS.get(claim['owner'],
                                           claim['owner'])}})
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
            if self.served_drops and peer == 'b' \
                    and self.orphans[peer] >= 1:
                raise urllib.error.URLError('journal read dropped')
            since = int(query.split('=', 1)[1]) if query else 0
            self._scan(peer)
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/role'):
            if self.starving and peer == 'b':
                raise urllib.error.URLError('monitor starved')
            self._scan(peer)
            return 200, self._report(peer)
        if (method, route) == ('POST', '/demote'):
            self._scan(peer)
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            self._scan(peer)
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class OrphanBoundTests(unittest.TestCase):
    """The orphan-episode-bound leg against the stubbed keyed pair:
    a clean rig passes with identical digests — the demote adopting
    the sibling, the held episode's alternating miss/pull window
    serving orphaned on every row with the miss count advancing,
    exactly one journaled field_orphaned per episode on the durable
    file and the served tail, the non-orphaned apply ending the
    episode, and the second episode's own entry — each doctored
    contract breach reports orphan-episode-bound-failed, each
    instability reports orphan-episode-bound-nondeterministic, and
    an unreachable, unconverged, unkeyed, or seam-less run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = OrphanBoundFeed(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'journal_files': {
                   'active': str(feed.journal_a),
                   'standby': str(feed.journal_b)},
               'pause_controller': feed.pause,
               'unpause_controller': feed.unpause}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'time', self.clock), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.01), \
                patch.object(scenarios, 'ORPHAN_SETTLE', 1.0), \
                patch.object(scenarios, 'ORPHAN_FORM', 0.5), \
                patch.object(scenarios, 'ORPHAN_POLL', 0.01), \
                patch.object(scenarios, 'MISS_HOLD', 0.06), \
                patch.object(scenarios, 'PULL_HOLD', 0.04):
            return scenarios.scenario_orphan_episode_bound(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The orphan-episode leg's window: behind the keyed
        # announced-source leg whose island staging it extends,
        # before the failover case whose launch roles it restores.
        self.assertLess(
            order.index(scenarios.scenario_keyed_announced_source),
            order.index(scenarios.scenario_orphan_episode_bound))
        self.assertLess(
            order.index(scenarios.scenario_orphan_episode_bound),
            order.index(scenarios.scenario_failover))
        self.assertIs(verify.case_function('orphan-episode-bound'),
                      scenarios.scenario_orphan_episode_bound)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = []
        for name in ('orphan-episode-bound-pass-1.json',
                     'orphan-episode-bound-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            passes.append(json.loads(path.read_text()))
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'adopted': 'sibling', 'island': 'formed',
             'episode': 'one-entry', 'verdict': 'held',
             'misses': 'advanced', 'pulls': 'landed',
             'ended': 'tracking', 'episode_two': 'second-entry',
             'roles': 'restored'})
        first = passes[0]['record']
        # The demotion journaled one adoption naming the sibling
        # standby's :8081 listen port.
        self.assertEqual(len(first['adoptions']), 1)
        self.assertTrue(
            first['adoptions'][0]['source'].endswith(':8081'),
            first['adoptions'])
        # The held window alternated miss/pull phases; every served
        # row read standby + orphaned with the miss count advancing.
        phases = [phase['phase'] for phase in first['window']]
        self.assertEqual(phases, ['miss', 'pull', 'miss', 'pull'])
        rows = [row for phase in first['window']
                for row in phase['rows']]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row['role'], 'standby', row)
            self.assertEqual(row['sync'], 'orphaned', row)
        misses = [row['misses'] for row in rows]
        self.assertLess(misses[0], misses[-1])
        self.assertEqual(misses, sorted(misses))
        # The bound: one entry for the held episode, one more for
        # the genuinely new episode — on both journal surfaces.
        self.assertEqual(first['durable_orphans'], 1)
        self.assertEqual(first['served_orphans'], 1)
        self.assertEqual(first['durable_orphans_2'], 2)
        self.assertEqual(first['served_orphans_2'], 2)
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_journal_flooded_fails(self):
        # The doctored negative the issue names first: every
        # ownerless apply re-journals the held episode.
        self.feed.journal_flood = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('field_orphaned', record['detail'])
        report.validate_scenario(record)

    def test_verdict_flickered_fails(self):
        # ... and the orphaned verdict asserted as flickered to
        # degraded on the evidence-free cycles.
        self.feed.flicker = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('flickered', record['detail'])
        report.validate_scenario(record)

    def test_misses_stalled_fails(self):
        # The evidence-free cycles never reached the failover
        # accounting — a frozen-source window produced no counted
        # misses.
        self.feed.stall_misses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('miss', record['detail'])
        report.validate_scenario(record)

    def test_island_never_forms_fails(self):
        # The pre-fix wedge: the demoted pair keeps reporting
        # tracking on an ownerless line.
        self.feed.never_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('island', record['detail'])
        report.validate_scenario(record)

    def test_orphan_journal_absent_fails(self):
        self.feed.orphan_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('field_orphaned', record['detail'])
        report.validate_scenario(record)

    def test_episode_unended_fails(self):
        # The non-orphaned apply never ended the episode — the peer
        # keeps reporting orphaned on a live owner.
        self.feed.stick_orphaned = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('ended', record['detail'])
        report.validate_scenario(record)

    def test_second_episode_silent_fails(self):
        # A genuinely new episode owes its own entry — the second
        # island journaled nothing.
        self.feed.no_second = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('two', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.restore_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-failed'), record['detail'])
        self.assertIn('launch', record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_silent_adoption_reports_nondeterministic(self):
        self.feed.silent_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('adopt', record['detail'])
        report.validate_scenario(record)

    def test_starved_watch_reports_nondeterministic(self):
        # The peer's monitor goes silent once the held episode
        # opens — the watch collects no rows to audit.
        self.feed.starve = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('watch', record['detail'])
        report.validate_scenario(record)

    def test_failover_fired_reports_nondeterministic(self):
        # The armed budget sits inside the calibrated window: the
        # failover gate fires mid-episode and the pinned peer
        # promotes out from under it — legal rig behavior, not a
        # verdict breach.
        self.feed.budget = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('failover', record['detail'])
        report.validate_scenario(record)

    def test_served_read_dropped_reports_nondeterministic(self):
        self.feed.served_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('served', record['detail'])
        report.validate_scenario(record)

    def test_pulls_never_landed_reports_nondeterministic(self):
        # The thawed source's document never advances — the
        # freeze/thaw alternation staged misses alone.
        self.feed.frozen_pulls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('pull', record['detail'])
        report.validate_scenario(record)

    def test_redemote_refused_reports_nondeterministic(self):
        self.feed.redemote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('/demote', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_episode',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-episode-bound-unchecked'), record['detail'])
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
        self.feed.role = {'a': 'standby', 'b': 'active'}
        self.feed.sync = {'a': 'unsynchronized', 'b': None}
        self.feed.source = {'a': None, 'b': 'a'}
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

    def test_missing_pause_action_reports_inconclusive(self):
        record = self.run_scenario(pause_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('pause_controller', record['detail'])
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

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: the fake clock fixes
        # every watch's row count, so two whole runs emit identical
        # reports and identical evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = OrphanBoundFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
