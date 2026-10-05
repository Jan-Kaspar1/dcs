"""The mutual_tracking_tick leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the deployed pair's launch layout and
# restores it — it demotes the field owner, both peers stand by and
# track each other, one of them is frozen while the other's quiesced
# scans seed its standing lead, and a following promote walks the pair
# back — so it sits in the launch-layout window the tracker's tick
# cluster keeps, behind the realign leg whose own restore leaves the
# same layout standing, and ahead of the failover leg that races a
# promote from the restored posture.
RUNS_AFTER = frozenset({'scenario_tracker_realign_tick_order'})
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The bounded tick-domain contract under mutual standby tracking —
# the per-revision lane evidence for #693's landed fix, in service of
# WW-LCM-001's continuity clause and WW-OPS-002's durable-record
# clause. Mutual tracking is the transient state every demote creates:
# the demoted owner adopts the source its sibling's pulls announced
# and the configured peer keeps its declared wiring, so each peer
# tracks the other while neither owns the field. Before the fix a
# tracking apply landed the pulled checkpoint at `tick + offset` — an
# offset carried unchanged since the run's clock was last seeded ahead
# of the stream — so a regressed apply seeded a nonzero tick_offset
# that never cleared: each peer's apply landed at the other's served
# tick plus its own offset and served the sum back, a positive-feedback
# ratchet measured at ~920 rising to ~3000 ticks/s against the
# container's own 10/s scan cadence, and the journaled and persisted
# discontinuity left every tick-ordered artifact — receipt apply
# ticks, journal event ticks, aligned values — on an epoch that never
# corresponded to real scans. The settled contract is that a tracking
# apply keeps the peers' tick domains within scan cadence of each other
# and of wall time, a seeded offset clears once the tracked stream
# recovers rather than ratcheting, and a promotion continues the tick
# domain without a journaled mega-jump.
#
# The leg stages that shape on the deployed pair through the runner's
# own freeze lever — `docker pause` on one peer while the other's own
# scans advance past its frozen served tick, which is the regression
# that seeds the offset — and judges it through both peers' serving
# monitors and their per-controller `--journal-file`s:
#
# - the cadence is measured, not assumed: before the staging the leg
#   samples both peers' served run ticks across a settled window, so
#   the pair's own `--scan-ms` pacing is the baseline every later
#   reading is judged against and the contract holds whatever the rig's
#   launch cadence is;
# - the mutual settle: `POST /demote` on the field owner, then both
#   peers reporting `standby` with the ownerless line's `orphaned`
#   verdict and an aligned stream tick — the mark the offset is
#   measured against;
# - the frozen window: one peer paused so its monitor socket stays
#   bound but unanswered while the survivor's scans advance past its
#   frozen served tick. The survivor's run tick must stay inside the
#   measured cadence's bound across the window — the ratchet the fix
#   closed ran two orders of magnitude past it — and the standing lead
#   over the frozen mark must form, or the staging proved nothing;
# - the recovery: the frozen peer thawed, the resumed stream's applies
#   realigning it, and both peers' aligned marks returning within the
#   declared clearing bound of their own run ticks while the runs stay
#   within scan cadence of each other — the seed cleared, not
#   compounded;
# - the durable half: each peer's declared journal file carries the
#   run's single cold-start boundary, a non-regressing integer tick
#   axis, every stamp inside the cadence's bound (no journaled
#   mega-jump), and no `source_restarted` on a same-generation stream
#   that never restarted;
# - the promote: the orphaned verdict is promotable, the promotion
#   continues the run's tick domain — the demoted peer's durable role
#   walk `standby → promoting → active` at continuing ticks under that
#   one boundary — and the pair's launch roles restore.
#
# Named diagnostics: mutual-tracking-tick-failed tags the contract
# clauses — a served run tick regressing, a run's tick rate leaving the
# measured cadence's bound, the peers' clocks leaving scan cadence of
# each other, an aligned mark never clearing, a durable tick axis
# regressing or stamping a tick past the bound, a second run boundary
# or a journaled source restart on the same-generation stream, the
# promotion restarting the tick domain, the launch roles unrestored —
# and mutual-tracking-tick-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a starved
# watch, a freeze that never took, the seed never forming, a cadence
# the rig never demonstrated, a durable journal that cannot be read, a
# staged run that predates the contract's surface — a served checkpoint
# without the `tick`/`source_owns_field`/`generation` vocabulary — and
# two passes whose digests diverge. A rig that cannot stage the leg at
# all — an unreachable pair, a single endpoint, no freeze lever, no
# per-controller journal file, an unkeyed pair with no staged probe —
# reports inconclusive. The unchecked-diagnostic self-check replays the
# judge over planted negatives — the issue's named ratcheting-offset case
# and the persisted discontinuity, the tick regression, the uncleared
# seed, the mega-jump, the restart boundary — and reports
# mutual-tracking-tick-unchecked for any that slip through.

MUTUAL_SETTLE = 30     # bound on the mutual-standby settle, the
                       # recovery's reconvergence, and the restore
MUTUAL_POLL = 0.4      # the watch cadence through the settle and the
                       # recovery
CADENCE_MEASURE = 4.0  # seconds the baseline cadence is sampled over
WINDOW_HOLD = 4.0      # seconds of the frozen window — dozens of
                       # ticks at the container's own pacing, and far
                       # under the armed failover budget
WINDOW_POLL = 0.5      # the frozen window's watch cadence
CADENCE_TOLERANCE = 4  # the ratchet bound as a multiple of the
                       # measured cadence: the defect ran ~90x past
                       # the rig's own 10/s cadence
CLEAR_BOUND = 3        # reader ticks the aligned mark may sit behind
                       # its run tick once the stream has recovered —
                       # the declared clearing bound
SPACING_BOUND = 3      # reader ticks the two peers' run ticks may
                       # differ by — scan cadence, not more
JOURNAL_SLACK = 40     # the pull pipeline's in-flight lag, in reader
                       # ticks, absorbed by the durable bound: the
                       # mega-jump this audit refuses is orders of
                       # magnitude past it
DIAG_FAILED = 'mutual-tracking-tick-failed'
DIAG_NONDET = 'mutual-tracking-tick-nondeterministic'
DIAG_UNCHECKED = 'mutual-tracking-tick-unchecked'


def _int_tick(value):
    """Whether a served or journaled field is an integer tick — a bool
    is not, and neither is an absent stamp."""
    return isinstance(value, int) and not isinstance(value, bool)


def _mutual_row(ctx, name):
    """One normalized watch row off a peer's served /role — the
    observation the tick-domain audit replays: the reported role, the
    run tick, the sync verdict's kind, and the aligned stream tick the
    converged verdicts carry. None when the peer does not answer — a
    dropped observation, never a row."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    sync = report.get('sync')
    kind = aligned = None
    if isinstance(sync, dict) and sync:
        kind = next(iter(sync))
        detail = sync.get(kind)
        aligned = detail.get('aligned') if isinstance(detail, dict) else None
    elif isinstance(sync, str):
        kind = sync
    return {
        'role': report.get('role'),
        'tick': report.get('tick'),
        'sync': kind,
        'aligned': aligned if _int_tick(aligned) else None,
    }


