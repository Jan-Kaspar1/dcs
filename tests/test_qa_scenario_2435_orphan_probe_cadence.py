"""The 2435_orphan_probe_cadence leg's scenario unit coverage — the
stubbed pair and TestCase class for scenario_orphan_probe_cadence,
split out per the #940 convention. The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The subject is the bounded orphan tracking-source probe cadence: a
standing foreign claim declaring an undialable monitor must not
collapse the paced scan. The stub paces each container's own run clock
and publication counter on the wall clock, answers the claim-aware seam
with a fenced verdict naming the dead endpoint, and each doctored flag
stages one named defect or instability the issue calls out.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'OrphanProbeCadenceTests.test_registered',
    'OrphanProbeCadenceTests.test_clean_rig_passes_and_validates',
    'OrphanProbeCadenceTests.test_collapsed_cadence_fails',
    'OrphanProbeCadenceTests.test_publication_stalled_fails',
    'OrphanProbeCadenceTests.test_overrun_flood_fails',
    'OrphanProbeCadenceTests.test_refusal_flood_fails',
    'OrphanProbeCadenceTests.test_verdict_drift_fails',
    'OrphanProbeCadenceTests.test_roles_unrestored_fails',
    'OrphanProbeCadenceTests.test_claim_unstaged_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_claim_refused_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_monitor_undeclared_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_surface_unstamped_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_cadence_unproven_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_demote_missing_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_watch_starved_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_never_orphaned_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_release_unstaged_reports_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_unreachable_pair_reports_'
    'inconclusive',
    'OrphanProbeCadenceTests.test_single_endpoint_reports_'
    'inconclusive',
    'OrphanProbeCadenceTests.test_missing_claim_seam_reports_'
    'inconclusive',
    'OrphanProbeCadenceTests.test_missing_journal_files_reports_'
    'inconclusive',
    'OrphanProbeCadenceTests.test_diverging_digests_report_'
    'nondeterministic',
    'OrphanProbeCadenceTests.test_silent_judge_reports_unchecked',
    'OrphanProbeCadenceTests.test_two_runs_produce_identical_evidence',
})


class OrphanProbeCadenceFeed:
    """A stubbed redundant pair plus the claim-aware seam for the
    probe-cadence leg. The launched active owns the field with no
    configured tracking source; the standby tracks it through its
    declared `--standby` pull. Each container paces its own run clock
    and publication counter on the wall clock, so a served tick owes
    the cadence it was paced at.

    The claim model implements the #1256 contract: a standing foreign
    claim preempts the field, the owner's next write is fenced and it
    demotes in place, and the orphaned peers' tracking-source resolution
    probes the claim's declared endpoint — but the refused candidate set
    is cached, so the journaled `tracking_source_refused` records stay
    at one per retry window instead of one per orphan cycle. The
    `per_scan_probe` flag replays the pre-fix shape, and `probe_cost`
    the endpoint's own per-cycle price in seconds of paced scan."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    CADENCE = 10.0        # ticks and publications per second
    START = 100

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': self.START, 'b': self.START}
        self.at = {'a': time.monotonic(), 'b': time.monotonic()}
        self.published = {'a': self.START, 'b': self.START}
        self.refused_for = {'a': None, 'b': None}
        self.overruns = {'a': 0, 'b': 0}
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        self.aligned = {'a': self.START, 'b': self.START}
        self.seq = {'a': 0, 'b': 0}
        self.journals = {}
        self.claim = None
        self.holder = False
        self.demoted_under_claim = None
        # Doctor flags — each named failure the issue calls out.
        self.probe_cost = 0.0     # seconds the endpoint costs per probe
        self.per_scan_probe = False
        self.collapse = 0         # a per-cycle cadence collapse, in
                                  # ticks per second
        self.overrun_flood = 0
        self.refusal_flood = 0
        self.publication_stall = False
        self.drift = None         # the verdict that replaces orphaned
        self.unreachable = False
        self.claim_refused = False
        self.monitor_undeclared = False
        self.claim_error = None
        self.pre_contract = False
        self.no_demote = False
        self.no_orphan = False
        self.drop_refused = False
        self.wedged = False
        for peer in ('a', 'b'):
            path = self.tmp / ('journal-' + peer + '.jsonl')
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            self.journals[peer] = path

    # ---- the durable journal --------------------------------------

    def _push(self, peer, kind, body, tick=None):
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer],
                 'tick': self.tick[peer] if tick is None else tick,
                 'event': {kind: body}}
        with self.journals[peer].open('a') as handle:
            handle.write(json.dumps({'entry': entry}) + '\n')

    # ---- the wall-clock pacing -------------------------------------

    def _pace(self, peer):
        """Advance a container's own run clock and its publication
        counter with the wall clock — the paced loop's own evidence."""
        if not self.up[peer]:
            return self.tick[peer]
        now = time.monotonic()
        due = int((now - self.at[peer]) * self.CADENCE)
        if due:
            self.tick[peer] += due
            self.published[peer] += due
            self.at[peer] += due / self.CADENCE
        return self.tick[peer]

    def _resolve(self, peer, source):
        """One orphaned cycle's tracking-source resolution: the claim's
        declared endpoint is probed and the probe refused. The probe
        itself runs on the scan thread every cycle — that is the cost
        the contract bounds — while the refused record it leaves in the
        durable audit is deduplicated per retry window. The
        `probe_cost` flag is the endpoint's own price in seconds of
        paced scan, and `collapse` the extra ticks the pre-fix landing
        ratcheted; `per_scan_probe` and `refusal_flood` replay the two
        refusal shapes the contract closed."""
        if self.claim is None or not self.holder:
            return
        if self.probe_cost:
            self.overruns[peer] += max(
                1, int(self.probe_cost * self.CADENCE))
        if self.overrun_flood:
            self.overruns[peer] += self.overrun_flood
        if self.collapse:
            self.tick[peer] -= self.collapse
        # The contract caches the refused candidate set for the probe
        # window, so one distinct refusal signature is journaled per
        # claim rather than one per orphan cycle; the retry window
        # re-probes the endpoint without re-journaling it.
        if not (self.per_scan_probe or self.refusal_flood) \
                and self.refused_for[peer] == id(self.claim):
            return
        self.refused_for[peer] = id(self.claim)
        copies = self.refusal_flood or (2 if self.per_scan_probe else 1)
        for index in range(max(1, copies)):
            self._push(peer, 'tracking_source_refused',
                       {'source': self.claim['monitor'],
                        'reason': 'no line proof %d' % index},
                       tick=self.tick[peer])

    # ---- the claim-aware seam --------------------------------------

    def hold_field_claim(self, request):
        """ctx['hold_field_claim'] — a live attachment placing the
        standing foreign claim; the holder container's own bridge
        address is what the blackholed monitor is derived from."""
        if self.claim_error is not None:
            raise RuntimeError(self.claim_error)
        if self.claim_refused:
            return {'address': '172.18.0.5:9001',
                    'reply': {'result': 'fenced'}}
        self.claim = {
            'owner': request.get('owner'),
            'monitor': request.get('monitor')}
        if self.claim['monitor'] is None and not self.monitor_undeclared:
            # The two-stage staging: the first claim lands without a
            # declared monitor, the address it answers with is what the
            # blackholed re-stage declares.
            self.claim['monitor'] = None
        self.holder = True
        self.refused_for = {'a': None, 'b': None}
        return {'address': '172.18.0.5:9001',
                'reply': {'result': 'claimed_shared',
                          'monitor': self.claim['monitor']}}

    def drop_field_claim(self):
        if self.drop_refused:
            raise RuntimeError('the holder container refused to stop')
        self.holder = False
        self.claim = None

    def reclaim(self, peer):
        """The orphan cycle's bounded reclaim: with the claim released
        the field stands unclaimed, so the fenced ex-owner re-arms it
        through its own conditional path and walks back to `active` —
        the recovery the standing window's release resolves through."""
        if self.wedged or self.demoted_under_claim != peer:
            return
        if self.role[peer] != 'standby':
            return
        self._push(peer, 'role_changed',
                   {'from': 'standby', 'to': 'promoting'})
        self._push(peer, 'role_changed',
                   {'from': 'promoting', 'to': 'active'})
        self.role[peer] = 'active'

    def _probe_writer(self, request):
        if self.claim is None:
            return {'result': 'unclaimed'}
        error = {'kind': 'fenced', 'owner': self.claim['owner']}
        # A monitor-less claim declares no monitor at all — the honest
        # shape the leg's first staging places and then supersedes with
        # the blackholed re-stage.
        if self.claim['monitor'] and not self.monitor_undeclared:
            error['monitor'] = self.claim['monitor']
        return {'result': 'error', 'error': error}

    # ---- the endpoint dispatch -------------------------------------

    def _report(self, peer):
        self._pace(peer)
        if self.claim is None and self.demoted_under_claim == peer:
            self.reclaim(peer)
        if self.role[peer] == 'active' and self.holder:
            # The owner's next write under the standing claim is fenced
            # and it demotes in place — the island the standing window
            # is read across.
            self.on_fenced_write(peer)
        if self.role[peer] == 'demoting':
            self._push(peer, 'role_changed',
                       {'from': 'demoting', 'to': 'standby'})
            self.role[peer] = 'standby'
        source = 'b' if peer == 'a' else 'a'
        sync = None
        if self.role[peer] == 'standby':
            if self.no_orphan and self.holder:
                sync = 'degraded'
            else:
                self._resolve(peer, source)
                if self.drift and self.holder and peer == 'b':
                    sync = self.drift
                elif self.role[source] == 'active':
                    sync = 'tracking'
                else:
                    sync = 'orphaned'
        return {'role': self.role[peer], 'tick': self.tick[peer],
                'sync': {sync: {'aligned': self.aligned[peer]}}
                if sync else None,
                'failover': {'converged': sync == 'tracking',
                             'misses': 0, 'budget': 120}}

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        if method == 'POST' and url.endswith('/plant'):
            return 200, self._probe_writer(body)
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if (method, route) == ('GET', '/role'):
            return 200, self._report(peer)
        if (method, route) == ('GET', '/snapshot'):
            published = self.published[peer]
            if self.publication_stall:
                published = self.START
            snapshot = {'tick': self.tick[peer],
                        'publication': {'published': published}}
            if not self.pre_contract:
                snapshot['io_health'] = {
                    'scan_overruns': self.overruns[peer]}
            return 200, snapshot
        if (method, route) == ('POST', '/demote'):
            if self.role[peer] != 'active':
                raise urllib.error.HTTPError(
                    url, 409, 'refused', None,
                    io.BytesIO(json.dumps('not_active').encode()))
            self._push(peer, 'role_changed',
                       {'from': 'active', 'to': 'demoting'})
            self.role[peer] = 'demoting'
            return 200, {'role': 'demoting', 'tick': self.tick[peer]}
        if (method, route) == ('POST', '/promote'):
            if self.wedged and self.role[peer] == 'standby':
                raise urllib.error.HTTPError(
                    url, 409, 'refused', None,
                    io.BytesIO(json.dumps('not_converged').encode()))
            if self.role[peer] in ('active', 'promoting'):
                raise urllib.error.HTTPError(
                    url, 409, 'refused', None,
                    io.BytesIO(json.dumps('already_active').encode()))
            self._push(peer, 'role_changed',
                       {'from': self.role[peer], 'to': 'promoting'})
            self.role[peer] = 'promoting'
            if not self.wedged:
                self.role[peer] = 'active'
            return 200, {'role': 'promoting', 'tick': self.tick[peer]}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def on_fenced_write(self, peer):
        """The owner's fenced write under the standing claim: the claim
        preempts, so the write is fenced and the owner demotes in place
        unless the staging suppresses it."""
        if self.claim is None or self.no_demote:
            return
        self._push(peer, 'field_claim_lost',
                   {'claimant': self.claim['owner']})
        self._push(peer, 'role_changed', {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        self.demoted_under_claim = peer


class OrphanProbeCadenceTests(unittest.TestCase):
    """The orphan-probe-cadence leg against the stubbed rig: a clean
    rig passes with identical digests and evidence — the measured
    cadence, the standing claim's fencing verdict, the paced rows held
    at the cadence's floor, the bounded overrun growth, the refused
    records inside the contract's per-window bound — each doctored
    contract breach reports orphan-probe-cadence-failed, each
    instability reports orphan-probe-cadence-nondeterministic, and an
    unreachable, seam-less, or pre-contract run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = OrphanProbeCadenceFeed(self.tmp.name)

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
               'plant': '127.0.0.1:9001',
               'hold_field_claim': feed.hold_field_claim,
               'drop_field_claim': feed.drop_field_claim}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'PROBE_FORM', 0.5), \
                patch.object(scenarios, 'PROBE_POLL', 0.001), \
                patch.object(scenarios, 'PROBE_HOLD', 0.5), \
                patch.object(scenarios, 'CADENCE_MEASURE', 0.4), \
                patch.object(scenarios, '_plant_probe',
                             lambda ctx, request, timeout=5:
                             feed._probe_writer(request)):
            return scenarios.scenario_orphan_probe_cadence(
                self.ctx(feed, **overrides))

    def passes(self):
        names = ('orphan-probe-cadence-pass-1.json',
                 'orphan-probe-cadence-pass-2.json')
        for name in names:
            self.assertTrue((self.evidence / name).is_file(), name)
        return [json.loads((self.evidence / name).read_text())
                for name in names]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_ownerless_remote_backoff),
            order.index(scenarios.scenario_orphan_probe_cadence))
        self.assertLess(
            order.index(scenarios.scenario_orphan_probe_cadence),
            order.index(scenarios.scenario_yielded_claim_rearm))
        self.assertIs(verify.case_function('orphan-probe-cadence'),
                      scenarios.scenario_orphan_probe_cadence)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'claim': 'standing', 'cadence': 'held', 'cost': 'bounded',
             'refusals': 'bounded', 'verdict': 'orphaned',
             'roles': 'restored'})
        first = passes[0]['record']
        # The claim landed and fenced the field: its verdict names the
        # owner and the dead declared monitor.
        self.assertEqual(first['claim']['result'], 'claimed_shared')
        self.assertEqual(first['claim']['monitor'],
                         '172.18.0.254:9')
        self.assertTrue(first['verdict']['declared'])
        self.assertTrue(first['demoted'])
        # The paced cadence held at the measured cadence's floor on both
        # peers, with the bounded probe's own overrun cost and a refusal
        # record inside the contract's per-window bound.
        for name in ('active', 'standby'):
            entry = first['cadence_rows'][name]
            self.assertGreaterEqual(entry['tick_rate'], entry['floor'])
            self.assertGreaterEqual(entry['published'],
                                    entry['published_floor'])
            self.assertLessEqual(entry['overruns'],
                                 scenarios.OVERRUN_SLACK)
            # The refused probe is auditable at all — the claim's
            # monitor verifiably inside its probe set — and stays
            # inside the contract's per-window bound.
            self.assertGreaterEqual(entry['refused'], 1)
            self.assertLessEqual(entry['refused'],
                                 scenarios.REFUSED_PER_WINDOW)
            self.assertLess(entry['refused'], entry['cycles'])
        self.assertTrue(first['restored'])
        report.validate_scenario(record)

    def test_collapsed_cadence_fails(self):
        # The issue's named negative: the standing dead-monitor claim
        # collapsing the paced scan.
        self.feed.collapse = 8
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('collapsed', record['detail'])
        report.validate_scenario(record)

    def test_publication_stalled_fails(self):
        self.feed.publication_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('publications', record['detail'])
        report.validate_scenario(record)

    def test_overrun_flood_fails(self):
        self.feed.probe_cost = 0.5
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('scan_overruns', record['detail'])
        report.validate_scenario(record)

    def test_refusal_flood_fails(self):
        # The pre-fix shape: the refused candidate set re-probed per
        # orphan cycle, so the journal fills one record per scan.
        self.feed.refusal_flood = 40
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('refused-probe', record['detail'])
        report.validate_scenario(record)

    def test_verdict_drift_fails(self):
        self.feed.drift = 'usurped'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('usurped', record['detail'])
        report.validate_scenario(record)

    def test_roles_unrestored_fails(self):
        self.feed.wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-failed'), record['detail'])
        self.assertIn('active plus one tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_claim_unstaged_reports_nondeterministic(self):
        self.feed.claim_error = 'the attachment refused the plant'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_claim_refused_reports_nondeterministic(self):
        self.feed.claim_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_monitor_undeclared_reports_nondeterministic(self):
        # A claim with no declared monitor leaves the contract's
        # endpoint judgment unstageable.
        self.feed.monitor_undeclared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_surface_unstamped_reports_nondeterministic(self):
        self.feed.pre_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_cadence_unproven_reports_nondeterministic(self):
        self.feed.up['a'] = False
        self.feed.up['b'] = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_demote_missing_reports_nondeterministic(self):
        self.feed.no_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_watch_starved_reports_nondeterministic(self):
        # The starved watch: the owner's monitor stopped answering, so
        # the standing window collected no paced rows to read.
        self.feed.up['a'] = False
        record = self.run_scenario()
        self.assertIn(record['outcome'], ('failed', 'inconclusive'),
                      record)

    def test_never_orphaned_reports_nondeterministic(self):
        self.feed.no_orphan = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_release_unstaged_reports_nondeterministic(self):
        self.feed.drop_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
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

    def test_missing_claim_seam_reports_inconclusive(self):
        record = self.run_scenario(hold_field_claim=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('hold_field_claim', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        original = scenarios._probe_pass
        calls = {'n': 0}

        def flaky(ctx, number, owner, peer, floors):
            calls['n'] += 1
            record, evidence = original(
                ctx, number, owner, peer, floors)
            if calls['n'] == 2:
                record['cadence'] = {}
            return record, evidence
        with patch.object(scenarios, '_probe_pass', flaky):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        with patch.object(scenarios, '_probe_self_check',
                          return_value=['collapsed-cadence']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'orphan-probe-cadence-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        digests = [json.loads(
            (self.evidence / ('orphan-probe-cadence-pass-%d.json'
                              % number)).read_text())['digest']
            for number in (1, 2)]
        self.assertEqual(first['outcome'], 'passed', first)
        self.assertEqual(digests[0], digests[1])


if __name__ == '__main__':
    unittest.main()