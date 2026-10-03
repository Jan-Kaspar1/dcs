"""The 2499_stale_budget_cadence leg's scenario unit coverage — the feed
fake and TestCase class for scenario_stale_budget_cadence, split out per
the leg-module convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape: the deployed pair owns a sim-tcp field
and never moves, while the born legs' scratch field serves the three
born seats through the born launcher's `--remote` seam — the field owner
declaring no pair at the documented cadence, a control reader declaring
`--standby <owner>` at that same cadence, and the subject reader
declaring `--standby <owner>` at the per-container `scan_ms` cadence the
leg drives. A seat's run tick accrues one per scan and every served read
stands in for one scan batch, so the subject seat's ticks really do run
ten per owner scan on the rig's own clock. The field publishes a fresh
value for both probes on every step, so the reader's served publications
are one per owner scan and the arrival gap it demonstrates is exactly the
cadence asymmetry the leg stages. Every scan judges the declared-budget
input the way #1411's fix declares — the declared budget widened to the
arrival period the reader itself demonstrated — and records the
quality_changed transitions on the reader's runner-owned journal file.
By default the frozen field ages the control reader to stale and the
subject reader stays Good; every fault flag stages one named defect, one
nondeterministic surface, or one rig shape the leg declines on."""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'StaleBudgetCadenceTests.test_registered',
    'StaleBudgetCadenceTests.test_clean_passes_validates_and_sweeps',
    'StaleBudgetCadenceTests.test_two_passes_produce_identical_digests',
    'StaleBudgetCadenceTests.test_reader_ticks_only_verdict_fails',
    'StaleBudgetCadenceTests.test_frozen_field_never_stale_fails',
    'StaleBudgetCadenceTests.test_declared_stale_never_journaled_fails',
    'StaleBudgetCadenceTests.test_budget_leaking_onto_the_witness_fails',
    'StaleBudgetCadenceTests.test_journal_flapping_past_the_evidence_fails',
    'StaleBudgetCadenceTests.test_starvation_asserted_fresh_fails',
    'StaleBudgetCadenceTests.test_stale_before_induction_fails',
    'StaleBudgetCadenceTests.test_diverging_digests_fail',
    'StaleBudgetCadenceTests.test_field_stage_failures_are_nondeterministic',
    'StaleBudgetCadenceTests.test_launch_failures_are_nondeterministic',
    'StaleBudgetCadenceTests.test_owner_never_claims_is_nondeterministic',
    'StaleBudgetCadenceTests.test_readers_never_converge_is_nondeterministic',
    'StaleBudgetCadenceTests.test_control_pace_off_parity_is_nondeterministic',
    'StaleBudgetCadenceTests.test_pace_never_staged_is_nondeterministic',
    'StaleBudgetCadenceTests.test_freeze_never_lands_is_nondeterministic',
    'StaleBudgetCadenceTests.test_arrival_never_arrives_is_nondeterministic',
    'StaleBudgetCadenceTests.test_empty_judged_window_is_nondeterministic',
    'StaleBudgetCadenceTests.test_starved_watch_is_nondeterministic',
    'StaleBudgetCadenceTests.test_unreadable_journal_is_nondeterministic',
    'StaleBudgetCadenceTests.test_undeclared_probes_are_nondeterministic',
    'StaleBudgetCadenceTests.test_degraded_contrast_is_nondeterministic',
    'StaleBudgetCadenceTests.test_pair_disturbance_is_nondeterministic',
    'StaleBudgetCadenceTests.test_rig_left_standing_is_nondeterministic',
    'StaleBudgetCadenceTests.test_field_left_serving_is_nondeterministic',
    'StaleBudgetCadenceTests.test_field_presence_is_nondeterministic',
    'StaleBudgetCadenceTests.test_unreachable_rig_is_inconclusive',
    'StaleBudgetCadenceTests.test_unsettled_pair_is_inconclusive',
    'StaleBudgetCadenceTests.test_missing_seams_are_inconclusive',
    'StaleBudgetCadenceTests.test_unchecked_self_check_fails',
    'StaleBudgetCadenceTests.test_self_check_is_complete',
})


OWNER = 'revised'
CONTROL = 'foreign'
SUBJECT = 'driven'


