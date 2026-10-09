"""The 0760_step_bound leg's scenario unit coverage — the feed fakes and
TestCase classes for scenario_step_bound, following the per-leg split
convention (#940): one scenario module plus one test module, no
shared-file edits. The shared fakes and helpers live in
tests/qa_scenario_support.py.
"""
import importlib
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class StepBoundPlantPeer(FakePlantPeer):
    """The step-bound rig's plant half: FakePlantPeer plus the
    write-ownership claim (`ensure_writer`/`release_writer`) and the
    `write`/`step` ops the scenario drives over the shared-claim path —
    mirroring the post-#683 sim-net contract, where a step `dt` that
    is not finite, is negative, or is above `MAX_STEP_DT` dies at the
    dispatched boundary as the named `invalid_request` refusal naming
    the bound, and stores nothing. The standing owner is pre-seeded
    with a live holder so the scenario's attachment answers
    `claimed_shared`, and an integrator over the inflow point gives the
    leg the driven accumulator an over-bound step would have wound past
    the f64 range. Doctor flags stage each named failure and each
    inconclusive rig the scenario reports."""

    def __init__(self, owner):
        super().__init__()
        # The pump-station shape the deployed dynamics document
        # carries: the level integrator's output, the forcing input the
        # harness legs write, and a bool point.
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
        self.probed = False             # an over-bound step arrived
        self.finite_writes = 0          # finite writes since probing
        # Doctor flags for the named-failure cases.
        self.refuse_claim = False    # ensure_writer answers fenced
        self.predates = False        # the claim ops answer unknown op
        self.applies_over_bound = False   # an over-bound step lands
        self.wrong_refusal = False   # refusals answer off-contract
        self.fences_probes = False   # probes meet the fencing verdict
        self.bare_detail = False     # refused, but the bound unnamed
        self.moves_despite_refusal = False  # the driven undriven
                                            # point moved anyway
        self.poisons_read = False    # the read serves {"float":null}
        self.poisons_census = False  # ... or a neighbour point does
        self.poisons_followup = False  # the finite step leaves one
        self.fences_steps = False    # the finite follow-up fences
        self.step_no_advance = False  # stepped answers a static tick
        self.diverges_followup = False  # done write, stale read-back
        self.unrestored = False      # the restore write lies
        self.claims_instead = False  # the probe answers the claim
                                     # verdict under a different kind

    @staticmethod
    def _invalid(detail):
        return {'result': 'error',
                'error': {'kind': 'invalid_request', 'detail': detail}}

    def _fenced(self):
        return {'result': 'error',
                'error': {'kind': 'io',
                          'error': {'fenced': self.owner}}}

    def _bound_refusal(self, dt):
        """The dispatched bound's refusal — the documented detail,
        unless the doctor strips it."""
        if self.bare_detail:
            return self._invalid('step dt is out of range, got '
                                 + str(dt))
        return self._invalid(
            'step dt must be finite, non-negative, and at most '
            '1000000, got ' + str(dt))

    def _claim_check(self):
        """The mutation gate's answer for the scenario attachment, or
        None while its hold stands."""
        if not self.writer_granted or (self.fences_steps and self.probed):
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
            refused = self._claim_check()
            if refused is not None:
                return refused
            if point not in self.samples:
                return {'result': 'error', 'error': {
                    'kind': 'io', 'error': {'unknown_point': point}}}
            self.finite_writes += 1
            if self.unrestored:
                # The only write this leg issues is the restore: a done
                # answer the field does not read back — the applied
                # verdict and the stored value disagreeing.
                self.samples[point]['value'] = {'float': 1.25}
                return {'result': 'done'}
            self.samples[point]['value'] = value
            return {'result': 'done'}
        if op == 'step':
            self.requests.append(request)
            dt = request.get('dt')
            if not isinstance(dt, (int, float)) or isinstance(dt, bool) \
                    or not math.isfinite(dt) or dt < 0 or dt > 1.0e6:
                # The documented bound, checked ahead of the claim the
                # way the server checks it — a malformed dt dies before
                # any fencing verdict.
                self.probed = True
                if self.fences_probes:
                    return self._fenced()
                if self.applies_over_bound:
                    return self._applied(dt)
                if self.wrong_refusal:
                    return {'result': 'error', 'error': {
                        'kind': 'io', 'error': {'unknown_point': 0}}}
                if self.claims_instead:
                    return {'result': 'error', 'error': {
                        'kind': 'unclaimed',
                        'detail': 'no writer claim stands'}}
                if self.moves_despite_refusal:
                    # Refused by name, yet the driven point's stored
                    # value moved anyway — the answer and the field
                    # state disagreeing.
                    self.samples[12]['value'] = {'float': 4.75}
                return self._bound_refusal(dt)
            refused = self._claim_check()
            if refused is not None:
                return refused
            tick = self.plant_tick
            if not self.step_no_advance:
                self.plant_tick += 1
            # The driven accumulator: inflow integrates into the level,
            # holding the last finite state marked bad:out_of_range when
            # the arithmetic would overflow — never storing a
            # non-finite. An over-bound dt, had it been applied, would
            # have wound it out of the range instead.
            inflow = self.samples[12]['value'].get('float', 0.0)
            level = self.samples[10]['value'].get('float', 0.0)
            y = level + inflow * dt
            if math.isfinite(y):
                self.samples[10]['value']['float'] = y
                self.samples[10]['quality'] = 'good'
            else:
                self.samples[10]['quality'] = {'bad': 'out_of_range'}
            if self.poisons_followup and self.probed:
                self.samples[10]['value'] = {'float': None}
            return {'result': 'stepped', 'tick': self.plant_tick}
        return super().dispatch(request)

    def _applied(self, dt):
        """A rig predating the bound: the over-bound advance lands —
        and winds the accumulator the way the finding recorded."""
        self.plant_tick += 1
        inflow = self.samples[12]['value'].get('float', 0.0)
        level = self.samples[10]['value'].get('float', 0.0)
        y = level + inflow * dt
        self.samples[10]['value']['float'] = y \
            if math.isfinite(y) else None
        return {'result': 'stepped', 'tick': self.plant_tick}

    def served(self, point):
        # The poison doctors only bite once the probes have landed: the
        # pre-probe census the leg resolves its driven point from must
        # still decode, or the leg would never reach the contract.
        sample = super().served(point)
        if self.probed and point == 12 and self.poisons_read:
            sample = dict(sample, value={'float': None})
        if self.probed and point == 10 and self.poisons_census:
            sample = dict(sample, value={'float': None})
        return sample

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
        """Whether a served answer carries a sample the protocol's own
        client cannot parse — a float payload that is null or
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


class StepBoundFeed:
    """A stubbed monitor pair for the step-bound scenario: ctrl-a
    active, ctrl-b tracking standby; every `http_json` call is one
    completed scan on the addressed peer — unless the stall doctors
    freeze it once the field saw the probes. /signals names the rig's
    'inflow' forcing input and /snapshot serves the scan tick, the
    plant points' stored samples, and a clean io_health block. Doctor
    flags stage each named failure the issue calls out."""

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
                                   # the refused probes

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


class StepBoundTests(unittest.TestCase):
    """scenario_step_bound against the stubbed rig: the fake plant
    mirrors the bounded-step contract — an over-bound dt dies at the
    dispatched boundary as invalid_request naming the bound while the
    standing claim holds the scenario's attachment — and each doctor
    flag stages a named acceptance failure or an inconclusive rig."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = StepBoundPlantPeer(self.OWNER)
        self.feed = StepBoundFeed(self.plant)

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
                patch.object(scenarios, 'STEP_BOUND_DEADLINE', 3.0):
            return scenarios.scenario_step_bound(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_step_bound, order)
        # The restored pre-switch window the nonfinite-refusal case
        # leaves, ahead of the fenced-writer case's misordered promote.
        self.assertLess(
            order.index(scenarios.scenario_nonfinite_refusal),
            order.index(scenarios.scenario_step_bound))
        self.assertLess(
            order.index(scenarios.scenario_step_bound),
            order.index(scenarios.scenario_fenced_writer_degrade))
        self.assertIs(verify.case_function('step-bound'),
                      scenarios.scenario_step_bound)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        for name in ('step-bound-claim.json',
                     'step-bound-before.json',
                     'step-bound-probe-over.json',
                     'step-bound-probe-huge.json',
                     'step-bound-decoding.json',
                     'step-bound-followup.json',
                     'step-bound-restored.json',
                     'step-bound-pair.json'):
            self.assertTrue((self.evidence / name).exists(), name)
        # The documented request surface: the shared claim, the dt:0
        # canary, the two refused over-bound steps, the finite follow-up
        # step, and the restore write — the reads and the census riding
        # the tool seam.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('ensure_writer', ops)
        self.assertIn('release_writer', ops)
        self.assertEqual(ops.count('write'), 1)
        self.assertEqual(ops.count('step'), 4)
        # Both probes carried a dt above the documented bound, and
        # neither advanced the plant tick.
        steps = [request for request in self.plant.requests
                 if request.get('op') == 'step']
        self.assertEqual([step['dt'] for step in steps],
                         [0, 1.0e7, 1.0e308, 0.25])
        # Both probes were refused before the field moved: the two
        # accepted steps are the dt:0 canary and the finite follow-up.
        self.assertEqual(self.plant.plant_tick, 2)
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
        plant2 = StepBoundPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = StepBoundFeed(plant2)
        ctx2 = self._ctx(plant=plant2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_the_probes_run_over_bound_first(self):
        # A rig predating the bound must absorb the ordinary over-bound
        # advance, never the finding's 1e308 vector: with the flag the
        # first probe lands, and the leg reports inconclusive before the
        # huge dt ever rides the wire.
        self.plant.applies_over_bound = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates the bounded step contract',
                      record['detail'])
        report.validate_scenario(record)
        steps = [request['dt'] for request in self.plant.requests
                 if request.get('op') == 'step']
        self.assertEqual(steps, [0, 1.0e7])

    def test_off_contract_refusal_fails(self):
        # Neither stepped nor the named bound refusal — an answer the
        # contract does not have.
        self.plant.wrong_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        self.assertIn('off-contract', record['detail'])
        report.validate_scenario(record)

    def test_poisoned_read_fails(self):
        # The subtle defect: the refusals answer by name yet the driven
        # point still serves the unspellable frame — the shipped
        # client's decode is what reports it.
        self.plant.poisons_read = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        self.assertIn('deserialize', record['detail'])
        report.validate_scenario(record)

    def test_poisoned_census_fails(self):
        # A neighbour point serves the poisoned frame — the census scan
        # catches what the single-point read misses.
        self.plant.poisons_census = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        report.validate_scenario(record)

    def test_moved_despite_refusal_fails(self):
        # The steps refused by name yet the driven undriven point's
        # stored value still moved — answer and state disagreeing is
        # the nondeterminism class.
        self.plant.moves_despite_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-nondeterministic', record['detail'])
        self.assertIn('moved', record['detail'])
        report.validate_scenario(record)

    def test_followup_step_refused_fails(self):
        self.plant.fences_steps = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        self.assertIn('wedged the step path', record['detail'])
        report.validate_scenario(record)

    def test_static_step_tick_fails(self):
        # The finite step answers stepped yet the plant's tick never
        # moved — the answer disagreeing with the state it reports.
        self.plant.step_no_advance = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-nondeterministic', record['detail'])
        self.assertIn('never advanced', record['detail'])
        report.validate_scenario(record)

    def test_unrestored_image_fails(self):
        self.plant.unrestored = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound', record['detail'])
        report.validate_scenario(record)

    def test_role_move_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        self.assertIn('role', record['detail'])
        report.validate_scenario(record)

    def test_stalled_scans_fail(self):
        # Either peer freezing its scans under the refused probes — the
        # field owner's and the tracking standby's alike.
        for flag in ('stall_active', 'stall_peer'):
            with self.subTest(flag=flag):
                plant = StepBoundPlantPeer(self.OWNER)
                self.addCleanup(plant.close)
                feed = StepBoundFeed(plant)
                setattr(feed, flag, True)
                record = self.run_scenario(ctx=self._ctx(plant=plant),
                                           feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('step-bound-failed', record['detail'])
                self.assertIn('scans stalled', record['detail'])
                report.validate_scenario(record)

    def test_io_health_counted_fails(self):
        self.feed.io_counted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-failed', record['detail'])
        self.assertIn('io_health', record['detail'])
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
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
        self.assertIn('no plant-writer owner token', record['detail'])
        report.validate_scenario(record)

    def test_refused_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('refused the shared attachment', record['detail'])
        report.validate_scenario(record)

    def test_predating_rig_is_inconclusive(self):
        # A rig predating the shared-claim seam: the ensure op reads as
        # an unknown op — the leg cannot attach.
        self.plant.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predating', record['detail'])
        report.validate_scenario(record)

    def test_no_float_point_is_inconclusive(self):
        # A census with no finite-float in-point gives the leg no
        # driven point to watch.
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
        # met the fencing verdict, not the bound contract.
        self.plant.fences_probes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('met the fenced claim verdict', record['detail'])
        report.validate_scenario(record)

    def test_the_refusal_classification_names_the_bound(self):
        leg = importlib.import_module('qa_lane.scenarios.0760_step_bound')
        detail = ('step dt must be finite, non-negative, and at most '
                  '1000000, got 1e+308')
        named = {'result': 'error',
                 'error': {'kind': 'invalid_request', 'detail': detail}}
        self.assertEqual(leg._bound_refusal(named), 'bound')
        # The kind alone still counts as a refusal — a release whose
        # wording differs is refused, not failed.
        bare = {'result': 'error',
                'error': {'kind': 'invalid_request',
                          'detail': 'step dt out of range'}}
        self.assertEqual(leg._bound_refusal(bare), 'invalid_request')
        # Applied is not a refusal: that is how a pre-bound rig is
        # recognized, and the claim verdicts are separate again.
        self.assertIsNone(leg._bound_refusal({'result': 'stepped',
                                              'tick': 7}))
        self.assertIsNone(leg._bound_refusal(
            {'result': 'error', 'error': {'kind': 'fenced'}}))
        self.assertEqual(leg._claim_verdict(
            {'result': 'error', 'error': {'kind': 'unclaimed'}}),
            'unclaimed')
        self.assertIsNone(leg._claim_verdict(named))
        self.assertTrue(leg._ctl_decodes({'result': 'sample'}, 'sample'))
        self.assertFalse(leg._ctl_decodes({'result': 'error'}, 'sample'))

    def test_the_float_payload_scans(self):
        leg = importlib.import_module('qa_lane.scenarios.0760_step_bound')
        self.assertEqual(leg._finite_float({'float': 1.5}), 1.5)
        self.assertIsNone(leg._finite_float({'float': None}))
        self.assertIsNone(leg._finite_float({'float': True}))
        self.assertIsNone(leg._finite_float({'bool': False}))
        entries = [
            {'point': 10, 'sample': {'value': {'float': 0.8}}},
            {'point': 12, 'sample': {'value': {'float': None}}},
            {'point': 40, 'sample': {'value': {'bool': False}}},
        ]
        self.assertEqual(leg._poisoned_entry(entries), entries[1])
        self.assertIsNone(leg._poisoned_entry(entries[:1]))
        # The documented bound the probes ride above.
        self.assertEqual(leg.STEP_BOUND, 1.0e6)
        self.assertGreater(leg.OVER_BOUND_DT, leg.STEP_BOUND)
        self.assertGreater(leg.HUGE_DT, leg.OVER_BOUND_DT)

    def test_unchecked_self_check_fails(self):
        # An audit that names nothing slips every planted negative — the
        # leg reports itself unchecked rather than passing a rig it can
        # no longer trust to catch what it names.
        with patch.object(scenarios, '_step_bound_violation',
                          lambda record, phase: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('step-bound-unchecked', record['detail'])
        self.assertIn('read-moved', record['detail'])
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        # Every planted negative the leg can stage names the diagnostic
        # it must — the self-check slips nothing.
        self.assertEqual(scenarios._step_bound_self_check(), [])


if __name__ == '__main__':
    unittest.main()