"""The 2190_tracker_realign_tick_order leg's scenario unit coverage —
the feed fakes and TestCase classes for
scenario_tracker_realign_tick_order, split out per the #940
convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails
the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'RealignTickOrderTests.test_registered',
    'RealignTickOrderTests.test_clean_rig_passes_and_validates',
    'RealignTickOrderTests.test_regressed_axis_fails',
    'RealignTickOrderTests.test_malformed_axis_entry_fails',
    'RealignTickOrderTests.test_adopted_settle_silent_fails',
    'RealignTickOrderTests.test_adopted_settle_twice_fails',
    'RealignTickOrderTests.test_never_realigns_fails',
    'RealignTickOrderTests.test_roles_unrestored_fails',
    'RealignTickOrderTests.test_never_degraded_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_failover_fires_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_admission_refused_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_settle_unapplied_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_adopted_early_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_window_silent_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_pause_refused_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_watch_starved_reports_'
    'nondeterministic',
    'RealignTickOrderTests.test_diverging_digests_report_'
    'nondeterministic',
    'RealignTickOrderTests.test_silent_judge_reports_unchecked',
    'RealignTickOrderTests.test_unreachable_pair_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_unconverged_pair_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_swapped_layout_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_missing_pause_action_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_missing_journal_files_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_predates_contract_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_single_endpoint_reports_'
    'inconclusive',
    'RealignTickOrderTests.test_two_runs_produce_identical_evidence',
})


class RealignTickOrderFeed:
    """A stubbed pair for the tick-order leg: ctrl-a owns the field
    with no configured tracking source; ctrl-b tracks a through its
    configured --standby pull — one pull per completed scan, each
    endpoint call on a paced peer advancing its own scan tick.

    The tracking half models the #830 contract the leg pins: while
    the owner is paused, b's pulls produce nothing — counted misses
    and the degraded verdict — while its own clock and its journaled
    stale transition run ahead of the frozen line; the resumed pull
    lands the covering checkpoint carrying the settled receipt, and
    the adopted `command_settled` stamps the run's held axis (the
    receipt's own `applied` tick still naming the line's apply tick)
    — unless the `defect_axis` flag replays the pre-fix shape and
    stamps the carried tick below the standing mark. Doctor flags
    stage each named defect and instability the issue calls out."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.paused = {'a': False, 'b': False}
        self.role = {'a': 'active', 'b': 'standby'}
        self.sync = 'tracking'     # b's sync kind
        self.aligned = 100
        self.misses = 0
        self.budget = 120
        self.was_degraded = False
        self.stale_age = 0
        self.quality = 'good'
        self.point = False
        self.last_pushed = {'a': 0, 'b': 0}
        self.seq = {'a': 0, 'b': 0}
        self.receipts = []         # the field owner's receipt log
        self.attempts = 0
        self.adopted = set()       # receipt indices b journaled
        self.served = {'a': [], 'b': []}
        self.journals = {}
        self._malformed_done = False
        # Fault injection — each named failure the issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_tracking = False      # the standby never converges
        self.wedged_restore = False   # the owner's re-promote is
                                      # refused and b never re-tracks
        self.pause_refused = False    # the docker pause never lands
        self.unpause_fails = False    # the thaw never lands
        self.partition_leaks = False  # pulls keep landing through the
                                      # pause — the window never
                                      # produces the degraded verdict
        self.never_realigns = False   # once degraded, the peer's
                                      # pulls never land again
        self.adopt_silent = False     # the adopted settle never
                                      # journals on the peer
        self.adopt_twice = False      # the adopted settle journals
                                      # twice
        self.early_adoption = False   # the settle journals on the
                                      # peer ahead of the partition
        self.defect_axis = False      # the doctored negative — the
                                      # adopted settle stamps the
                                      # carried apply tick below the
                                      # standing mark
        self.malformed_entry = False  # a durable record carries no
                                      # integer tick
        self.no_transition = False    # the degraded window journals
                                      # no transition ahead of the
                                      # adopted settle
        self.failover_fires = False   # the armed peer self-promotes
                                      # inside the held window
        self.settle_never = False     # the admission stays accepted
        self.command_refused = False  # the command POST is refused
        self.drop_role = False        # /role drops inside the window
        self.predates_contract = False  # durable records carry no
                                        # tick axis at all
        for peer in ('a', 'b'):
            path = self.tmp / ('journal-' + peer + '.jsonl')
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            self.journals[peer] = path
        # The baseline durable marks — the tick axis the contract
        # gate reads ahead of the staging.
        self._push('a', 'role_changed',
                   {'from': 'standby', 'to': 'active'})
        self._push('b', 'quality_changed',
                   {'point': 10, 'from': None, 'to': 'good'})

    # ---- the durable journal --------------------------------------

    def _push(self, peer, kind, body, tick=None):
        """One journaled record on `peer`: the entry stamps
        max(tick, last_pushed) — the #830 clamped append axis —
        unless the defect flag replays the unclamped shape."""
        self.seq[peer] += 1
        at = self.tick[peer] if tick is None else tick
        stamped = at if self.defect_axis \
            else max(at, self.last_pushed[peer])
        self.last_pushed[peer] = stamped
        entry = {'seq': self.seq[peer], 'tick': stamped,
                 'event': {kind: body}}
        if self.predates_contract:
            del entry['tick']
        elif self.malformed_entry and peer == 'b' \
                and kind == 'quality_changed' \
                and not self._malformed_done:
            self._malformed_done = True
            del entry['tick']
        self.served[peer].append(entry)
        with self.journals[peer].open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    # ---- the tracking model ----------------------------------------

    def _pull(self):
        """One tracking pull of the configured source: a produced
        nothing while the source is frozen or dead — counted miss,
        degraded verdict — else the covering checkpoint lands:
        realigned on the source's tick, the settled receipts it
        carries adopted into the peer's durable journal."""
        dead = (self.paused['a'] or not self.up['a']) \
            and not self.partition_leaks
        if dead or (self.never_realigns and self.was_degraded):
            self.misses += 1
            if self.failover_fires and self.misses >= 3 \
                    and self.role['b'] == 'standby':
                self.role['b'] = 'promoting'
            else:
                self.sync = 'degraded'
                self.was_degraded = True
            return
        self.misses = 0
        if self.no_tracking or (self.wedged_restore
                                and self.role['a'] != 'active'):
            self.sync = 'unsynchronized'
            return
        self.sync = 'tracking' if self.role['a'] == 'active' \
            else 'orphaned'
        self.aligned = self.tick['a']
        for index, receipt in enumerate(self.receipts):
            outcome = receipt.get('outcome') or {}
            if 'accepted' in outcome or index in self.adopted:
                continue
            self.adopted.add(index)
            if self.adopt_silent:
                continue
            apply_tick = (outcome.get('applied') or {}).get('tick')
            # An applied receipt pushes at its carried apply tick —
            # the cross-domain stamp the #830 clamp holds to the
            # run's axis; a rejection pushes at the scan tick.
            self._push('b', 'command_settled',
                       {'receipt': json.loads(json.dumps(receipt))},
                       tick=apply_tick)
            if self.adopt_twice:
                self._push('b', 'command_settled',
                           {'receipt': json.loads(
                               json.dumps(receipt))},
                           tick=apply_tick)

    def _freshness(self):
        """The standby's own freshness bookkeeping: the frozen
        owner's field stops stepping under the pause, so the
        budgeted point's unchanged driver sample ages to stale at
        the peer's own held clock — the window's transition
        traffic — and clears on the thaw."""
        frozen = self.paused['a'] or not self.up['a']
        if frozen:
            self.stale_age += 1
            if self.stale_age > 5 and self.quality == 'good':
                self.quality = 'uncertain:stale'
                if not self.no_transition:
                    self._push('b', 'quality_changed',
                               {'point': 10, 'from': 'good',
                                'to': {'uncertain': 'stale'}})
        else:
            if self.quality != 'good':
                if not self.no_transition:
                    self._push('b', 'quality_changed',
                               {'point': 10, 'from': self.quality,
                                'to': 'good'})
                self.quality = 'good'
            self.stale_age = 0

    def _apply_due(self):
        """The owner's scan-boundary settlements: an accepted receipt
        whose apply tick has arrived applies and journals."""
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and self.tick['a'] >= accepted['apply_tick'] \
                    and not self.settle_never:
                receipt['outcome'] = {
                    'applied': {'tick': self.tick['a']}}
                write = receipt['command']['write_value']
                self.point = write['value']['bool']
                self._push('a', 'command_settled',
                           {'receipt': json.loads(
                               json.dumps(receipt))})

    def _scan(self, peer):
        """One completed scan: the peer's own tick advances, role
        transitions settle at the boundary, the owner applies due
        receipts, and the standby pulls its source."""
        self.tick[peer] += 1
        if self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
            if peer == 'b':
                self.sync = 'unsynchronized'
        elif self.role[peer] == 'promoting':
            self.role[peer] = 'active'
            if peer == 'b':
                self.sync = None
        if peer == 'a':
            self._apply_due()
        if peer == 'b' and self.role['b'] == 'standby':
            self._pull()
            self._freshness()
        elif peer == 'b':
            self._freshness()

    # ---- the runner-owned lifecycle actions ------------------------

    def pause_controller(self, name):
        if self.pause_refused:
            raise RuntimeError('docker pause failed: refused')
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = True

    def unpause_controller(self, name):
        if self.unpause_fails:
            raise RuntimeError('docker unpause failed: refused')
        self.paused[{'active': 'a', 'standby': 'b'}[name]] = False

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.role[peer] != 'active':
            self._raise(409, 'not_active')
        self._push(peer, 'role_changed',
                   {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        if peer == 'b':
            self.sync = 'unsynchronized'
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.role[peer] in ('active', 'promoting'):
            self._raise(409, 'already_active')
        if self.wedged_restore:
            self._raise(409, {'not_converged': {
                'sync': self.sync if peer == 'b'
                else 'unsynchronized'}})
        self.role[peer] = 'promoting'
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    def _admit(self, body):
        if self.command_refused:
            self._raise(400, 'invalid_command')
        self.attempts += 1
        receipt = {'command': body['command'],
                   'outcome': {'accepted': {
                       'apply_tick': self.tick['a'] + 1}},
                   'actor': body.get('actor')}
        self.receipts.append(receipt)
        if self.early_adoption:
            # The standby's pull lands between the admission's apply
            # and the freeze — the settle the leg stages lands ahead
            # of the window.
            receipt['outcome'] = {
                'applied': {'tick': self.tick['a']}}
            self._pull()
        return 200, receipt

    # ---- the endpoint dispatch -------------------------------------

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] == 'standby':
            sync = self.sync if peer == 'b' else 'unsynchronized'
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.aligned}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {
                    'detail': 'fetch from ctrl-a:8080: timed out'}}
            else:
                report['sync'] = sync or 'unsynchronized'
        if peer == 'b' and self.role['b'] == 'standby':
            report['failover'] = {'converged': self.sync == 'tracking',
                                  'misses': self.misses,
                                  'budget': self.budget}
        return report

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer] or self.paused[peer]:
            raise urllib.error.URLError('timed out')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if peer == 'b' and self.drop_role and self.paused['a'] \
                and route == '/role':
            raise urllib.error.URLError('timed out')
        # The paced peers: every endpoint call is one completed scan.
        self._scan(peer)
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}], 'components': []}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {
                'receipts': list(self.receipts),
                'command_admission': {'attempts': self.attempts}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if query else 0
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            if peer != 'a':
                self._raise(409, 'not_active')
            return self._admit(body)
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class RealignTickOrderTests(unittest.TestCase):
    """The tracker-realign-tick-order leg against the stubbed pair:
    a clean rig passes with identical digests and evidence — the
    degraded window observed, the realign landing, the adopted
    settle journaled behind the window's transition marks, the run's
    tick axis ordered, the launch roles restored — each doctored
    contract breach reports realign-tick-order-failed, each
    instability reports realign-tick-order-nondeterministic, and an
    unreachable, unconverged, seam-less, or pre-contract run is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RealignTickOrderFeed(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
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
                patch.object(scenarios, 'ORDER_SETTLE', 0.5), \
                patch.object(scenarios, 'ORDER_HOLD', 0.3), \
                patch.object(scenarios, 'ORDER_ADOPT', 0.5), \
                patch.object(scenarios, 'ORDER_POLL', 0.001):
            return scenarios.scenario_tracker_realign_tick_order(
                self.ctx(feed, **overrides))

    def passes(self):
        names = ('realign-tick-order-pass-1.json',
                 'realign-tick-order-pass-2.json')
        for name in names:
            self.assertTrue((self.evidence / name).is_file(), name)
        return [json.loads((self.evidence / name).read_text())
                for name in names]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The leg's window: behind the track-source-rediscovery leg
        # whose restore leaves the launch layout standing, before the
        # failover case the layout is owed to.
        self.assertLess(
            order.index(
                scenarios.scenario_track_source_rediscovery),
            order.index(
                scenarios.scenario_tracker_realign_tick_order))
        self.assertLess(
            order.index(
                scenarios.scenario_tracker_realign_tick_order),
            order.index(scenarios.scenario_failover))
        self.assertIs(verify.case_function(
            'tracker-realign-tick-order'),
            scenarios.scenario_tracker_realign_tick_order)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'admission': 'settled', 'window': 'degraded',
             'realigned': 'tracking', 'adopted': 'journaled',
             'axis': 'ordered', 'roles': 'restored'})
        record1 = passes[0]['record']
        # The degraded window was observed on the tracking peer and
        # the peer reconverged onto the resumed pulls.
        self.assertEqual(record1['degraded']['sync'], 'degraded')
        self.assertIn('tracking',
                      record1['realigned']['sync'])
        # The adopted settle journaled exactly once, behind the
        # window's transition marks, and the run's axis held.
        self.assertEqual(len(record1['adopted']), 1)
        self.assertGreaterEqual(record1['window_marks'], 1)
        self.assertFalse(record1['axis']['regressions'])
        self.assertTrue(record1['restored'])
        # The adopted receipt keeps the line's apply tick — the
        # stamp the journal's axis absorbed.
        self.assertIsNotNone(record1['adopted'][0]['apply_tick'])
        report.validate_scenario(record)

    def test_regressed_axis_fails(self):
        # The doctored negative the issue names: the journal asserted
        # as ordered while the adopted settle stamps a tick below an
        # earlier entry's within the run.
        self.feed.defect_axis = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        self.assertIn('regressed', record['detail'])
        report.validate_scenario(record)

    def test_malformed_axis_entry_fails(self):
        self.feed.malformed_entry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        self.assertIn('tick', record['detail'])
        report.validate_scenario(record)

    def test_adopted_settle_silent_fails(self):
        self.feed.adopt_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        self.assertIn('command_settled', record['detail'])
        report.validate_scenario(record)

    def test_adopted_settle_twice_fails(self):
        self.feed.adopt_twice = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        report.validate_scenario(record)

    def test_never_realigns_fails(self):
        self.feed.never_realigns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_roles_unrestored_fails(self):
        # The peer stays wedged off tracking after the realign — the
        # launch layout the cases behind this one meet is unrestored.
        self.feed.never_realigns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-failed'), record['detail'])
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_never_degraded_reports_nondeterministic(self):
        self.feed.partition_leaks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        self.assertIn('degraded', record['detail'])
        report.validate_scenario(record)

    def test_failover_fires_reports_nondeterministic(self):
        self.feed.failover_fires = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        self.assertIn('failover', record['detail'])
        report.validate_scenario(record)

    def test_admission_refused_reports_nondeterministic(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_settle_unapplied_reports_nondeterministic(self):
        self.feed.settle_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_adopted_early_reports_nondeterministic(self):
        self.feed.early_adoption = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        self.assertIn('ahead', record['detail'])
        report.validate_scenario(record)

    def test_window_silent_reports_nondeterministic(self):
        self.feed.no_transition = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_pause_refused_reports_nondeterministic(self):
        self.feed.pause_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_watch_starved_reports_nondeterministic(self):
        self.feed.drop_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_tick_order_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_tick_order',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'realign-tick-order-unchecked'), record['detail'])
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
        # ctrl-b already owns the field and ctrl-a never converged —
        # and the wedged restore refuses the walk back, so the
        # launch-layout pair the leg stages never settles.
        self.feed.wedged_restore = True
        self.feed.role = {'a': 'standby', 'b': 'active'}
        self.feed.sync = 'unsynchronized'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
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

    def test_predates_contract_reports_inconclusive(self):
        # A staged run that predates the attributed durable record:
        # the journal carries no tick-axis records to audit.
        self.feed.predates_contract = True
        for path in self.feed.journals.values():
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = RealignTickOrderFeed(str(evidence / 'journals'))
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
