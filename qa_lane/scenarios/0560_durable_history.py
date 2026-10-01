"""The durable_history acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the durable-history leg joins the restart legs' window —
# it reuses the runner's restart action on the settled launch layout
# and the drain-stall tracer lever — behind the run-marker leg's
# restart pair, before the tune case's a->b switch.
RUNS_AFTER = frozenset({'scenario_history_run_marker'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The durable process-history store's restart-continuity and retention
# contract (WW-REP-001's retention clause, WW-LCM-002's
# audit-retention duty — the #894 decision record's served surface,
# implemented by #907): a declared `record` duty on a model point
# makes the field owner's post-scan sample a durable record —
# appended to the --history-file through the bounded history drain and
# replayed back into the served window on restart. The stream is
# `seq`-cursor read like the journal's: `seq` is assigned in append
# order and never reused across restarts, so bounded eviction reads
# as a numbering gap rather than silent loss. The run-boundary and
# domain markers are the attribution records — pinned out of the
# tail's bound, so ordinary record volume can never age a lifetime or
# a tick-domain seam out of the served answer; each boundary marker
# stamps the domain's declared tick-to-civil anchor. The file itself
# is the auditable record — line-delimited, replayed on bind, one
# run_boundary record per process lifetime — and the sink between the
# recorder and the file is the decision's isolated append: a stalled
# writer reports `publication.history_sink` lagging inside its bounded
# queue and lengthens no scan, the same isolation the journal sink
# carries. This leg exercises the whole shape on the deployed pair:
# the duty's samples served and point-filtered, the retained window's
# front rolling with the eviction gap visible to a stale cursor, the
# field owner's restart re-serving the replayed window with a new
# run-boundary entry — anchored, never restarting empty — the parked
# `dcs-drain` writer stalling the sink's counters while scans keep
# completing and a durability-attesting read stands parked until the
# writer's release drains the queue, and the file's contiguous
# append-axis audit beside one run-boundary record per lifetime.
# Named diagnostics durable-history-failed for a contract miss and
# durable-history-nondeterministic when the passes disagree; two
# passes produce identical digests; inconclusive when the rig is
# unreachable, carries no history file, or predates the store.

DURABLE_SETTLE = 45      # bound on the pair's settle and role restore
DURABLE_RETURN = 60      # bound on the restarted monitor's return
DURABLE_POLL = 0.3       # durable-stream and role watch cadence
DURABLE_STIR_POLL = 0.1  # sink-stall probe cadence
DURABLE_DEADLINE = 30    # bound on a durable-stream or drain wait
DURABLE_RETAIN = 60      # bound on the retained window rolling
DURABLE_STIR = 8         # queued records that name the parked sink
DURABLE_PROBE = 2.0      # bound on a parked candidate stirring its sink
DURABLE_TICKS = 6        # served scans the stalled window must span
DURABLE_HOLD = 1.2       # the parked-read stand-unanswered window
DURABLE_ANSWER = 45      # bound on the parked attesting read's answer


def _durable_pull(ctx, base, since=0, point=None):
    """One /history/durable read — the served entry list, or None on a
    dropped read or a non-list answer: one lost poll, never the leg's
    verdict."""
    query = '?since=' + str(since)
    if point is not None:
        query += '&point=' + str(point)
    try:
        _, body = http_json('GET', base + '/history/durable' + query)
    except AssertionError:
        raise
    except Exception:
        return None
    return body if isinstance(body, list) else None


def _durable_kind(entry):
    """The event kind a served durable entry carries — 'sampled',
    'run_boundary', or 'domain' — else None."""
    event = entry.get('event') if isinstance(entry, dict) else None
    if isinstance(event, dict) and event:
        return next(iter(event))
    return None


def _durable_body(entry):
    """The event body a served durable entry carries."""
    event = entry.get('event') if isinstance(entry, dict) else None
    kind = _durable_kind(entry)
    if isinstance(event, dict) and kind in event:
        return event[kind] or {}
    return {}


def _durable_sampled(entries, point=None):
    """The served entries' sampled records — optionally scoped to one
    declared-duty point."""
    return [entry for entry in entries
            if _durable_kind(entry) == 'sampled'
            and (point is None
                 or _durable_body(entry).get('point') == point)]


def _durable_seqs(entries):
    """The served entries' seq axis, in answer order."""
    return [entry.get('seq') for entry in entries
            if isinstance(entry, dict)]


