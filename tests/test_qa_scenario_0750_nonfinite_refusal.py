"""The 0750_nonfinite_refusal leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_nonfinite_refusal, following
the per-leg split convention (#940): one scenario module plus one
test module, no shared-file edits. The shared fakes and helpers live
in tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
case set for the discovery check in tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'NonfiniteRefusalTests.test_registered_in_scenarios',
    'NonfiniteRefusalTests.test_clean_feed_passes_and_validates',
    'NonfiniteRefusalTests.test_two_runs_produce_identical_evidence',
    'NonfiniteRefusalTests.test_applied_write_fails',
    'NonfiniteRefusalTests.test_write_wrong_refusal_fails',
    'NonfiniteRefusalTests.test_applied_step_fails',
    'NonfiniteRefusalTests.test_step_wrong_refusal_fails',
    'NonfiniteRefusalTests.test_poisoned_read_fails',
    'NonfiniteRefusalTests.test_poisoned_census_fails',
    'NonfiniteRefusalTests.test_refused_write_moved_field_fails',
    'NonfiniteRefusalTests.test_followup_write_refused_fails',
    'NonfiniteRefusalTests.test_followup_step_refused_fails',
    'NonfiniteRefusalTests.test_followup_not_landing_fails',
    'NonfiniteRefusalTests.test_static_step_tick_fails',
    'NonfiniteRefusalTests.test_poisoned_after_followup_fails',
    'NonfiniteRefusalTests.test_unrestored_image_fails',
    'NonfiniteRefusalTests.test_role_move_fails',
    'NonfiniteRefusalTests.test_stalled_scans_fail',
    'NonfiniteRefusalTests.test_io_health_counted_fails',
    'NonfiniteRefusalTests.test_no_active_is_failed',
    'NonfiniteRefusalTests.test_unreachable_pair_is_inconclusive',
    'NonfiniteRefusalTests.test_no_tracking_is_inconclusive',
    'NonfiniteRefusalTests.test_no_plant_endpoint_is_inconclusive',
    'NonfiniteRefusalTests.test_no_owner_token_is_inconclusive',
    'NonfiniteRefusalTests.test_refused_claim_is_inconclusive',
    'NonfiniteRefusalTests.test_predating_rig_is_inconclusive',
    'NonfiniteRefusalTests.test_no_float_point_is_inconclusive',
    'NonfiniteRefusalTests.test_fenced_probes_are_inconclusive',
})


class NonfinitePlantPeer(FakePlantPeer):
    """The nonfinite-refusal rig's plant half: FakePlantPeer plus the
    write-ownership claim (`ensure_writer`/`release_writer`) and the
    `write`/`step` ops the scenario drives over the shared-claim path
    — mirroring the post-#834/#866 sim-net contract, where a float
    write or step dt that is not a finite number dies at the request
    boundary as the named `invalid_request` refusal and stores
    nothing. The standing owner is pre-seeded with a live holder so
    the scenario's attachment answers `claimed_shared`, and an
    integrator over the inflow point gives the leg the driven
    accumulator whose finite-hold the census legs prove: a step whose
    arithmetic would leave the element non-finite holds its last
    finite state stamped bad:out_of_range instead. Doctor flags stage
    each named failure the scenario reports."""

    def __init__(self, owner):
        super().__init__()
        # The pump-station shape the deployed dynamics document
        # carries: the level integrator's output, the forcing input
        # the harness legs write, and a bool point.
        self.samples = {
            10: {'value': {'float': 0.8}, 'quality': 'good',
                 'tick': 0},
            12: {'value': {'float': 0.0}, 'quality': 'good',
                 'tick': 0},
            40: {'value': {'bool': False}, 'quality': 'good',
                 'tick': 0}}
        self.owner = owner
        self.holders = {'controller'}   # the active's standing claim
        self.writer_granted = False     # the scenario's hold
        self.plant_tick = 0
        self.probed = False             # a non-finite payload arrived
        self.finite_writes = 0          # finite writes since probing
        # Doctor flags for the named-failure cases.
        self.refuse_claim = False    # ensure_writer answers fenced
        self.predates = False        # the claim ops answer unknown op
        self.applies_write = False   # the non-finite write lands
        self.applies_step = False    # the non-finite step lands
        self.wrong_refusal = False   # refusals answer off-contract
        self.fences_probes = False   # probes meet the fencing verdict
        self.stores_despite_refusal = False  # refused, but poisoned
        self.moved_despite_refusal = False  # refused, but the finite
                                            # stored value moved
        self.poisons_neighbor = False  # the refused write poisons an
                                       # undriven point, not its own
        self.fences_holder = False   # mutations fence a live holder
        self.fences_steps = False    # finite steps alone fence
        self.step_no_advance = False  # stepped answers a static tick
        self.diverges_followup = False  # done write, stale read-back
        self.unrestored = False      # the restore write lies
        self.poisoned_peer = False   # the follow-up step leaves the
                                     # accumulator unspellable

    @staticmethod
    def _invalid(detail):
        return {'result': 'error',
                'error': {'kind': 'invalid_request', 'detail': detail}}

    def _fenced(self):
        return {'result': 'error',
                'error': {'kind': 'io',
                          'error': {'fenced': self.owner}}}

    @staticmethod
    def _nonfinite_float(value):
        """Whether a request value dict spells the float leg with a
        payload the field cannot hold — non-finite or null."""
        if not isinstance(value, dict) or 'float' not in value:
            return False
        raw = value.get('float')
        return not isinstance(raw, (int, float)) \
            or isinstance(raw, bool) or not math.isfinite(raw)

    def _claim_check(self):
        """The mutation gate's answer for the scenario attachment, or
        None while its hold stands."""
        if not self.writer_granted \
                or (self.fences_holder and self.probed):
            if self.holders:
                return self._fenced()
            return {'result': 'error', 'error': {
                'kind': 'unclaimed',
                'detail': 'no writer claim stands'}}
        return None

    def dispatch(self, request):
        op = request.get('op')
        if op in ('ensure_writer', 'claim_writer'):
            self.requests.append(request)
            if self.predates:
                return self._invalid('unknown op')
            owner = request.get('owner')
            if self.refuse_claim or owner != self.owner:
                return {'result': 'error', 'error': {
                    'kind': 'fenced',
                    'detail': 'the field is owned by another '
                              'attachment'}}
            shared = bool(self.holders)
            self.holders.add('scenario')
            self.writer_granted = True
            return {'result': 'claimed_shared' if shared else 'done',
                    'owner': owner}
        if op == 'release_writer':
            self.requests.append(request)
            self.holders.discard('scenario')
            self.writer_granted = False
            return {'result': 'done'}
        if op == 'write':
            self.requests.append(request)
            point = request.get('point')
            value = request.get('value')
            # The parse boundary precedes the claim check on the wire
            # — a non-finite payload dies before any fencing verdict.
            if self._nonfinite_float(value):
                self.probed = True
                if self.fences_probes:
                    return self._fenced()
                if self.applies_write:
                    self.samples[point]['value'] = value
                    return {'result': 'done'}
                if self.stores_despite_refusal:
                    # The refused-by-name answer masking the stored
                    # {"float":null} frame — the defect the leg
                    # exists to catch.
                    self.samples[point]['value'] = {'float': None}
                if self.moved_despite_refusal:
                    # Refused by name yet the stored value still
                    # moved — the answer/state disagreement.
                    self.samples[point]['value'] = {'float': 4.75}
                if self.poisons_neighbor:
                    self.samples[10]['value'] = {'float': None}
                if self.wrong_refusal:
                    return {'result': 'error', 'error': {
                        'kind': 'io',
                        'error': {'unknown_point': point}}}
                return self._invalid('number out of range at the '
                                     'payload\'s float literal')
            refused = self._claim_check()
            if refused is not None:
                return refused
            if point not in self.samples:
                return {'result': 'error', 'error': {
                    'kind': 'io', 'error': {'unknown_point': point}}}
            self.finite_writes += 1
            if self.diverges_followup and self.probed \
                    and self.finite_writes == 1:
                return {'result': 'done'}       # applied verdict,
                                                # stale store
            if self.unrestored and self.probed \
                    and self.finite_writes >= 2:
                # Only the restore write — the last the leg issues —
                # lies.
                return {'result': 'done'}
            self.samples[point]['value'] = value
            return {'result': 'done'}
        if op == 'step':
            self.requests.append(request)
            dt = request.get('dt')
            if not isinstance(dt, (int, float)) or isinstance(dt, bool) \
                    or not math.isfinite(dt) or dt < 0:
                self.probed = True
                if self.fences_probes:
                    return self._fenced()
                if self.applies_step:
                    self.plant_tick += 1
                    return {'result': 'stepped',
                            'tick': self.plant_tick}
                if self.wrong_refusal:
                    return {'result': 'error', 'error': {
                        'kind': 'io', 'error': {'unknown_point': 0}}}
                return self._invalid('step dt must be finite and '
                                     'non-negative, got ' + str(dt))
            refused = self._claim_check()
            if refused is not None:
                return refused
            if self.fences_steps and self.probed:
                return self._fenced()
            tick = self.plant_tick
            if not self.step_no_advance:
                self.plant_tick += 1
            # The driven accumulator: inflow integrates into the
            # level — holding the last finite state marked
            # bad:out_of_range when the arithmetic would overflow,
            # never storing a non-finite.
            inflow = self.samples[12]['value'].get('float', 0.0)
            level = self.samples[10]['value'].get('float', 0.0)
            y = level + inflow * dt
            if math.isfinite(y):
                self.samples[10]['value']['float'] = y
                self.samples[10]['quality'] = 'good'
            else:
                self.samples[10]['quality'] = {'bad': 'out_of_range'}
            if self.poisoned_peer and self.probed:
                self.samples[10]['value'] = {'float': None}
            return {'result': 'stepped', 'tick': self.plant_tick}
        return super().dispatch(request)

    def ctl(self, *args):
        """The ctx['plant_ctl'] seam — FakePlantPeer's wire exchange
        plus the shipped client's strict decode: an answer carrying a
        non-finite or null float payload is the `{"float":null}` frame
        the tool cannot print, surfacing as its nonzero exit."""
        result = super().ctl(*args)
        if result.returncode == 0 \
                and self._undecodable(json.loads(result.stdout)):
            return _ctl_process(
                stderr='dcs-plant-ctl: cannot decode the server '
                       'response: non-finite float payload',
                returncode=1)
        return result

    @staticmethod
    def _undecodable(body):
        """Whether a served answer carries a sample the protocol's
        own client cannot parse — a float payload that is null or
        non-finite."""
        samples = []
        if isinstance(body, dict):
            if isinstance(body.get('sample'), dict):
                samples.append(body['sample'])
            for entry in body.get('points') or []:
                if isinstance(entry, dict) \
                        and isinstance(entry.get('sample'), dict):
                    samples.append(entry['sample'])
        for sample in samples:
            value = sample.get('value')
            if isinstance(value, dict) and 'float' in value:
                raw = value.get('float')
                if not isinstance(raw, (int, float)) \
                        or isinstance(raw, bool) \
                        or not math.isfinite(raw):
                    return True
        return False


class NonfiniteFeed:
    """A stubbed monitor pair for the nonfinite-refusal scenario:
    ctrl-a active, ctrl-b tracking standby; every `http_json` call is
    one completed scan on the addressed peer — unless the stall
    doctors freeze it once the plant saw the probes. /signals names
    the rig's 'inflow' forcing input and /snapshot serves the scan
    tick, the plant points' stored samples, and a clean io_health
    block. Doctor flags stage each named failure the issue calls
    out."""

    POINTS = (10, 12, 40)

    def __init__(self, plant):
        self.plant = plant
        self.ticks = {'ctrl-a:1': 0, 'ctrl-b:2': 0}
        # Fault injection for the named-failure cases.
        self.no_active = False       # neither peer reports active
        self.pair_down = False       # neither monitor answers
        self.no_tracking = False     # the peer never converges
        self.stall_active = False    # the probes freeze ctrl-a's scans
        self.stall_peer = False      # ... or the tracking peer's
        self.role_moves = False      # the probes move the active role
        self.bare_signals = False    # the census names no inflow
        self.io_counted = False      # the owner's io_health counts
                                   # the refused payloads

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        if self.pair_down:
            raise urllib.error.URLError('connection refused')
        peer_b = host.startswith('ctrl-b')
        stalled = (self.stall_peer if peer_b else self.stall_active) \
            and self.plant.probed
        if not stalled:
            self.ticks[host] += 1
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            if peer_b:
                sync = {'unsynchronized': {}} if self.no_tracking \
                    else {'tracking': {'aligned': self.ticks[host]}}
                return 200, {'role': 'standby',
                             'tick': self.ticks[host], 'sync': sync}
            role = 'standby' if self.no_active \
                or (self.role_moves and self.plant.probed) \
                else 'active'
            return 200, {'role': role, 'tick': self.ticks[host]}
        if (method, route) == ('GET', '/signals'):
            points = [
                {'point': 10, 'signal': 10010, 'name': 'level',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False},
                {'point': 40, 'signal': 10040, 'name': 'p101-run',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': False}]
            if not self.bare_signals:
                points.insert(0, {
                    'point': 12, 'signal': 10012, 'name': 'inflow',
                    'direction': 'in', 'value_type': 'float',
                    'writable': False})
            return 200, {'points': points, 'components': []}
        if (method, route) == ('GET', '/snapshot'):
            failed = 1 if self.io_counted and self.plant.probed else 0
            return 200, {
                'tick': self.ticks[host],
                'points': [{'point': p, 'direction': 'in',
                            'sample': dict(self.plant.samples[p],
                                           tick=self.ticks[host])}
                           for p in self.POINTS],
                'io_health': {'failed_reads': failed,
                              'failed_writes': 0,
                              'consecutive_failures': 0,
                              'last_error': None,
                              'scan_overruns': 0,
                              'driver': {'link': 'connected'}}}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class NonfiniteRefusalTests(unittest.TestCase):
    """scenario_nonfinite_refusal against the stubbed rig: the fake
    plant mirrors the named-refusal contract — non-finite write and
    step payloads die at the boundary as invalid_request while the
    standing claim holds the scenario's attachment — and each doctor
    flag stages a named acceptance failure or an inconclusive rig."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = NonfinitePlantPeer(self.OWNER)
        self.feed = NonfiniteFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self, plant=None):
        plant = plant or self.plant
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': plant.address,
                'plant_ctl': plant.ctl,
                'plant_owner': {'active': self.OWNER,
                                'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'NONFINITE_DEADLINE', 3.0):
            return scenarios.scenario_nonfinite_refusal(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_nonfinite_refusal, order)
        # The restored pre-switch window the field-claim leg
        # re-establishes — ahead of the tune case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_field_claim),
            order.index(scenarios.scenario_nonfinite_refusal))
        self.assertLess(
            order.index(scenarios.scenario_nonfinite_refusal),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('nonfinite-refusal'),
                      scenarios.scenario_nonfinite_refusal)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        for name in ('nonfinite-refusal-claim.json',
                     'nonfinite-refusal-before.json',
                     'nonfinite-refusal-probes.json',
                     'nonfinite-refusal-decoding.json',
                     'nonfinite-refusal-followup.json',
                     'nonfinite-refusal-restored.json',
                     'nonfinite-refusal-pair.json'):
            self.assertTrue((self.evidence / name).exists(), name)
        # The documented request surface: the shared claim, the dt:0
        # canary, the two refused probes, the finite write+step
        # follow-up, the restore write, and the claim release — the
        # reads and the census riding the tool seam.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('ensure_writer', ops)
        self.assertIn('release_writer', ops)
        # The refused write probe, the follow-up finite write, and the
        # restore write; the dt:0 canary, the refused step probe, and
        # the follow-up finite step.
        self.assertEqual(ops.count('write'), 3)
        self.assertEqual(ops.count('step'), 3)
        probes = [r for r in self.plant.requests
                  if r.get('op') == 'write']
        self.assertFalse(
            math.isfinite(probes[0]['value']['float']))
        # The driven point restored and the claim released.
        self.assertEqual(
            self.plant.samples[12]['value'], {'float': 0.0})
        self.assertNotIn('scenario', self.plant.holders)
        self.assertFalse(self.plant.writer_granted)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = NonfinitePlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = NonfiniteFeed(plant2)
        ctx2 = self._ctx(plant=plant2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_applied_write_fails(self):
        # The defect class: the non-finite write lands instead of
        # meeting the named refusal.
        self.plant.applies_write = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('write was applied', record['detail'])
        report.validate_scenario(record)

    def test_write_wrong_refusal_fails(self):
        self.plant.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('off-contract', record['detail'])
        report.validate_scenario(record)

    def test_applied_step_fails(self):
        self.plant.applies_step = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('step was applied', record['detail'])
        report.validate_scenario(record)

    def test_step_wrong_refusal_fails(self):
        self.plant.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        report.validate_scenario(record)

    def test_poisoned_read_fails(self):
        # The subtle defect: the write answers the named refusal yet
        # still leaves the {"float":null} frame behind — the shipped
        # client's decode is what reports it.
        self.plant.stores_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('deserialize', record['detail'])
        report.validate_scenario(record)

    def test_poisoned_census_fails(self):
        # The refused payloads left an undriven point serving the
        # unspellable frame — the list_points leg catches what the
        # single-point read misses.
        self.plant.poisons_neighbor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        report.validate_scenario(record)

    def test_refused_write_moved_field_fails(self):
        # The write refused by name but the stored value still moved —
        # answer and state disagreeing is the nondeterminism class.
        self.plant.moved_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-nondeterministic',
                      record['detail'])
        self.assertIn('moved', record['detail'])
        report.validate_scenario(record)

    def test_followup_write_refused_fails(self):
        self.plant.fences_holder = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('finite write', record['detail'])
        report.validate_scenario(record)

    def test_followup_step_refused_fails(self):
        # The step op alone fences the holder — the write lands, the
        # step does not.
        self.plant.fences_steps = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('finite step', record['detail'])
        report.validate_scenario(record)

    def test_followup_not_landing_fails(self):
        # The finite write answers done but the point reads the old
        # value — the applied answer disagreeing with the field.
        self.plant.diverges_followup = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-nondeterministic',
                      record['detail'])
        report.validate_scenario(record)

    def test_static_step_tick_fails(self):
        # The finite step answers stepped yet the plant's tick never
        # moved — the answer disagreeing with the state it reports.
        self.plant.step_no_advance = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-nondeterministic',
                      record['detail'])
        self.assertIn('never advanced', record['detail'])
        report.validate_scenario(record)

    def test_poisoned_after_followup_fails(self):
        # The follow-up step surfaces a poisoned accumulator the
        # refused probes never stored — the post-step census catches
        # it.
        self.plant.poisoned_peer = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        report.validate_scenario(record)

    def test_unrestored_image_fails(self):
        self.plant.unrestored = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        report.validate_scenario(record)
        self.assertIn('nonfinite-refusal', record['detail'])

    def test_role_move_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('role', record['detail'])
        report.validate_scenario(record)

    def test_stalled_scans_fail(self):
        # Either peer freezing its scans under the refused payloads —
        # the field owner's and the tracking standby's alike.
        for flag in ('stall_active', 'stall_peer'):
            with self.subTest(flag=flag):
                plant = NonfinitePlantPeer(self.OWNER)
                self.addCleanup(plant.close)
                feed = NonfiniteFeed(plant)
                setattr(feed, flag, True)
                record = self.run_scenario(ctx=self._ctx(plant=plant),
                                           feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('nonfinite-refusal-failed',
                              record['detail'])
                self.assertIn('scans stalled', record['detail'])
                report.validate_scenario(record)

    def test_io_health_counted_fails(self):
        self.feed.io_counted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('nonfinite-refusal-failed', record['detail'])
        self.assertIn('io_health', record['detail'])
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_is_inconclusive(self):
        self.feed.pair_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_no_tracking_is_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record['detail'])
        report.validate_scenario(record)

    def test_no_owner_token_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_owner']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant-writer owner token',
                      record['detail'])
        report.validate_scenario(record)

    def test_refused_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('refused the shared attachment',
                      record['detail'])
        report.validate_scenario(record)

    def test_predating_rig_is_inconclusive(self):
        # A rig predating the refusal contract: the shared-claim op
        # reads as an unknown op — the leg cannot attach.
        self.plant.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predating', record['detail'])
        report.validate_scenario(record)

    def test_no_float_point_is_inconclusive(self):
        # A census with no finite-float in-point gives the leg no
        # driven point to write.
        self.plant.samples = {
            40: {'value': {'bool': False}, 'quality': 'good',
                 'tick': 0}}
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('finite float', record['detail'])
        report.validate_scenario(record)

    def test_fenced_probes_are_inconclusive(self):
        # The granted claim never covered the attachment — the probes
        # met the fencing verdict, not the value contract.
        self.plant.fences_probes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('fencing verdict', record['detail'])
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