def _sample_ticks(ctx, names, seconds):
    """One cadence sample: each named peer's served run tick and the
    monotonic reading it was taken at. The pair scans itself, so the
    wall clock beside the served tick is what makes the cadence
    measurable — the rig's launch pacing, read rather than assumed."""
    read = {'at': time.monotonic()}
    for name in names:
        row = _mutual_row(ctx, name)
        read[name] = (row or {}).get('tick')
    return read


def _measured_cadence(ctx, names, seconds):
    """Each peer's observed run ticks per second across a settled
    `seconds` window — the pair's own scan cadence, the baseline every
    later reading is judged against. The measured rate below the floor
    is clamped to the floor so a coarse clock cannot make the bound
    vacuous."""
    first = _sample_ticks(ctx, names, seconds)
    deadline = first['at'] + seconds
    while time.monotonic() < deadline:
        time.sleep(CADENCE_MEASURE / 4.0)
    last = _sample_ticks(ctx, names, seconds)
    span = last['at'] - first['at']
    rates = {}
    for name in names:
        before, after = first.get(name), last.get(name)
        if _int_tick(before) and _int_tick(after) and span > 0:
            rates[name] = (after - before) / span
    return rates


def _contract_checkpoint(ctx, name):
    """A peer's served checkpoint when it carries the tick-domain
    contract surface the leg audits — the integer run `tick`, the
    `source_owns_field` stamp the ownerless verdict is reported off,
    and the line's `generation` whose continuity the promotion must
    keep. None otherwise: the pre-contract shape, which the judge
    classifies rather than measures."""
    try:
        _, document = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    if not isinstance(document, dict):
        return None
    if not _int_tick(document.get('tick')):
        return None
    if not isinstance(document.get('source_owns_field'), bool):
        return None
    if 'generation' not in document:
        return None
    return document


def _run_tail(path):
    """A peer journal file's run-boundary count and the current run's
    entry records after the last marker — file order is append order,
    the axis the audit walks."""
    boundaries, entries = 0, []
    for item in _journal_entries(path):
        if 'run_boundary' in item:
            boundaries += 1
            entries = []
            continue
        entry = item.get('entry')
        if isinstance(entry, dict):
            entries.append(entry)
    return boundaries, entries


def _audit_ticks(rows, name):
    """One peer's served-tick audit over its collected rows: no run tick
    regresses — a tracking apply lands at the later of the two clocks,
    so the run's clock never goes back. Returns the rewinds."""
    regressions, mark = [], None
    for row in rows:
        if not isinstance(row, dict):
            continue
        tick = row.get('tick')
        if not _int_tick(tick):
            continue
        if mark is not None and tick < mark:
            regressions.append({'from': mark, 'to': tick})
        else:
            mark = tick
    return regressions