class CadenceSeat:
    """One born controller as the freshness contract sees it: a run tick
    per scan, the field's reports as change evidence, the declared
    budget widened to the arrival period this seat has demonstrated, and
    a durable `quality_changed` trail for every transition the verdict
    made. `step` is the seat's scan batch — one served read stands for
    `BORN_SCAN_MS / scan_ms` scans, so a seat paced ten times faster
    really does accrue ten run ticks per owner scan."""

    def __init__(self, seat, scan_ms, standby):
        self.seat = seat
        self.scan_ms = scan_ms
        self.step = max(1, runner.BORN_SCAN_MS // scan_ms)
        self.standby = standby
        self.owns = False
        self.tracking = standby is not None
        self.tick = 0
        self.seq = 0
        self.serving = True
        self.rows = {}          # point -> [row], the served history
        self.observed = {}      # point -> the driver report last seen
        self.last_change = {}   # point -> the tick that report arrived
        self.gaps = {}          # point -> the two most recent arrival gaps
        self.quality = {}       # point -> the last journaled quality
        self.armed = {}         # point -> the cold-start seed
        self.publications = {}  # point -> the changes it has watched

    def scan(self, feed):
        """One scan batch: `step` scans, each refreshing both probes from
        the field's current reports and judging the declared-budget
        input against this seat's patience."""
        for _ in range(self.step):
            self.tick += 1
            for point in (CadenceFeed.BUDGETED, CadenceFeed.WITNESS):
                self._observe(feed, point)
        return self

    def _observe(self, feed, point):
        """One scan's read of a field input: the report the driver
        returned is change evidence, never a timestamp — a report that
        has not changed ages one run tick per scan, and stale lands once
        the age passes the patience."""
        stamp, value = feed.report(point)
        quality = 'good'
        publication = 0
        if point not in self.armed:
            # The cold start: the first observation seeds the age without
            # demonstrating any pace at all.
            self.armed[point] = self.tick
            self.last_change[point] = self.tick
            self.observed[point] = (stamp, value)
            self.gaps[point] = [0, 0]
        elif self.observed[point] != (stamp, value):
            # A changed report restarts the age and measures the gap
            # since the previous change — the same-domain run ticks the
            # arrival period is built from.
            self.gaps[point] = [self.gaps[point][1],
                                self.tick - self.last_change[point]]
            self.last_change[point] = self.tick
            self.observed[point] = (stamp, value)
            publication = self.publications.get(point, 0) + 1
            self.publications[point] = publication
        elif point in feed.budgeted_points() \
                and not feed.suppressed(self, point) \
                and self.tick - self.last_change[point] > feed.patience(
                    self, point):
            quality = 'uncertain:stale'
        self.seq += 1
        self.rows.setdefault(point, []).append(
            {'seq': self.seq, 'tick': self.tick, 'value': value,
             'quality': quality})
        if publication and point == CadenceFeed.BUDGETED \
                and self.seat == SUBJECT:
            feed.flap(self, point, self.tick)
        if feed.journaled:
            previous = self.quality.get(point)
            if previous != quality:
                self.quality[point] = quality
                feed.journal(self, point, previous, quality, self.tick)

    def serve(self, point):
        """The point's latest served sample, or None before its first
        scan."""
        rows = self.rows.get(point) or []
        return rows[-1] if rows else None


class CadenceFeed:
    """A stubbed rig for the declared-freshness cadence-domain leg.

    The deployed pair owns its own field and never moves; the born legs'
    scratch field serves the three born seats, each attaching through the
    born launcher's `--remote` seam with its own `--owner-token` pin, its
    own cadence, and its own runner-owned journal file. The field steps
    once per scan of whichever seat holds its write-ownership claim, and
    publishes a fresh value for both probes on every step — so stopping
    the owner freezes the field exactly as the single-writer claim
    freezes the deployed pair's. Every transition keys off the leg's
    lever calls so two passes emit identical digests; the fault flags
    stage each named defect, each nondeterministic surface, and each rig
    shape the leg declines on."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': OWNER, 'ctrl-f:4': CONTROL, 'ctrl-d:5': SUBJECT}
    FIELD = 'dcs-hw-qa-1-born-plant:9003'
    TOKENS = {OWNER: 424243, CONTROL: 424244, SUBJECT: 424245}
    SEATS = (OWNER, CONTROL, SUBJECT)
    BUDGETED, WITNESS = 13, 10
    BUDGET = 5                  # the model's declared stale_after_ticks
    SIGNALS = [{'point': BUDGETED, 'name': 'net-flow',
                'direction': 'in', 'value_type': 'float',
                'writable': False},
               {'point': WITNESS, 'name': 'level-primary',
                'direction': 'in', 'value_type': 'float',
                'writable': False}]

    def __init__(self, journals):
        self.tick = 900            # the deployed pair's scan tick
        self.field = None          # the scratch field, while it serves
        self.claim = None          # the seat holding the field's claim
        self.seats = {}            # seat -> CadenceSeat
        self.calls = []
        self.reads = {'ctrl-a:1': 0, 'ctrl-b:2': 0}
        self.journals = journals   # {seat: path} the runner owns
        self.written = {seat: [] for seat in self.SEATS}
        self.sweeps = 0            # completed teardown sweeps
        self.steps = 0             # the field's own step counter
        self.stamp = 0             # the stamp a publication carries
        self.journaled = True      # the recorder writes its transitions
        self.journal_vanishes = False   # the durable sink never lands
        self.starve_after = None   # the field stops stepping at this step
        # Doctors staging each named defect.
        self.reader_ticks_only = False   # the pre-fix judgment: the
                                         # declared budget alone
        self.frozen_never_stale = False  # the frozen field ages out no
                                         # stale verdict at all
        self.budget_leaks = False        # an undeclared point goes stale
        self.journal_flaps = False       # transitions past the verdict
        self.starved_but_good = False    # a real starvation the reader
                                         # never ages out
        # Doctors staging each nondeterministic surface.
        self.stage_fails = False         # start_born_field raises
        self.freeze_stuck = False        # the frozen field keeps stepping
        self.pace_ignored = False        # the cadence lever is dropped
        self.control_paced_fast = False  # the control reader is not paced
                                         # like the owner
        self.history_stalls = False      # the reader's samples stop
        self.field_starved = False       # the field never publishes
        self.silent_rig = False          # every endpoint refuses
        self.unsettled_pair = False      # the standby never tracks
        self.owner_never_claims = False  # no startup grant lands
        self.never_converges = False     # a reader never tracks
        self.watch_starves = False       # a born monitor goes quiet
        self.probes_undeclared = False   # the model declares neither
        self.degraded_contrast = False   # the witness starts degraded
        self.pair_moves = False          # the standby reports active
        self.pair_wedged = False         # the owner's tick freezes
        self.teardown_keeps = frozenset()  # seats that outlive the sweep
        self.field_left_serving = False  # the field survives the sweep

    # --- the freshness contract the leg judges ----------------------

    def report(self, point):
        """The field's current report for a point — `(stamp, value)`. A
        step publishes a new stamp and a new value, so a reader's report
        changes once per field step."""
        base = 0.2 if point == self.BUDGETED else 0.8
        drift = 0.05 if point == self.BUDGETED else 0.02
        return self.stamp, round(base + drift * self.steps, 6)

    def patience(self, seat, point):
        """The reader-tick bound this seat's verdict is measured against:
        the declared `stale_after_ticks`, widened to the arrival period
        the seat has demonstrated on the point. `reader_ticks_only` drops
        the widening — the pre-fix judgment, which reads the report's age
        in the reader's own ticks alone."""
        if self.reader_ticks_only:
            return self.BUDGET
        gaps = seat.gaps.get(point) or [0, 0]
        return max(self.BUDGET, max(gaps))

    def budgeted_points(self):
        """The points carrying a freshness judgment on this revision:
        the model's one declared `stale_after_ticks` point, and — under
        `budget_leaks` — the undeclared input beside it, the per-point
        declaration the leak doctoring breaks."""
        return (self.BUDGETED, self.WITNESS) if self.budget_leaks \
            else (self.BUDGETED,)

    def wire(self, quality):
        """A verdict in the served quality shape — the bare `good` stamp
        or the named `uncertain` reason every consumer reads."""
        if quality == 'good':
            return 'good'
        return {'uncertain': quality.split(':', 1)[-1]}

    def suppressed(self, seat, point):
        """Whether this seat's verdict on this point never ages out:
        `starved_but_good` stages the doctoring of a starvation the
        subject reader does not catch, and `frozen_never_stale` the
        over-widened patience a frozen field never ages past."""
        if self.frozen_never_stale and seat.seat == CONTROL:
            return True
        return self.starved_but_good and seat.seat == SUBJECT

    def scan_seat(self, seat):
        """One scan batch of a born seat — the field's writer steps the
        plant as it scans, exactly as the single-writer claim makes it,
        so a fast reader's served publications really are one per owner
        scan."""
        if seat.owns:
            self.step_field()
        return seat.scan(self)

    def step_field(self):
        """One scan batch of the field's writer: the plant steps once and
        both probes publish. A field with no live claim, a field that
        starves, and a frozen field that keeps stepping are the staged
        shapes the leg's freeze gate tells apart."""
        if self.claim is None or self.field_starved:
            return
        if self.starve_after is not None \
                and self.steps >= self.starve_after:
            return
        self.steps += 1
        self.stamp = self.steps

    def flap(self, seat, point, tick):
        """A `quality_changed` pair the recorder writes on every field
        step while the seat's served verdict holds — the recorded
        defect's durable half, journaled in the reader's own run ticks."""
        if not self.journal_flaps:
            return
        records = self.written[seat.seat]
        for before, after in (('uncertain:stale', 'uncertain:stale'),
                              ('uncertain:stale', 'good')):
            records.append({'entry': {'seq': len(records), 'tick': tick,
                                      'event': {'quality_changed': {
                                          'point': point,
                                          'from': self.wire(before),
                                          'to': self.wire(after)}}}})
        self._write_journals()

    def journal(self, seat, point, previous, quality, tick):
        """One `quality_changed` record for the seat's runner-owned
        `--journal-file` — `from` None on the point's first observed
        sample, the served quality shape on both ends — plus the staged
        journal that flaps past the verdict the served surface shows."""
        records = self.written[seat.seat]
        for before, after in [(previous, quality)]:
            records.append({'entry': {'seq': len(records), 'tick': tick,
                                      'event': {'quality_changed': {
                                          'point': point,
                                          'from': None if before is None
                                          else self.wire(before),
                                          'to': self.wire(after)}}}})
        self._write_journals()

    def _write_journals(self):
        for seat, records in self.written.items():
            path = self.journals.get(seat)
            if not path or self.journal_vanishes:
                continue
            Path(path).write_text(
                ''.join(json.dumps(record) + '\n'
                        for record in records))

    # --- the runner's born-field and born-seat levers, faked ---------

    def start_field(self, mode):
        self.calls.append(('start_born_field', mode))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self.field = 'dcs-hw-qa-1-born-plant'
        self.claim = None       # a fresh field's arbitration
        self.steps = 0
        self.stamp = 0
        return {'container': self.field, 'remote': self.FIELD,
                'mode': mode}

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        if not self.field_left_serving:
            self.field = None
        self.claim = None
        self.sweeps += 1

    def field_ctl(self, *args):
        self.calls.append(('born_field_ctl', args))
        serving = self.field is not None
        return subprocess.CompletedProcess(
            args=('dcs-plant-ctl',),
            returncode=0 if serving else 1,
            stdout=json.dumps({'result': 'points', 'points': []})
            if serving else '',
            stderr='' if serving else 'no such container')

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None, scan_ms=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document, scan_ms))
        if remote != self.FIELD or document is not None:
            raise AssertionError('a born launch on the scratch field '
                                 'is --remote-addressed: remote=%r '
                                 'document=%r' % (remote, document))
        self.written[seat] = []
        pace = scan_ms if scan_ms is not None else runner.BORN_SCAN_MS
        if self.pace_ignored:
            pace = runner.BORN_SCAN_MS
        elif self.control_paced_fast and seat == CONTROL:
            pace = runner.BORN_SCAN_MS // 10
        state = CadenceSeat(seat, pace, standby)
        self.seats[seat] = state
        if standby is None and not self.owner_never_claims:
            # A born-active declaring no pair takes an unclaimed field's
            # write-ownership claim through its conditional startup
            # grant.
            self.claim = seat
            state.owns = True
            state.tracking = False
        if standby is None:
            self.scan_seat(state)
        self._write_journals()
        return {'container': 'dcs-hw-' + seat, 'seat': seat,
                'address': 'dcs-hw-' + seat + ':'
                + str(runner.BORN_MONITOR_PORT),
                'remote': remote, 'peer': None, 'standby': standby,
                'model': '/run/qa-1/model.json', 'scan_ms': pace,
                'monitor': 'http://' + self._host(seat)}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        if seat in self.teardown_keeps:
            return          # a seat the sweep could not remove
        if self.claim == seat:
            if self.freeze_stuck and CONTROL in self.seats:
                # The staged freeze that never froze: a surviving peer
                # keeps the write-ownership claim, so the field's
                # stepping goes on after the owner is stopped.
                self.claim = CONTROL
                self.seats[CONTROL].owns = True
            else:
                self.claim = None  # the field's stepping stops with it
        self.seats.pop(seat, None)

    def state(self, seat):
        self.calls.append(('born_controller_state', seat))
        if seat not in self.seats:
            return {'container': 'dcs-hw-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        return {'container': 'dcs-hw-' + seat, 'running': True,
                'exit': None, 'logs': '', 'absent': False}

    def _host(self, seat):
        for host, name in self.HOSTS.items():
            if name == seat:
                return host
        return 'ctrl-' + seat + ':9'

    # --- the monitor channel — replaces scenarios.http_json ---------

    def _pair_role(self, host):
        self.reads[host] += 1
        reads = self.reads[host]
        if not (self.pair_wedged and reads > 1):
            self.tick += 1
        if host == 'ctrl-a:1':
            return {'role': 'active', 'tick': self.tick}
        if self.pair_moves and reads > 1:
            return {'role': 'active', 'tick': self.tick}
        report = {'role': 'standby', 'tick': self.tick}
        report['sync'] = {'degraded': {'reason': 'unsettled'}} \
            if self.unsettled_pair \
            else {'tracking': {'aligned': self.tick}}
        return report

    def _seat_role(self, seat, state):
        if state.owns:
            return {'role': 'active', 'field_claim': 'held',
                    'tick': state.tick}
        converged = state.tracking and not (
            self.never_converges and seat != OWNER)
        return {'role': 'standby', 'field_claim': self.claim,
                'tick': state.tick,
                'sync': {'tracking': {'aligned': state.tick}}
                if converged else 'unsynchronized'}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        if host in ('ctrl-a:1', 'ctrl-b:2'):
            if (method, route) == ('GET', '/role'):
                return 200, self._pair_role(host)
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        seat = self.HOSTS.get(host)
        state = self.seats.get(seat) if seat else None
        if state is None:
            raise urllib.error.URLError('connection refused')
        if self.watch_starves and seat == CONTROL \
                and route in ('/history', '/snapshot'):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            self.scan_seat(state)
            return 200, self._seat_role(seat, state)
        if (method, route) == ('GET', '/snapshot'):
            self.scan_seat(state)
            if self.history_stalls and seat == SUBJECT \
                    and self.steps > 3:
                # The reader's own samples stop landing while its posture
                # keeps answering: a judged window with no evidence.
                state.rows = {}
            points = []
            for point in (self.BUDGETED, self.WITNESS):
                row = state.serve(point)
                if row is None:
                    continue
                points.append({'point': point, 'sample': {
                    'value': {'float': row['value']},
                    'quality': self.wire(row['quality']),
                    'tick': row['tick']}})
            return 200, {'tick': state.tick, 'points': points}
        if (method, route) == ('GET', '/signals'):
            points = [] if self.probes_undeclared else self.SIGNALS
            return 200, {'points': points, 'components': []}
        if (method, route) == ('GET', '/history'):
            query = dict(part.split('=', 1)
                         for part in url.split('/', 3)[3]
                         .partition('?')[2].split('&'))
            point, since = int(query['point']), int(query['since'])
            rows = [{'seq': row['seq'],
                     'sample': {'value': {'float': row['value']},
                                'quality': self.wire(row['quality']),
                                'tick': row['tick']}}
                    for row in state.rows.get(point, [])
                    if row['seq'] > since]
            return 200, [{'point': point, 'samples': rows}]
        raise AssertionError('unexpected request %s %s' % (method, url))


