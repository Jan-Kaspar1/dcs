"""The 0650_bare_point_restamp leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_bare_point_restamp,
split out of the test_qa_scenarios monolith (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's
case coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'BarePointRestampTests.test_registered_in_scenarios',
    'BarePointRestampTests.test_clean_feed_passes_and_validates',
    'BarePointRestampTests.test_predating_rig_is_inconclusive',
    'BarePointRestampTests.test_stamp_gap_fails',
    'BarePointRestampTests.test_stepped_rewind_fails',
    'BarePointRestampTests.test_freeze_rewind_fails',
    'BarePointRestampTests.test_unstamped_freeze_read_fails',
    'BarePointRestampTests.test_element_driven_point_is_inconclusive',
    'BarePointRestampTests.test_no_bare_input_is_inconclusive',
    'BarePointRestampTests.test_no_driven_contrast_is_inconclusive',
    'BarePointRestampTests.test_refused_claim_is_inconclusive',
    'BarePointRestampTests.test_refused_probe_write_is_inconclusive',
    'BarePointRestampTests.test_fenced_step_is_inconclusive',
    'BarePointRestampTests.test_freeze_never_took_is_inconclusive',
    'BarePointRestampTests.test_resume_never_restores_fails',
    'BarePointRestampTests.test_promotion_mid_freeze_passes',
    'BarePointRestampTests.test_unreturned_writer_is_inconclusive',
    'BarePointRestampTests.test_failed_stop_action_is_inconclusive',
    'BarePointRestampTests.test_failed_start_action_is_inconclusive',
    'BarePointRestampTests.test_unreachable_pair_is_inconclusive',
    'BarePointRestampTests.test_no_tracking_peer_is_inconclusive',
    'BarePointRestampTests.test_silent_census_is_inconclusive',
    'BarePointRestampTests.test_missing_actions_is_inconclusive',
    'BarePointRestampTests.test_two_runs_produce_identical_evidence',
})


class RestampPlantPeer(FakePlantPeer):
    """The shared plant for the bare-point-restamp scenario: two
    served in-points — inflow (12), the bare channel-bound input no
    element owns, and level-primary (10), the integrator-driven
    contrast stamped by its own rule — behind the single-writer claim
    the scenario's raw-client attachment shares under the settled
    active's pinned owner token. While the field owner steps
    (`stepping` set) every request carries one plant step — the
    owner's scans — driving the integrator's fresh stamp and
    re-stamping the bare channel at the new tick, the #988 rule; a
    stopped plant serves its stored samples unchanged. Fault flags
    stage each named failure the issue calls out."""

    def __init__(self, owner):
        super().__init__()
        self.samples = {
            10: {'value': {'float': 0.8}, 'quality': 'good',
                 'tick': 0},
            12: {'value': {'float': 0.25}, 'quality': 'good',
                 'tick': 0},
        }
        self.owner = owner
        self.holders = {'controller'}
        self.writer_granted = False
        self.plant_tick = 0
        self.advances = 0
        self.stepping = True
        self.step_requests = 0
        self.steps_allowed = None   # cap before step answers fenced
        self.bare_reads = 0
        self._skip_bare = False
        # Fault injection for the named-failure cases.
        self.predates = False       # the bare channel never re-stamps
        self.element_drives = False # an element owns the named point
        self.refuse_claim = False   # ensure_writer answers refused
        self.refuse_write = False   # a granted write answers fenced
        self.gap_steps = frozenset()  # step indexes losing the re-stamp
        self.rewind_reads = frozenset()  # bare reads stamping backwards
        self.rewind_frozen = False  # a frozen read serves a rewound stamp
        self.unstamp_frozen = False # a frozen read serves no stamp
        self._rewound_once = False

    def _advance(self, dt=0.25):
        """One field-owner step: the plant tick advances, the
        integrator drives level-primary's fresh stamp, and the bare
        channel is re-stamped like a scanned input card — unless a
        fault flag stages the miss."""
        self.plant_tick += 1
        self.advances += 1
        tick = self.plant_tick
        inflow = self.samples[12].get('value') or {}
        rate = inflow.get('float') or 0.0
        level = self.samples[10]['value']['float'] + rate * dt
        self.samples[10] = {'value': {'float': level},
                            'quality': 'good', 'tick': tick}
        if self.element_drives:
            self.samples[12] = {'value': {'float': 0.5},
                                'quality': 'good', 'tick': tick}
        elif not (self.predates or self._skip_bare):
            self.samples[12] = dict(self.samples[12], tick=tick)

    @staticmethod
    def _fenced():
        return {'result': 'error',
                'error': {'kind': 'fenced',
                          'detail': 'writer claim held by another '
                                    'attachment'}}

    def dispatch(self, request):
        op, point = request.get('op'), request.get('point')
        if op == 'ensure_writer':
            self.requests.append(request)
            if self.refuse_claim \
                    or request.get('owner') != self.owner:
                return {'result': 'refused'}
            self.holders.add('scenario')
            self.writer_granted = True
            return {'result': 'claimed_shared', 'owner': self.owner}
        if op == 'release_writer':
            self.requests.append(request)
            self.holders.discard('scenario')
            self.writer_granted = False
            return {'result': 'done'}
        if op in ('write', 'step') and not self.writer_granted:
            self.requests.append(request)
            return self._fenced()
        if op == 'write':
            self.requests.append(request)
            if self.refuse_write:
                return self._fenced()
            if self.stepping:
                self._advance()
            self.samples[point] = {'value': request.get('value'),
                                   'quality': 'good',
                                   'tick': self.plant_tick}
            return {'result': 'done'}
        if op == 'step':
            self.requests.append(request)
            self.step_requests += 1
            if self.steps_allowed is not None \
                    and self.step_requests > self.steps_allowed:
                return self._fenced()
            # The driven step and the reads it sets up lose the bare
            # re-stamp while a gap is staged for this step index.
            self._skip_bare = self.step_requests in self.gap_steps
            self._advance(request.get('dt') or 0.25)
            return {'result': 'stepped', 'tick': self.plant_tick}
        if op == 'read' and not self.stepping:
            self.requests.append(request)
            if self.rewind_frozen and not self._rewound_once:
                self._rewound_once = True
                sample = dict(self.served(point))
                sample['tick'] = (sample.get('tick') or 0) - 1
                return {'result': 'sample', 'sample': sample}
            if self.unstamp_frozen:
                sample = dict(self.served(point))
                sample.pop('tick', None)
                return {'result': 'sample', 'sample': sample}
            return {'result': 'sample', 'sample': self.served(point)}
        if self.stepping:
            self._advance()
        if op == 'read' and point == 12:
            self.bare_reads += 1
            self.requests.append(request)
            sample = dict(self.served(point))
            if self.bare_reads in self.rewind_reads:
                sample['tick'] = (sample.get('tick') or 0) - 10
            return {'result': 'sample', 'sample': sample}
        return super().dispatch(request)


class RestampFeed:
    """A stubbed monitor pair for the bare-point-restamp scenario:
    ctrl-a owns the field — the plant's `stepping` flag is the owner
    scanning — and ctrl-b tracks until the promoted fault flag stages
    a failover-budget promotion. `stop`/`start` replace the runner's
    ctx['stop_controller']/ctx['start_controller'] actions: a stopped
    writer stops answering and the plant's stepping freezes; the
    restarted writer reclaims the field — or stays fenced out when
    the peer promoted first. Fault flags stage each named failure the
    issue calls out."""

    PROMOTE_MISSES = 4  # ctrl-b /role polls through the outage to it

    def __init__(self, plant):
        self.plant = plant
        self.ticks = {'ctrl-a': 0, 'ctrl-b': 0}
        self.owner = 'ctrl-a'    # the endpoint stepping the field
        self.writer_up = True
        self.misses = 0
        self.calls = []
        # Fault injection for the named-failure cases.
        self.pair_down = False         # every request is unreachable
        self.no_active = False         # neither peer reports active
        self.never_tracks = False      # ctrl-b's sync never converges
        self.promote = False           # the standby's budget fires
        self.stepping_persists = False # the stopped writer still steps
        self.stop_fails = False
        self.start_fails = False
        self.never_steps = False       # the restart never re-steps
        self.never_returns = False     # the restart leaves ctrl-a down
        self.bareless = False          # /signals omits the bare names
        self.drivenless = False        # /signals omits the driven names

    # The runner-owned lifecycle actions — replace
    # ctx['stop_controller']/ctx['start_controller'].
    def stop(self, name):
        self.calls.append(('stop', name))
        if self.stop_fails:
            raise RuntimeError('docker stop failed: no such container')
        self.writer_up = False
        if not self.stepping_persists:
            self.plant.stepping = False

    def start(self, name):
        self.calls.append(('start', name))
        if self.start_fails:
            raise RuntimeError('docker start failed: no such container')
        if self.owner == 'ctrl-b':
            self.writer_up = True   # the fenced restart — serving again
            return
        self.writer_up = not self.never_returns
        self.owner = 'ctrl-a'
        self.plant.stepping = not self.never_steps

    def http_json(self, method, url, body=None, timeout=10):
        if self.pair_down:
            raise urllib.error.URLError('simulated: pair unreachable')
        host = 'ctrl-a' if 'ctrl-a' in url else 'ctrl-b'
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.ticks[host] += 1
        if host == 'ctrl-a' and not self.writer_up:
            raise urllib.error.URLError('simulated: controller down')
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-a':
                role = 'active' if self.owner == 'ctrl-a' \
                    and not self.no_active else 'standby'
                return 200, {'role': role,
                             'tick': self.ticks[host]}
            if not self.writer_up:
                self.misses += 1
                if self.promote \
                        and self.misses >= self.PROMOTE_MISSES:
                    self.owner = 'ctrl-b'
                    self.plant.stepping = True
            if self.owner == 'ctrl-b':
                return 200, {'role': 'active',
                             'tick': self.ticks[host]}
            sync = {'unsynchronized': {}} if self.never_tracks \
                else {'tracking': {'aligned': self.ticks[host]}}
            return 200, {'role': 'standby',
                         'tick': self.ticks[host], 'sync': sync}
        if (method, route) == ('GET', '/signals') \
                and host == 'ctrl-a':
            points = []
            if not self.drivenless:
                points.append({'point': 10, 'name': 'level-primary',
                               'direction': 'in',
                               'value_type': 'float'})
            if not self.bareless:
                points.append({'point': 12, 'name': 'inflow',
                               'direction': 'in',
                               'value_type': 'float'})
            return 200, {'points': points, 'components': []}
        raise AssertionError('unexpected request %s %s' % (method, url))


class BarePointRestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = RestampPlantPeer('token-a')
        self.feed = RestampFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def run_scenario(self, ctx_extra=None):
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'plant_ctl': self.plant.ctl,
               'plant_owner': {'active': 'token-a',
                               'standby': 'token-b'},
               'evidence_dir': str(self.evidence),
               'stop_controller': self.feed.stop,
               'start_controller': self.feed.start,
               'failover_misses': self.feed.PROMOTE_MISSES + 2}
        ctx.update(ctx_extra or {})
        with patch.object(scenarios, 'http_json',
                          self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RESTAMP_POLL', 0.001), \
                patch.object(scenarios, 'RESTAMP_SETTLE', 0.3), \
                patch.object(scenarios, 'RESTAMP_FREEZE_DEADLINE',
                             1.0), \
                patch.object(scenarios, 'RESTAMP_RESUME_DEADLINE',
                             0.5), \
                patch.object(scenarios, 'RESTAMP_RETURN_DEADLINE',
                             0.3):
            return scenarios.scenario_bare_point_restamp(ctx)

    def test_registered_in_scenarios(self):
        names = [fn.__name__ for fn in scenarios.SCENARIOS]
        self.assertIn('scenario_bare_point_restamp', names)
        # The writer-stop leg sits inside the stale-freshness leg's
        # restored pre-switch window, ahead of the field-claim case.
        self.assertLess(names.index('scenario_stale_freshness'),
                        names.index('scenario_bare_point_restamp'))
        self.assertLess(names.index('scenario_bare_point_restamp'),
                        names.index('scenario_field_claim'))
        self.assertIs(verify.case_function('bare-point-restamp'),
                      scenarios.scenario_bare_point_restamp)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The leg drove the documented surface: the shared claim's
        # ensure/release, the bareness probe's write+restore, the
        # driven steps, and the writer stop/start.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('ensure_writer', ops)
        self.assertIn('release_writer', ops)
        self.assertGreaterEqual(ops.count('write'), 2)
        self.assertGreaterEqual(ops.count('step'),
                                scenarios.RESTAMP_STEPS + 1)
        self.assertEqual(self.feed.calls,
                         [('stop', 'active'), ('start', 'active')])
        # The stepped window's evidence shows the advancing bare
        # stamp; the freeze evidence shows the held floor and the
        # resumed read.
        steps = json.loads((self.evidence
                            / 'bare-point-restamp-steps.json')
                           .read_text())
        self.assertEqual(steps['bare'], 12)
        bare_ticks = [steps['baseline']['bare']] \
            + [row['bare'] for row in steps['rows']]
        self.assertEqual(bare_ticks, sorted(set(bare_ticks)))
        self.assertGreater(bare_ticks[-1], bare_ticks[0])
        freeze = json.loads((self.evidence
                             / 'bare-point-restamp-freeze.json')
                            .read_text())
        self.assertEqual(freeze['seam'], 'writer-stop')
        self.assertIsNone(freeze['moved'])
        self.assertFalse(freeze['promoted'])
        self.assertGreaterEqual(freeze['static'],
                                scenarios.RESTAMP_HOLD)
        self.assertGreater(freeze['resumed']['tick'],
                           freeze['floor']['bare'])

    def test_predating_rig_is_inconclusive(self):
        # The pre-#988 shape: the bare channel never re-stamps, so it
        # holds its stamp across the whole stepped window while the
        # element-driven contrast advances.
        self.plant.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the restamping contract',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stamp_gap_fails(self):
        # A step whose bare re-stamp is lost — the served stamp held
        # across one driven plant step mid-window.
        self.plant.gap_steps = frozenset({3})
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare-point-restamp-failed',
                      record.get('detail', ''))
        self.assertIn('held across a driven plant step',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stepped_rewind_fails(self):
        # A served stamp that rewinds inside the stepped window is the
        # nondeterminism the named diagnostic answers for.
        self.plant.rewind_reads = frozenset({5})
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare-point-restamp-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('rewound', record.get('detail', ''))
        report.validate_scenario(record)

    def test_freeze_rewind_fails(self):
        self.plant.rewind_frozen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare-point-restamp-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('frozen plant', record.get('detail', ''))

    def test_unstamped_freeze_read_fails(self):
        self.plant.unstamp_frozen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare-point-restamp-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('no integer stamp', record.get('detail', ''))

    def test_element_driven_point_is_inconclusive(self):
        # The named point turns out element-owned — the written value
        # a driver overwrites on the next step — so the rig exposes
        # no bare channel-bound input for the contract.
        self.plant.element_drives = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no bare channel-bound input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_bare_input_is_inconclusive(self):
        self.feed.bareless = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no bare channel-bound field input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_driven_contrast_is_inconclusive(self):
        self.feed.drivenless = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no dynamics-driven field input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('refused the shared attachment',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_probe_write_is_inconclusive(self):
        self.plant.refuse_write = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('probe write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_fenced_step_is_inconclusive(self):
        # The claim hold lapses mid-window: the probe step and one
        # window step land, the second window step meets fencing.
        self.plant.steps_allowed = 2
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('fencing verdict', record.get('detail', ''))
        report.validate_scenario(record)

    def test_freeze_never_took_is_inconclusive(self):
        # The writer-stop induction never froze stepping — the served
        # stamps keep advancing through the freeze window.
        self.feed.stepping_persists = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never took effect', record.get('detail', ''))
        report.validate_scenario(record)

    def test_resume_never_restores_fails(self):
        # The restarted writer serves again but its stepping never
        # resumes — the bare stamp stays at the frozen floor.
        self.feed.never_steps = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('bare-point-restamp-failed',
                      record.get('detail', ''))
        self.assertIn('never resumed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_mid_freeze_passes(self):
        # The standby's failover budget fires inside the freeze
        # window: the promoted run reclaims the writer, resumes
        # stepping, and the restarted endpoint stays fenced — the
        # documented recovery path 0600 reports.
        self.feed.promote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        freeze = json.loads((self.evidence
                             / 'bare-point-restamp-freeze.json')
                            .read_text())
        self.assertTrue(freeze['promoted'])
        self.assertTrue(freeze['resumed'])
        report.validate_scenario(record)

    def test_unreturned_writer_is_inconclusive(self):
        # Stepping resumes but the restarted writer's monitor never
        # serves again.
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restarted writer never returned',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_stop_action_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer-stop induction never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_start_action_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer restart never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_pair_is_inconclusive(self):
        self.feed.pair_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_tracking_peer_is_inconclusive(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no tracking peer', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silent_census_is_inconclusive(self):
        def dead_ctl(*args):
            return _ctl_process(stderr='connection refused',
                                returncode=1)
        record = self.run_scenario({'plant_ctl': dead_ctl})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('census never answered',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_actions_is_inconclusive(self):
        record = self.run_scenario({'stop_controller': None,
                                    'start_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no controller stop/start action',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        self.assertEqual(first['outcome'], 'passed', first)
        files = {}
        for entry in first['evidence']:
            path = self.evidence.parent / entry['ref']
            files[entry['ref']] = path.read_text()
        self.plant.close()
        self.plant = RestampPlantPeer('token-a')
        self.feed = RestampFeed(self.plant)
        second = self.run_scenario()
        self.assertEqual(second['outcome'], 'passed', second)
        self.assertEqual(first, second)
        for entry in second['evidence']:
            path = self.evidence.parent / entry['ref']
            self.assertEqual(files[entry['ref']], path.read_text(),
                             entry['ref'])
