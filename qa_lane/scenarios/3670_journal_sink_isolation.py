"""The journal_sink_isolation acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the journal-sink-isolation case needs only the settled,
# tracking pair the early legs establish, and it parks the field
# owner's journal writer, releases it, and removes its injected fault
# before the case ends — leaving the launch roles, the sink's drained
# queue, and the durable stream as it found them, so it declares no
# window of its own. It sits beside the state-file isolation leg, the
# other half of the same decision's evidence.


# --------------------------------------------------------------------
# The --journal-file sink-isolation contract on the deployed pair
# (WW-FND-004's "no durable sink may pace the scan"; the lane evidence
# for #942's journal-append isolation decision — the state-file
# isolation leg covers the checkpoint sink, this one the audit trail).
# The recorder hands every journaled entry to a bounded drain queue
# under the executor lock and the dedicated `dcs-drain` writer appends
# it to the file in `seq` order off that lock, so a stalled or
# throttled sink can neither lengthen a scan nor pin the executor lock
# the control plane serializes on; `publication.journal_sink` rides the
# named `healthy`/`lagging`/`failed` state with the accepted, drained,
# and lost counters, and the file's own append axis stays contiguous.
#
# The impediment is the mount-impediment lever the sink-isolation legs
# share, #999's staging idea pointed at the journal path. A
# `--journal-file` writer opens its file once at bind and appends
# through the held descriptor, so no staged node can park a mid-run
# write; what has to stall is the writer thread itself, and the runner
# reaches it through the controller container's host pid and its `/proc`
# task surface: `dcs-drain` is every durable sink writer's comm, and the
# leg tells the journal one from its siblings by observable effect — a
# journaled record queues behind it only while the journal writer is
# the one held. Park and release are the same seam the durable-history
# leg drives; the state-file leg's staged FIFO cannot be reused here,
# and the leg reports inconclusive rather than probing a lever the run
# context was never granted.
#
# With the pair settled and tracking and journaled traffic flowing
# (receipted `write_value` admissions plus an injected quality
# transition), the leg parks the field owner's journal writer and then
# asserts through the serving monitor that the scan cadence holds
# inside the documented bound — the served tick advancing while the
# `io_health` counters stand unmoved — that `publication.journal_sink`
# reports its named `lagging` state with the accepted counter moving and
# the depth bounded inside the queue's declared capacity, and that the
# durability-attesting `GET /journal` read is answered with the
# retained window rather than refused or emptied — it parks on its own
# worker while the sink stands stalled, exactly as the contract
# promises. Releasing the writer must drain the standing queue in `seq`
# order into the durable file with no torn or duplicated record, answer
# every parked request with a receipt, and reconverge the pair to one
# active plus one tracking standby with launch roles restored.
# Functional misses name journal-sink-isolation-failed; ordering
# violations, served-counter regressions, and divergent passes name
# journal-sink-isolation-nondeterministic; a rig that is unreachable,
# that predates the served contract, or whose run context admits no
# mount lever reports inconclusive.

JOURNAL_SETTLE = 45          # bound on the pair's settle and role restore
JOURNAL_POLL = 0.05          # observation cadence — under the 100ms scan
JOURNAL_DEADLINE = 30        # bound on the lagging and drained waits
JOURNAL_STIR_POLL = 0.1      # sink-health probe cadence
JOURNAL_PROBE = 4.0          # bound on a parked candidate holding a
                             # journaled record in the queue
JOURNAL_TICKS = 6            # served scans the lagging window must span
JOURNAL_COMMANDS = 3         # receipted submissions whose records queue
JOURNAL_ANSWER = 45          # post-release bound on a parked answer
JOURNAL_BOUND = LATENCY_BOUND  # the declared per-request serving bound
JOURNAL_ACTOR = 'qa-lane'
JOURNAL_BAD_QUALITY = 'bad:device_fault'

IO_COUNTER_KEYS = ('failed_reads', 'failed_writes', 'failed_exchanges',
                   'consecutive_failures', 'scan_overruns')


def _journal_sink(snapshot):
    """The snapshot's publication.journal_sink section, or None — the
    contract's served sink-health surface, absent on a revision that
    predates it."""
    return ((snapshot or {}).get('publication') or {}).get('journal_sink')


def _io_counters(snapshot):
    """The io_health counters the cadence claim correlates, in the
    declared order, or None when the section is absent."""
    health = (snapshot or {}).get('io_health')
    if health is None:
        return None
    return tuple(health.get(key) for key in IO_COUNTER_KEYS)


def _sink_row(snapshot):
    """One impeded-window observation row: the served tick beside the
    journal sink's named state and queue counters (None where
    unserved)."""
    sink = _journal_sink(snapshot) or {}
    return {'tick': snapshot.get('tick'), 'state': sink.get('state'),
            'depth': sink.get('depth'), 'drained': sink.get('drained'),
            'accepted': sink.get('accepted'), 'lost': sink.get('lost'),
            'capacity': sink.get('capacity'),
            'counters': _io_counters(snapshot)}


def _sink_depth(ctx, base):
    """The served journal sink's queue depth, or None when the section
    is absent — the parked-writer probe's observable effect."""
    depth = (_journal_sink(_try_snapshot(ctx, base)) or {}).get('depth')
    return depth if isinstance(depth, int) else None


