"""The stale_budget_cadence acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seats 'revised'/'driven'/
# 'foreign' and the born legs' scratch field — it needs the sim-bus
# claim-refusal leg's seats and its device server released, and must be
# done before the revision legs take the born seats over.
RUNS_AFTER = frozenset({'scenario_sim_bus_startup_claim_refusal'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The declared freshness budget's measurement domain — the per-revision
# lane evidence for the contract #1411's fix declares (decision 45's
# `stale_after_ticks`, WW-OPS-003's stale-data surface, and the tick
# domains CONTEXT.md separates): a declared freshness budget is measured
# in the *reader's* run ticks, so a peer whose scan cadence outpaces the
# field owner's step cadence reads the identical driver report on every
# scan in between. Before the fix a budget below the owner's step period
# therefore paced a `quality_changed` pair per field step on that peer —
# the recorded reproduction: ~118 stale/good flap pairs in ~6 s and ~40
# `quality_changed` journal records per second on one seat, while the
# field stayed healthy and the same input read Good on the owner. The fix
# widens the judgment to the greater of the declared budget and the
# arrival period the reader has itself demonstrated on that point, so
# freshness is decided in the declared domain rather than in the
# reader's raw tick count.
#
# The filed legs so far pin the symmetric case only: #434's rig leg and
# #704's consumer mirror exercise a reader paced like the owner, where
# the two domains happen to agree. Nothing pinned the asymmetry the
# defect record is actually about. This leg stages it on the rig through
# the per-container `scan_ms` lever scenario 2490 introduced: the same
# born staging surface, the same scratch field, and a reader launched at
# a tenth of the owner's own pace.
#
# Two arms, two scratch fields, one pass — the cadence is the only
# variable between them:
#
# - the control arm (`foreign`): a reader paced exactly like the field
#   owner (`scan_ms` unset, the documented born cadence), tracking it
#   through `--standby`. Freezing the field — stopping its only writer,
#   which under the sim-net single-writer claim is the only way the
#   shared plant stops stepping — must still age the declared-budget
#   input to `Uncertain(Stale)` while the undeclared input beside it
#   keeps Good. That is the declared staleness behavior the widening must
#   leave alone, and it is where the leg *measures* the declared budget
#   off the rig: the reader-tick age at which the frozen field's
#   budgeted input first presents stale is the declared floor the
#   subject arm is judged against, read in the reader's own domain to
#   the tick.
# - the subject arm (`driven`): the same reader shape launched through
#   the cadence lever at a tenth of the owner's pace on a fresh field.
#   The asymmetry is declared, not assumed: the leg reads both seats'
#   served run ticks and requires the measured ratio to clear the
#   recorded staging bound, since a reader paced like the owner says
#   nothing about one that is not. Once the reader has watched the field
#   publish twice — the two-gap arrival evidence the contract's own
#   cold-start limit names — the judged window opens, and inside it the
#   budgeted input must present `Good` on every served sample.
#
# The witness is what makes the cadence-domain claim measurable on the
# served surface: the reader cannot see the driver report's own stamp,
# which is minted in the field owner's step domain, but it does see
# every value that step moves. So the leg tracks the undeclared level
# input — the per-point contrast the declared budget is measured against
# anyway — as its pace witness: the reader-tick gaps between that
# point's served changes are the field's demonstrated step period
# expressed in the reader's own run ticks, the same-domain quantity the
# contract judges the freshness verdict in. Two consequences the judge
# leans on:
#
# - a stale presentation inside the patience floor — the greater of the
#   measured declared budget and the last two witnessed gaps, the
#   two-deep window the run itself holds — is the recorded defect: the
#   reader called a publication the field had not sent yet stale;
# - a witnessed silence longer than that floor *and* longer than any gap
#   the field itself demonstrated, with the budgeted input still Good,
#   is the opposite doctoring: the leg would be asserting freshness over
#   a genuine starvation the declared budget has to catch.
#
# Both judgments ride the judged window, and deliberately so: before the
# arrival evidence exists the contract answers a peer to the declared
# budget alone, which is the cold-start limit decision 45 records, and
# the age there is measured back to a seed the run mints from the driver
# stamp the leg cannot read. The leg reports that region's evidence and
# judges the window the arrival evidence opens.
#
# Either way the verdict is derived from the served rows, never from the
# record's own stored field, so a doctored verdict field proves nothing.
# The journal is the durable half: the reader's own `--journal-file` may
# carry only the transitions the contract names — the cold-start pair
# before the arrival evidence exists, and nothing after it. A healthy
# field flapping per field step is exactly what fills that file.
#
# The deployed pair never enters the staging — the scratch field is a
# different container on a different claim token — and the leg frames
# the pair's roles and advancing scan before, after, and once the leg's
# own seats are gone. Each pass sweeps the rig between the arms and after
# them, audits the sweep back over the rig (a seat or a field that
# outlived it is a claim the legs behind this one would inherit), and two
# consecutive passes must produce identical outcome digests.
#
# Named diagnostics: stale-budget-cadence-failed tags the contract
# clauses — a frozen field whose declared-budget input never presented
# stale, a freshness verdict that leaked onto an undeclared point, a
# declared staleness transition that reached no durable record, a stale
# presentation inside the declared patience on a reader outpacing the
# field owner, journaled quality_changed traffic past the arrival
# evidence, a freshness verdict asserted over a genuine starvation longer
# than the declared budget, or the control reader's budgeted input stale
# before any induction — while stale-budget-cadence-nondeterministic tags
# the instability the contract does not answer for: refused staging
# calls, an owner that never claimed the field, a reader that never
# converged or never paced as declared, a freeze that never took effect, a
# declared budget or arrival period the rig never showed, a starved watch
# or an empty judged window, an unreadable durable journal, a degradation
# the freshness contract does not name, a moved or wedged deployed pair, a
# rig the sweep did not restore, or two passes whose digests diverge.
#
# No staged revision predates this contract in a shape the leg declines
# on: a reader outpacing the field owner either holds the declared
# freshness contract or presents stale on a healthy field, so every
# inconclusive verdict here is rig-side — an unstaged lever, an
# unreachable endpoint, an unsettled pair, or an arm whose staging never
# produced the cadence, publication, or budget it declared. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives — the issue's two doctored cases, freshness asserted over a
# starvation the declared budget must catch and stale asserted on inputs
# the contract declares fresh — and reports
# stale-budget-cadence-unchecked for any that slip through.

OWNER_SEAT = 'revised'       # the field owner, at the declared cadence
SLOW_SEAT = 'foreign'        # the control reader — paced like the owner
FAST_SEAT = 'driven'         # the subject reader — the cadence lever
FAST_SCAN_MS = 10            # its per-container `--scan-ms` pacing
CADENCE_RATIO = 4            # the measured asymmetry the subject arm's
                             # staging must clear — a reader several
                             # times faster than the field owner, and a
                             # margin under the rig's own 10:1 lever
SYMMETRIC_SLACK = 1          # the control arm's measured ratio must sit
                             # this far either side of parity
CADENCE_SETTLE = 45          # bound on each launch settling, on the
                             # arrival evidence arriving, and on the
                             # freeze's verdict landing
CADENCE_POLL = 0.4           # cadence polling a seat's verdict mid-arm
CADENCE_PACE_POLL = 3        # polls a measured cadence comparison spans
CADENCE_WARM = 2             # witnessed publications the subject arm
                             # waits for before its judged window opens:
                             # two gaps complete the two-deep arrival
                             # window the contract itself holds
CADENCE_WATCH = 8            # seconds the judged window spans — the
                             # recorded reproduction's ~6 s of flapping,
                             # with margin
CADENCE_HOLD = 20            # polls the control arm keeps sampling past
                             # its first stale verdict — the relapse
                             # check
FREEZE_SILENCE = 6           # reader ticks the witness must go silent
                             # for the freeze to count as staged: a field
                             # stepping at any cadence publishes again
                             # within a tick or two, while a frozen one
                             # is silent for the whole watch
CLAUSE = 'stale-budget-cadence-failed'
NONDET = 'stale-budget-cadence-nondeterministic'
UNCHECKED = 'stale-budget-cadence-unchecked'

# The probe pair out of the served SignalIndex: the model's one declared
# stale_after_ticks point, and the undeclared field input sharing the
# same field step — the per-point-contract contrast and the leg's pace
# witness in one.
BUDGETED_NAME = 'net-flow'
WITNESS_NAME = 'level-primary'


# --------------------------------------------------------------------
# The rig reads every seat through these shapes. A seat's own
# `field_claim: held` is never its own claim — the role is what says who
# owns the field.

def _cadence_posture(ctx, base):
    """One normalized `/role` read — role, the field's claim posture,
    tracking convergence, and the served scan tick — or None when the
    endpoint dropped it."""
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    tick = report.get('tick')
    return {'role': report.get('role'),
            'field_claim': report.get('field_claim'),
            'tracking': 'tracking' in (report.get('sync') or {}),
            'tick': tick if isinstance(tick, int)
            and not isinstance(tick, bool) else None}


def _cadence_view(ctx, seat):
    """One monitor read of a born seat, or None."""
    base = ctx.get(seat)
    return _cadence_posture(ctx, base) if base else None


def _cadence_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever — the presence read the sweep's audit takes,
    and the one surface that answers for a seat whose monitor has
    stopped serving."""
    lever = ctx.get('born_controller_state')
    if lever is None:
        return None
    try:
        return lever(seat)
    except Exception:
        return None


