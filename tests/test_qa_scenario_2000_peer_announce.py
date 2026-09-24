"""The 2000_peer_announce leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_peer_announce, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PeerAnnounceTests.test_registered',
    'PeerAnnounceTests.test_clean_pair_passes_and_validates',
    'PeerAnnounceTests.test_landed_announce_strands_unsynchronized',
    'PeerAnnounceTests.test_refused_checkpoint_read_fails',
    'PeerAnnounceTests.test_refused_demote_fails',
    'PeerAnnounceTests.test_missing_announce_refuses_demote',
    'PeerAnnounceTests.test_refused_promote_fails',
    'PeerAnnounceTests.test_stranded_demotion_fails',
    'PeerAnnounceTests.test_open_claim_fails',
    'PeerAnnounceTests.test_silent_plant_fails',
    'PeerAnnounceTests.test_admitted_command_fails',
    'PeerAnnounceTests.test_no_active_reports_failed',
    'PeerAnnounceTests.test_unconverged_pair_reports_inconclusive',
    'PeerAnnounceTests.test_unreachable_peer_reports_inconclusive',
    'PeerAnnounceTests.test_diverging_digests_report_nondeterministic',
    'PeerAnnounceTests.test_two_runs_produce_identical_evidence',
})


class PeerAnnounceFeed:
    """A stubbed pair for the peer-announce scenario: ctrl-a owns the
    field with no configured tracking source — its demotion follows
    the address a tracking peer announced on its checkpoint pulls —
    while ctrl-b is the tracking standby whose pulls recorded the
    genuine announce before the leg runs. Every endpoint call is one
    completed scan. A foreign `GET /checkpoint?peer=` naming anything
    but the genuine successor is ignored while the checkpoint still
    answers — the settled #645 acceptance — so the demoted peer
    reconverges tracking; every transition is call-count keyed, never
    wall-clock, so two scenario runs emit identical evidence. Doctor
    flags stage each named defect the issue calls out."""

    GENUINE = '10.0.0.2:8081'
    FINGERPRINT = 'peer-announce-fp'

    def __init__(self):
        self.tick = 100
        self.a_role = 'active'
        self.a_tracking = False
        self.a_track_left = 0
        self.b_role = 'standby'
        self.b_tracking = True
        self.b_track_left = 0
        # The announced fallback the field owner's monitor recorded
        # through the tracking peer's own pulls — ahead of the leg.
        self.announced = self.GENUINE
        self.point = False
        # Fault injection — each named failure the issue calls out.
        self.landed_announce = False  # the foreign announce lands —
                                      # the acceptance check regressed
        self.no_announce = False      # no genuine announce recorded
        self.checkpoint_refused = False  # the crafted read errors
        self.demote_refused = False   # every demote is refused
        self.promote_refused = False  # every promote is refused
        self.never_tracks = False     # the demoted peer strands
                                      # unsynchronized
        self.claim_open = False       # the plant answers stepped —
                                      # no writer claim stands
        self.plant_silent = False     # the plant never answers
        self.command_admitted = False  # the demoted peer admits a
                                       # field write
        self.no_active = False        # no peer reports active
        self.no_tracking = False      # the standby never converges
        self.unreachable = False      # the monitors never answer
        if self.no_announce:
            self.announced = None

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _poll_a(self):
        # The demoted writer's paced pulls converge it onto the
        # announced successor — each /role poll models one tracking
        # cadence, landing only on the genuine announce.
        if self.a_role == 'standby' and not self.a_tracking \
                and self.a_track_left > 0:
            self.a_track_left -= 1
            if self.a_track_left == 0 \
                    and self.announced == self.GENUINE \
                    and not self.never_tracks:
                self.a_tracking = True

    def _poll_b(self):
        if self.b_role == 'standby' and not self.b_tracking \
                and not self.no_tracking and self.b_track_left > 0:
            self.b_track_left -= 1
            if self.b_track_left == 0:
                self.b_tracking = True

    def _role_a(self):
        if self.no_active:
            return {'role': 'standby', 'tick': self.tick,
                    'sync': 'unsynchronized'}
        report = {'role': self.a_role, 'tick': self.tick}
        if self.a_role == 'standby':
            self._poll_a()
            report['sync'] = {'tracking': {'aligned': self.tick}} \
                if self.a_tracking else 'unsynchronized'
        return report

    def _role_b(self):
        report = {'role': self.b_role, 'tick': self.tick}
        if self.b_role == 'standby':
            self._poll_b()
            tracking = self.b_tracking and not self.no_tracking
            report['sync'] = {'tracking': {'aligned': self.tick}} \
                if tracking else 'unsynchronized'
        return report

    def _snapshot(self):
        return {'tick': self.tick, 'points': [
            {'point': 10, 'sample': {
                'value': {'bool': self.point}, 'quality': 'good'}}]}

    def _demote_a(self):
        self.a_role = 'standby'
        self.a_tracking = False
        self.a_track_left = 2

    def _demote_b(self):
        self.b_role = 'standby'
        self.b_tracking = False
        self.b_track_left = 2

    def try_plant(self, ctx, request):
        """The plant-protocol fencing probe: `step` answers fenced
        while the promoted peer's writer claim stands."""
        self.tick += 0  # the probe is field-side, never a scan
        if request.get('op') != 'step':
            raise AssertionError('unexpected plant request %s'
                                 % (request,))
        if self.plant_silent:
            return None
        if self.claim_open:
            return {'result': 'stepped', 'tick': self.tick}
        return {'result': 'error',
                'error': {'kind': 'fenced',
                          'detail': 'writer claim held by the field '
                                    'owner'}}

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        self.tick += 1
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        if (method, route) == ('GET', '/role'):
            return 200, self._role_a() if peer == 'a' \
                else self._role_b()
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot()
        if (method, route) == ('GET', '/checkpoint'):
            if query.startswith('peer='):
                announced = query.split('=', 1)[1]
                if self.checkpoint_refused:
                    self._raise(500, {'checkpoint': 'refused'})
                if announced == self.GENUINE:
                    self.announced = self.GENUINE
                elif self.landed_announce:
                    # The regressed acceptance: the foreign address
                    # lands exactly as it would without the #645 fix.
                    self.announced = announced
                # Else refused — the announce is ignored while the
                # checkpoint still answers.
            return 200, {'tick': self.tick,
                         'model_fingerprint': self.FINGERPRINT}
        if (method, route) == ('POST', '/command'):
            active = (peer == 'a' and self.a_role == 'active') \
                or (peer == 'b' and self.b_role == 'active')
            if active or self.command_admitted:
                return 200, {'command': (body or {}).get('command'),
                             'actor': (body or {}).get('actor'),
                             'outcome': {'accepted': {
                                 'apply_tick': self.tick + 1}}}
            return 200, {'command': (body or {}).get('command'),
                         'actor': (body or {}).get('actor'),
                         'outcome': {'rejected': {'reason': {
                             'not_active': {
                                 'point': 10,
                                 'role': 'standby'}}}}}
        if (method, route) == ('POST', '/demote'):
            if peer == 'a':
                if self.a_role != 'active' or self.demote_refused:
                    self._raise(409, {'not_active': {}})
                if self.announced is None:
                    self._raise(409, {'no_tracking_source': {}})
                self._demote_a()
                return 200, {'role': 'demoting', 'tick': self.tick}
            if self.b_role != 'active' or self.demote_refused:
                self._raise(409, {'not_active': {}})
            self._demote_b()
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if peer == 'a':
                if self.a_role == 'active':
                    self._raise(409, {'already_active': {}})
                if not self.a_tracking or self.promote_refused:
                    self._raise(409, {'not_converged': {
                        'sync': 'unsynchronized'}})
                self.a_role = 'active'
                if self.b_role == 'active':
                    self._demote_b()
                return 200, {'role': 'promoting', 'tick': self.tick}
            if self.b_role == 'active':
                self._raise(409, {'already_active': {}})
            if not self.b_tracking or self.no_tracking \
                    or self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': 'unsynchronized'}})
            self.b_role = 'active'
            if self.a_role == 'active':
                self._demote_a()
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class PeerAnnounceTests(unittest.TestCase):
    """The peer-announce leg against the stubbed pair: a clean rig
    passes with identical digests and evidence — the crafted read
    answering while the demotion tracks the genuine successor — each
    doctored defect reports the named diagnostic, and an unconverged
    or unreachable pair is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = PeerAnnounceFeed()
        if self.feed.no_announce:
            self.feed.announced = None

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': '127.0.0.1:9999',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_try_plant',
                             feed.try_plant), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'PEER_ANNOUNCE_SETTLE', 1.0), \
                patch.object(scenarios, 'PEER_ANNOUNCE_POLL', 0.001):
            return scenarios.scenario_peer_announce(ctx)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The restored pre-switch window behind the demote-carry
        # case — the settled tracking pair ahead of the tune case's
        # a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_demote_settle_uniqueness),
            order.index(scenarios.scenario_peer_announce))
        self.assertEqual(
            order.index(scenarios.scenario_demote_carry_settle) + 1,
            order.index(scenarios.scenario_peer_announce))
        self.assertEqual(
            order.index(scenarios.scenario_peer_announce) + 1,
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('peer-announce'),
                      scenarios.scenario_peer_announce)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('peer-announce-signals.json',
                     'peer-announce-pass-1.json',
                     'peer-announce-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('peer-announce-pass-1.json',
                               'peer-announce-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest'],
                         {'announce': 'answered',
                          'demoted': 'tracking', 'claim': 'fenced',
                          'command': 'refused', 'roles': 'restored'})
        self.assertEqual(passes[0]['announce']['peer'],
                         scenarios.PEER_ANNOUNCE_DEAD)
        report.validate_scenario(record)

    def test_landed_announce_strands_unsynchronized(self):
        # The doctored rig whose acceptance check regressed: the
        # foreign announce lands, so the demoted peer follows the
        # dead address into unsynchronized instead of tracking.
        self.feed.landed_announce = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('unsynchronized', record['detail'])
        report.validate_scenario(record)

    def test_refused_checkpoint_read_fails(self):
        self.feed.checkpoint_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('checkpoint', record['detail'])
        report.validate_scenario(record)

    def test_refused_demote_fails(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_missing_announce_refuses_demote(self):
        self.feed.announced = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_fails(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_stranded_demotion_fails(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('unsynchronized', record['detail'])
        report.validate_scenario(record)

    def test_open_claim_fails(self):
        self.feed.claim_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('writer claim', record['detail'])
        report.validate_scenario(record)

    def test_silent_plant_fails(self):
        self.feed.plant_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('plant', record['detail'])
        report.validate_scenario(record)

    def test_admitted_command_fails(self):
        self.feed.command_admitted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-failed'), record['detail'])
        self.assertIn('not_active', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_peer_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'roles': 'restored'}, {}, {'pass': 1}),
                       ({'roles': 'switched'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_peer_announce_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'peer-announce-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = PeerAnnounceFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
