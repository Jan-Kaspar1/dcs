"""The 2180_track_source_rediscovery leg's scenario unit coverage —
the feed fake and TestCase classes for
scenario_track_source_rediscovery, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


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


class RediscoveryFeed:
    """A stubbed deployed pair for the address-move leg: ctrl-a owns
    the field and answers the configured `--standby
    dcs-hw-qa-a:8080` name the tracking ctrl-b resolves; the
    `move_controller_address` ctx action reproduces the finding's
    staging — ctrl-a's container removed, its freed bridge address
    held by a placeholder, the launch recreated onto a NEW bridge
    address while the configured name resolves onward.

    The tracking half models the #1202 contract: on a conformant
    run each produced-nothing pull against the configured NAME
    degrades with the pull's own 'fetch from <name>' detail — the
    fallback journaled by name — and the pull after the recreated
    source starts serving reconverges tracking and journals the
    tracking_source_adopted carrying the new address. The
    `pre_contract` flag stages the defect #1202 removed: the peer
    pins the startup-resolved address for the process lifetime, so
    every miss names the stale IP — the shape the leg reports
    inconclusive — and, armed, lets the failover gate fire on the
    stale evidence. Every endpoint call on a peer is one completed
    scan; a pull paces its source's warm-up. Doctor flags stage
    each named defect the issue calls out."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    OLD_ADDR = '172.22.0.3'
    NEW_ADDR = '172.22.0.6'
    CONTAINER = 'dcs-hw-qa-a'
    NAME = 'dcs-hw-qa-a:8080'
    WARM = 2          # pulls before the recreated source serves

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.addr = {'a': self.OLD_ADDR}
        self.warming = 0          # pulls until the recreated a serves
        self.role = {'a': 'active', 'b': 'standby'}
        self.sync = {'b': 'tracking'}
        self.detail = None
        self.aligned = 100
        self.misses = 0
        self.moved = 0
        self.placeholder_held = None
        self.placeholder_released = True
        self.adopted_addr = self.OLD_ADDR
        self.seq = {'a': 0, 'b': 0}
        self.pair_token = 'deployed-pair'
        self.journal_a = self.tmp / 'journal-a.jsonl'
        self.journal_b = self.tmp / 'journal-b.jsonl'
        for path in (self.journal_a, self.journal_b):
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False    # the monitors never answer
        self.no_tracking = False    # the standby never converges
        self.swapped = False        # ctrl-b already owns the field
        self.stamps_absent = False  # the served checkpoint drops the
                                    # ownership stamps — a run
                                    # predating the substrate
        self.move_error = False     # the address-move lever refuses
        self.same_address = False   # the recreate lands on the same
                                    # address — no staged change
        self.pre_contract = False   # the peer pins the
                                    # startup-resolved address for
                                    # its lifetime — the staged run
                                    # predates the #1202 contract
        self.pinned_reconverge = False  # the pinned peer asserts a
                                        # tracking verdict anyway —
                                        # the doctored negative the
                                        # issue names
        self.failover_at = None     # the miss count the armed gate
                                    # fires at — the phantom
                                    # promotion on stale evidence
        self.armed_refused = False  # the gate reaches its budget and
                                    # journals promotion_refused
        self.silent_byname = False  # the degraded details name a
                                    # foreign endpoint, never the
                                    # configured name
        self.no_degraded = False    # the window reports
                                    # unsynchronized, never a
                                    # degraded detail to audit
        self.no_reconverge = False  # the recreated source never
                                    # serves — the peer strands
        self.peer_restart = False   # a run boundary opens across the
                                    # rediscovery — the reconvergence
                                    # cost a restart
        self.no_source_boundary = False  # the recreate journals no
                                         # new lifetime
        self.starved = False        # the peer's monitor answers
                                    # nothing across the window
        self.owner_slips = False    # the restored owner slips off
                                    # the field again
        self._refused_journaled = False

    # ---- the served surface --------------------------------------

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _journal(self, peer, kind, body):
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick[peer],
                 'event': {kind: body}}
        path = {'a': self.journal_a, 'b': self.journal_b}[peer]
        with path.open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _boundary(self, peer, run):
        path = {'a': self.journal_a, 'b': self.journal_b}[peer]
        with path.open('a') as handle:
            handle.write(json.dumps(
                {'run_boundary': {'run': run,
                                  'tick': self.tick[peer]}}) + '\n')

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] == 'standby':
            sync = self.sync[peer]
            if sync == 'tracking':
                report['sync'] = {'tracking': {'aligned': self.aligned}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {'detail': self.detail}}
            else:
                report['sync'] = 'unsynchronized'
        return report

    # ---- the tracking model ---------------------------------------

    def _live(self, peer):
        return self.up[peer]

    def _missed(self):
        """A produced-nothing pull: the standing verdict degrades and
        the in-flight miss counts toward the armed failover gate —
        which, staged, fires the phantom promotion or journals the
        incumbent's refusal."""
        self.misses += 1
        if self.armed_refused and not self._refused_journaled \
                and self.misses >= 2:
            self._refused_journaled = True
            self._journal('b', 'promotion_refused',
                          {'detail': 'the incumbent still holds '
                           'the claim'})
        if self.no_degraded:
            self.sync['b'] = 'unsynchronized'
            return
        self.sync['b'] = 'degraded'
        if self.pre_contract:
            self.detail = ('fetch from ' + self.OLD_ADDR
                           + ':8080: checkpoint pull produced '
                           'nothing')
        elif self.silent_byname:
            self.detail = ('fetch from 172.22.0.9:8080: checkpoint '
                           'pull produced nothing')
        else:
            self.detail = ('fetch from ' + self.NAME
                           + ': checkpoint pull produced nothing')
        if self.failover_at is not None \
                and self.misses >= self.failover_at:
            if self.role['b'] == 'standby':
                self.role['b'] = 'promoting'
                self._journal('b', 'role_changed',
                              {'from': 'standby', 'to': 'promoting'})
                if self.up['a']:
                    self._journal('a', 'field_claim_lost',
                                  {'detail': 'the armed peer '
                                   'preempted the claim'})

    def _pull(self):
        """One tracking pull on the configured source name: on a
        conformant run the name re-resolves every pull — producing
        nothing while the source is absent or warming, reconverging
        onto the new address once it serves; on the staged
        pre-contract run the startup-resolved pin produces nothing
        forever."""
        if self.pre_contract and self.moved:
            # The startup-resolved pin was the source's own address —
            # pulls produced until the address changed; from the move
            # on they produce nothing forever.
            self._missed()
            if self.pinned_reconverge and self.misses >= 3:
                # The doctored negative: the peer asserts a
                # reconverged tracking verdict while its pull
                # evidence stays pinned on the stale address.
                self.sync['b'] = 'tracking'
            return
        if self.up['a'] and self.warming == 0:
            self.misses = 0
            if self.adopted_addr != self.addr['a']:
                self.adopted_addr = self.addr['a']
                self._journal('b', 'tracking_source_adopted',
                              {'source': self.addr['a'] + ':8080'})
                if self.peer_restart:
                    self._boundary('b', 2)
            self.sync['b'] = 'tracking'
            self.aligned = self.tick['a']
            if self.owner_slips and self.moved:
                self.role['a'] = 'standby'
        else:
            if self.warming > 0:
                self.warming -= 1
                if self.warming == 0 and not self.no_reconverge:
                    self.up['a'] = True
            self._missed()

    def _scan(self, peer):
        """One completed scan: role transitions settle at the
        boundary and a standby pulls its tracked source."""
        if not self.up[peer]:
            return
        self.tick[peer] += 1
        if self.role[peer] == 'promoting':
            self.role[peer] = 'active'
        elif self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
            if peer == 'b':
                self.sync['b'] = 'unsynchronized'
        if peer == 'b' and self.role[peer] == 'standby':
            if self.no_tracking:
                self.sync['b'] = 'unsynchronized'
            else:
                self._pull()

    # ---- the runner-owned lifecycle actions ------------------------

    def move_controller_address(self, name):
        """The ctx lever: remove the named member's container, hold
        its freed bridge address on a placeholder, and recreate the
        launch so the configured name resolves to a NEW address."""
        if name != 'active':
            raise RuntimeError('the move stages the tracking source')
        if self.move_error:
            raise RuntimeError('docker rm -f failed')
        self.moved += 1
        old = self.addr['a']
        self.up['a'] = False
        self.placeholder_held = old
        self.placeholder_released = False
        if not self.same_address:
            # The placeholder holds the freed address — IPAM hands
            # the recreate a different one each move.
            self.addr['a'] = self.OLD_ADDR if old == self.NEW_ADDR \
                else self.NEW_ADDR
        self.warming = self.WARM
        if not self.no_source_boundary:
            self._boundary('a', self.moved + 1)
        return {'container': self.CONTAINER,
                'placeholder': 'dcs-hw-qa-placeholder',
                'old_address': old, 'new_address': self.addr['a']}

    def release_address_placeholder(self):
        self.placeholder_held = None
        self.placeholder_released = True

    def relaunch_controller(self, name, track=None):
        if name == 'active':
            self.up['a'] = True
            self.warming = 0

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        if self.swapped:
            self._raise(409, 'not_active')
        self._journal(peer, 'role_changed',
                      {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.sync.get('b') != 'tracking' and peer == 'b':
            self._raise(409, {'not_converged': {
                'sync': self.sync.get('b') or 'unsynchronized'}})
        self.role[peer] = 'promoting'
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    # ---- the endpoint dispatch -------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self._live(peer) or (peer == 'a' and self.warming > 0):
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            if self.starved and peer == 'b' and self.moved:
                raise urllib.error.URLError('monitor starved')
            self._scan(peer)
            return 200, self._report(peer)
        if (method, route) == ('GET', '/checkpoint'):
            self._scan(peer)
            if self.stamps_absent:
                return 200, {'tick': self.tick[peer]}
            return 200, {
                'source_owns_field': self.role[peer] == 'active',
                'line_owner': self.addr['a'] + ':8080',
                'tick': self.tick[peer]}
        if (method, route) == ('POST', '/demote'):
            self._scan(peer)
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            self._scan(peer)
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class TrackSourceRediscoveryTests(unittest.TestCase):
    """The track-source-rediscovery leg against the stubbed deployed
    pair: a clean rig passes with identical digests — the address
    move landing the source on a new bridge address, the miss
    window's degraded evidence naming the configured source by
    name, the reconverged tracking verdict inside one process
    lifetime, the new run boundary on the source alone, the armed
    gate silent, and the restored launch roles — each doctored
    contract breach reports track-source-rediscovery-failed, each
    instability reports track-source-rediscovery-nondeterministic,
    and an unreachable, unconverged, seam-less, same-address, or
    pre-contract run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RediscoveryFeed(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'pair_token': feed.pair_token,
               'failover_misses': 30,
               'journal_files': {
                   'active': str(feed.journal_a),
                   'standby': str(feed.journal_b)},
               'move_controller_address':
                   feed.move_controller_address,
               'release_address_placeholder':
                   feed.release_address_placeholder,
               'relaunch_controller': feed.relaunch_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'time', self.clock), \
                patch.object(scenarios, 'REDISC_SETTLE', 10.0), \
                patch.object(scenarios, 'REDISC_POLL', 0.4):
            return scenarios.scenario_track_source_rediscovery(
                self.ctx(feed, **overrides))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The rediscovery leg's window: after the tracking-source
        # fallback leg whose pin contract it extends, before the
        # failover case whose launch roles it restores.
        self.assertLess(
            order.index(scenarios.scenario_tracking_source_fallback),
            order.index(scenarios.scenario_track_source_rediscovery))
        self.assertLess(
            order.index(scenarios.scenario_track_source_rediscovery),
            order.index(scenarios.scenario_failover))
        self.assertIs(
            verify.case_function('track-source-rediscovery'),
            scenarios.scenario_track_source_rediscovery)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = []
        for name in ('track-source-rediscovery-pass-1.json',
                     'track-source-rediscovery-pass-2.json'):
            path = self.evidence / name
            self.assertTrue(path.is_file(), name)
            passes.append(json.loads(path.read_text()))
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'moved': 'new-address', 'degraded': 'by-name',
             'reconverged': 'tracking', 'failover': 'none',
             'restart': 'none', 'roles': 'restored'})
        first = passes[0]['record']
        # The move landed the source on a NEW bridge address while
        # the placeholder held the old one.
        self.assertEqual(first['move']['old_address'],
                         RediscoveryFeed.OLD_ADDR)
        self.assertEqual(first['move']['new_address'],
                         RediscoveryFeed.NEW_ADDR)
        self.assertFalse(first['same_address'])
        # The miss window's degraded evidence names the configured
        # source by name — the fallback journaled by name — never
        # the stale startup-resolved address.
        self.assertTrue(first['by_name_evidence'])
        self.assertFalse(first['stale_evidence'])
        self.assertTrue(
            all(RediscoveryFeed.NAME in detail
                for detail in first['degraded']), first['degraded'])
        # The peer reconverged tracking inside one process lifetime;
        # the source's journal opened the new run boundary the
        # recreate owed.
        self.assertEqual(first['reconverged']['role'], 'standby')
        self.assertIn('tracking', first['reconverged']['sync'])
        self.assertEqual(first['boundaries'],
                         {'peer_before': 1, 'peer_after': 1,
                          'source_before': 1, 'source_after': 2})
        self.assertFalse(first['failover']['promoted'])
        self.assertTrue(first['restored'])
        self.assertTrue(self.feed.placeholder_released)
        report.validate_scenario(record)

    def test_pinned_stale_verdict_fails(self):
        # The doctored negative the issue names first: the peer
        # asserted as reconverged while the served pull evidence
        # stayed pinned on the stale address.
        self.feed.pre_contract = True
        self.feed.pinned_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('stale', record['detail'])
        report.validate_scenario(record)

    def test_failover_on_stale_fails(self):
        # The doctored negative the issue names second: the armed
        # peer fires failover against the still-healthy owner on
        # stale-pin evidence, inside its documented budget.
        self.feed.pre_contract = True
        self.feed.failover_at = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('failover', record['detail'])
        report.validate_scenario(record)

    def test_by_name_missing_fails(self):
        # The fallback reconverges but never journals by name — the
        # degraded record names a foreign endpoint.
        self.feed.silent_byname = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('by name', record['detail'])
        report.validate_scenario(record)

    def test_reconverge_never_fails(self):
        # The recreated source never serves — the peer strands
        # degraded on the configured name without reconverging.
        self.feed.no_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_restart_needed_fails(self):
        # A run boundary opens across the rediscovery — the
        # reconvergence cost the restart the contract forbids.
        self.feed.peer_restart = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('run boundary', record['detail'])
        report.validate_scenario(record)

    def test_restore_fails(self):
        self.feed.owner_slips = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-failed'), record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_move_refused_reports_nondeterministic(self):
        self.feed.move_error = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        self.assertIn('move', record['detail'])
        report.validate_scenario(record)

    def test_starved_watch_reports_nondeterministic(self):
        self.feed.starved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_failover_past_budget_reports_nondeterministic(self):
        # The armed gate fires only past its documented miss budget —
        # the move window outlived the failover gate rather than
        # tripping it on stale evidence.
        self.feed.pre_contract = True
        self.feed.failover_at = 12
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_armed_refused_reports_nondeterministic(self):
        self.feed.armed_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_no_degraded_window_reports_nondeterministic(self):
        self.feed.no_degraded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_source_not_restarted_reports_nondeterministic(self):
        self.feed.no_source_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        self.assertIn('boundary', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_digest_rediscovery',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-nondeterministic'),
            record['detail'])
        self.assertIn('digests', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_rediscovery',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'track-source-rediscovery-unchecked'), record['detail'])
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
        # ctrl-b already owns the field and refuses to let it go —
        # the launch layout the passes stage from never lands.
        self.feed.swapped = True
        self.feed.role = {'a': 'standby', 'b': 'active'}
        self.feed.sync['b'] = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_move_lever_reports_inconclusive(self):
        record = self.run_scenario(move_controller_address=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('address-move', record['detail'])
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
        self.feed.stamps_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_pre_contract_strand_reports_inconclusive(self):
        # The staged run predates the contract: the peer stays
        # pinned on the stale startup-resolved address through the
        # whole bound — never reconverged, never fired.
        self.feed.pre_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_same_address_reports_inconclusive(self):
        self.feed.same_address = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('same bridge address', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        first_files = {path.name: path.read_text()
                       for path in self.evidence.glob('*.json')}
        sub = Path(self.tmp.name) / 'other'
        sub.mkdir()
        second_feed = RediscoveryFeed(sub)
        second = self.run_scenario(feed=second_feed)
        second_files = {path.name: path.read_text()
                        for path in self.evidence.glob('*.json')}
        self.assertEqual(first['outcome'], 'passed', first)
        self.assertEqual(second['outcome'], 'passed', second)
        self.assertEqual(first_files, second_files)