def _cadence_journal(ctx, seat):
    """The seat's durable journal records — its runner-owned
    --journal-file. An unreadable or absent file reads as no records:
    the judge tells a silent journal from an unread one through the
    specific clause it looks for."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return []
    try:
        return _journal_entries(path)
    except Exception:
        return []


def _cadence_journal_read(ctx, seat):
    """Whether the seat's durable journal file is readable at all — the
    audit's own precondition, so a silent journal and an unread one are
    distinguishable."""
    path = (ctx.get('journal_files') or {}).get(seat)
    return bool(path) and Path(path).is_file()


def _cadence_quality_events(ctx, seat, points):
    """The `quality_changed` records a seat's durable journal carries for
    the named points — `{'point', 'from', 'to', 'tick'}` — the durable
    quality-transition trail, stamped in the seat's own run ticks like
    every other journal entry."""
    out = []
    for item in _cadence_journal(ctx, seat):
        entry = item.get('entry') or {}
        change = (entry.get('event') or {}).get('quality_changed')
        if isinstance(change, dict) and change.get('point') in points:
            out.append({'point': change.get('point'),
                        'from': _quality_key(change.get('from')),
                        'to': _quality_key(change.get('to')),
                        'tick': entry.get('tick')})
    return out


def _cadence_launch(ctx, seat, remote, standby=None, scan_ms=None):
    """Launch one seat on this arm's field: the scratch sim-serve address
    as `--remote`, the holder as the launch's tracking wiring in
    `standby`, and the per-container cadence lever in `scan_ms` — None
    keeps the born launch's documented pace, the control arm's shape. A
    refused staging call is the record's own answer — the judge's
    instability class, never a verdict."""
    try:
        launched = ctx['start_born_controller'](
            seat, remote, standby=standby, scan_ms=scan_ms)
    except Exception as exc:
        return {'seat': seat, 'stage_error': str(exc)[:300]}
    return {'seat': seat, 'launch': launched,
            'scan_ms': (launched or {}).get('scan_ms'),
            'view': _cadence_view(ctx, seat)}


def _cadence_tracking(ctx, seat):
    """The served view of a converged tracking standby, or None."""
    return wait_for(
        lambda: (lambda view: view if view is not None
                 and view.get('role') == 'standby'
                 and view.get('tracking') is True else None)(
                     _cadence_view(ctx, seat)),
        time.monotonic() + CADENCE_SETTLE, interval=CADENCE_POLL)


def _cadence_holder(ctx, seat):
    """The served view of a seat that reports itself the field's writer,
    or None — the born-active's conditional startup grant."""
    return wait_for(
        lambda: (lambda view: view if view is not None
                 and view.get('role') == 'active'
                 and view.get('field_claim') == 'held' else None)(
                     _cadence_view(ctx, seat)),
        time.monotonic() + CADENCE_SETTLE, interval=CADENCE_POLL)


def _cadence_probes(ctx, seat):
    """The probe pair's point ids out of a seat's served SignalIndex — the
    declared-budget point and the undeclared witness sharing its field
    step. The freshness budget itself is model data, so the index names
    its point by signal name; an undeclared probe pair reads as two
    Nones, the staging surface declining."""
    try:
        _, signals = http_json('GET', ctx[seat] + '/signals')
    except Exception:
        return {'budgeted': None, 'witness': None}
    named = {entry.get('name'): entry.get('point')
             for entry in (signals or {}).get('points', [])}
    return {'budgeted': named.get(BUDGETED_NAME),
            'witness': named.get(WITNESS_NAME)}


def _cadence_baseline(ctx, seat, probes):
    """The pre-induction read of both probes on a healthy field, or None
    when the served surface dropped it — the healthy contrast the arms
    judge every verdict against."""
    if probes.get('budgeted') is None or probes.get('witness') is None:
        return None
    snapshot = _try_snapshot(ctx, ctx.get(seat))
    if snapshot is None:
        return None
    return {'budgeted': _sample_quality(snapshot, probes['budgeted']),
            'witness': _sample_quality(snapshot, probes['witness'])}


# --------------------------------------------------------------------
# The served sample series: the reader's own run-domain record of what
# its input phase stamped, the only surface where a quality verdict and
# a publication cadence are both legible.

def _cadence_rows(ctx, seat, point, since=0):
    """One point's retained served samples as `[seq, quality, tick,
    value]` rows. An unreadable read is an empty series; the judge tells
    an empty window from a lost one through the watch that took it."""
    if point is None:
        return []
    try:
        _, payload = http_json('GET', ctx[seat] + '/history?point='
                               + str(point) + '&since=' + str(since))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    for entry in payload:
        if entry.get('point') != point:
            continue
        return [[sample.get('seq'),
                 _quality_key((sample.get('sample') or {}).get('quality')),
                 (sample.get('sample') or {}).get('tick'),
                 json.dumps((sample.get('sample') or {}).get('value'),
                            sort_keys=True)]
                for sample in entry.get('samples', [])]
    return []


def _cadence_pull(ctx, seat, point, cursor):
    """The rows a point gained since `cursor`, and the cursor to read the
    next window from — the incremental read that keeps a long watch
    inside the served history ring."""
    rows = _cadence_rows(ctx, seat, point, cursor)
    return rows, max([_cadence_tail(rows), cursor])


def _cadence_tail(rows):
    """The read cursor a pulled series leaves behind — its newest seq, or
    0 when it carried none."""
    seqs = [row[0] for row in rows or [] if isinstance(row[0], int)]
    return max(seqs) if seqs else 0


def _cadence_ticks(rows):
    """The integer ticks a served series carries, in order."""
    return [row[2] for row in rows or []
            if isinstance(row[2], int) and not isinstance(row[2], bool)]


def _cadence_publications(rows):
    """The ticks at which a point's served samples changed — the
    publications this reader watched. The served *value* is the change
    marker a consumer can read: the driver report's own stamp is minted
    in the field owner's step domain and never crosses onto the
    reader's served surface, so a value that step moves is what a
    publication looks like from here."""
    marks = []
    for previous, current in zip(rows or [], (rows or [])[1:]):
        if isinstance(previous[2], int) and isinstance(current[2], int) \
                and current[3] != previous[3]:
            marks.append(current[2])
    if marks:
        return marks
    ticks = _cadence_ticks(rows)
    return ticks[:1]


def _cadence_gaps(rows):
    """The reader-tick gaps between a series' publications — the reader's
    own measure of how often the field publishes to it, and the
    same-domain quantity the contract judges freshness in."""
    marks = _cadence_publications(rows)
    return [later - earlier
            for earlier, later in zip(marks, marks[1:])]


def _cadence_age(publications, tick, floor):
    """The reader-tick age of one sample: back to the last publication at
    or before it, or to the reader's first observation when the field has
    published nothing yet — the same two same-domain measures the run's
    own freshness record keeps."""
    earlier = [mark for mark in publications if mark <= tick]
    origin = max(earlier) if earlier else floor
    return tick - origin


def _cadence_trailing(rows, publications):
    """The reader ticks a series went silent for at its end — the frozen
    field's own evidence that it stopped publishing: a stepping field
    publishes again within a tick or two at any cadence, while a frozen
    one is silent for the whole watch."""
    ticks = _cadence_ticks(rows)
    if not ticks:
        return None
    last = max(ticks)
    earlier = [mark for mark in publications if mark <= last]
    return last - (max(earlier) if earlier else ticks[0])


def _cadence_silence(rows, publications, boundary, floor):
    """The longest run of reader ticks the witness went without a
    publication inside the judged window: the gap spanning the window's
    own boundary, each publication-to-publication gap past it, and the
    trailing silence up to the last served sample. A silence past what
    the field itself demonstrated means it stopped publishing for longer
    than the freshness contract tolerates, so a Good verdict across it
    would be the leg asserting freshness over a real starvation."""
    origin = [mark for mark in publications if mark <= boundary]
    start = max(origin) if origin else floor
    longest = 0
    for mark in [entry for entry in publications if entry > boundary]:
        longest = max(longest, mark - start)
        start = mark
    trailing = _cadence_trailing(rows, publications)
    if trailing is not None:
        longest = max(longest, trailing)
    return longest