def _measured_rate(clocks, index):
    """One peer's run ticks per second across the pass's collected
    `(monotonic, owner tick, peer tick)` clocks — the ratchet claim the
    contract makes is a rate, not an absolute stamp, so this is what
    the measured cadence is compared against."""
    usable = [entry for entry in clocks
              if _int_tick(entry[index])]
    if len(usable) < 2:
        return None
    span = usable[-1][0] - usable[0][0]
    if span <= 0:
        return None
    return (usable[-1][index] - usable[0][index]) / span


def _audit_axis(entries, name, ceiling):
    """One peer's durable tick-axis audit: every gained entry carries an
    integer tick, the axis never regresses within the run, and no stamp
    leaves the run's own ceiling — a journaled mega-jump onto an epoch
    no scan ever reached. Returns `(regressions, over, malformed)`."""
    regressions, over, malformed, mark = [], [], 0, None
    for entry in entries:
        tick = entry.get('tick')
        if not _int_tick(tick):
            malformed += 1
            continue
        if mark is not None and tick < mark:
            regressions.append({'seq': entry.get('seq'), 'tick': tick,
                                'below': mark})
        else:
            mark = tick
        if tick > ceiling:
            over.append({'seq': entry.get('seq'), 'tick': tick})
    return regressions, over, malformed


def _role_walk(entries):
    """The `role_changed` stream of a journal entry list —
    `(from, to)` per entry, in `seq` order."""
    return [
        (entry['event']['role_changed']['from'],
         entry['event']['role_changed']['to'])
        for entry in entries
        if isinstance(entry.get('event'), dict)
        and isinstance(entry['event'].get('role_changed'), dict)
    ]