def _sink_caught_up(health):
    """The served journal sink once it reports `healthy` with an empty
    queue and every accepted record accounted — the post-release
    convergence the decision promises."""
    if not isinstance(health, dict) or health.get('state') != 'healthy' \
            or health.get('depth') != 0:
        return None
    accepted = health.get('accepted')
    if not isinstance(accepted, int):
        return None
    return health if (health.get('drained') or 0) \
        + (health.get('lost') or 0) >= accepted else None


def _journal_axis(seqs):
    """`ascending` | the violation — a journal axis's ordering verdict.
    The durability contract's order claim: strictly increasing with no
    duplicate, since a record the queue admitted twice or reordered
    breaks the file's gap-free continuity."""
    if not seqs:
        return 'empty'
    if any(not isinstance(seq, int) or isinstance(seq, bool)
           for seq in seqs):
        return 'noninteger'
    if any(b <= a for a, b in zip(seqs, seqs[1:])):
        return 'disordered'
    return 'ascending'


def _settlements(entries, point):
    """The served window's `command_settled` records writing `point` —
    the journaled traffic the leg drove actually reaching the retained
    window."""
    found = 0
    for entry in entries:
        event = (entry or {}).get('event') or {}
        receipt = (event.get('command_settled') or {}).get('receipt') or {}
        write = (receipt.get('command') or {}).get('write_value') or {}
        if write.get('point') == point:
            found += 1
    return found


def _file_axis(path):
    """The `--journal-file`'s own entry seq axis, or the reason it could
    not be audited: a torn trailing line (a crash's partial append) is
    dropped, an unparseable or unrecognized line earlier in the file is
    the nondeterministic verdict."""
    seqs = []
    lines = Path(path).read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue
            return None, 'the durable record carries a torn line at ' \
                         'line ' + str(index + 1)
        if isinstance(record, dict) and 'entry' in record:
            seqs.append(record['entry'].get('seq'))
        elif not (isinstance(record, dict) and 'run_boundary' in record):
            return None, 'durable line ' + str(index + 1) \
                         + ' is not a journal record'
    return seqs, None


def _drain_candidates(ctx, owner):
    """The field owner's `dcs-drain` writer tids — the park candidates,
    in spawn order. Raises ConnectionError where the lever admits none
    or the container exposes no writer, the inconclusive condition the
    leg reports rather than probing threads it was never granted."""
    try:
        candidates = ctx['drain_writers'](owner)
    except Exception as exc:
        raise ConnectionError('the drain-writer discovery never '
                              'answered: ' + str(exc)[:200])
    if not candidates:
        raise ConnectionError("the field owner's container reports no "
                              "dcs-drain writer threads — the journal "
                              "sink's writer cannot be named")
    return list(candidates)