def _cadence_qualities(rows, boundary=None):
    """The quality tally of a served series — `{'good', 'stale',
    'other'}` — counted past `boundary` where a judged window names its
    own."""
    counts = {'good': 0, 'stale': 0, 'other': []}
    for row in rows or []:
        quality, tick = row[1], row[2]
        if boundary is not None and isinstance(tick, int) \
                and tick <= boundary:
            continue
        if quality == 'good':
            counts['good'] += 1
        elif quality == 'uncertain:stale':
            counts['stale'] += 1
        else:
            counts['other'].append(quality)
    return counts


def _cadence_first_stale(rows, publications, floor):
    """The first stale presentation in a served series with the reader-tick
    age it carried, or None — a frozen field's declared verdict measured
    in the reader's own domain."""
    for row in rows or []:
        if row[1] == 'uncertain:stale' and isinstance(row[2], int):
            return {'seq': row[0], 'tick': row[2],
                    'age': _cadence_age(publications, row[2], floor)}
    return None


def _cadence_floor_tick(record):
    """The reader's first observation of an arm's probe — the cold-start
    seed its freshness record starts from, and the floor every age in
    that arm is measured back to."""
    rows = (record.get('rows') or {})
    ticks = _cadence_ticks(rows.get('budgeted')) \
        + _cadence_ticks(rows.get('witness'))
    return min(ticks) if ticks else 0


def _cadence_declared(symmetric):
    """The declared budget the control arm measured off the rig: the
    reader-tick age at which the frozen field's budgeted input first
    presented stale, less the one tick the worst-of merge needs to cross
    the bound. None where the arm measured none."""
    rows = (symmetric.get('rows') or {})
    stale = _cadence_first_stale(rows.get('budgeted'),
                                 _cadence_publications(rows.get('witness')),
                                 _cadence_floor_tick(symmetric))
    if stale is None:
        return None
    return max(0, stale['age'] - 1)


def _cadence_pace_floor(asymmetric, declared):
    """The reader's own floor on the patience the contract may judge
    with: the greater of the declared budget the control arm measured and
    the arrival period this reader has demonstrated — the last two
    witnessed gaps, the same two-deep window the run itself holds."""
    gaps = [gap for gap in _cadence_gaps(
        (asymmetric.get('rows') or {}).get('witness'))
        if isinstance(gap, int)]
    if not gaps:
        return declared if isinstance(declared, int) else None
    return max(declared if isinstance(declared, int) else 0,
               gaps[-1], gaps[-2] if len(gaps) > 1 else 0)


def _cadence_watched(arm):
    """Whether the arm's watch answered at all — the empty-window check
    the judge reads before it judges the window."""
    rows = (arm.get('rows') or {})
    return bool(rows.get('budgeted') and rows.get('witness')
                and arm.get('observations'))


def _cadence_measured_pace(reader, holder):
    """The opening frame of a measured cadence comparison: two served run
    ticks, one per seat. A run tick accrues one per scan, so the reader's
    advance against the field owner's is the rig's own clock ratio — the
    staging read off the rig rather than assumed from the launch."""
    reader_tick = (reader or {}).get('tick')
    holder_tick = (holder or {}).get('tick')
    if not isinstance(reader_tick, int) or not isinstance(holder_tick, int):
        return None
    return {'reader_start': reader_tick, 'holder_start': holder_tick,
            'reader': 0, 'holder': 0, 'ratio': None}


def _cadence_advance(pace, reader, holder):
    """Fold one more pair of served ticks into a measured cadence
    comparison, resolving the ratio once both seats have moved."""
    if pace is None:
        return pace
    reader_tick = (reader or {}).get('tick')
    holder_tick = (holder or {}).get('tick')
    if not isinstance(reader_tick, int) or not isinstance(holder_tick, int):
        return pace
    pace['reader'] = reader_tick - pace['reader_start']
    pace['holder'] = holder_tick - pace['holder_start']
    pace['ratio'] = (float(pace['reader']) / float(pace['holder'])
                     if pace['holder'] > 0 else None)
    return pace


def _cadence_pace_window(ctx, reader_seat, holder_seat, polls):
    """The measured cadence comparison across `polls` served reads of
    both seats — the asymmetry (or the parity) the arm was staged with,
    taken from the rig."""
    pace = _cadence_measured_pace(_cadence_view(ctx, reader_seat),
                                  _cadence_view(ctx, holder_seat))
    for _ in range(polls):
        time.sleep(CADENCE_POLL)
        pace = _cadence_advance(pace, _cadence_view(ctx, reader_seat),
                               _cadence_view(ctx, holder_seat))
    return pace


# --------------------------------------------------------------------
# The watch both arms take: every poll pulls both probes' served samples
# off the history ring as they are retained, so a long window's evidence
# is the reader's own record rather than a poll's sparse view.

def _cadence_watch(ctx, seat, probes, seconds, holder=None, seed=None):
    """One arm's watch window over a reader's served surface.

    `seed` continues an earlier window — the warm-up's rows, cursors, and
    cadence frame — so the subject arm's judged window picks up exactly
    where its arrival evidence left off. Returns the accumulated series,
    the served quality trail, the measured cadence, and whether the
    watch ever answered."""
    seed = seed or {}
    rows = seed.get('rows') or {'budgeted': [], 'witness': []}
    cursors = seed.get('cursors') or {
        'budgeted': _cadence_tail(rows.get('budgeted')),
        'witness': _cadence_tail(rows.get('witness'))}
    paced = seed.get('paced')
    observations = list(seed.get('observations') or [])
    deadline = time.monotonic() + seconds
    while True:
        view = _cadence_view(ctx, seat)
        snapshot = _try_snapshot(ctx, ctx.get(seat))
        if view is not None and snapshot is not None:
            observations.append({
                'tick': view.get('tick'), 'role': view.get('role'),
                'budgeted': _sample_quality(snapshot,
                                            probes.get('budgeted')),
                'witness': _sample_quality(snapshot,
                                           probes.get('witness'))})
            paced = _cadence_advance(
                paced, view, _cadence_view(ctx, holder)
                if holder else None)
        for name in ('budgeted', 'witness'):
            fresh, cursors[name] = _cadence_pull(ctx, seat,
                                                 probes.get(name),
                                                 cursors[name])
            rows[name].extend(fresh)
        if time.monotonic() >= deadline:
            break
        time.sleep(CADENCE_POLL)
    return {'rows': rows, 'cursors': cursors, 'observations': observations,
            'paced': paced}


def _cadence_warm_up(ctx, seat, probes, holder, publications):
    """Wait until the reader has watched the witness publish `publications`
    times — the two-gap arrival evidence the contract's cold-start limit
    names — pulling the served samples as it waits. Returns the seed the
    judged window continues from, carrying the boundary the wait opened
    the window after and whether it ever arrived."""
    state = {'rows': {'budgeted': [], 'witness': []}, 'watched': False,
             'boundary': None,
             'paced': _cadence_measured_pace(
                 _cadence_view(ctx, seat),
                 _cadence_view(ctx, holder) if holder else None)}

    def ready():
        read = _cadence_watch(ctx, seat, probes, 0.0, holder=holder,
                              seed={'rows': state['rows'],
                                    'observations': [],
                                    'paced': state.get('paced')})
        state['rows'] = read['rows']
        state['paced'] = read['paced']
        marks = _cadence_publications(state['rows']['witness'])
        if len(marks) <= publications:
            return None
        state['boundary'] = marks[-1]
        state['watched'] = True
        return dict(state)

    read = wait_for(ready, time.monotonic() + CADENCE_SETTLE,
                    interval=CADENCE_POLL)
    if read is None:
        # Nothing arrived inside the bound: the seed the judge reads is
        # the series the wait accumulated, unwatched.
        read = {'rows': state['rows'], 'paced': state.get('paced'),
                'observations': [], 'cursors': {
                    'budgeted': _cadence_tail(state['rows']['budgeted']),
                    'witness': _cadence_tail(state['rows']['witness'])}}
        read['watched'] = False
        read['boundary'] = None
        return read
    read['cursors'] = {'budgeted': _cadence_tail(read['rows']['budgeted']),
                       'witness': _cadence_tail(read['rows']['witness'])}
    read['observations'] = []
    return read