def _judge_mutual_tick(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the tick-domain
    contract clauses — the served or journaled tick regressing, a run's
    rate leaving the measured cadence's bound, the peers' clocks
    leaving scan cadence of each other, an aligned mark that never
    cleared, a journaled mega-jump, a second run boundary, a journaled
    source restart on a same-generation stream, the promotion
    restarting the tick domain, the launch roles unrestored — and
    DIAG_NONDET tags the instability the contract does not answer for:
    a refused staging call, a starved watch, a freeze that never took,
    the seed never forming, a cadence the rig never demonstrated, a
    durable journal that cannot be read. An aborted stage ends the
    audit where the pass ended — the later keys it never wrote are not
    clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('journal_error') is not None:
        nondet('journal-read', "a peer's durable journal could not be "
               'read: ' + str(record['journal_error']))
        return
    surface = record.get('surface')
    if not isinstance(surface, dict) or not surface.get('ok'):
        nondet('surface', 'the staged run served no tick-domain '
               'contract surface: ' + json.dumps(surface)[:200])
        return
    cadence = record.get('cadence')
    if not isinstance(cadence, dict) or not cadence:
        nondet('cadence', 'the rig never demonstrated a scan cadence on '
               'the pair the staging needs: '
               + json.dumps(cadence)[:200])
        return
    for name, regressions in sorted((record.get('regressions')
                                     or {}).items()):
        if regressions:
            failed('regression', "the " + name + " peer's served run "
                   'tick rewound: ' + json.dumps(regressions[:4])[:240])
    for name, entry in sorted((record.get('rates') or {}).items()):
        rate = entry.get('rate')
        if rate is None:
            nondet('cadence', "the " + name + " peer's run tick rate "
                   'could not be measured across the pass — the ratchet '
                   'claim has no window to read')
        elif rate > entry.get('bound', 0):
            failed('ratchet', "the " + name + " peer's run clock advanced "
                   + str(round(rate, 1)) + ' ticks/s against the '
                   "measured cadence's bound of "
                   + str(round(entry.get('bound', 0), 1))
                   + ' ticks/s — the seeded offset ratcheted instead of '
                   'clearing')
    spacing = record.get('spacing')
    if isinstance(spacing, list) and spacing:
        drifted = next((entry for entry in spacing
                        if entry.get('apart', 0) > SPACING_BOUND), None)
        if drifted is not None:
            failed('spacing', "the peers' run ticks left scan cadence of "
                   'each other: ' + json.dumps(drifted)[:240])
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        nondet('demote', "the mutual-standby demote answered "
               + str(demote.get('status')) + ' '
               + json.dumps(demote.get('body'))[:160])
        return
    settled = record.get('mutual')
    if not isinstance(settled, dict) or not settled:
        nondet('watch', 'the mutual settle collected no served rows — '
               'the starved monitor gave the audit nothing to read')
    else:
        off = next((name for name, entry in sorted(settled.items())
                    if not entry.get('posture', '').startswith(
                        'standby/orphaned')), None)
        if off is not None:
            failed('posture', 'the ' + off + " peer settled "
                   + str((settled[off] or {}).get('posture'))
                   + ' inside the mutual-standby window — the tracked '
                   'line stamps no field owner, so the verdict owed is '
                   'orphaned with an aligned mark')
    window = [row for row in (record.get('window') or []) if row]
    if not window:
        nondet('watch', 'the frozen window collected no served rows — '
               'the starved monitor gave the audit nothing to read')
    if record.get('freeze_error') is not None:
        nondet('freeze', 'the frozen-source induction never landed: '
               + str(record['freeze_error']))
    if record.get('seeded') is None:
        nondet('seed', "the frozen window left no standing lead over "
               "the held stream — the quiesced scans did not pace the "
               "run's clock past the frozen mark: "
               + json.dumps(window[-3:])[:240])
    cleared = record.get('cleared')
    if record.get('seeded') is not None and cleared is None:
        failed('clear', 'the recovered stream never cleared the seeded '
               'offset — both peers stayed outside the declared '
               'clearing bound of their own run ticks: '
               + json.dumps(record.get('recovery') or [])[:300])
    elif isinstance(cleared, dict):
        for name, offset in sorted(cleared.items()):
            if not _int_tick(offset) or abs(offset) > CLEAR_BOUND:
                failed('clear', "the " + name + " peer's aligned mark sits "
                       + str(offset) + ' ticks from its run tick — the '
                       'seeded offset must clear inside the declared '
                       'bound of ' + str(CLEAR_BOUND))
    promote = record.get('promote') or {}
    if record.get('cleared') is not None and promote.get('status') != 200:
        nondet('promote', "the promotion out of the orphaned verdict "
               'answered ' + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160])
    elif promote.get('status') == 200:
        axis = record.get('axis') or {}
        ceiling = record.get('ceiling')
        for name, entry in sorted(axis.items()):
            if entry.get('regressions'):
                failed('axis', "the " + name + " peer's durable tick "
                       'axis regressed: '
                       + json.dumps(entry['regressions'][:4])[:240])
            if entry.get('over'):
                failed('mega-jump', "the " + name + " peer's durable "
                       'journal stamped a tick past the run\'s own '
                       'ceiling '
                       + str(ceiling) + ' — a journaled discontinuity '
                       'onto an epoch no scan reached: '
                       + json.dumps(entry['over'][:4])[:240])
            if entry.get('malformed'):
                failed('axis-shape', "the " + name + " peer's durable "
                       'journal holds ' + str(entry['malformed'])
                       + ' entry records carrying no integer tick — the '
                       'attributed-record axis the contract pins is '
                       'malformed')
            if entry.get('boundaries', 1) != 1:
                failed('boundary', "the " + name + " peer's durable "
                       'journal carries '
                       + str(entry.get('boundaries'))
                       + ' run boundaries — the promotion must continue '
                       "the run's tick domain, not restart it")
            if entry.get('restarts'):
                failed('restart', "the " + name + " peer's durable "
                       'journal records a source restart the '
                       'same-generation stream never had')
        walk = record.get('walk') or {}
        promoted = walk.get('owner') or []
        if promoted and promoted[-2:] != [('standby', 'promoting'),
                                          ('promoting', 'active')]:
            failed('walk', "the promoted peer's durable role walk reads "
                   + json.dumps(promoted)
                   + " — the promotion must continue the demoted peer's "
                   'run through standby → promoting → active')
        demoted = walk.get('peer') or []
        if demoted and demoted != [('active', 'demoting'),
                                   ('demoting', 'standby')]:
            failed('walk', "the frozen peer's durable role walk reads "
                   + json.dumps(demoted)
                   + ' — the demotion that opened the mutual settle must '
                   'record exactly that walk')
    # The launch roles are a contract clause only where the leg's own
    # staging reached the restore: a refused promotion is the
    # instability the `promote` clause above names, and reading the
    # unrestored layout on top of it would report the rig's refusal as
    # a product failure.
    if not record.get('restored') and (promote or {}).get('status') == 200:
        failed('roles', 'the pair never settled back to its launch roles '
               '— ' + str(record.get('final')))


def _mutual_tick_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'surface': 'stamped' if clean('surface') else 'unstamped',
        'cadence': 'measured' if clean('cadence') else 'unproven',
        'ticks': 'bounded' if clean('regression', 'ratchet', 'spacing')
            else 'ratcheting',
        'posture': 'mutual' if clean('posture', 'demote', 'watch')
            else 'unsettled',
        'seed': 'standing' if clean('seed', 'freeze') else 'none',
        'offset': 'cleared' if clean('clear') else 'standing',
        'axis': 'ordered' if clean('axis', 'axis-shape', 'mega-jump',
                                   'boundary', 'restart', 'walk')
            else 'discontinuous',
        'roles': 'restored'
            if clean('roles', 'promote') else 'unrestored'}