def _attribute_writer(ctx, owner, base, point, parked, observe):
    """Hold each `dcs-drain` candidate in turn and submit a receipted
    command against it; the journal writer is the one whose park holds
    the admission's `command_settled` record in the queue and leaves
    the durability-attesting answer standing, while a sibling writer's
    park lets the record drain and the answer return. The released
    binary's bind order spawns the journal writer after the state-file
    writer, so the latest drain thread probes first.

    Returns `(held_tid, probe_box)` — the held writer and the parked
    submission that attributed it, which is the impeded window's first
    journaled record — or `(None, None)` when no candidate could
    attribute the sink, the lever-absent condition the leg reports
    inconclusive. Every unproductive candidate is released before the
    next is tried."""
    candidates = _drain_candidates(ctx, owner)
    observe('journal drain candidates on ' + owner + ': '
            + json.dumps(candidates))
    for tid in reversed(candidates):
        try:
            ctx['park_drain_writer'](tid)
        except Exception as exc:
            observe('drain writer ' + str(tid) + ' refused the park: '
                    + str(exc)[:120])
            continue
        parked.append(tid)
        box = _parked_command(base, point)
        deadline = time.monotonic() + JOURNAL_PROBE
        while box['thread'].is_alive() and time.monotonic() < deadline:
            time.sleep(JOURNAL_POLL)
        if box['thread'].is_alive():
            observe('journal drain writer ' + str(tid) + ' held: the '
                    'journaled record queues behind it and its '
                    'attesting answer parks')
            return tid, box
        observe('drain writer ' + str(tid) + ' parked but the '
                'journaled record drained anyway — not the journal '
                "sink's writer")
        _release(ctx, [tid])
        parked.remove(tid)
    return None, None


def _release(ctx, parked):
    """Detach every parked writer — the stall's restore. A writer the
    release cannot reach is already gone; the leg never leaves one held
    behind it."""
    for tid in list(parked):
        try:
            ctx['release_drain_writer'](tid)
        except Exception:
            pass


def _bounded_read(base, path):
    """One timed serving-lane read — `(body, elapsed)` inside the
    declared bound, or `(None, elapsed)` when the read was dropped. The
    leg's served-surface claim rides the elapsed value: a dropped read
    is one lost sample, a late answer is the bound breach."""
    started = time.monotonic()
    try:
        _status, body = http_json('GET', base + path,
                                  timeout=JOURNAL_BOUND)
    except Exception:
        return None, time.monotonic() - started
    return body, time.monotonic() - started


def _parked_command(base, point):
    """One receipted `write_value` submission, parked on its own worker:
    the admission record journals at submission and the answer waits the
    drain out off the lock, so with the writer held the record queues
    while the request stands unanswered. Returns the worker's box."""
    box = {'thread': None, 'status': None, 'receipt': None,
           'error': None}

    def submit():
        try:
            box['status'], box['receipt'] = http_json(
                'POST', base + '/command',
                {'command': {'write_value': {
                    'point': point, 'kind': 'bool',
                    'value': {'bool': True}}},
                 'actor': JOURNAL_ACTOR},
                timeout=JOURNAL_ANSWER)
        except Exception as exc:
            box['error'] = str(exc)[:200]

    box['thread'] = threading.Thread(target=submit, daemon=True)
    box['thread'].start()
    return box


def _parked_read(base, path):
    """One request parked on its own worker — the durability-attesting
    journal read's answer, sampled while the sink stands stalled."""
    box = {'thread': None, 'status': None, 'entries': [],
           'detail': None}

    def read():
        try:
            status, body = http_json('GET', base + path,
                                     timeout=JOURNAL_ANSWER)
            box['status'] = status
            box['entries'] = body if isinstance(body, list) else []
        except Exception as exc:
            box['detail'] = str(exc)[:200]

    box['thread'] = threading.Thread(target=read, daemon=True)
    box['thread'].start()
    return box


def _inject_quality(ctx, point):
    """An injected quality fault on the leg's own command point — the
    journaled transition the scans record with no request of the leg's
    outstanding. Returns whether the field took it."""
    verdict = _try_plant_ctl(ctx, 'fault', str(point),
                             JOURNAL_BAD_QUALITY)
    return verdict is not None and verdict.get('result') == 'done'


def _clear_quality(ctx, point):
    """Remove the injected fault — the restoration the leg owes the
    deployed field."""
    _try_plant_ctl(ctx, 'clear-fault', str(point))