def _cadence_open(ctx, arm, scan_ms):
    """Stage one arm: the scratch sim-serve field, the field owner at the
    documented born cadence, and this arm's reader launched on it through
    the born launcher's `--remote`/`--standby`/`--scan-ms` seams. Returns
    the field address the reader dials, or None when the field itself
    never staged."""
    try:
        field = ctx['start_born_field']('serving')
    except Exception as exc:
        arm['field_error'] = str(exc)[:300]
        return None
    arm['field'] = {'remote': field.get('remote'),
                    'mode': field.get('mode')}
    arm['holder'] = _cadence_launch(ctx, OWNER_SEAT, field.get('remote'))
    if arm['holder'].get('stage_error') is None:
        arm['holder']['granted'] = _cadence_holder(ctx, OWNER_SEAT)
    arm['reader'] = _cadence_launch(ctx, arm['seat'], field.get('remote'),
                                    standby=OWNER_SEAT,
                                    scan_ms=scan_ms)
    if arm['reader'].get('stage_error') is None:
        arm['reader']['converged'] = _cadence_tracking(ctx, arm['seat'])
    return field.get('remote')


def _cadence_journal_of(ctx, arm, seat):
    """The arm's durable quality-transition record for its two probes —
    the reader's own `--journal-file`, filtered to the points the leg
    judges."""
    probes = arm.get('probes') or {}
    points = {probes.get('budgeted'), probes.get('witness')} - {None}
    return _cadence_quality_events(ctx, seat, points)


def _cadence_symmetric(ctx):
    """The control arm — a reader paced exactly like the field owner.
    Stopping that owner is the only way the shared plant stops stepping
    under its single-writer claim, so the reader's scans outrun frozen
    driver reports and the declared-budget input must age to
    Uncertain(Stale) while the undeclared witness keeps Good. The age it
    does so at is the declared budget this arm measures off the rig for
    the subject arm to judge against."""
    arm = {'seat': SLOW_SEAT, 'scan_ms': None}
    if _cadence_open(ctx, arm, None) is None:
        return arm
    probes = _cadence_probes(ctx, SLOW_SEAT)
    arm['probes'] = probes
    arm['baseline'] = _cadence_baseline(ctx, SLOW_SEAT, probes)
    # The cadence comparison first, while both seats are alive: after the
    # freeze the owner is gone and its advance could never be read.
    arm['paced'] = _cadence_pace_window(ctx, SLOW_SEAT, OWNER_SEAT,
                                        CADENCE_PACE_POLL)
    arm['holder_before'] = _cadence_view(ctx, OWNER_SEAT)
    arm['reader_before'] = _cadence_view(ctx, SLOW_SEAT)
    # The freeze: the field's only writer goes, its stepping stops, and
    # the reader keeps scanning.
    try:
        ctx['stop_born_controller'](OWNER_SEAT)
        arm['freeze'] = {'stopped': True,
                         'tick': (arm['reader_before'] or {}).get('tick')}
    except Exception as exc:
        arm['freeze'] = {'stopped': False, 'error': str(exc)[:300]}
    arm['owner_after'] = _cadence_state(ctx, OWNER_SEAT)
    watch = _cadence_watch(ctx, SLOW_SEAT, probes,
                           CADENCE_HOLD * CADENCE_POLL)
    arm['rows'] = watch['rows']
    arm['observations'] = watch['observations']
    arm['journal_read'] = _cadence_journal_read(ctx, SLOW_SEAT)
    arm['journal'] = _cadence_journal_of(ctx, arm, SLOW_SEAT)
    return arm


def _cadence_asymmetric(ctx, declared):
    """The subject arm — the same reader shape launched through the
    per-container cadence lever at a tenth of the field owner's pace.
    Once the reader has watched the field publish twice, its judged
    window opens and the declared-budget input must present Good on every
    served sample: the freshness verdict belongs to the declared domain,
    and the durable journal may carry no transition past the arrival
    evidence."""
    arm = {'seat': FAST_SEAT, 'scan_ms': FAST_SCAN_MS}
    if _cadence_open(ctx, arm, FAST_SCAN_MS) is None:
        return arm
    probes = _cadence_probes(ctx, FAST_SEAT)
    arm['probes'] = probes
    arm['baseline'] = _cadence_baseline(ctx, FAST_SEAT, probes)
    arm['holder_before'] = _cadence_view(ctx, OWNER_SEAT)
    # The arrival evidence, then the judged window: the warm-up's rows
    # and cadence frame are the seed the window continues from, so the
    # boundary the judge reads is a real reader tick.
    warm = _cadence_warm_up(ctx, FAST_SEAT, probes, OWNER_SEAT,
                            CADENCE_WARM)
    arm['warm'] = {'boundary': warm.get('boundary'),
                   'watched': bool(warm.get('watched'))}
    watch = _cadence_watch(ctx, FAST_SEAT, probes, CADENCE_WATCH,
                           holder=OWNER_SEAT, seed=warm)
    arm['rows'] = watch['rows']
    arm['observations'] = watch['observations']
    arm['paced'] = _cadence_advance(watch.get('paced'),
                                    _cadence_view(ctx, FAST_SEAT),
                                    _cadence_view(ctx, OWNER_SEAT))
    arm['declared'] = declared
    arm['holder_after'] = _cadence_view(ctx, OWNER_SEAT)
    arm['journal_read'] = _cadence_journal_read(ctx, FAST_SEAT)
    arm['journal'] = _cadence_journal_of(ctx, arm, FAST_SEAT)
    return arm


# --------------------------------------------------------------------
# The judge. Every verdict is derived from the record's served rows,
# durable journal records, and served postures — a doctored record has
# to doctor evidence to slip through.