class StaleBudgetCadenceTests(unittest.TestCase):
    """scenario_stale_budget_cadence against the stubbed rig: the control
    arm's frozen field ages its declared-budget input to stale one
    measured declared budget past its last publication while the
    undeclared input beside it keeps Good, the subject reader paced ten
    times faster than the field owner holds Good on every served sample
    past its arrival evidence with no journaled transition after it, and
    the deployed pair never moves — two passes, identical digests. Each
    fault flag stages a named failure, a nondeterministic surface, or a
    rig shape the leg declines on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.journals = {seat: str(self.root / (seat + '.jsonl'))
                         for seat in CadenceFeed.SEATS}
        self.feed = CadenceFeed(self.journals)

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'revised': 'http://ctrl-c:3',
               'foreign': 'http://ctrl-f:4',
               'driven': 'http://ctrl-d:5',
               'evidence_dir': str(self.evidence),
               'journal_files': dict(self.journals),
               'plant_owner': {'active': 424240, 'standby': 424241,
                               'revised': 424243, 'foreign': 424244,
                               'driven': 424245},
               'start_born_field': feed.start_field,
               'stop_born_field': feed.stop_field,
               'born_field_ctl': feed.field_ctl,
               'start_born_controller': feed.start_controller,
               'stop_born_controller': feed.stop_controller,
               'born_controller_state': feed.state}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CADENCE_POLL', 0.001), \
                patch.object(scenarios, 'CADENCE_PACE_POLL', 6), \
                patch.object(scenarios, 'CADENCE_WARM', 2), \
                patch.object(scenarios, 'CADENCE_WATCH', 0.4), \
                patch.object(scenarios, 'CADENCE_HOLD', 60), \
                patch.object(scenarios, 'CADENCE_SETTLE', 1.5):
            return scenarios.scenario_stale_budget_cadence(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads((self.evidence
                           / ('stale-budget-cadence-pass-' + str(number)
                              + '.json')).read_text())

    def _violations(self, number=1):
        return self._pass(number)['violations']

    def _assert_failed(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale-budget-cadence-failed',
                      record.get('detail', ''))
        self.assertIn(key, str(self._violations()), record)
        report.validate_scenario(record)

    def _assert_nondeterministic(self, key, record):
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale-budget-cadence-nondeterministic',
                      record.get('detail', ''))
        self.assertIn(key, str(self._violations()), record)
        report.validate_scenario(record)

    def test_registered(self):
        self.assertIn(scenarios.scenario_stale_budget_cadence,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('stale-budget-cadence'),
                      scenarios.scenario_stale_budget_cadence)
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_sim_bus_startup_claim_refusal),
            order.index(scenarios.scenario_stale_budget_cadence))
        self.assertLess(
            order.index(scenarios.scenario_stale_budget_cadence),
            order.index(scenarios.scenario_incompatible_revision))

    def test_clean_passes_validates_and_sweeps(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/stale-budget-cadence-pass-1.json',
             'evidence/stale-budget-cadence-pass-2.json'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        first = self._pass(1)
        self.assertEqual(first['symmetric']['field'],
                         {'remote': CadenceFeed.FIELD, 'mode': 'serving'})
        self.assertEqual(first['asymmetric']['field'],
                         {'remote': CadenceFeed.FIELD, 'mode': 'serving'})
        # The control arm: a reader paced like the owner, and the frozen
        # field's declared staleness verdict measured off the rig.
        self.assertEqual(first['symmetric']['reader']['scan_ms'],
                         runner.BORN_SCAN_MS)
        self.assertAlmostEqual(first['symmetric']['paced']['ratio'], 1.0,
                               places=1)
        self.assertEqual(first['symmetric']['holder']['granted']
                         ['field_claim'], 'held')
        self.assertTrue(first['symmetric']['freeze']['stopped'])
        self.assertEqual(scenarios._cadence_declared(first['symmetric']),
                         CadenceFeed.BUDGET)
        self.assertEqual(first['symmetric']['rows']['witness'][-1][1],
                         'good')
        # The subject arm: the per-container cadence lever drove this
        # seat's ticks ten per owner scan, the arrival evidence landed,
        # and the judged window holds no transition at all.
        self.assertEqual(first['asymmetric']['reader']['scan_ms'],
                         scenarios.FAST_SCAN_MS)
        self.assertGreaterEqual(first['asymmetric']['paced']['ratio'],
                                scenarios.CADENCE_RATIO)
        self.assertTrue(first['asymmetric']['warm']['watched'])
        boundary = first['asymmetric']['warm']['boundary']
        judged = [row for row in first['asymmetric']['rows']['budgeted']
                  if row[2] > boundary]
        self.assertTrue(judged)
        self.assertEqual({row[1] for row in judged}, {'good'})
        self.assertEqual(first['digest'],
                         {'symmetric': 'stale-on-freeze',
                          'declared': 'measured',
                          'asymmetric': 'fresh-under-asymmetry',
                          'pair': 'held',
                          'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The sweep is audited back over the rig: every born seat proven
        # gone, the scratch field's own tool refusing, and the deployed
        # pair's launch roles undisturbed in the framing the leg reads
        # once its own claim is gone.
        self.assertEqual(first['rig'],
                         {'seats': {'revised': True, 'foreign': True,
                                    'driven': True},
                          'field_error': None,
                          'field_serving': False})
        self.assertEqual(first['roles']['final']['active']['role'],
                         'active')
        self.assertIs(first['roles']['final']['standby']['tracking'], True)
        self.assertEqual(first['violations'], {})
        # Every pass ends torn down — the seats and the scratch field
        # removed, so the next pass finds free seats and a fresh field.
        self.assertFalse(self.feed.seats)
        self.assertIsNone(self.feed.field)
        self.assertIsNone(self.feed.claim)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_born_field'), 4)
        self.assertEqual(kinds.count('stop_born_field'), 4)
        self.assertEqual(kinds.count('start_born_controller'), 8)
        for seat in CadenceFeed.SEATS:
            self.assertIn(('stop_born_controller', seat),
                          self.feed.calls)
        # The cadence lever rode the subject reader's launches and no
        # other: the control arm paced like the owner.
        paced = [call for call in self.feed.calls
                 if call[0] == 'start_born_controller'
                 and call[6] is not None]
        self.assertEqual([(call[1], call[6]) for call in paced],
                         [(SUBJECT, scenarios.FAST_SCAN_MS)] * 2)
        wiring = {call[1]: call[4] for call in self.feed.calls
                  if call[0] == 'start_born_controller'}
        self.assertEqual(wiring[OWNER], None)
        self.assertEqual(wiring[CONTROL], OWNER)
        self.assertEqual(wiring[SUBJECT], OWNER)

    def test_two_passes_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])
        self.assertIn('identical digests', ' '.join(record['observations']))

    def test_reader_ticks_only_verdict_fails(self):
        # The recorded defect, staged on the rig: the freshness verdict
        # judged in the reader's own run ticks alone, so a reader ten
        # times faster than the field owner calls every publication the
        # field has not sent yet stale.
        self.feed.reader_ticks_only = True
        record = self.run_scenario()
        self._assert_failed('stale-inside-the-pace', record)
        first = self._pass(1)
        self.assertEqual(first['digest']['asymmetric'], 'flapping')
        # The control arm still holds its declared staleness behavior.
        self.assertEqual(first['digest']['symmetric'], 'stale-on-freeze')
        judged = [row for row in first['asymmetric']['rows']['budgeted']
                  if row[2] > first['asymmetric']['warm']['boundary']]
        self.assertIn('uncertain:stale', [row[1] for row in judged])

    def test_frozen_field_never_stale_fails(self):
        # The widening over-corrected: the declared staleness verdict a
        # frozen field must still produce never lands.
        self.feed.frozen_never_stale = True
        record = self.run_scenario()
        self._assert_failed('symmetric-stale', record)
        self.assertEqual(self._pass(1)['digest']['symmetric'],
                         'silent-on-freeze')
        self.assertIsNone(scenarios._cadence_declared(self._pass(1)[
            'symmetric']))

    def test_declared_stale_never_journaled_fails(self):
        # The durable half: the verdict reached the reader's served
        # surface and no operator or consumer read.
        self.feed.journaled = False
        record = self.run_scenario()
        self._assert_failed('journal-silent', record)

    def test_budget_leaking_onto_the_witness_fails(self):
        # The per-point declaration leaked: an input that declares no
        # budget aged out with the field frozen.
        self.feed.budget_leaks = True
        record = self.run_scenario()
        self._assert_failed('budget-leaked', record)

    def test_journal_flapping_past_the_evidence_fails(self):
        # The served verdict holds while the durable journal keeps
        # recording stale/good pairs past the arrival evidence.
        self.feed.journal_flaps = True
        record = self.run_scenario()
        self._assert_failed('journal-outside-the-bound', record)
        self.assertEqual(self._pass(1)['digest']['asymmetric'], 'flapping')

    def test_starvation_asserted_fresh_fails(self):
        # The issue's first doctored negative, staged on the rig: a field
        # that stops publishing well past the declared patience while the
        # reader keeps reporting Good — the leg asserting freshness over a
        # real starvation.
        self.feed.starved_but_good = True
        self.feed.starve_after = 12
        record = self.run_scenario()
        self._assert_failed('starved-field-asserted-fresh', record)
        self.assertEqual(self._pass(1)['digest']['asymmetric'], 'flapping')

    def test_stale_before_induction_fails(self):
        # The declared budget misfires on a healthy field: the budgeted
        # input is stale before anything was staged.
        feed = CadenceFeed(self.journals)
        original = feed.http_json

        def http_json(method, url, body=None, timeout=10):
            if url.endswith('/snapshot') \
                    and feed.HOSTS.get(url.split('/')[2]) == CONTROL:
                return 200, {'tick': 20, 'points': [
                    {'point': CadenceFeed.BUDGETED,
                     'sample': {'value': {'float': 0.2},
                                'quality': {'uncertain': 'stale'},
                                'tick': 20}},
                    {'point': CadenceFeed.WITNESS,
                     'sample': {'value': {'float': 0.8},
                                'quality': 'good', 'tick': 20}}]}
            return original(method, url, body, timeout)

        feed.http_json = http_json
        record = self.run_scenario(feed=feed)
        self._assert_failed('stale-before-induction', record)

    def test_diverging_digests_fail(self):
        # Both passes audit clean, yet their normalized digests differ —
        # the determinism contract's own failure, staged at the digest
        # seam the leg compares.
        digests = iter([
            {'symmetric': 'stale-on-freeze', 'declared': 'measured',
             'asymmetric': 'fresh-under-asymmetry', 'pair': 'held',
             'rig': 'restored'},
            {'symmetric': 'stale-on-freeze', 'declared': 'measured',
             'asymmetric': 'flapping', 'pair': 'held',
             'rig': 'restored'}])
        with patch.object(scenarios, '_cadence_digest',
                          lambda record, violations: next(digests)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale-budget-cadence-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_stage_failures_are_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self._assert_nondeterministic('field-stage', record)

    def test_launch_failures_are_nondeterministic(self):
        feed = CadenceFeed(self.journals)
        original = feed.start_controller

        def start_controller(seat, remote, peer=None, standby=None,
                             document=None, scan_ms=None):
            if seat in (CONTROL, SUBJECT):
                raise RuntimeError('docker run failed: name in use')
            return original(seat, remote, peer=peer, standby=standby,
                            document=document, scan_ms=scan_ms)

        feed.start_controller = start_controller
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale-budget-cadence-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('symmetric-stage', str(self._violations()), record)

    def test_owner_never_claims_is_nondeterministic(self):
        self.feed.owner_never_claims = True
        record = self.run_scenario()
        self._assert_nondeterministic('holder-claim', record)

    def test_readers_never_converge_is_nondeterministic(self):
        self.feed.never_converges = True
        record = self.run_scenario()
        self._assert_nondeterministic('symmetric-converge', record)

    def test_control_pace_off_parity_is_nondeterministic(self):
        # The control arm is no longer a symmetric-cadence pair, so its
        # frozen-field verdict is not the contrast the subject arm is
        # read against.
        self.feed.control_paced_fast = True
        record = self.run_scenario()
        self._assert_nondeterministic('symmetric-pace', record)

    def test_pace_never_staged_is_nondeterministic(self):
        # The per-container cadence lever dropped: both seats pace alike,
        # so a clean window says nothing about a reader outpacing the
        # field owner.
        self.feed.pace_ignored = True
        record = self.run_scenario()
        self._assert_nondeterministic('asymmetric-pace', record)
        self.assertEqual(self._pass(1)['digest']['asymmetric'], 'unstaged')

    def test_freeze_never_lands_is_nondeterministic(self):
        # Stopping the owner never stopped the field: no staleness
        # verdict can be attributed to a freeze that never froze.
        self.feed.freeze_stuck = True
        record = self.run_scenario()
        self._assert_nondeterministic('freeze', record)

    def test_arrival_never_arrives_is_nondeterministic(self):
        # The field never publishes, so the reader never demonstrates an
        # arrival period and the judged window has no evidence to judge.
        self.feed.field_starved = True
        record = self.run_scenario()
        self._assert_nondeterministic('arrival-unread', record)

    def test_empty_judged_window_is_nondeterministic(self):
        self.feed.history_stalls = True
        record = self.run_scenario()
        self._assert_nondeterministic('asymmetric-watch', record)

    def test_starved_watch_is_nondeterministic(self):
        # The reader's posture keeps answering while its served samples
        # stop landing: the watch is starved, not the reader.
        self.feed.watch_starves = True
        record = self.run_scenario()
        self._assert_nondeterministic('symmetric-watch', record)

    def test_unreadable_journal_is_nondeterministic(self):
        # The runner-owned sink never lands: the durable half of the
        # audit cannot run, which is the rig's staging surface rather
        # than a contract miss.
        self.feed.journal_vanishes = True
        for seat in CadenceFeed.SEATS:
            Path(self.journals[seat]).unlink(missing_ok=True)
        record = self.run_scenario()
        self._assert_nondeterministic('journal-unread', record)
        self.assertIs(self._pass(1)['symmetric']['journal_read'], False)

    def test_undeclared_probes_are_nondeterministic(self):
        self.feed.probes_undeclared = True
        record = self.run_scenario()
        self._assert_nondeterministic('symmetric-probes', record)

    def test_degraded_contrast_is_nondeterministic(self):
        # The undeclared contrast already degraded before the freeze: no
        # healthy baseline to read the per-point declaration against.
        feed = CadenceFeed(self.journals)
        original = feed.http_json

        def http_json(method, url, body=None, timeout=10):
            if url.endswith('/snapshot') \
                    and feed.HOSTS.get(url.split('/')[2]) == CONTROL:
                return 200, {'tick': 20, 'points': [
                    {'point': CadenceFeed.BUDGETED,
                     'sample': {'value': {'float': 0.2},
                                'quality': 'good', 'tick': 20}},
                    {'point': CadenceFeed.WITNESS,
                     'sample': {'value': {'float': 0.8},
                                'quality': {'bad': 'out_of_range'},
                                'tick': 20}}]}
            return original(method, url, body, timeout)

        feed.http_json = http_json
        record = self.run_scenario(feed=feed)
        self._assert_nondeterministic('symmetric-baseline', record)

    def test_pair_disturbance_is_nondeterministic(self):
        for flag in ('pair_moves', 'pair_wedged'):
            with self.subTest(flag=flag):
                feed = CadenceFeed(self.journals)
                setattr(feed, flag, True)
                record = self.run_scenario(feed=feed)
                self.assertEqual(record['outcome'], 'failed', record)
                self.assertIn('stale-budget-cadence-nondeterministic',
                              record.get('detail', ''))

    def test_rig_left_standing_is_nondeterministic(self):
        self.feed.teardown_keeps = frozenset({'driven'})
        record = self.run_scenario()
        self._assert_nondeterministic('rig-not-restored', record)
        self.assertFalse(self._pass(1)['rig']['seats']['driven'])

    def test_field_left_serving_is_nondeterministic(self):
        self.feed.field_left_serving = True
        record = self.run_scenario()
        self._assert_nondeterministic('rig-not-restored', record)
        self.assertIs(self._pass(1)['rig']['field_serving'], True)

    def test_field_presence_is_nondeterministic(self):
        # No shipped-tool seam at all: the scratch field's own removal
        # cannot be audited, which is the rig's staging surface rather
        # than a leg that left something standing.
        ctx = self._ctx()
        ctx['born_field_ctl'] = None
        record = self.run_scenario(ctx=ctx)
        self._assert_nondeterministic('field-unread', record)
        self.assertIsNone(self._pass(1)['rig']['field_serving'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled_pair = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no tracking standby', record.get('detail', ''))

    def test_missing_seams_are_inconclusive(self):
        for key in ('start_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state'):
            with self.subTest(key=key):
                ctx = self._ctx()
                ctx[key] = None
                record = self.run_scenario(ctx=ctx)
                self.assertEqual(record['outcome'], 'inconclusive',
                                 record)
                self.assertIn(key, record.get('detail', ''))
        # A seat with no published monitor, and a seat with no durable
        # journal to audit, are equally unstageable.
        ctx = self._ctx()
        ctx['driven'] = None
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('published monitor', record.get('detail', ''))
        ctx = self._ctx()
        ctx['journal_files'] = dict(self.journals)
        ctx['journal_files'].pop('foreign')
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal files', record.get('detail', ''))

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_cadence_self_check',
                          lambda: ['stale-asserted-on-fresh-inputs']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale-budget-cadence-unchecked',
                      record.get('detail', ''))
        self.assertIn('stale-asserted-on-fresh-inputs',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        # The self-check plants a negative for every clause class the
        # judge's own naming reaches, and the audit catches each one — so
        # the two lists cannot drift apart, and a judge that let any of
        # them slip fails the leg's self-check.
        clause_classes = {
            'fresh-asserted-over-a-starvation':
                'starved-field-asserted-fresh',
            'stale-asserted-on-fresh-inputs': 'stale-inside-the-pace',
            'frozen-field-never-stale': 'symmetric-stale',
            'declared-stale-never-journaled': 'journal-silent',
            'budget-leaked-onto-the-witness': 'budget-leaked',
            'journal-transitions-past-the-arrival-evidence':
                'journal-outside-the-bound',
            'arrival-never-arrived': 'arrival-unread'}
        source = (Path(scenarios.__file__).parent
                  / '2499_stale_budget_cadence.py').read_text()
        for negative, clause in clause_classes.items():
            with self.subTest(negative=negative):
                self.assertIn("expect('" + negative + "'", source)
                self.assertIn("'" + clause + "'", source)
        self.assertEqual(scenarios._cadence_self_check(), [])