def scenario_journal_sink_isolation(ctx):
    """Park the field-owning controller's journal drain writer on the
    deployed pair, prove through the serving monitor that the scan
    cadence, the io_health counters, and the retained `/journal` window
    hold while `publication.journal_sink` reports its named lagging
    state inside the queue's bound, and prove on the writer's release
    that the standing queue drains in `seq` order with no torn or
    duplicated record while the pair reconverges to one active plus one
    tracking standby with launch roles restored."""
    case = Case(
        'journal-sink-isolation',
        'A stalled --journal-file sink leaves the scan and the served '
        'journal alone, reports the named sink lag, and drains in seq '
        'order on release',
        'the parked writer surfaces as publication.journal_sink '
        'lagging with the accepted counter advancing and the depth '
        "bounded inside the queue's declared capacity while the served "
        'tick advances inside the documented bound and the io_health '
        'counters stand unmoved; the window\'s receipted commands and an '
        "injected quality transition keep the journal queueing behind "
        'the parked writer while a GET /journal read stands parked on '
        'its own worker and is answered with the retained window; the '
        'release drains the standing queue into the durable file in seq '
        'order with no torn or duplicated record and answers every '
        'parked request; the pair reconverges to one active plus one '
        'tracking standby with launch roles restored')
    parked = []
    injected = []
    owner = None
    try:
        for seam in ('drain_writers', 'park_drain_writer',
                     'release_drain_writer'):
            if ctx.get(seam) is None:
                return case.finish(
                    'inconclusive', 'the run context carries no '
                    + seam + ' seam — the rig predates the '
                    'journal-sink mount lever')
        journal_path = (ctx.get('journal_files') or {}).get('active')
        if not journal_path or not Path(journal_path).exists():
            return case.finish(
                'inconclusive', 'the run context declares no '
                '--journal-file for the field owner — the leg has no '
                'durable journal sink to impede')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish(
                    'inconclusive', name + "'s monitor is unreachable: "
                    + str(exc)[:200])
        deadline = time.monotonic() + JOURNAL_SETTLE
        owner = wait_for(lambda: _settled_active(ctx), deadline,
                         interval=JOURNAL_POLL)
        if owner is None:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: no peer '
                'reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=JOURNAL_POLL) is None:
            return case.finish(
                'inconclusive', 'the deployed pair never settled to '
                'one active plus a tracking standby')
        base = ctx[owner]
        snapshot = _try_snapshot(ctx, base) or {}
        if _journal_sink(snapshot) is None:
            return case.finish(
                'inconclusive', 'the deployed revision predates the '
                'contract — /snapshot publication serves no '
                'journal_sink section')
        counters0 = _io_counters(snapshot)
        if counters0 is None:
            return case.finish(
                'inconclusive', 'the served snapshot carries no '
                'io_health section')
        _, signals = http_json('GET', base + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish(
                'inconclusive', 'the model declares no writable bool '
                'command point')
        point = target['point']

        # The quiet baseline: a healthy sink with an empty queue and the
        # accepted counter the window's journaled traffic must move.
        def quiet():
            health = _journal_sink(_try_snapshot(ctx, base)) or {}
            if health.get('state') == 'healthy' and health.get('depth') == 0:
                return health
            return None

        settled = wait_for(quiet, time.monotonic() + JOURNAL_DEADLINE,
                           interval=JOURNAL_STIR_POLL)
        if settled is None:
            return case.finish(
                'inconclusive', 'the journal sink never reported '
                'healthy with an empty queue before the window')
        accepted0 = settled.get('accepted')
        if not isinstance(accepted0, int):
            return case.finish(
                'inconclusive', 'the served journal sink carries no '
                'integer accepted counter')
        before, torn = _file_axis(journal_path)
        if torn:
            return case.finish(
                'inconclusive', 'the durable journal could not be '
                'audited before the window: ' + torn)
        ref = save_evidence(ctx['evidence_dir'],
                            'journal-sink-baseline.json',
                            {'endpoint': owner, 'sink_state': 'healthy',
                             'accepted': accepted0,
                             'io_counters': counters0,
                             'durable_entries': len(before)})
        case.evidence('file', ref, 'the settled pair and the served '
                      'journal sink health ahead of the park')

        # The park: hold each candidate until one of them holds a
        # journaled record in the queue.
        held, probe = _attribute_writer(ctx, owner, base, point,
                                        parked, case.observe)
        if held is None:
            return case.finish(
                'inconclusive', 'every reachable dcs-drain writer '
                'parked without a journaled record queueing — the run '
                'context admits no mount lever for the journal sink')

        # The cadence claim starts where the impediment starts: the
        # served tick as of the confirmed park, so the probe's own scans
        # are not charged to the window the writer holds.
        tick0 = (_try_snapshot(ctx, base) or {}).get('tick')

        # The impeded window: journaled traffic queues behind the parked
        # writer — the attributing submission and its siblings, whose
        # attesting answers park beside the journal read's, plus an
        # injected quality transition the scans journal on their own
        # path.
        commands = [probe] + [_parked_command(base, point)
                              for _ in range(JOURNAL_COMMANDS - 1)]
        if _inject_quality(ctx, point):
            injected.append(point)
        read = _parked_read(base, '/journal?since=0')
        rows = []
        served = None
        elapsed = 0.0
        window_end = time.monotonic() + JOURNAL_DEADLINE
        while time.monotonic() < window_end:
            snapshot, elapsed = _bounded_read(base, '/snapshot')
            if snapshot is not None:
                served = _sink_row(snapshot)
                served['elapsed'] = elapsed
                served['late'] = elapsed > JOURNAL_BOUND
                rows.append(served)
                if served['late']:
                    return case.finish(
                        'failed', 'journal-sink-isolation-failed: '
                        'GET /snapshot answered past the '
                        + str(JOURNAL_BOUND) + 's bound while the '
                        'journal sink stood stalled — the sink paced '
                        'the served surface')
                if served['state'] == 'failed':
                    return case.finish(
                        'failed', 'journal-sink-isolation-failed: the '
                        'stalled journal sink escalated to its failed '
                        'state')
                if isinstance(served['tick'], int) \
                        and isinstance(tick0, int) \
                        and served['tick'] - tick0 >= JOURNAL_TICKS:
                    break
            time.sleep(JOURNAL_POLL)
        if served is None:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the serving '
                'monitor never answered /snapshot inside the window')
        ref = save_evidence(ctx['evidence_dir'],
                            'journal-sink-window.json',
                            {'endpoint': owner, 'lag': served['state'],
                             'ticks': JOURNAL_TICKS, 'rows': len(rows),
                             'served': {key: value for key, value
                                        in served.items()
                                        if key != 'elapsed'},
                             'answered_while_lagging':
                                 read['status'] is not None})
        case.evidence('file', ref, 'the impeded window — the parked '
                      "sink's named accounting beside the served "
                      'cadence')
        if not isinstance(served['tick'], int) \
                or not isinstance(tick0, int) \
                or served['tick'] - tick0 < JOURNAL_TICKS:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the served '
                'tick stopped advancing inside the documented bound '
                'while the journal sink stood stalled — the parked '
                'sink paced the scan')
        if served['state'] != 'lagging':
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the stalled '
                'journal sink never surfaced the named lagging state — '
                'it reports ' + repr(served['state']) + ' after the '
                'documented bound')
        if not isinstance(served['depth'], int) or served['depth'] < 1:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the lagging '
                'journal sink reports depth ' + repr(served['depth'])
                + ' — the impediment queued nothing')
        if isinstance(served['capacity'], int) \
                and served['depth'] > served['capacity']:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the lagging '
                'journal sink reports depth ' + str(served['depth'])
                + ' past its declared capacity ' + str(served['capacity'])
                + ' — the queue grew unbounded')
        if not isinstance(served['accepted'], int) \
                or served['accepted'] <= accepted0:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the journal '
                'sink accepted ' + str(served['accepted'])
                + ' records against the baseline ' + str(accepted0)
                + ' — the parked writer never held the queue')
        if served['lost']:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the stalled '
                'journal sink lost ' + str(served['lost']) + ' records')
        drift = [key for index, key in enumerate(IO_COUNTER_KEYS)
                 if served['counters'][index] != counters0[index]]
        if drift:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the stall '
                'surfaced as io_health drift on ' + ', '.join(drift)
                + ' — the parked sink touched the scan path')
        regress = [key for index, key in enumerate(IO_COUNTER_KEYS)
                   if isinstance(served['counters'][index], int)
                   and isinstance(counters0[index], int)
                   and served['counters'][index] < counters0[index]]
        if regress:
            return case.finish(
                'failed', 'journal-sink-isolation-nondeterministic: '
                'io_health regressed under the stall on '
                + ', '.join(regress))
        if read['status'] is not None:
            return case.finish(
                'failed', 'journal-sink-isolation-nondeterministic: '
                'the attesting GET /journal read answered while the '
                'journal sink stood stalled — the answer cannot attest '
                'the durable record caught up')

        # The release: the writer resumes and the standing queue drains
        # in push order — `seq` order for the journal — while every
        # parked request answers on its own worker.
        _release(ctx, parked)
        released, parked = parked, []
        case.observe('the journal drain writer resumed')
        _clear_quality(ctx, point)
        if point in injected:
            injected.remove(point)
        for box in commands + [read]:
            box['thread'].join(JOURNAL_ANSWER)
        drained = wait_for(
            lambda: _sink_caught_up(
                _journal_sink(_try_snapshot(ctx, base))),
            time.monotonic() + JOURNAL_DEADLINE,
            interval=JOURNAL_STIR_POLL)
        if drained is None:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the released '
                'journal writer never drained its standing queue to '
                'healthy with an empty queue')
        if drained.get('lost'):
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the drained '
                'journal sink accounts ' + str(drained['lost'])
                + ' lost records')

        # The parked requests' answers: every submission receipted and
        # the journal read served the retained window, never refused.
        refused = [box for box in commands
                   if box['status'] != 200
                   or box['receipt'] is None]
        if refused:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: a window '
                'admission never answered with a receipt after the '
                'release: '
                + json.dumps([{'status': box['status'],
                               'error': box['error']}
                              for box in refused])[:300])
        for box in commands:
            outcome = _outcome_key(box['receipt'])
            if outcome not in ('accepted', 'applied'):
                return case.finish(
                    'failed', 'journal-sink-isolation-failed: a '
                    'window admission settled ' + outcome
                    + ' rather than accepted')
        if read['status'] != 200:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the parked '
                'GET /journal read never answered 200 after the '
                'release: ' + str(read['detail'] or read['status']))
        window = read['entries']
        axis = _journal_axis([entry.get('seq') for entry in window])
        if axis != 'ascending':
            return case.finish(
                'failed', 'journal-sink-isolation-nondeterministic: '
                "the retained journal window's seq axis is " + axis
                + ' — ' + json.dumps([entry.get('seq') for entry
                                      in window][:12]))
        settlements = _settlements(window, point)
        if not settlements:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the retained '
                'journal window carries no settlement for the window\'s '
                'receipted commands — the served audit trail lost the '
                'records the queue admitted')
        ref = save_evidence(ctx['evidence_dir'],
                            'journal-sink-command.json',
                            {'endpoint': owner, 'point': point,
                             'submissions': JOURNAL_COMMANDS,
                             'settlements': settlements,
                             'cursor': max((entry.get('seq') or 0)
                                           for entry in window),
                             'entries': len(window)})
        case.evidence('file', ref, 'the window\'s receipted admissions '
                      'and the settlements the retained journal window '
                      'answered with')

        # The durable audit: the file's own append axis continued
        # contiguously through the stall — no torn line, no duplicate,
        # no reordered record.
        after, torn = _file_axis(journal_path)
        if torn:
            return case.finish(
                'failed', 'journal-sink-isolation-nondeterministic: '
                + torn)
        if not before or not after:
            return case.finish(
                'inconclusive', 'the durable journal carries no entry '
                'to audit')
        contiguous = after[:len(before)] == before \
            and _journal_axis(after) == 'ascending'
        ref = save_evidence(ctx['evidence_dir'],
                            'journal-sink-restored.json',
                            {'endpoint': owner, 'released': released,
                             'drained': drained.get('drained'),
                             'lost': drained.get('lost'),
                             'entries': len(after),
                             'contiguous': contiguous})
        case.evidence('file', ref, 'the drained sink beside the durable '
                      "file's contiguous append axis")
        if not contiguous:
            return case.finish(
                'failed', 'journal-sink-isolation-nondeterministic: '
                "the durable journal's entry seqs are not the "
                'contiguous append axis: '
                + json.dumps(after[-12:])[:300])

        # The launch roles the cases behind this one meet.
        if wait_for(lambda: _pair_active(ctx),
                    time.monotonic() + JOURNAL_SETTLE,
                    interval=JOURNAL_POLL) != owner:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: the pair '
                'never reconverged to its launch roles — the field '
                'owner moved')
        if wait_for(lambda: _tracking_standby(ctx, peer),
                    time.monotonic() + JOURNAL_SETTLE,
                    interval=JOURNAL_POLL) is None:
            return case.finish(
                'failed', 'journal-sink-isolation-failed: no tracking '
                'standby after the journal writer resumed')
        case.observe('the pair reconverged with launch roles restored')
        return case.finish(
            'passed', 'the stalled --journal-file sink held inside the '
            'contract: lagging surfaced while the cadence, the '
            'io_health counters, and the retained journal window held, '
            'the release drained the queue in seq order with no torn or '
            'duplicated record, and the pair reconverged')
    except ConnectionError as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        for point in list(injected):
            _clear_quality(ctx, point)
        _release(ctx, parked)