def _judge_cadence(record, note):
    """Replay one pass's record. `note(key, diagnostic, detail)` records
    each clause the record violates: CLAUSE tags the contract clauses and
    NONDET the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, CLAUSE, detail)

    def nondet(key, detail):
        note(key, NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the born field never staged: '
               + str(record['stage_error']))
        return
    symmetric = record.get('symmetric') or {}
    asymmetric = record.get('asymmetric') or {}
    declared = _cadence_declared(symmetric)

    # ---- the control arm: the declared staleness behavior the widening
    # must leave alone, and the declared budget it measures off the rig.
    if symmetric.get('field_error') is not None:
        nondet('field-stage', 'the control arm\'s field never staged: '
               + str(symmetric['field_error']))
    elif symmetric.get('holder', {}).get('stage_error') is not None:
        nondet('symmetric-stage', 'the field owner never staged: '
               + str(symmetric['holder']['stage_error']))
    elif not symmetric.get('holder', {}).get('granted'):
        nondet('holder-claim', 'the field owner never reported itself the '
               'field\'s writer inside the bound — the frozen field the '
               'control arm judges has no writer to lose: '
               + json.dumps(symmetric.get('holder'))[:250])
    elif symmetric.get('reader', {}).get('stage_error') is not None:
        nondet('symmetric-stage', 'the control reader never staged: '
               + str(symmetric['reader']['stage_error']))
    elif not symmetric.get('reader', {}).get('converged'):
        nondet('symmetric-converge', 'the control reader never converged '
               'tracking on the field owner: '
               + json.dumps(symmetric.get('reader'))[:250])
    elif symmetric.get('probes', {}).get('budgeted') is None \
            or symmetric.get('probes', {}).get('witness') is None:
        nondet('symmetric-probes', 'the rig model declares no '
               + BUDGETED_NAME + '/' + WITNESS_NAME + ' field input — '
               'the freshness probe is not declared: '
               + json.dumps(symmetric.get('probes')))
    elif not _cadence_watched(symmetric):
        nondet('symmetric-watch', 'the control reader\'s monitor never '
               'answered the watch across the freeze')
    else:
        rows = symmetric.get('rows') or {}
        marks = _cadence_publications(rows.get('witness'))
        trailing = _cadence_trailing(rows.get('witness'), marks)
        ratio = (symmetric.get('paced') or {}).get('ratio')
        if not isinstance(ratio, (int, float)) \
                or abs(float(ratio) - 1.0) > SYMMETRIC_SLACK:
            nondet('symmetric-pace', 'the control reader did not pace like '
                   'the field owner — its measured cadence ratio is '
                   + str(ratio) + ' against the recorded parity window of '
                   + str(SYMMETRIC_SLACK) + ': '
                   + json.dumps(symmetric.get('paced'))[:250])
        elif (symmetric.get('baseline') or {}).get('budgeted') != 'good':
            failed('stale-before-induction', 'the budgeted point '
                   'presented ' + str((symmetric.get('baseline') or {})
                                      .get('budgeted'))
                   + ' before any induction — the declared budget misfires '
                   'on a healthy rig: '
                   + json.dumps(symmetric.get('baseline')))
        elif (symmetric.get('baseline') or {}).get('witness') != 'good':
            nondet('symmetric-baseline', 'the undeclared witness presented '
                   + str((symmetric.get('baseline') or {}).get('witness'))
                   + ' before the freeze — no healthy contrast to judge '
                   'the per-point declaration against')
        elif not (symmetric.get('freeze') or {}).get('stopped'):
            nondet('freeze', 'the freeze induction never completed: '
                   + json.dumps(symmetric.get('freeze'))[:250])
        elif trailing is None or trailing < FREEZE_SILENCE:
            nondet('freeze', 'the field kept publishing through the '
                   'freeze — the driver stamps never stopped, so no '
                   'staleness verdict can be attributed to the freeze: '
                   + json.dumps({'trailing_silence': trailing,
                                 'publications': len(marks),
                                 'samples': len(_cadence_ticks(
                                     rows.get('witness')))})[:250])
        else:
            stale = _cadence_first_stale(rows.get('budgeted'), marks,
                                        _cadence_floor_tick(symmetric))
            witness = _cadence_qualities(rows.get('witness'))
            if stale is None:
                failed('symmetric-stale', 'a frozen field never presented '
                       'Uncertain(Stale) on its declared-budget input — '
                       'the widening retired the staleness verdict the '
                       'declaration exists for: '
                       + json.dumps(_cadence_qualities(
                           rows.get('budgeted')))[:250])
            elif witness['stale'] or witness['other']:
                failed('budget-leaked', 'the undeclared witness presented '
                       'a degraded quality while the field\'s stepping was '
                       'frozen — the budget leaked past its per-point '
                       'declaration: ' + json.dumps(witness)[:250])
            elif not symmetric.get('journal_read'):
                nondet('journal-unread', 'the control reader\'s durable '
                       'journal file could not be read — the declared '
                       'staleness verdict cannot be audited where it '
                       'reaches operators')
            else:
                journaled = [event for event
                             in symmetric.get('journal') or []
                             if event.get('point')
                             == symmetric['probes']['budgeted']
                             and event.get('to') == 'uncertain:stale'
                             and isinstance(event.get('tick'), int)]
                if not journaled:
                    failed('journal-silent', 'the frozen field\'s declared '
                           'staleness verdict reached no durable '
                           'quality_changed record — the transition '
                           'operators and every consumer read is missing: '
                           + json.dumps(symmetric.get('journal'))[:300])

    if declared is None and symmetric.get('holder', {}).get('granted') \
            and symmetric.get('reader', {}).get('converged') \
            and _cadence_watched(symmetric):
        nondet('declared-unread', 'the rig never showed the declared '
               'budget — no stale presentation carried a measurable '
               'reader-tick age off the control arm: '
               + json.dumps(_cadence_qualities(
                   (symmetric.get('rows') or {}).get('budgeted')))[:250])

    # ---- the subject arm: a reader outpacing the field owner must hold
    # the declared freshness contract.
    if asymmetric.get('field_error') is not None:
        nondet('field-stage', 'the subject arm\'s field never staged: '
               + str(asymmetric['field_error']))
    elif asymmetric.get('holder', {}).get('stage_error') is not None:
        nondet('asymmetric-stage', 'the field owner never staged: '
               + str(asymmetric['holder']['stage_error']))
    elif not asymmetric.get('holder', {}).get('granted'):
        nondet('holder-claim', 'the subject arm\'s field owner never '
               'reported itself the field\'s writer — the healthy field '
               'its reader must stay fresh on never existed: '
               + json.dumps(asymmetric.get('holder'))[:250])
    elif asymmetric.get('reader', {}).get('stage_error') is not None:
        nondet('asymmetric-stage', 'the subject reader never staged: '
               + str(asymmetric['reader']['stage_error']))
    elif not asymmetric.get('reader', {}).get('converged'):
        nondet('asymmetric-converge', 'the subject reader never converged '
               'tracking on the field owner: '
               + json.dumps(asymmetric.get('reader'))[:250])
    elif asymmetric.get('probes', {}).get('budgeted') is None \
            or asymmetric.get('probes', {}).get('witness') is None:
        nondet('asymmetric-probes', 'the rig model declares no '
               + BUDGETED_NAME + '/' + WITNESS_NAME + ' field input — '
               'the freshness probe is not declared: '
               + json.dumps(asymmetric.get('probes')))
    elif not _cadence_watched(asymmetric):
        nondet('asymmetric-watch', 'the subject reader\'s monitor never '
               'answered the judged window')
    elif not (asymmetric.get('warm') or {}).get('watched'):
        nondet('arrival-unread', 'the subject reader never watched the '
               'witness publish ' + str(CADENCE_WARM)
               + ' times — the arrival evidence the judged window rests on '
               'never arrived: ' + json.dumps(asymmetric.get('warm'))[:250])
    else:
        rows = asymmetric.get('rows') or {}
        boundary = (asymmetric.get('warm') or {}).get('boundary')
        floor = _cadence_pace_floor(asymmetric, declared)
        ratio = (asymmetric.get('paced') or {}).get('ratio')
        inside = [row for row in rows.get('budgeted') or []
                  if isinstance(row[2], int) and boundary is not None
                  and row[2] > boundary]
        if not isinstance(ratio, (int, float)) \
                or float(ratio) < CADENCE_RATIO:
            nondet('asymmetric-pace', 'the per-container cadence lever '
                   'never drove the reader past the field owner\'s pace '
                   'by the recorded ' + str(CADENCE_RATIO) + 'x — a reader '
                   'paced alike says nothing about one that is not: '
                   + json.dumps(asymmetric.get('paced'))[:250])
        elif floor is None:
            nondet('arrival-unread', 'the reader\'s demonstrated arrival '
                   'period and the declared budget are both unreadable — '
                   'no patience floor can be derived: '
                   + json.dumps({'gaps': _cadence_gaps(
                       rows.get('witness')), 'declared': declared})[:250])
        elif not inside:
            nondet('asymmetric-watch', 'the judged window carries no '
                   'served sample past its own boundary: '
                   + json.dumps({'boundary': boundary,
                                 'rows': len(rows.get('budgeted') or []),
                                 'witness': len(_cadence_ticks(
                                     rows.get('witness')))})[:250])
        else:
            marks = _cadence_publications(rows.get('witness'))
            origin = _cadence_floor_tick(asymmetric)
            # Every stale presentation the reader served inside the
            # judged window, each with the reader-tick age it carried.
            # The cold start before the arrival evidence is the
            # contract's own recorded limit — answered to the declared
            # budget alone, which the leg reports as evidence and does
            # not assert: the age there is measured back to the seed the
            # run mints in the driver stamp's own domain, and the leg
            # cannot read that stamp.
            stale = [{'seq': row[0], 'tick': row[2],
                      'age': _cadence_age(marks, row[2], origin)}
                     for row in rows.get('budgeted') or []
                     if row[1] == 'uncertain:stale'
                     and isinstance(row[2], int) and row[2] > boundary]
            tight = [item for item in stale
                     if item['age'] <= floor]
            gaps = _cadence_gaps(rows.get('witness'))
            tolerance = max([floor] + ([max(gaps)] if gaps else []))
            silence = _cadence_silence(rows.get('witness'), marks,
                                       boundary, origin)
            journaled = [event for event
                         in asymmetric.get('journal') or []
                         if event.get('point')
                         == asymmetric['probes']['budgeted']
                         and isinstance(event.get('tick'), int)
                         and event['tick'] > boundary]
            witness = _cadence_qualities(rows.get('witness'), boundary)
            budgeted = _cadence_qualities(rows.get('budgeted'), boundary)
            if tight:
                failed('stale-inside-the-pace', 'a stale presentation '
                       'landed ' + json.dumps(tight[:6])
                       + ' reader ticks after the last publication the '
                       'witness saw — inside the declared patience it is '
                       'measured against, so the field was never silent '
                       'that long and the verdict names an input the '
                       'contract declares fresh: '
                       + json.dumps({'floor': floor, 'declared': declared,
                                     'gaps': gaps[:8]})[:300])
            elif witness['stale'] or witness['other']:
                failed('budget-leaked', 'the undeclared witness left Good '
                       'inside the judged window — the declared point\'s '
                       'freshness verdict is not per-point anymore: '
                       + json.dumps(witness)[:250])
            elif budgeted['other']:
                nondet('window-degraded', 'the budgeted input presented '
                       + str(sorted(set(budgeted['other'])))
                       + ' inside the judged window — a degradation the '
                       'freshness contract does not name, and one this '
                       'leg cannot attribute: '
                       + json.dumps(budgeted)[:250])
            elif silence > tolerance and not stale:
                failed('starved-field-asserted-fresh', 'the field went '
                       'silent for ' + str(silence) + ' reader ticks '
                       'inside the judged window — past the declared '
                       'patience of ' + str(floor) + ' ticks and past '
                       'every gap the field itself demonstrated — while '
                       'the budgeted input stayed Good: the leg asserted '
                       'freshness over a starvation the declared budget '
                       'has to catch: '
                       + json.dumps({'tolerance': tolerance,
                                     'boundary': boundary,
                                     'gaps': gaps[:8]})[:300])
            elif not asymmetric.get('journal_read'):
                nondet('journal-unread', 'the subject reader\'s durable '
                       'journal file could not be read — the '
                       'quality_changed traffic the contract bounds cannot '
                       'be audited')
            elif journaled:
                failed('journal-outside-the-bound', 'the reader journaled '
                       + str(len(journaled)) + ' quality_changed '
                       'transitions on the budgeted point after its '
                       'arrival evidence existed — past the transitions '
                       'the contract names: '
                       + json.dumps(journaled[:8])[:400])

    if not _cadence_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged across '
               'the staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])

    rig = record.get('rig') or {}
    seats = rig.get('seats') or {}
    standing = sorted(seat for seat, absent in seats.items()
                      if absent is not True)
    if standing:
        nondet('rig-not-restored', 'the leg left a seat\'s claim state '
               'standing — a seat that outlived the sweep the legs behind '
               'this one inherit: '
               + json.dumps({'standing': standing,
                             'field_error': rig.get('field_error')},
                            sort_keys=True)[:300])
    serving = rig.get('field_serving')
    if serving is True:
        nondet('rig-not-restored', 'the scratch field outlived the sweep '
               '— its own tool still answers, and the legs behind this one '
               'would stage onto a field carrying the claim this leg '
               'left: ' + json.dumps(rig, sort_keys=True)[:300])
    elif serving is None:
        nondet('field-unread', 'the scratch field\'s own shipped tool '
               'could not be read back after the sweep, so the field\'s '
               'own removal cannot be audited: '
               + json.dumps(rig, sort_keys=True)[:300])


def _cadence_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's staging, the peer still a
    tracking standby — in every framing the record carries, the `before`
    and `after` of the staging and the `final` one the leg reads once its
    own seats are gone."""
    launch = record.get('launch') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    tick = None
    for phase in ('before', 'after', 'final'):
        view = roles.get(phase)
        if view is None:
            continue
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' \
                or seen.get('tracking') is not True:
            return False
        seen_tick = (view.get(owner) or {}).get('tick')
        if not isinstance(seen_tick, int) or (tick is not None
                                              and seen_tick <= tick):
            return False
        tick = seen_tick
    return tick is not None