def _mutual_tick_self_check():
    """The leg's unchecked-diagnostic self-test: replay the tick-domain
    judge over each planted negative the issue names — the seeded
    offset ratcheting, the persisted discontinuity, a served tick
    regressing, the seed never clearing, a journaled mega-jump, a
    second run boundary, a journaled source restart — and require the
    judge to note each. The instability shapes must report
    nondeterministic, not failed. A silent judge returns the negative
    names it let through."""
    slipped = []

    def clean_record():
        return {
            'surface': {'ok': True},
            'cadence': {'active': 10.0, 'standby': 10.0},
            'regressions': {},
            'rates': {'active': {'rate': 10.0, 'bound': 40.0},
                      'standby': {'rate': 10.0, 'bound': 40.0}},
            'spacing': [{'apart': 1}],
            'demote': {'status': 200, 'body': {'role': 'demoting'}},
            'mutual': {'active': {'posture': 'standby/orphaned',
                                  'aligned': 120},
                       'standby': {'posture': 'standby/orphaned',
                                   'aligned': 119}},
            'window': [{'tick': 121, 'sync': 'degraded'},
                       {'tick': 124, 'sync': 'degraded'}],
            'freeze_error': None,
            'seeded': 4,
            'cleared': {'active': 0, 'standby': -1},
            'promote': {'status': 200, 'body': {'role': 'promoting'}},
            'ceiling': 400,
            'axis': {'active': {'boundaries': 1, 'regressions': [],
                                'over': [], 'malformed': 0,
                                'restarts': 0},
                     'standby': {'boundaries': 1, 'regressions': [],
                                 'over': [], 'malformed': 0,
                                 'restarts': 0}},
            'walk': {'owner': [('active', 'demoting'),
                               ('demoting', 'standby'),
                               ('standby', 'promoting'),
                               ('promoting', 'active')],
                     'peer': []},
            'restored': {'active': 'active', 'standby': 'tracking'},
        }

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_mutual_tick(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The issue's named negative first: the seeded offset ratcheting —
    # the compounded lead the defect served back, both peers' ticks
    # leaving the measured cadence's bound.
    expect('ratcheting-offset', lambda record: record.update(
        {'rates': {'active': {'rate': 920.0, 'bound': 40.0},
                   'standby': {'rate': 2990.0, 'bound': 40.0}},
         'spacing': [{'apart': 240}]}))
    # The persisted discontinuity: a journaled mega-jump past the
    # cadence bound, and the tick axis regressing under it.
    expect('persisted-discontinuity', lambda record: record['axis']
           .__setitem__('standby', {'boundaries': 1, 'regressions': [],
                                    'over': [{'seq': 9, 'tick': 3000}],
                                    'malformed': 0, 'restarts': 0}))
    expect('axis-regressed', lambda record: record['axis']
           .__setitem__('active', {'boundaries': 1,
                                   'regressions': [{'seq': 9,
                                                    'tick': 12,
                                                    'below': 120}],
                                   'over': [], 'malformed': 0,
                                   'restarts': 0}))
    # A served run tick rewinding — a tracking apply lands at the later
    # of the two clocks, so the run's clock never goes back.
    expect('served-regression', lambda record: record.update(
        {'regressions': {'active': [{'from': 121, 'to': 90}]}}))
    # The seed never clearing once the stream recovered.
    expect('seed-standing', lambda record: record.update({'cleared': None}))
    expect('offset-uncleared', lambda record: record['cleared']
           .__setitem__('active', 40))
    # A second run boundary — the promotion restarting the tick domain —
    # and a journaled source restart on a stream that never restarted.
    expect('restart-boundary', lambda record: record['axis']
           .__setitem__('standby', {'boundaries': 2, 'regressions': [],
                                    'over': [], 'malformed': 0,
                                    'restarts': 0}))
    expect('source-restarted', lambda record: record['axis']
           .__setitem__('active', {'boundaries': 1, 'regressions': [],
                                   'over': [], 'malformed': 0,
                                   'restarts': 1}))
    # The mutual posture settling on anything but the ownerless verdict.
    expect('posture-unsettled', lambda record: record['mutual']
           .__setitem__('active', {'posture': 'standby/tracking'}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: an unstaged contract surface, an
    # unmeasured cadence, a starved watch, a refused freeze, the seed
    # never forming, an unreadable durable journal, a refused demote.
    expect('surface-unstamped', lambda record: record.update(
        {'surface': {'ok': False}}), DIAG_NONDET)
    expect('cadence-unproven', lambda record: record.update(
        {'cadence': {}}), DIAG_NONDET)
    expect('rate-unmeasurable', lambda record: record['rates']
           .__setitem__('active', {'rate': None, 'bound': 40.0}),
           DIAG_NONDET)
    expect('watch-starved', lambda record: record.update(
        {'window': [], 'mutual': {}, 'seeded': None}), DIAG_NONDET)
    expect('freeze-refused', lambda record: record.update(
        {'freeze_error': 'docker pause failed', 'seeded': None}),
        DIAG_NONDET)
    expect('journal-unreadable', lambda record: record.update(
        {'journal_error': 'the journal does not parse'}), DIAG_NONDET)
    expect('demote-refused', lambda record: record.update(
        {'demote': {'status': 409, 'body': 'no_tracking_source'}}),
        DIAG_NONDET)
    return slipped


def _restore_launch(ctx, owner, peer):
    """Best-effort launch-layout restore on the deployed pair: thaw a
    frozen peer, then walk the pair back to the launch roles — the
    named owner holding the field, the sibling tracking behind it.
    Every step is retried inside the bound and swallowed on refusal."""
    try:
        ctx['unpause_controller'](owner)
    except Exception:
        pass
    try:
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + MUTUAL_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(MUTUAL_POLL)
    except Exception:
        pass


def _mutual_pass(ctx, number, owner, peer, journal_paths):
    """One bounded tick-domain pass: measure the pair's scan cadence,
    demote the field owner so both peers stand by and track each
    other, freeze one peer so the survivor's quiesced scans seed its
    standing lead, thaw it and watch the recovery clear the seed, then
    promote the demoted peer back and audit both durable journals
    against the cadence bound. Returns `(record, evidence)`: the record
    is what the judge replays; an aborted stage simply leaves its later
    keys absent for the judge to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}

    # Phase 1 — the contract surface and the measured cadence. Each
    # absence is the staged run's pre-contract shape, which the judge
    # names rather than measures.
    record['surface'] = {
        name: bool(_contract_checkpoint(ctx, name))
        for name in (owner, peer)
    }
    record['surface']['ok'] = all(record['surface'][name]
                                  for name in (owner, peer))
    if not record['surface']['ok']:
        return record, evidence
    rates = _measured_cadence(ctx, (owner, peer), CADENCE_MEASURE)
    record['cadence'] = {name: round(rate, 3)
                         for name, rate in rates.items()}
    evidence['cadence'] = record['cadence']
    if not rates:
        return record, evidence
    # The rate bound: the measured cadence per peer, widened by the
    # declared tolerance. The defect ratcheted at ~90x its own
    # cadence, so the margin is wide enough for a coarse clock and
    # nowhere near the compounding landing it must catch.
    record['rate_bounds'] = {
        name: rate * CADENCE_TOLERANCE for name, rate in rates.items()}

    rows = {owner: [], peer: []}
    clocks = []
    spacing = []

    def watch(names):
        frame = {name: _mutual_row(ctx, name) for name in names}
        at = time.monotonic()
        for name, row in frame.items():
            rows[name].append(row)
        clocks.append(
            (at, frame[owner].get('tick') if frame[owner] else None,
             frame[peer].get('tick') if frame[peer] else None))
        # The peers' run-tick spacing is a settled-stream reading: the
        # seeded lead legitimately stands between the thaw and the
        # realigning apply that clears it — the seed the clear clause
        # names — so a frame counts toward the spacing bound only once
        # both peers' own clocks sit within the clearing bound of their
        # aligned marks. A frame carrying a standing seed is the
        # contract's recovery window, not a spacing violation.
        settled = [
            row['tick'] for row in frame.values()
            if row and _int_tick(row.get('tick'))
            and _int_tick(row.get('aligned'))
            and abs(row['tick'] - row['aligned']) <= CLEAR_BOUND]
        if len(settled) == 2:
            spacing.append({'apart': abs(settled[0] - settled[1])})
        return frame

    # Phase 2 — the mutual settle: `POST /demote` on the field owner.
    # The demoted member adopts the announced source its sibling's
    # pulls recorded while the configured peer keeps its declared
    # wiring, so each peer tracks the other while neither owns the
    # field — the transient state the defect was found in.
    status, body = _settle_call(ctx[owner] + '/demote')
    record['demote'] = {'status': status, 'body': body}
    if status != 200:
        return record, evidence
    deadline = time.monotonic() + MUTUAL_SETTLE
    while time.monotonic() < deadline:
        frame = watch((owner, peer))
        if all(row and row.get('role') == 'standby'
               and row.get('sync') == 'orphaned'
               and _int_tick(row.get('aligned'))
               for row in frame.values()):
            break
        time.sleep(MUTUAL_POLL)
    record['mutual'] = {
        name: {
            'posture': str((row or {}).get('role')) + '/'
            + str((row or {}).get('sync')),
            'aligned': (row or {}).get('aligned'),
        }
        for name, row in (
            (owner, frame.get(owner)), (peer, frame.get(peer)))
    }
    evidence['mutual'] = record['mutual']
    if not record['mutual'][owner]['posture'].startswith('standby/orphaned'):
        return record, evidence

    # Phase 3 — the seeded offset: the demoted owner frozen so the
    # configured peer's own scans advance its run clock past the frozen
    # peer's served tick. The stream position cannot move while the
    # frozen peer stands still, so the survivor's standing lead over
    # that mark is the offset the recovered stream must clear.
    mark = (frame.get(owner) or {}).get('tick')
    if not _int_tick(mark):
        record['window'] = []
        return record, evidence
    try:
        ctx['pause_controller'](peer)
    except Exception as exc:
        record['freeze_error'] = str(exc)[:300]
        record['window'] = []
        return record, evidence
    window = []
    try:
        deadline = time.monotonic() + WINDOW_HOLD
        while time.monotonic() < deadline:
            row = _mutual_row(ctx, owner)
            window.append(row)
            rows[owner].append(row)
            clocks.append(
                (time.monotonic(), row.get('tick') if row else None, None))
            time.sleep(WINDOW_POLL)
    finally:
        try:
            ctx['unpause_controller'](peer)
        except Exception as exc:
            record['freeze_error'] = str(exc)[:300]
    record['window'] = window
    advanced = [row['tick'] for row in window
                if row and _int_tick(row.get('tick'))]
    # The seed is a *lead*: the survivor's run clock standing ahead of
    # the held stream. No lead means the staging proved nothing, which
    # the judge reads as an instability rather than a verdict.
    record['seeded'] = (advanced[-1] - mark) \
        if advanced and advanced[-1] > mark else None
    evidence['seeded'] = record['seeded']
    evidence['window'] = [
        {'tick': row.get('tick'), 'sync': row.get('sync')}
        for row in window if row]

    # Phase 4 — the recovery: the thawed peer's resumed stream
    # realigns the pair, and both aligned marks return inside the
    # declared clearing bound of their own run ticks — the seed
    # cleared rather than compounding.
    recovery = []
    cleared = None
    deadline = time.monotonic() + MUTUAL_SETTLE
    while time.monotonic() < deadline:
        frame = watch((owner, peer))
        recovery.append({
            name: {'tick': (row or {}).get('tick'),
                   'aligned': (row or {}).get('aligned'),
                   'sync': (row or {}).get('sync')}
            for name, row in ((owner, frame.get(owner)),
                              (peer, frame.get(peer)))})
        settled = all(
            row and _int_tick(row.get('aligned'))
            and _int_tick(row.get('tick'))
            for row in frame.values())
        if settled:
            offsets = {
                name: frame[name]['tick'] - frame[name]['aligned']
                for name in (owner, peer)}
            apart = abs(frame[owner]['tick'] - frame[peer]['tick'])
            if all(abs(offset) <= CLEAR_BOUND for offset in offsets.values()) \
                    and apart <= SPACING_BOUND:
                cleared = offsets
                break
        time.sleep(MUTUAL_POLL)
    record['cleared'] = cleared
    record['recovery'] = recovery[-1:] if recovery else []
    evidence['cleared'] = cleared
    if cleared is None:
        return record, evidence

    # Phase 5 — the promote: the orphaned verdict is promotable, and
    # the promotion continues the same run — the demoted peer's durable
    # role walk at continuing ticks under the launch's single
    # cold-start boundary, no restart marker and no mega-jump. The
    # demoted peer is the former field owner, so promoting it back
    # *is* the launch layout rather than a swap.
    status, body = _settle_call(ctx[owner] + '/promote')
    record['promote'] = {'status': status, 'body': body}
    if status != 200:
        return record, evidence
    wait_for(
        lambda: _pair_active(ctx) == owner or None,
        time.monotonic() + MUTUAL_SETTLE, interval=MUTUAL_POLL)
    record['final'] = {name: (_try_role(ctx, ctx[name]) or {}).get('role')
                       for name in (owner, peer)}

    # Phase 6 — the durable half: each peer's declared journal file
    # audited on its run-boundary count, its integer tick axis, its
    # cadence bound, and the attributed role walk the promotion owes.
    axis, walks = {}, {}
    # The durable ceiling: the run's own highest served tick plus the
    # pipeline lag the audit absorbs. A journaled stamp past it is the
    # recorded discontinuity the issue's defect left behind, not a
    # timing artefact.
    served_ticks = [row.get('tick') for entries in rows.values()
                    for row in entries
                    if row and _int_tick(row.get('tick'))]
    ceiling = (max(served_ticks) if served_ticks else 0) + JOURNAL_SLACK
    record['ceiling'] = ceiling
    for name in (owner, peer):
        try:
            boundaries, entries = _run_tail(journal_paths[name])
        except Exception as exc:
            record['journal_error'] = str(exc)[:300]
            return record, evidence
        regressions, over, malformed = _audit_axis(
            entries, name, ceiling)
        axis[name] = {
            'boundaries': boundaries,
            'regressions': regressions,
            'over': over,
            'malformed': malformed,
            'restarts': sum(
                1 for entry in entries
                if isinstance(entry.get('event'), dict)
                and 'source_restarted' in entry['event']),
        }
        walks[name] = _role_walk(entries)
    record['axis'] = axis
    record['walk'] = {'owner': walks[owner], 'peer': walks[peer]}
    evidence['axis'] = {name: dict(entry) for name, entry in axis.items()}
    evidence['walk'] = record['walk']

    record['regressions'] = {
        name: _audit_ticks(rows[name], name)
        for name in rows}
    # The ratchet reading: each peer's own run-clock rate across the
    # pass against the bound its measured cadence set. The frozen
    # window can only hold a peer below its cadence, never past it.
    record['rates'] = {}
    for index, name in ((1, owner), (2, peer)):
        rate = _measured_rate(clocks, index)
        record['rates'][name] = {
            'rate': round(rate, 3) if rate is not None else None,
            'bound': round(record['rate_bounds'].get(name, 0.0), 3)}
    record['spacing'] = spacing
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + MUTUAL_SETTLE, interval=MUTUAL_POLL)
    return record, evidence


def scenario_mutual_tracking_tick(ctx):
    """Exercise the bounded tick-domain contract under mutual standby
    tracking on the deployed pair: with the pair settled on its launch
    roles and its scan cadence measured, the field owner is demoted so
    both controllers stand by and track each other; one peer is frozen
    so the survivor's own scans seed a standing lead over the held
    stream; the frozen peer is thawed and the resumed stream must clear
    that lead — neither run's tick may regress or leave the measured
    cadence's bound, and the peers' clocks must stay within scan
    cadence of each other — a following promote must continue the tick
    domain with a non-regressing durable axis and no journaled
    mega-jump, and the pair must restore its launch roles. Two passes
    produce identical digests."""
    case = Case(
        'mutual-tracking-tick',
        'Mutual standby tracking keeps both peers\' tick domains '
        'bounded',
        'with the pair settled on its launch roles and its own scan '
        'cadence measured from the served run ticks, each pass demotes '
        'the field owner so both controllers stand by and track each '
        'other reporting the ownerless line\'s orphaned verdict; one '
        'peer is frozen so the survivor\'s scans advance its run clock '
        'past the frozen mark — a regression-seeded offset; the frozen '
        'peer is thawed and the resumed stream must clear that offset, '
        'with neither run\'s tick regressing or leaving the measured '
        'cadence\'s bound and the peers\' clocks within scan cadence of '
        'each other; a following promote must continue the tick domain '
        'with a non-regressing durable axis under the launch\'s single '
        'cold-start boundary and no journaled mega-jump; the pair\'s '
        'launch roles restore and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the tick-domain leg needs is absent')
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no keyed '
                               'probe pair is staged — the '
                               'announced-source demotion the mutual '
                               'settle is staged on is off')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed probe '
                         'pair — the deployed pair runs unkeyed')
            ctx = subject
        for action in ('pause_controller', 'unpause_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the frozen-source window cannot '
                                   'be driven')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-controller journal files — the '
                               'durable tick-axis audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s monitor '
                                   'is unreachable: ' + str(exc)[:200])

        # The launch layout the passes stage from: the pair settled with
        # the field owner active and its sibling tracking behind it. A
        # swapped layout is walked back before the leg reports.
        if _pair_active(ctx) != 'active':
            _restore_launch(ctx, 'active', 'standby')
        deadline = time.monotonic() + MUTUAL_SETTLE
        if wait_for(lambda: _pair_active(ctx) == 'active'
                    and 'active' or None, deadline,
                    interval=MUTUAL_POLL) != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never settled '
                               'on its launch layout — the demotion '
                               'the leg stages is only honest from the '
                               'declared active/standby postures')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=MUTUAL_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the mutual settle '
                               'the leg stages has no peer to track')
        owner, peer = 'active', 'standby'
        journal_paths = {owner: journals[owner], peer: journals[peer]}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); frozen peer: ' + peer + ' (' + ctx[peer]
                     + '); the frozen-source induction is the '
                     'pause_controller action on ' + peer)

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _mutual_pass(
                    ctx, number, owner, peer, journal_paths)
                _judge_mutual_tick(record, note)
                digest = _mutual_tick_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'mutual-tracking-tick-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'tick-domain pass '
                              + str(number) + ' — the measured cadence '
                              'and its bound, the mutual settle, the '
                              'frozen window and its seeded lead, the '
                              'recovery\'s cleared offsets, the '
                              'durable axis and role walk, and the '
                              'normalized digest')
                if violations:
                    name = DIAG_FAILED if any(
                        diagnostic == DIAG_FAILED
                        for diagnostic, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', name + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the legs behind this one — a clean
            # pass restores it by construction; an aborted pass gets the
            # peer thawed and the documented role order run again,
            # best-effort.
            _restore_launch(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two mutual-tracking passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the tick-domain judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _mutual_tick_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))