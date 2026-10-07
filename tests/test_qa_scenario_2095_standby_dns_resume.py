"""The 2095_standby_dns_resume leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_standby_dns_resume,
split out per the #940 convention. The shared fakes and helpers live
in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam
from test_qa_scenario_2090_resume_settle_once import (
    ResumeSettleFeed, ResumeSettlePeer)


class DnsResumeFeed(ResumeSettleFeed):
    """A stubbed pair for the standby-dns-resume leg: the
    resume-settle pair plus the configured tracking-source flag the
    runner's relaunch lever doctors — `track` standing in for the
    launched active's `--peer` argument and the launched standby's
    `--standby` target. `relaunch_controller` recreates the member's
    container like the lever does: the doctored flag replaces the
    launch command's tracking source, then the same conditional
    startup grant `start_controller` runs resumes the process. A
    configured name no rig peer answers resolves as a pull source:
    the demoted peer's every pull misses the same way, the named
    degraded standby evidence — never a process exit — unless the
    exit_unresolvable doctor stages the pre-#1081 shape the leg
    exists to catch."""

    def __init__(self, plant, journal_files=None, state_files=None):
        super().__init__(plant, journal_files, state_files)
        # The configured tracking-source flags: the launched active
        # carries no --peer at launch; the launched standby's is
        # ctrl-a's name.
        self.a.track = None
        self.b.track = 'a'
        self.a.degraded_detail = None
        self.b.degraded_detail = None
        self.relaunch_calls = []
        # The doctors staging each named defect.
        self.exit_unresolvable = False  # the pre-#1081 shape: an
                                        # unresolvable tracking name
                                        # exits the resumed process
        self.relaunch_fails = False     # the lever never stages the
                                        # doctored flag
        self.peer_promotes = False      # the sibling takes the field
                                        # inside the relaunch window
        self.no_degraded = False        # the demoted peer's standby
                                        # evidence never names the
                                        # resolution failure
        self.freeze_a = False           # the resumed process serves
                                        # but never scans
        self.deaf_served = False        # the resumed monitor's read
                                        # surfaces stop answering
        self.refuse_writes = False      # the resumed owner's command
                                        # path refuses the write

    # ---- the tracking pull's deferred resolution ---------------------

    def _pull(self, peer):
        # The demote's writer release: a standby never holds the
        # field claim — the release the demotion ran freed the field
        # for the relaunched owner's conditional grant.
        with self.plant.lock:
            if (self.plant.claim or {}).get('owner') == peer.token:
                self.plant.claim = None
                self.plant.shared_conns = set()
        if peer.source is None and peer.track is not None:
            # The configured tracking source — --peer's name on a
            # demoted launched-active, --standby's on the launched
            # standby — the demotion adopts it ahead of any announced
            # probing.
            peer.source = peer.track
        if peer.source is not None \
                and peer.source not in self._peers():
            # A configured name that never resolves: every pull
            # misses the same way — the named degraded standby
            # condition the #1081 contract puts in place of a process
            # exit, unless the doctor strips the evidence.
            if self.no_degraded:
                peer.sync = 'unsynchronized'
                return
            peer.sync = 'degraded'
            peer.degraded_detail = 'fetch from ' + peer.source \
                + ': cannot resolve: name or service not known'
            return
        super()._pull(peer)

    def _report(self, peer):
        report = super()._report(peer)
        if report['role'] == 'standby' and peer.sync == 'degraded':
            report['sync'] = {'degraded': {
                'detail': peer.degraded_detail}}
        return report

    def _scan(self, peer):
        if peer.name == 'a' and self.freeze_a:
            return      # the resumed process serves but never scans
        super()._scan(peer)

    # ---- the runner-owned flag-doctoring relaunch ---------------------

    def relaunch_controller(self, key, track=None):
        """The ctx['relaunch_controller'] seam: `track` becomes the
        member's tracking-source flag — --peer on the launched
        active, --standby's target on the launched standby — and
        track=None recreates the launch command unchanged. The
        recreate itself is the stop/start lifecycle halves; the
        process resumes through the conditional startup grant."""
        self.relaunch_calls.append((key, track))
        if self.relaunch_fails:
            raise RuntimeError('docker run failed: relaunch refused')
        peer = self._peers()[self.KEYS[key]]
        peer.track = track if track is not None \
            else ('a' if peer.name == 'b' else None)
        if track is not None and self.exit_unresolvable:
            # The pre-contract shape the leg names: the unresolvable
            # tracking name ended the process at startup — the
            # observed exit(1), its monitor never answering again.
            self.up[peer.name] = False
            return
        if peer.name == 'a' and track is not None \
                and self.peer_promotes:
            # The sibling's failover landed inside the relaunch
            # window: the resumed grant's refusal into the live
            # incumbent's field exits the relaunched process.
            self.b.role = 'active'
            self.b.sync = None
            self.b.stamp = 'b'
            self.b.source = None
            self._wire_claim(self.TOKEN_B)
        self.restart_controller(key)

    def start_controller(self, key):
        super().start_controller(key)
        peer = self._peers()[self.KEYS[key]]
        if peer.name == 'b' and self.up[peer.name]:
            # A relaunched standby tracks its configured --standby
            # name — the launch target absent doctoring, the doctored
            # name when the lever staged one.
            peer.source = peer.track or 'a'

    # ---- the endpoint dispatch ---------------------------------------

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        route, _, _ = ('/' + url.split('/', 3)[3]).partition('?')
        if self.up.get(peer.name) and peer.track is not None:
            # The doctored-resume doctors: staged only on the
            # recreated process, so the settle gate's pre-relaunch
            # reads still answer.
            if self.deaf_served and (method, route) in (
                    ('GET', '/signals'), ('GET', '/snapshot')):
                raise urllib.error.URLError('served surface down')
            if self.refuse_writes \
                    and (method, route) == ('POST', '/command'):
                return 200, {'command': (body or {}).get('command'),
                             'outcome': {'rejected': {'reason': {
                                 'doctored_fencing': {}}}}}
        return super().http_json(method, url, body, timeout)


class DnsResumeTests(unittest.TestCase):
    """The standby-dns-resume leg against the stubbed pair over a
    real claim-arbitrating plant: a clean rig passes with identical
    digests — the field owner resumed through the doctored relaunch
    keeps owning, scanning, writing, and serving, and the demoted
    resume reports the doctored name's resolution failure as named
    degraded standby evidence — each doctored defect reports the
    named diagnostic, and an unreachable, unconverged, inverted, or
    lever-less run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = ClaimPlantPeer()
        self.feed = DnsResumeFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'evidence_dir': str(self.evidence),
               'relaunch_controller': feed.relaunch_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DNS_RESUME_SETTLE', 2.0), \
                patch.object(scenarios, 'DNS_RESUME_AUDIT', 1.0), \
                patch.object(scenarios, 'DNS_RESUME_POLL', 0.001), \
                patch.object(scenarios, 'DNS_RESUME_WATCH', 0.001):
            return scenarios.scenario_standby_dns_resume(
                ctx or self._ctx(feed=feed))

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The standby-dns-resume leg's window: behind the
        # resume-settle-once leg, still inside the launch-layout
        # window the tune case's a->b switch closes.
        self.assertLess(
            order.index(scenarios.scenario_resume_settle_once),
            order.index(scenarios.scenario_standby_dns_resume))
        self.assertLess(
            order.index(scenarios.scenario_standby_dns_resume),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(
            verify.case_function('standby-dns-resume'),
            scenarios.scenario_standby_dns_resume)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('standby-dns-resume-signals.json',
                     'standby-dns-resume-pass-1.json',
                     'standby-dns-resume-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('standby-dns-resume-pass-1.json',
                               'standby-dns-resume-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'resume': 'up', 'scans': 'advanced',
             'served': 'answered', 'write': 'landed',
             'degraded': 'named', 'roles': 'restored'})
        # The relaunch lever ran twice per pass — the doctored
        # tracking source, then the launch flags restored.
        self.assertEqual(
            self.feed.relaunch_calls,
            [('active', scenarios.UNRESOLVABLE_TRACK),
             ('active', None)] * 2)
        # The degraded evidence named the doctored source's
        # resolution failure on the still-serving monitor.
        self.assertIn(scenarios.UNRESOLVABLE_TRACK,
                      passes[0]['degraded']['sync']['degraded']
                      ['detail'])
        report.validate_scenario(record)

    def test_resumed_process_exits_reports_failed(self):
        # The pre-#1081 shape the leg exists to name: the unresolvable
        # tracking name ended the resumed process — the observed
        # exit(1), not a degraded standby condition.
        self.feed.exit_unresolvable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('never answered /role', record['detail'])
        report.validate_scenario(record)

    def test_degraded_never_named_reports_failed(self):
        self.feed.no_degraded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('resolution failure', record['detail'])
        report.validate_scenario(record)

    def test_peer_promoted_reports_failed(self):
        self.feed.peer_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('took the field', record['detail'])
        report.validate_scenario(record)

    def test_scans_stall_reports_failed(self):
        self.feed.freeze_a = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('tick', record['detail'])
        report.validate_scenario(record)

    def test_served_unanswered_reports_failed(self):
        self.feed.deaf_served = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('served surface', record['detail'])
        report.validate_scenario(record)

    def test_write_refused_reports_failed(self):
        self.feed.refuse_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-failed'), record['detail'])
        self.assertIn('write', record['detail'])
        report.validate_scenario(record)

    def test_missing_relaunch_reports_inconclusive(self):
        record = self.run_scenario(
            self._ctx(relaunch_controller=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('relaunch_controller', record['detail'])
        report.validate_scenario(record)

    def test_relaunch_failure_reports_inconclusive(self):
        self.feed.relaunch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('relaunch', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_inverted_launch_reports_inconclusive(self):
        self.feed.invert()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launched-active', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unchecked_selfcheck_reports_failed(self):
        # The self-check leg's own negative: a degraded-standby audit
        # that cannot convict a planted wrong-source record must
        # report its named diagnostic rather than passing silently.
        with patch.object(scenarios, '_degraded_verdict',
                          lambda *a, **k: 'named'):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'resume': 'up'}, {}, {'pass': 1}),
                       ({'resume': 'down'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_dns_resume_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'standby-dns-resume-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = ClaimPlantPeer()
            feed = DnsResumeFeed(plant)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