def _durable_shape(entry):
    """One served entry's well-formedness verdict: the record carries an
    int seq, an int tick attribution, and exactly one known event
    kind; a sampled record's body carries the point and its
    quality-stamped sample."""
    if not isinstance(entry, dict):
        return 'non-dict'
    if not isinstance(entry.get('seq'), int) \
            or isinstance(entry.get('seq'), bool):
        return 'seq'
    if not isinstance(entry.get('tick'), int) \
            or isinstance(entry.get('tick'), bool):
        return 'tick'
    kind = _durable_kind(entry)
    if kind not in ('sampled', 'run_boundary', 'domain'):
        return 'event'
    body = _durable_body(entry)
    if kind == 'sampled' \
            and (not isinstance(body.get('point'), int)
                 or not isinstance(body.get('sample'), dict)
                 or 'value' not in body['sample']):
        return 'sampled'
    return None


def _durable_axis(seqs):
    """'empty' | 'ascending' | the violation — a served seq axis's
    ordering verdict, the same shape the run-marker leg's axis takes."""
    if not seqs:
        return 'empty'
    if any(not isinstance(seq, int) or isinstance(seq, bool)
           for seq in seqs):
        return 'noninteger'
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        return 'disordered'
    return 'ascending'


def _history_sink(snapshot):
    """The publication's history-sink health report — the decision's
    named backpressure surface — or None."""
    return ((snapshot or {}).get('publication') or {}).get('history_sink')


def _history_records(path):
    """The --history-file's ordered records: {'run_boundary': {...}}
    file-lifetime markers and {'entry': {...}} durable entries. A torn
    final line — a killed writer's partial append — is dropped; an
    unparseable or unrecognized record anywhere else raises."""
    records = []
    lines = [line for line in Path(path).read_text().splitlines()
             if line.strip()]
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except ValueError:
            if index + 1 == len(lines):
                continue
            raise ValueError('corrupt history record at line '
                             + str(index + 1))
        if isinstance(record, dict) \
                and (('entry' in record) != ('run_boundary' in record)):
            records.append(record)
        else:
            raise ValueError('unrecognized history record at line '
                             + str(index + 1))
    return records