def _cadence_digest(record, violations):
    """The pass's normalized verdict set — identical digests across two
    consecutive passes is the determinism contract. An arm reads
    `unstaged` when the instability classes touched it (nothing was
    staged to judge), and its own verdict when the contract clauses
    did."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'symmetric': 'unstaged' if not clean(
            'field-stage', 'symmetric-stage', 'symmetric-converge',
            'symmetric-probes', 'symmetric-watch', 'symmetric-pace',
            'symmetric-baseline', 'freeze', 'holder-claim',
            'journal-unread') else (
                'silent-on-freeze' if not clean(
                    'symmetric-stale', 'budget-leaked',
                    'stale-before-induction', 'journal-silent')
                else 'stale-on-freeze'),
        'declared': 'unread' if 'declared-unread' in violations else (
            'defect' if not clean(
                'field-stage', 'symmetric-stage', 'symmetric-converge',
                'symmetric-pace', 'symmetric-watch', 'freeze',
                'holder-claim', 'symmetric-stale', 'budget-leaked',
                'stale-before-induction', 'journal-silent',
                'journal-unread')
            else 'measured'),
        'asymmetric': 'unstaged' if not clean(
            'field-stage', 'asymmetric-stage', 'asymmetric-converge',
            'asymmetric-probes', 'asymmetric-watch', 'asymmetric-pace',
            'arrival-unread', 'holder-claim', 'journal-unread',
            'window-degraded') else (
                'flapping' if not clean(
                    'stale-inside-the-pace',
                    'starved-field-asserted-fresh',
                    'journal-outside-the-bound', 'budget-leaked')
                else 'fresh-under-asymmetry'),
        'pair': 'held' if clean('pair-disturbed') else 'disturbed',
        'rig': 'restored' if clean('rig-not-restored', 'field-unread')
        else 'dirty'}


def _cadence_pass(ctx, number, launch):
    """One pass over the contract: frame the deployed pair's roles, stage
    the control arm on a scratch field and measure the declared budget off
    the frozen field it stages, sweep that field, then stage the subject
    arm on a fresh field and judge its judged window, and frame the pair
    again. The `final` framing and the rig's restoration read belong to
    the caller: they only mean anything once the sweep has run."""
    record = {'pass': number, 'launch': dict(launch), 'roles': {},
              'symmetric': {}, 'asymmetric': {}, 'rig': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _cadence_posture(ctx, ctx[name]) for name in (owner, peer)}
    record['symmetric'] = _cadence_symmetric(ctx)
    # The control arm's sweep frees the claim state and the scratch field
    # for the subject arm, so the cadence stays the only variable between
    # the two.
    record['symmetric']['sweep'] = {'field_error': _cadence_sweep(
        ctx, (SLOW_SEAT, OWNER_SEAT))}
    record['asymmetric'] = _cadence_asymmetric(
        ctx, _cadence_declared(record['symmetric']))
    record['roles']['after'] = {
        name: _cadence_posture(ctx, ctx[name]) for name in (owner, peer)}
    return record


def _cadence_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — the issue's two doctored cases, freshness asserted over a
    starvation the declared budget must catch and stale asserted on
    inputs the contract declares fresh — beside a silent declared
    staleness verdict, a leaked budget, an unjournaled transition, a stale
    presentation inside the declared patience, and every instability
    class the leg reports nondeterministic, and report every one let slip.
    """
    def series(start, qualities, step=1, value=0.2):
        """A fabricated served series — `[seq, quality, tick, value]` rows
        whose ticks advance `step` and whose values move with them, so
        every row is a publication."""
        return [[start + index, quality, start + index * step,
                 {'float': round(value + index * 0.01, 4)}]
                for index, quality in enumerate(qualities)]

    def held(start, publications, samples, stale_from=None, value=0.9):
        """A fabricated series of a field whose stepping stopped: its
        value moves for the first `publications` samples and holds
        afterwards while the reader's own scans go on — the frozen
        field's served shape, with the quality turning to
        `uncertain:stale` at `stale_from` when the arm declares one."""
        rows = []
        for index in range(samples):
            moved = value + (index if index < publications
                             else publications - 1) * 0.01
            tick = start + index
            quality = 'good' if stale_from is None \
                or tick < stale_from else 'uncertain:stale'
            rows.append([index, quality, tick, {'float': round(moved, 4)}])
        return rows

    def starving(start, steps, step, silence, value=0.9):
        """A fabricated witness series of a field that published every
        `step` reader ticks for `steps` steps and then went silent for
        `silence` ticks — a real starvation inside the judged window."""
        rows = [[index, 'good', start + index * step,
                 {'float': round(value + index * 0.01, 4)}]
                for index in range(steps + 1)]
        held_value = rows[-1][3]
        for offset in range(1, silence + 1):
            rows.append([len(rows), 'good', start + steps * step + offset,
                         held_value])
        return rows

    def paced(start, count, step, value=0.9):
        """A fabricated witness series whose value moves once every `step`
        reader ticks — `count` of a stepping field's publications."""
        return [[index, 'good', start + index * step,
                 {'float': round(value + index * 0.01, 4)}]
                for index in range(count)]

    def clean_record():
        return {
            'pass': 1,
            'launch': {'owner': 'active', 'peer': 'standby'},
            'symmetric': {
                'seat': SLOW_SEAT, 'scan_ms': None,
                'field': {'remote': 'dcs-hw-qa-1-born-plant:9003',
                          'mode': 'serving'},
                'holder': {'seat': OWNER_SEAT, 'scan_ms': 100,
                           'granted': {'role': 'active',
                                       'field_claim': 'held',
                                       'tracking': False, 'tick': 20}},
                'reader': {'seat': SLOW_SEAT, 'scan_ms': 100,
                           'converged': {'role': 'standby',
                                         'field_claim': None,
                                         'tracking': True, 'tick': 20}},
                'probes': {'budgeted': 13, 'witness': 10},
                'baseline': {'budgeted': 'good', 'witness': 'good'},
                'paced': {'reader_start': 20, 'holder_start': 20,
                          'reader': 12, 'holder': 12, 'ratio': 1.0},
                'holder_before': {'role': 'active',
                                  'field_claim': 'held',
                                  'tracking': False, 'tick': 20},
                'reader_before': {'role': 'standby', 'field_claim': None,
                                  'tracking': True, 'tick': 20},
                'freeze': {'stopped': True, 'tick': 20},
                'owner_after': {'container': 'dcs-hw-revised',
                                'running': False, 'exit': None,
                                'logs': '', 'absent': True},
                # The frozen field: the value moved once, at tick 11, and
                # held for the rest of the watch while the reader's own
                # scans went on — so the budgeted input ages six reader
                # ticks past it, the declared budget of 5 plus the one
                # tick the worst-of merge needs to cross the bound.
                'rows': {'budgeted': held(10, 2, 18, stale_from=17,
                                          value=0.2),
                         'witness': held(10, 2, 18)},
                'observations': [{'tick': 11, 'role': 'standby',
                                  'budgeted': 'good',
                                  'witness': 'good'},
                                 {'tick': 18, 'role': 'standby',
                                  'budgeted': 'uncertain:stale',
                                  'witness': 'good'}],
                'journal_read': True,
                'journal': [{'point': 10, 'from': 'unknown',
                             'to': 'good', 'tick': 10},
                            {'point': 13, 'from': 'good',
                             'to': 'uncertain:stale', 'tick': 17}],
                'sweep': {'field_error': None}},
            'asymmetric': {
                'seat': FAST_SEAT, 'scan_ms': FAST_SCAN_MS,
                'field': {'remote': 'dcs-hw-qa-1-born-plant:9003',
                          'mode': 'serving'},
                'holder': {'seat': OWNER_SEAT, 'scan_ms': 100,
                           'granted': {'role': 'active',
                                       'field_claim': 'held',
                                       'tracking': False, 'tick': 50}},
                'reader': {'seat': FAST_SEAT, 'scan_ms': FAST_SCAN_MS,
                           'converged': {'role': 'standby',
                                         'field_claim': None,
                                         'tracking': True, 'tick': 8}},
                'probes': {'budgeted': 13, 'witness': 10},
                'baseline': {'budgeted': 'good', 'witness': 'good'},
                'paced': {'reader_start': 8, 'holder_start': 30,
                          'reader': 100, 'holder': 10, 'ratio': 10.0},
                'holder_before': {'role': 'active',
                                  'field_claim': 'held',
                                  'tracking': False, 'tick': 30},
                'warm': {'boundary': 40, 'watched': True},
                # A cold start on the fast reader: stale from the sixth
                # reader tick to the first publication, the arrival
                # evidence complete by tick 30, and Good from there on —
                # the transitions the contract names, and no other.
                'rows': {'budgeted': series(
                    1, ['good'] * 5 + ['uncertain:stale'] * 4
                    + ['good'] * 95),
                    'witness': paced(0, 20, 10)},
                'observations': [{'tick': 11, 'role': 'standby',
                                  'budgeted': 'good',
                                  'witness': 'good'},
                                 {'tick': 95, 'role': 'standby',
                                  'budgeted': 'good',
                                  'witness': 'good'}],
                'declared': 5,
                'holder_after': {'role': 'active',
                                 'field_claim': 'held',
                                 'tracking': False, 'tick': 40},
                'journal_read': True,
                'journal': [{'point': 13, 'from': 'good',
                             'to': 'uncertain:stale', 'tick': 6},
                            {'point': 13, 'from': 'uncertain:stale',
                             'to': 'good', 'tick': 10}]},
            'rig': {'seats': {OWNER_SEAT: True, SLOW_SEAT: True,
                              FAST_SEAT: True},
                    'field_error': None,
                    'field_serving': False},
            'roles': {
                'before': {
                    'active': {'role': 'active', 'tick': 900,
                               'tracking': False},
                    'standby': {'role': 'standby', 'tick': 900,
                                'tracking': True}},
                'after': {
                    'active': {'role': 'active', 'tick': 902,
                               'tracking': False},
                    'standby': {'role': 'standby', 'tick': 902,
                                'tracking': True}},
                'final': {
                    'active': {'role': 'active', 'tick': 904,
                               'tracking': False},
                    'standby': {'role': 'standby', 'tick': 904,
                                'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_cadence(record,
                       lambda key, diagnostic, detail:
                       found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=CLAUSE):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    # The issue's doctored negatives. Each doctors the *evidence* — the
    # served rows, the durable journal records — because the judge
    # derives its verdict from those and never reads a stored verdict
    # field.
    #
    # Freshness asserted over a starvation that genuinely exceeds the
    # declared budget: the witness falls silent for far longer than the
    # patience — and longer than every gap the field itself demonstrated —
    # while every budgeted sample stays Good.
    expect('fresh-asserted-over-a-starvation',
           lambda r: r['asymmetric'].update(
               rows={'budgeted': held(1, 100, 300, value=0.2),
                     'witness': starving(0, 10, 10, 200)},
               journal=[]))
    # Stale asserted on inputs the contract declares fresh: the recorded
    # defect's own shape — a stale/good pair inside the judged window,
    # aged far inside the arrival period the witness demonstrated, on a
    # field that kept publishing every ten reader ticks.
    expect('stale-asserted-on-fresh-inputs',
           lambda r: r['asymmetric'].update(
               rows={'budgeted': series(
                   1, ['good'] * 5 + ['uncertain:stale'] * 4
                   + ['good'] * 45 + ['uncertain:stale'] * 5),
                   'witness': paced(0, 20, 10)}))
    # The same defect at the window's own opening: a stale presentation
    # one reader tick after a publication the witness just saw, on the
    # arrival evidence the run itself would judge fresh.
    expect('stale-at-the-windows-opening',
           lambda r: r['asymmetric'].update(
               rows={'budgeted': series(
                   1, ['good'] * 5 + ['uncertain:stale'] * 4
                   + ['good'] * 40 + ['uncertain:stale'] * 55),
                   'witness': paced(0, 20, 10)}))
    # Journaled transitions past the arrival evidence: a healthy field, a
    # Good verdict on every served sample, and a journal full of pairs.
    expect('journal-transitions-past-the-arrival-evidence',
           lambda r: r['asymmetric'].update(
               journal=[{'point': 13, 'from': 'good',
                         'to': 'uncertain:stale', 'tick': 42},
                        {'point': 13, 'from': 'uncertain:stale',
                         'to': 'good', 'tick': 60}]))
    # The control arm: the frozen field never presented its declared
    # staleness verdict.
    expect('frozen-field-never-stale',
           lambda r: r['symmetric'].update(
               rows={'budgeted': held(10, 2, 18, value=0.2),
                     'witness': held(10, 2, 18)}))
    # The declared staleness verdict reached no durable record.
    expect('declared-stale-never-journaled',
           lambda r: r['symmetric'].update(
               journal=[{'point': 10, 'from': 'unknown', 'to': 'good',
                         'tick': 10}]))
    # The budget leaked onto the undeclared witness.
    expect('budget-leaked-onto-the-witness',
           lambda r: r['asymmetric'].update(
               rows={'budgeted': r['asymmetric']['rows']['budgeted'],
                     'witness': [row[:1] + ['uncertain:stale'] + row[2:]
                                 if row[2] > 40 else row
                                 for row in paced(0, 20, 10)]}))
    # The budgeted point was stale before any induction, on the reader
    # paced like its field owner: the declared budget misfires on a
    # healthy rig.
    expect('stale-before-any-induction',
           lambda r: r['symmetric'].update(
               baseline={'budgeted': 'uncertain:stale',
                         'witness': 'good'}))
    # A degradation the freshness contract does not name: the rig's own
    # evidence, which the leg declines to attribute either way.
    expect('unnamed-degradation-inside-the-window',
           lambda r: r['asymmetric'].update(
               rows={'budgeted': series(
                   1, ['good'] * 5 + ['uncertain:stale'] * 4
                   + ['good'] * 90 + ['bad:communication_fault'] * 4),
                   'witness': paced(0, 20, 10)}), NONDET)
    # The instability shapes must report nondeterministic: refused
    # staging calls, an owner that never claimed, a reader that never
    # converged, an undeclared probe pair, a cadence never staged, a
    # freeze that never took, arrival evidence that never arrived, a
    # starved watch, an empty judged window, an unreadable journal, a
    # degraded contrast, a moved pair, a rig left standing.
    expect('field-stage-refused', lambda r:
           r.update(stage_error='docker run failed'), NONDET)
    expect('control-field-refused', lambda r:
           r['symmetric'].update(field_error='docker run failed'), NONDET)
    expect('subject-field-refused', lambda r:
           r['asymmetric'].update(field_error='docker run failed'), NONDET)
    expect('owner-launch-refused', lambda r:
           r['symmetric'].update(holder={'seat': OWNER_SEAT,
                                         'stage_error': 'docker run '
                                         'failed'}), NONDET)
    expect('control-reader-refused', lambda r:
           r['symmetric'].update(reader={'seat': SLOW_SEAT,
                                         'stage_error': 'docker run '
                                         'failed'}), NONDET)
    expect('subject-reader-refused', lambda r:
           r['asymmetric'].update(reader={'seat': FAST_SEAT,
                                          'stage_error': 'docker run '
                                          'failed'}), NONDET)
    expect('owner-never-claimed', lambda r:
           r['symmetric'].update(holder={'seat': OWNER_SEAT,
                                         'granted': None}), NONDET)
    expect('control-never-converged', lambda r:
           r['symmetric'].update(reader={'seat': SLOW_SEAT,
                                         'converged': None}), NONDET)
    expect('subject-never-converged', lambda r:
           r['asymmetric'].update(reader={'seat': FAST_SEAT,
                                          'converged': None}), NONDET)
    expect('control-probes-undeclared', lambda r:
           r['symmetric'].update(probes={'budgeted': None,
                                         'witness': None}), NONDET)
    expect('subject-probes-undeclared', lambda r:
           r['asymmetric'].update(probes={'budgeted': None,
                                          'witness': None}), NONDET)
    expect('control-pace-off-parity', lambda r:
           r['symmetric'].update(paced={'reader': 40, 'holder': 10,
                                        'ratio': 4.0}), NONDET)
    expect('subject-pace-never-staged', lambda r:
           r['asymmetric'].update(paced={'reader': 11, 'holder': 10,
                                         'ratio': 1.1}), NONDET)
    expect('freeze-never-completed', lambda r:
           r['symmetric'].update(freeze={'stopped': False,
                                         'error': 'no such container'}),
           NONDET)
    expect('freeze-never-froze-the-field', lambda r:
           r['symmetric'].update(rows={
               'budgeted': held(10, 2, 8, value=0.2),
               'witness': held(10, 8, 8)}), NONDET)
    expect('control-watch-starved', lambda r:
           r['symmetric'].update(rows={'budgeted': [], 'witness': []},
                                 observations=[]), NONDET)
    expect('subject-watch-starved', lambda r:
           r['asymmetric'].update(rows={'budgeted': [], 'witness': []},
                                  observations=[]), NONDET)
    expect('arrival-never-arrived', lambda r:
           r['asymmetric'].update(warm={'boundary': 40,
                                        'watched': False}), NONDET)
    expect('judged-window-empty', lambda r:
           r['asymmetric'].update(
               rows={'budgeted': series(1, ['good'] * 9),
                     'witness': paced(0, 20, 10)},
               warm={'boundary': 40, 'watched': True}), NONDET)
    expect('control-journal-unreadable', lambda r:
           r['symmetric'].update(journal_read=False), NONDET)
    expect('subject-journal-unreadable', lambda r:
           r['asymmetric'].update(journal_read=False), NONDET)
    expect('control-baseline-degraded', lambda r:
           r['symmetric'].update(
               baseline={'budgeted': 'good',
                         'witness': 'bad:out_of_range'}), NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'), NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['final']['standby'].update(tracking=False), NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=900), NONDET)
    expect('rig-left-standing', lambda r:
           r['rig']['seats'].update({FAST_SEAT: False}), NONDET)
    expect('field-left-serving', lambda r:
           r['rig'].update(field_serving=True), NONDET)
    expect('field-presence-unreadable', lambda r:
           r['rig'].update(field_serving=None), NONDET)
    return slipped


def _cadence_sweep(ctx, seats):
    """Best-effort teardown of one arm's seats and the scratch field — a
    clean pass leaves nothing standing and the rig's claim state free, and
    an aborted arm gets the same sweep so the next staging starts on free
    seats and a fresh field. Returns the field's own removal error, or
    None when it came down."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        for seat in seats:
            try:
                lever(seat)
            except Exception:
                pass
    try:
        if ctx.get('stop_born_field') is None:
            return None
        ctx['stop_born_field']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _cadence_rig_state(ctx, field_error):
    """The rig's claim state after the sweep: every born seat's own
    presence read back through the read-only state lever, beside the
    scratch field's own removal error and whether its own shipped tool
    still answers — a field or a seat that outlived the sweep is a claim
    the legs behind this one would inherit. `absent` None is a read the
    lever could not answer, never a seat proven gone."""
    serving = None
    probe = ctx.get('born_field_ctl')
    if probe is not None:
        try:
            serving = probe('list').returncode == 0
        except Exception:
            serving = None
    return {'seats': {seat: (_cadence_state(ctx, seat) or {}).get('absent')
                      for seat in (OWNER_SEAT, SLOW_SEAT, FAST_SEAT)},
            'field_error': field_error,
            'field_serving': serving}


def scenario_stale_budget_cadence(ctx):
    """Exercise the declared freshness budget's cadence-domain contract on
    the simulated QA rig: with the field owner held at its declared
    cadence, a standby seat launched through the per-container cadence
    lever at several times the owner's pace must hold the freshness
    contract the fix declares — the declared-budget input Good on every
    served sample past the reader's own arrival evidence, with the
    durable journal carrying no transition the contract does not name —
    while the symmetric-cadence pair keeps its declared staleness
    behavior, a frozen field's budgeted input aging to Uncertain(Stale)
    one measured declared budget past its last publication. Two
    consecutive passes produce identical outcome digests."""
    case = Case(
        'stale-budget-cadence',
        'A reader outpacing the field owner never calls a healthy remote '
        'input stale',
        'on the lane\'s own scratch field a born-active holds '
        'write-ownership at the documented cadence while a reader '
        'launched through the per-container cadence lever at a tenth of '
        'it tracks the owner: past the arrival evidence the reader has '
        'demonstrated, the declared-budget field input presents Good on '
        'every served sample — freshness judged in the declared domain — '
        'and the reader\'s durable journal carries no quality_changed '
        'transition past that evidence, while the control arm\'s '
        'symmetric-cadence reader still ages the same input to '
        'Uncertain(Stale) once the field stops stepping, one measured '
        'declared budget past its last publication, with the undeclared '
        'input beside it Good throughout; the rig\'s claim state and '
        'launch roles are restored — audited back over the swept rig, a '
        'seat or field that outlived the sweep fails the leg — and two '
        'passes produce identical digests')
    try:
        missing = [key for key in ('start_born_field', 'stop_born_field',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no born-field staging levers: '
                               + ', '.join(missing))
        if not all(ctx.get(seat)
                   for seat in (OWNER_SEAT, SLOW_SEAT, FAST_SEAT)):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the leg\'s born '
                               'seats')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in (SLOW_SEAT, FAST_SEAT)):
            return case.finish('inconclusive', 'the run context carries no '
                               'per-seat journal files — the durable half '
                               'of the audit cannot run')
        deadline = time.monotonic() + CADENCE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=CADENCE_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is unreachable — '
                    'monitor endpoints ' + str(ctx.get('active'))
                    + ' and ' + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=CADENCE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no tracking '
                               'standby — the settled posture the leg '
                               'proves undisturbed was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' (' + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            try:
                record = _cadence_pass(ctx, number, launch)
            finally:
                # The pass ends with the rig swept — the subject arm's
                # seats and its scratch field removed, so the next pass
                # and the legs behind this one start on a free claim
                # state, free seats, and no field.
                field_error = _cadence_sweep(ctx, (FAST_SEAT, OWNER_SEAT))
            # The restoration audit, read back over the swept rig: the
            # seats' own presence, the scratch field's own liveness, and
            # the pair's launch roles once the leg's claim is gone.
            record['rig'] = _cadence_rig_state(ctx, field_error)
            record['roles']['final'] = {
                name: _cadence_posture(ctx, ctx[name])
                for name in (owner, peer)}
            _judge_cadence(record, note)
            digest = _cadence_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'stale-budget-cadence-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'stale-budget-cadence pass ' + str(number)
                          + ' — the control arm\'s staged field and the '
                          'declared budget its frozen field measured, the '
                          'subject arm\'s measured cadence ratio and '
                          'arrival evidence, its judged window of served '
                          'rows and durable quality_changed records, the '
                          'deployed pair\'s before/after/final framing, '
                          'the swept rig\'s restoration read, and the '
                          'normalized digest')
            if violations:
                name = CLAUSE if any(
                    diagnostic == CLAUSE
                    for diagnostic, _ in violations.values()) else NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', NONDET + ': the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two stale-budget-cadence passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg can
        # no longer catch what it names.
        slipped = _cadence_self_check()
        if slipped:
            return case.finish('failed', UNCHECKED + ': planted negatives '
                               'slipped the leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