def _durable_pass(ctx, number, owner, peer):
    """One durable-history pass: the settled pair's served stream —
    declared-duty samples, the retained window's eviction gap, the
    field owner's restart re-serving the file's axis behind a new
    anchored run-boundary marker, the parked drain writer's lagging
    sink that lengthens no scan, the released writer's drain, and the
    file's own append-axis audit — then the launch roles restored.
    Returns (digest, violations, evidence): digest is the pass's
    normalized verdict record, identical across clean passes;
    violations is {key: (diagnostic, detail)} in first-seen order. A
    lost restart, an unreachable monitor, or a lever that cannot name
    the sink's writer raises — the rig-side failures the scenario
    reports inconclusive."""
    violations = {}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    digest = {'duty': 'unseen', 'filter': 'unseen',
              'retention': 'unseen', 'resume': 'unseen',
              'anchor': 'unseen', 'stall': 'unexercised',
              'drain': 'unexercised', 'file': 'unseen',
              'roles': 'unrestored'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'durable-history-failed', detail)

    owner_base, peer_base = ctx[owner], ctx[peer]
    history_path = (ctx.get('history_files') or {})[owner]

    # The settled gate: the launch layout the pass's restore owes —
    # the field owner holding and the tracked peer behind it.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # ---- the duty: declared-`record` points' served samples ----
    leg = {}
    evidence['duty'] = leg
    sink0 = _history_sink(_try_snapshot(ctx, owner_base))
    leg['sink'] = sink0
    if not isinstance(sink0, dict) \
            or not isinstance(sink0.get('capacity'), int):
        raise ConnectionError('the served publication carries no '
                              'history_sink section — the rig predates '
                              'the durable-sink contract')

    def served_duty():
        entries = _durable_pull(ctx, owner_base)
        if entries and _durable_sampled(entries):
            return entries
        return None

    entries0 = wait_for(served_duty,
                        time.monotonic() + DURABLE_DEADLINE,
                        interval=DURABLE_POLL)
    if entries0 is None:
        raise ConnectionError('the field owner\'s durable stream never '
                              'served a declared-duty sample — the rig '
                              'predates the record-duty contract')
    points = sorted(
        {body.get('point')
         for body in (_durable_body(entry) for entry in entries0)
         if isinstance(body.get('point'), int)})
    seqs0 = _durable_seqs(entries0)
    leg['points'] = points
    leg['head'] = seqs0[:8]
    leg['kinds'] = sorted({_durable_kind(entry) for entry in entries0
                           if _durable_kind(entry)})
    malformed = [entry for entry in entries0
                 if _durable_shape(entry)]
    if malformed:
        failed('shape', 'the served durable stream carries malformed '
               'entries: ' + json.dumps(malformed[:2])[:300])
    elif not points:
        failed('shape', 'the served durable stream\'s sampled records '
               'carry no integer point id: '
               + json.dumps(entries0[:2])[:300])
    elif _durable_axis(seqs0) != 'ascending':
        failed('shape', 'the served durable stream\'s seq axis is '
               + _durable_axis(seqs0) + ': ' + json.dumps(seqs0[:12]))
    else:
        digest['duty'] = 'recording'

    if points:
        # The point filter scopes `sampled` records to the declared
        # point while the pinned markers answer through — the
        # attribution records no consumer filter can lose.
        point = points[0]
        filtered = _durable_pull(ctx, owner_base, point=point)
        if filtered is None:
            filtered = _durable_pull(ctx, owner_base, point=point)
        if filtered is None:
            raise ConnectionError('the field owner\'s /history/durable '
                                  'stopped answering')
        stray = [entry for entry in _durable_sampled(filtered)
                 if _durable_body(entry).get('point') != point]
        leg['filtered'] = {'entries': len(filtered),
                           'markers': sum(1 for entry in filtered
                                          if _durable_kind(entry)
                                          in ('run_boundary',
                                              'domain'))}
        if stray:
            failed('filter', 'a point-filtered durable read served '
                   'another point\'s sample: '
                   + json.dumps(stray[:2])[:300])
        else:
            digest['filter'] = 'point-scoped'

    # ---- retention: the bounded window's eviction gap ----
    leg = {}
    evidence['retention'] = leg

    # Rolled = the answer itself carries a numbering gap: pinned
    # markers can keep seq 1 serving after the tail's sampled records
    # evict, so only a jump between adjacent served seqs proves the
    # evicted stretch the probe needs.
    def rolled():
        entries = _durable_pull(ctx, owner_base)
        if not entries:
            return None
        seqs = _durable_seqs(entries)
        if any(isinstance(a, int) and isinstance(b, int)
               and b > a + 1
               for a, b in zip(seqs, seqs[1:])):
            return entries
        return None

    window = wait_for(rolled, time.monotonic() + DURABLE_RETAIN,
                      interval=DURABLE_POLL)
    if window is None:
        raise ConnectionError('the retained window never rolled a '
                              'numbering gap inside the bound — the '
                              'leg cannot show eviction')
    seqs_w = _durable_seqs(window)
    front_w = _durable_sampled(window)[0]['seq']
    leg['retained'] = {'entries': len(window), 'front': front_w,
                       'markers': [entry['seq'] for entry in window
                                   if _durable_kind(entry)
                                   in ('run_boundary', 'domain')]}
    # The gap probe: a since-cursor inside an evicted stretch is
    # answered by the retained successor — the numbering jump naming
    # the evicted records — never fabricated continuity.
    since = next(a for a, b in zip(seqs_w, seqs_w[1:])
                 if isinstance(a, int) and isinstance(b, int)
                 and b > a + 1)
    probe = _durable_pull(ctx, owner_base, since=since)
    if probe is None:
        probe = _durable_pull(ctx, owner_base, since=since)
    if probe is None:
        raise ConnectionError('the field owner\'s /history/durable '
                              'stopped answering')
    probe_seqs = _durable_seqs(probe)
    leg['gap_probe'] = {'since': since, 'head': probe_seqs[:8]}
    head = probe_seqs[0] if probe_seqs else None
    if not isinstance(head, int):
        failed('gap', 'a since-cursor at ' + str(since) + ' answered '
               'with an empty or non-integer head: '
               + json.dumps(probe_seqs[:8]))
    elif head <= since + 1:
        failed('gap', 'a since-cursor at ' + str(since) + ' inside the '
               'evicted stretch was answered contiguously at seq '
               + str(head) + ' — the fabricated continuity the '
               'numbering-gap honesty forbids')
    else:
        digest['retention'] = 'gap-honest'

    # The bound: the retained tail never outgrows its window while the
    # front keeps rolling — the same answer's eviction bound, read
    # twice.
    def advanced():
        entries = _durable_pull(ctx, owner_base)
        sampled = _durable_sampled(entries or [])
        if sampled and (sampled[0].get('seq') or 0) > front_w:
            return entries
        return None

    later = wait_for(advanced, time.monotonic() + DURABLE_DEADLINE,
                     interval=DURABLE_POLL)
    if later is None:
        raise ConnectionError('the durable stream stopped advancing '
                              'behind the retention wait')
    # The bound is the sampled tail's — pinned markers answer beside
    # it and grow only when a lifetime's marker evicts, so the
    # comparison counts the bounded records, not the answer.
    bound0 = len(_durable_sampled(window))
    bound1 = len(_durable_sampled(later))
    leg['bound'] = {'retained0': bound0, 'retained1': bound1,
                    'front1': _durable_sampled(later)[0]['seq']}
    if bound1 > bound0:
        failed('bound', 'the retained window grew from '
               + str(bound0) + ' to ' + str(bound1)
               + ' records while evicting — the declared '
               'bound did not hold')

    # ---- the restart: the file's axis continues, attributed ----
    leg = {}
    evidence['restart'] = leg
    try:
        records0 = _history_records(history_path)
    except OSError as exc:
        raise ConnectionError('the field owner\'s history file cannot '
                              'be read: ' + str(exc)[:200])
    except ValueError as exc:
        records0 = None
        failed('file', 'the history file holds a corrupt record: '
               + str(exc)[:200])
    runs0 = sum(1 for record in (records0 or [])
                if 'run_boundary' in record)
    seqs_l = [seq for seq in _durable_seqs(later)
              if isinstance(seq, int) and not isinstance(seq, bool)]
    if not seqs_l:
        raise ConnectionError('the retained window\'s seq axis '
                              'carries no integer cursor to hold '
                              'across the restart')
    cursor = max(seqs_l)
    expected_run = runs0 + 1
    leg['before'] = {'cursor': cursor, 'file_runs': runs0}
    # The standby's armed failover budget in seconds — a promotion
    # past it is the documented bound on the outage, not a defect;
    # one inside it is the spurious promotion the contract forbids.
    budget = (ctx.get('failover_misses') or 0) * 0.1
    stopped_at = time.monotonic()
    try:
        ctx['restart_controller'](owner)
    except Exception as exc:
        raise ConnectionError('the restart action never completed: '
                              + str(exc)[:300])

    promoted = []

    def returned():
        report = _try_role(ctx, peer_base)
        if (report or {}).get('role') in ('promoting', 'active'):
            promoted.append({'report': report,
                             'elapsed': time.monotonic()
                             - stopped_at})
        own = _try_role(ctx, owner_base)
        return own if (own or {}).get('role') == 'active' else None

    back = wait_for(returned,
                    time.monotonic() + DURABLE_RETURN,
                    interval=DURABLE_POLL)
    leg['promoted'] = promoted
    if back is None:
        raise ConnectionError('the restarted monitor never returned')
    if promoted:
        elapsed = promoted[0]['elapsed']
        if budget and elapsed > budget:
            raise ConnectionError('the tracked peer promoted '
                                  + str(elapsed)[:5]
                                  + 's into the outage — past the '
                                  'armed failover budget of '
                                  + str(budget) + 's, the documented '
                                  'bound rather than a defect')
        failed('promoted', 'the tracked peer reported '
               + str(promoted[0]['report'].get('role'))
               + ' while the restarted controller was down — a '
               'spurious promotion inside the armed failover '
               'budget: '
               + json.dumps(promoted[0]['report'])[:300])

    # The replayed window re-serves rather than restarting empty —
    # read the first post-restart answer before the live churn can
    # age the seam's tail out: the retained tail stands below the new
    # lifetime's boundary entry.
    first = _durable_pull(ctx, owner_base)
    if first is None:
        first = _durable_pull(ctx, owner_base)
    if first is None:
        raise ConnectionError('the restarted monitor never re-served '
                              '/history/durable')
    boundary0 = next(
        (entry['seq'] for entry in first
         if _durable_kind(entry) == 'run_boundary'
         and _durable_body(entry).get('run') == expected_run), None)
    leg['window'] = {'front': _durable_seqs(first)[:4],
                     'boundary_seq': boundary0}
    if boundary0 is None:
        failed('resume-window', 'the restarted durable window\'s '
               'first answer carries no boundary marker for '
               'lifetime ' + str(expected_run) + ': '
               + json.dumps(_durable_seqs(first)[:8]))
    elif boundary0 <= 1:
        # A store that restarted empty mints its boundary at seq 1;
        # a replayed axis continues the file — the boundary lands
        # above everything the retained tail kept.
        failed('resume-window', 'the restarted durable window\'s '
               'boundary marker carries seq ' + str(boundary0)
               + ' — the axis restarted rather than continuing the '
               'file\'s: the recorded datasets did not re-serve')
    else:
        leg['window']['tail_below_boundary'] = any(
            isinstance(seq, int) and seq < boundary0
            for seq in _durable_seqs(first))

    # The resumed stream: the file's seq axis continues — new samples
    # land above the held cursor — and the new lifetime's boundary
    # entry lands served with the domain's anchor.
    def resumed_stream():
        entries = _durable_pull(ctx, owner_base)
        if entries and any((entry.get('seq') or 0) > cursor
                           for entry in _durable_sampled(entries)):
            return entries
        return None

    whole = wait_for(resumed_stream,
                     time.monotonic() + DURABLE_DEADLINE,
                     interval=DURABLE_POLL)
    resumed = whole is not None
    if not resumed:
        probe_all = _durable_pull(ctx, owner_base)
        if probe_all is None:
            raise ConnectionError('the restarted monitor never '
                                  're-served /history/durable')
        failed('resume', 'the durable stream never served a '
               'post-restart sample above the held cursor '
               + str(cursor) + ' — the file\'s axis did not '
               'continue: '
               + json.dumps(_durable_seqs(probe_all)[:8]))
        whole = probe_all
    seqs1 = _durable_seqs(whole)
    boundaries = [_durable_body(entry) for entry in whole
                  if _durable_kind(entry) == 'run_boundary']
    mine = [body for body in boundaries
            if body.get('run') == expected_run]
    leg['resumed'] = {'tail_head': seqs1[:8],
                      'post': [seq for seq in seqs1
                               if isinstance(seq, int)
                               and seq > cursor][:8],
                      'boundary_runs': [body.get('run')
                                        for body in boundaries],
                      'domains': sum(1 for entry in whole
                                     if _durable_kind(entry)
                                     == 'domain')}
    if _durable_axis(seqs1) != 'ascending':
        failed('resume-axis', 'the resumed stream\'s seq axis is '
               + _durable_axis(seqs1) + ': ' + json.dumps(seqs1[:12]))
    elif resumed:
        digest['resume'] = 'continued'
    if not mine:
        failed('marker', 'no run_boundary entry for lifetime '
               + str(expected_run) + ' landed on the served stream — '
               'the restart is unattributed: '
               + json.dumps(leg['resumed'])[:300])
    else:
        anchor = (mine[0] or {}).get('anchor')
        if isinstance(anchor, dict) \
                and isinstance(anchor.get('epoch_ms'), int) \
                and not isinstance(anchor.get('epoch_ms'), bool) \
                and anchor['epoch_ms'] > 0:
            digest['anchor'] = 'stamped'
        else:
            failed('anchor', 'the served run_boundary marker for '
                   'lifetime ' + str(expected_run) + ' carries no '
                   'civil-time anchor: ' + json.dumps(mine[0])[:200])

    # ---- the stall: a parked drain writer lags the sink, not a scan ----
    leg = {}
    evidence['stall'] = leg
    try:
        candidates = ctx['drain_writers'](owner)
    except Exception as exc:
        raise ConnectionError('the drain-writer discovery never '
                              'answered: ' + str(exc)[:200])
    if not candidates:
        raise ConnectionError('the field owner\'s container reports '
                              'no dcs-drain writer threads — the '
                              'lever cannot name the sink\'s writer')
    leg['candidates'] = candidates

    def sink_health():
        return _history_sink(_try_snapshot(ctx, owner_base)) or {}

    held = None
    refused = []
    parked = set()

    def unpark(tid):
        if tid in parked:
            parked.discard(tid)
            try:
                ctx['release_drain_writer'](tid)
            except Exception:
                pass

    try:
        for tid in candidates:
            try:
                ctx['park_drain_writer'](tid)
            except Exception as exc:
                refused.append({'tid': tid, 'error': str(exc)[:120]})
                continue
            parked.add(tid)
            stirred = wait_for(
                lambda: (sink_health().get('depth') or 0)
                >= DURABLE_STIR or None,
                time.monotonic() + DURABLE_PROBE,
                interval=DURABLE_STIR_POLL)
            if stirred:
                held = tid
                break
            unpark(tid)
        leg['refused'] = refused
        if held is None:
            raise ConnectionError('every reachable dcs-drain writer '
                                  'parked without the history sink '
                                  'stirring — the lever cannot '
                                  'attribute the sink\'s writer: '
                                  + json.dumps({'candidates':
                                                candidates,
                                                'refused': refused})
                                  [:300])
        leg['held'] = held

        # The stalled window: a durability-attesting read stands
        # parked while the served tick keeps advancing and the
        # sink's standing queue stays inside its bound — the
        # decision's lagging state, never a lengthened scan.
        box = {}

        def attested_read():
            try:
                box['answer'] = http_json(
                    'GET', owner_base + '/history/durable?since=0',
                    timeout=DURABLE_ANSWER)
            except Exception as exc:
                box['answer'] = ('error', str(exc)[:200])

        reader = threading.Thread(target=attested_read, daemon=True)
        reader.start()
        tick0 = (_try_snapshot(ctx, owner_base) or {}).get('tick')
        samples = []
        deadline = time.monotonic() + DURABLE_HOLD
        while time.monotonic() < deadline:
            samples.append(sink_health())
            time.sleep(DURABLE_STIR_POLL)
        standing = 'answer' not in box
        tick1 = (_try_snapshot(ctx, owner_base) or {}).get('tick')
        role = _try_role(ctx, owner_base)
        leg['held_samples'] = samples
        leg['ticks'] = {'before': tick0, 'after': tick1}
        leg['standing'] = standing
    finally:
        for tid in list(parked):
            unpark(tid)
    reader.join(DURABLE_ANSWER)
    lagged = [sample for sample in samples
              if sample.get('state') == 'lagging'
              and (sample.get('depth') or 0) > 0]
    cap = next((sample.get('capacity') for sample in samples
                if isinstance(sample.get('capacity'), int)
                and sample['capacity']), None)
    if not lagged:
        failed('stall', 'the parked writer never surfaced '
               'publication.history_sink lagging: '
               + json.dumps(samples)[:300])
    elif any((sample.get('lost') or 0) for sample in samples) \
            or (cap is not None and any(
                (sample.get('depth') or 0) > cap
                for sample in samples)):
        failed('stall', 'the stalled sink lost records or overran its '
               'bound: ' + json.dumps(samples)[:300])
    else:
        digest['stall'] = 'lagging-bounded'
    if not standing:
        failed('stall-answer', 'the durability-attesting durable read '
               'answered while its sink stood stalled — the answer '
               'cannot attest the file caught up')
    if role is None or role.get('role') != 'active':
        failed('stall-monitor', 'the serving monitor stopped '
               'answering /role behind the stalled sink')
    if not isinstance(tick0, int) or not isinstance(tick1, int) \
            or tick1 < tick0 + DURABLE_TICKS:
        failed('stall-scan', 'the stalled window lengthened the scan: '
               'the served tick moved ' + str(tick0) + ' -> '
               + str(tick1) + ' across the hold')
    answer = box.get('answer')
    leg['answer'] = {'status': answer[0] if answer else None,
                     'entries': len(answer[1])
                     if answer and isinstance(answer[1], list)
                     else None}
    if not answer or answer[0] != 200 \
            or not isinstance(answer[1], list):
        failed('stall-answer', 'the parked durable read never '
               'answered 200 after the writer\'s release: '
               + str(answer)[:200])

    def caught_up():
        health = sink_health()
        if health.get('depth') == 0 and (health.get('drained') or 0) \
                + (health.get('lost') or 0) \
                >= (health.get('accepted') or 1):
            return health
        return None

    drained = wait_for(caught_up,
                       time.monotonic() + DURABLE_DEADLINE,
                       interval=DURABLE_STIR_POLL)
    leg['restored_sink'] = drained
    if drained is None or (drained.get('lost') or 0) \
            or drained.get('state') != 'healthy':
        failed('drain', 'the released writer never drained its '
               'standing queue: ' + json.dumps(drained)[:200])
    else:
        digest['drain'] = 'caught-up'

    # ---- the file audit: one append axis, one marker per lifetime ----
    leg = {}
    evidence['file'] = leg
    try:
        records1 = _history_records(history_path)
    except (OSError, ValueError) as exc:
        records1 = None
        failed('file', 'the history file failed its audit: '
               + str(exc)[:200])
    if records1 is not None:
        file_seqs = [record['entry'].get('seq')
                     for record in records1 if 'entry' in record]
        file_runs = [record['run_boundary'] for record in records1
                     if 'run_boundary' in record]
        contiguous = file_seqs == list(range(1, len(file_seqs) + 1))
        leg['audit'] = {'entries': len(file_seqs),
                        'runs': [mark.get('run') for mark in file_runs],
                        'contiguous': contiguous}
        if not contiguous:
            failed('file', 'the history file\'s entry seqs are not '
                   'the contiguous append axis: '
                   + json.dumps(file_seqs[:12])[:200])
        elif len(file_runs) != expected_run \
                or [mark.get('run') for mark in file_runs] \
                != list(range(1, expected_run + 1)):
            failed('file-runs', 'the history file records '
                   + json.dumps([mark.get('run')
                                 for mark in file_runs])
                   + ' lifetimes — not one marker per run through '
                   + str(expected_run))
        else:
            mark = file_runs[-1] or {}
            anchor = mark.get('anchor')
            if not isinstance(mark.get('tick'), int) \
                    or not isinstance(anchor, dict) \
                    or not isinstance(anchor.get('epoch_ms'), int):
                failed('file-anchor', 'the file\'s run-boundary '
                       'marker carries no tick or civil-time anchor: '
                       + json.dumps(mark)[:200])
            else:
                digest['file'] = 'audited'

    # The launch roles the cases behind this one meet.
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
                and _tracking_standby(ctx, peer),
        time.monotonic() + DURABLE_SETTLE, interval=DURABLE_POLL)
    evidence['restored'] = bool(settled)
    if settled:
        digest['roles'] = 'restored'
    else:
        failed('restore', 'the pair never settled back to the launch '
               'roles — ' + owner + ' active, ' + peer + ' tracking')
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_durable_history(ctx):
    """Exercise the durable process-history store on the deployed
    pair: declared-duty samples served and point-filtered, the bounded
    window's eviction reading as a numbering gap to a stale cursor,
    the field owner's restart re-serving the file's axis behind a new
    anchored run-boundary marker rather than restarting empty, the
    parked drain writer stalling publication.history_sink to lagging
    without lengthening a scan, the released writer draining the
    standing queue, and the history file's contiguous append audit —
    the pair restoring its launch roles; two consecutive passes
    produce identical digests."""
    case = Case(
        'durable-history',
        'The durable process-history store survives restart and stays bounded',
        'with the deployed pair settled and declared-duty points '
        'recording, /history/durable serves the bounded stream: '
        'point-filtered samples beside pinned markers, retention '
        'eviction visible as a numbering gap to a stale cursor, the '
        'field owner\'s restart_controller action re-serving the '
        'replayed window with a new anchored run-boundary marker and '
        'the file\'s seq axis continued — never restarted — while the '
        'history file audits one contiguous append axis and one '
        'run-boundary record per lifetime; a parked dcs-drain writer '
        'reports publication.history_sink lagging inside its bound '
        'while the served tick keeps advancing and a '
        'durability-attesting read stands parked until the writer\'s '
        'release drains the queue; the pair restores its launch '
        'roles; two consecutive passes produce identical digests')
    try:
        for seam in ('restart_controller', 'drain_writers',
                     'park_drain_writer', 'release_drain_writer'):
            if ctx.get(seam) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + seam + ' seam — '
                                   'the rig predates the durable-'
                                   'history induction')
        if not (ctx.get('history_files') or {}).get('active'):
            return case.finish('inconclusive', 'the run context '
                               'carries no history-file path for the '
                               'pair — the file-side audit cannot '
                               'run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DURABLE_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DURABLE_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if owner != 'active':
            return case.finish('inconclusive', 'the field owner is '
                               + owner + ' — the leg\'s restart '
                               'restore assumes the launch layout '
                               'where ctrl-a owns the field')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DURABLE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the restart leg '
                               'has no peer to watch')

        # The contract surface: /history/durable answers a list —
        # a non-list answer is a rig predating the durable store.
        try:
            _, body = http_json('GET', ctx[owner]
                                + '/history/durable?since=0')
        except Exception as exc:
            return case.finish('inconclusive', 'the field owner\'s '
                               '/history/durable never answered: '
                               + str(exc)[:200])
        if not isinstance(body, list):
            return case.finish('inconclusive', 'the field owner\'s '
                               '/history/durable answered a non-list '
                               'payload — the rig predates the '
                               'durable store')
        case.observe('durable stream serving: ' + owner
                     + ' owns the field, ' + peer + ' tracks')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _durable_pass(
                    ctx, number, owner, peer)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'durable-history-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'durable-history pass '
                              + str(number) + ' — the served stream, '
                              'retention gap probe, restart '
                              'attribution, stall window, and file '
                              'audit beside the normalized digest')
                if violations or digest is None:
                    diagnostic = 'durable-history-failed' \
                        if digest is None or any(
                            name == 'durable-history-failed'
                            for name, _ in violations.values()) \
                        else 'durable-history-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted pass
            # gets the same bounded settle wait.
            restored = wait_for(
                lambda: (_pair_active(ctx) == 'active' or None)
                        and _tracking_standby(ctx, 'standby'),
                time.monotonic() + DURABLE_SETTLE,
                interval=DURABLE_POLL)
            if not restored:
                case.observe('cleanup: the pair never settled back '
                             'to the launch roles')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'durable-history-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two durable-history passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
