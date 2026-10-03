"""The state_file_isolation acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the state-file-isolation case perturbs no role and leaves
# nothing staged — the impeded mount restores before the case ends —
# so it needs no declared window.


# --------------------------------------------------------------------
# The --state-file sink-isolation contract on the deployed pair
# (WW-FND-004's "no durable sink may pace the scan"; the lane evidence
# for #982's isolation fix — the restart legs cover resume on healthy
# mounts only). The controller captures a checkpoint under the
# executor lock at every completed scan and accepted admission, but
# the serialization and the write-then-rename drain on a dedicated
# writer behind a bounded queue: a stalled or throttled mount can
# neither lengthen a scan nor pin the serving lane. With the pair
# settled and tracking, the leg stalls the field-owning controller's
# --state-file mount through the runner's declared lever — the run
# config's state_file_mounts names the staged-target kind per
# endpoint, 'fifo' parking a reader-less FIFO at the sink's
# write-then-rename temporary path so the drain writer's next open()
# blocks inside the mount while captures queue. Through the serving
# monitor the leg then proves the contract's three claims: the served
# tick and io_health keep advancing inside the documented cadence
# bound while publication.state_sink reports the named 'lagging'
# state, and a command admitted inside the impeded window answers
# only after the file caught up through its admission — the request
# worker's own bounded wait, never the scan's. Restoring the mount
# drains the queue, the sink reports 'healthy' again, and the pair
# stands one active plus one tracking standby with its launch roles
# restored. Functional misses name state-file-isolation-failed;
# ordering violations and served-counter regressions name
# state-file-isolation-nondeterministic; a rig that is unreachable,
# that predates the served contract, or whose run config declares no
# mount lever reports inconclusive.

SINK_SETTLE = 45            # bound on the pair's settle/role checks
SINK_POLL = 0.05            # observation cadence — under the 100ms scan
SINK_DEADLINE = 30          # bound on the lagging and drained waits
SINK_TICKS = 6              # served scans the lagging window must span
SINK_HOLD_TICKS = 4         # served scans the parked admission must
                          # stand unanswered across while the sink lags
SINK_ANSWER_DEADLINE = 15   # post-restore bound on the command's 200
SINK_ACTOR = 'qa-lane'


def _state_sink(snapshot):
    """The snapshot's publication.state_sink section, or None — the
    contract's served sink-health surface, absent on a revision that
    predates it."""
    return ((snapshot or {}).get('publication') or {}).get('state_sink')


def _io_counters(snapshot):
    """(failed_reads, failed_writes, failed_exchanges,
    consecutive_failures, scan_overruns) — the io_health counters the
    cadence claim correlates — or None when the section is absent."""
    health = (snapshot or {}).get('io_health')
    if health is None:
        return None
    return tuple(health.get(key) for key in (
        'failed_reads', 'failed_writes', 'failed_exchanges',
        'consecutive_failures', 'scan_overruns'))


def _sink_row(snapshot):
    """One impeded-window observation row: the served tick beside the
    sink's named state and queue counters (None where unserved)."""
    sink = _state_sink(snapshot) or {}
    return {'tick': snapshot.get('tick'), 'state': sink.get('state'),
            'depth': sink.get('depth'), 'drained': sink.get('drained'),
            'accepted': sink.get('accepted'), 'lost': sink.get('lost'),
            'counters': _io_counters(snapshot)}


def _sink_healthy(ctx, base):
    """The served sink health once it reports 'healthy' with an empty
    queue — the post-restore convergence the contract promises."""
    sink = _state_sink(_try_snapshot(ctx, base)) or {}
    if sink.get('state') == 'healthy' and sink.get('depth') == 0:
        return sink
    return None


def scenario_state_file_isolation(ctx):
    """Impede the field-owning controller's --state-file mount on the
    deployed pair, prove through the serving monitor that cadence and
    the served surface hold — tick and io_health advancing while the
    sink reports its named lagging state — and that a receipted
    command inside the window keeps its admission-durability ordering;
    restore the mount and prove the pair reconverges to one active
    plus one tracking standby with launch roles restored."""
    case = Case(
        'state-file-isolation',
        'A stalled --state-file mount leaves the scan and serving '
        'lanes alone, reports the named sink lag, keeps '
        'admission-durability ordering, and recovers on restore',
        'the staged stall surfaces as publication.state_sink lagging '
        'while the served tick and io_health keep advancing inside '
        'the documented bound; a command admitted inside the window '
        'answers only after the file caught up through its '
        'admission; restore drains the sink to healthy and the pair '
        'reconverges to one active plus one tracking standby with '
        'launch roles restored')
    impede = ctx.get('impede_state_file')
    restore = ctx.get('restore_state_file')
    if not callable(impede) or not callable(restore):
        return case.finish(
            'inconclusive', 'the run context carries no state-file '
            'mount lever — the stalled mount target the run config '
            'must declare')
    active = wait_for(lambda: _settled_active(ctx),
                      time.monotonic() + SINK_SETTLE)
    if active is None:
        unreachable = all(
            _try_role(ctx, ctx[name]) is None
            for name in ('active', 'standby') if ctx.get(name))
        if unreachable:
            return case.finish('inconclusive', 'the rig is '
                               'unreachable')
        return case.finish('failed', 'state-file-isolation-failed: '
                           'no peer reports role=active')
    peer = 'standby' if active == 'active' else 'active'
    if wait_for(lambda: _tracking_standby(ctx, peer),
                time.monotonic() + SINK_SETTLE) is None:
        return case.finish(
            'inconclusive', 'the deployed pair never settled to one '
            'active plus a tracking standby')
    base = ctx[active]
    snapshot = _try_snapshot(ctx, base) or {}
    if _state_sink(snapshot) is None:
        return case.finish(
            'inconclusive', 'the deployed revision predates the '
            'contract — /snapshot publication serves no state_sink '
            'section')
    counters0 = _io_counters(snapshot)
    if counters0 is None:
        return case.finish(
            'inconclusive', 'the served snapshot carries no io_health '
            'section')
    tick0 = snapshot.get('tick')
    _, signals = http_json('GET', base + '/signals')
    target = _writable_bool_point(signals)
    if target is None:
        return case.finish('inconclusive', 'the model declares no '
                           'writable bool command point')
    command = {'command': {'write_value': {'point': target['point'],
                                           'kind': 'bool',
                                           'value': {'bool': True}}},
               'actor': SINK_ACTOR}
    ref = save_evidence(ctx['evidence_dir'],
                        'state-file-baseline.json',
                        {'endpoint': active,
                         'sink_state': 'served',
                         'io_counters': counters0})
    case.evidence('file', ref, 'the settled pair and served sink '
                  'health ahead of the stall')
    try:
        impede(active)
    except Exception as exc:
        return case.finish(
            'inconclusive', 'the state-file impede action never '
            'completed: ' + str(exc)[:200])
    impeded = True
    polls = []
    answer = {'done': False}

    def submit():
        try:
            _, receipt = http_json('POST', base + '/command', command,
                                   timeout=SINK_ANSWER_DEADLINE + 15)
            answer['receipt'] = receipt
        except Exception as exc:
            answer['error'] = str(exc)[:300]
        answer['done'] = True

    def check_row(row):
        """The (diagnostic, detail) a served row violates, or None —
        the assertions every impeded-window poll owes: a monotonic
        tick, an unchanged io_health counter set, and the sink's named
        states alone."""
        tick = row['tick']
        if not isinstance(tick, int) or tick < tick0:
            return ('state-file-isolation-nondeterministic',
                    'the served tick regressed under the stall')
        if row['counters'] is None:
            return ('state-file-isolation-nondeterministic',
                    'the io_health section vanished under the stall')
        for index, key in enumerate(
                ('failed_reads', 'failed_writes', 'failed_exchanges',
                 'consecutive_failures', 'scan_overruns')):
            now, before = row['counters'][index], counters0[index]
            if now is None or before is None or now < before:
                return ('state-file-isolation-nondeterministic',
                        'io_health.%s regressed under the stall'
                        % key)
            if now > before:
                return ('state-file-isolation-failed',
                        'the stall surfaced as io_health.%s growth '
                        '(r%d -> r%d)' % (key, before, now))
        if row['state'] == 'failed':
            return ('state-file-isolation-failed',
                    'the stalled mount escalated to the sink\'s '
                    'failed state')
        return None

    def poll_window(until):
        """Poll the serving monitor through the impeded window until
        `until(row)` stands; returns (row, violation) — the row when
        the condition landed, else the violation detail or None when
        the cadence deadline passed."""
        deadline = time.monotonic() + SINK_DEADLINE
        row = None
        while time.monotonic() < deadline:
            snap = _try_snapshot(ctx, base)
            if snap is None:
                # A dropped read is one lost sample, not the leg's
                # verdict — cadence is judged on the ticks served.
                time.sleep(SINK_POLL)
                continue
            row = _sink_row(snap)
            polls.append(row)
            violation = check_row(row)
            if violation:
                return row, violation
            if until(row):
                return row, None
            time.sleep(SINK_POLL)
        return row, None

    try:
        # Phase 1: the stall must surface the named lagging state.
        row, violation = poll_window(
            lambda row: row['state'] == 'lagging')
        if violation:
            return case.finish('failed', '%s: %s' % violation)
        if row is None or row['state'] != 'lagging':
            return case.finish(
                'failed', 'state-file-isolation-failed: the stalled '
                'mount never surfaced the named lagging state — the '
                'sink reports %r after the documented bound'
                % (None if row is None else row['state']))
        lag_tick = row['tick']
        case.observe('the staged stall surfaced the named lagging '
                     'sink state')
        # Phase 2: cadence — the served tick and io_health keep
        # advancing across the window the lag stands.
        row, violation = poll_window(
            lambda row: row['state'] != 'lagging'
            or row['tick'] - lag_tick >= SINK_TICKS)
        if violation:
            return case.finish('failed', '%s: %s' % violation)
        if row is not None and row['state'] != 'lagging':
            return case.finish(
                'failed', 'state-file-isolation-nondeterministic: '
                'the sink left its lagging state without a restore')
        if row is None or row['tick'] - lag_tick < SINK_TICKS:
            return case.finish(
                'failed', 'state-file-isolation-failed: the served '
                'tick stopped advancing while the sink stood lagging '
                '— the impeded mount paced the scan')
        case.observe('the served tick and io_health kept advancing '
                     'across the lagging window')
        # Phase 3: a receipted command inside the impeded window must
        # stand unanswered until the file catches up — the request
        # worker's own bounded wait, never the scan's.
        index = _next_receipt_index(ctx, base)
        thread = threading.Thread(target=submit, daemon=True)
        submit_tick = row['tick']
        thread.start()
        row, violation = poll_window(
            lambda row: answer['done']
            or row['state'] != 'lagging'
            or row['tick'] - submit_tick >= SINK_HOLD_TICKS)
        if violation:
            return case.finish('failed', '%s: %s' % violation)
        if answer['done']:
            receipt = answer.get('receipt')
            if receipt is not None:
                if not _outcome_key(receipt).startswith('rejected'):
                    return case.finish(
                        'failed',
                        'state-file-isolation-nondeterministic: a '
                        'receipted command answered while the file '
                        'could not have persisted its admission')
                return case.finish(
                    'failed', 'state-file-isolation-failed: the '
                    'window submission settled '
                    + str(_outcome_key(receipt)) + ' while the '
                    'mount stood stalled')
            return case.finish(
                'failed', 'state-file-isolation-failed: the window '
                'submission erred while the mount stood stalled: '
                + str(answer.get('error')))
        if row is not None and row['state'] != 'lagging':
            return case.finish(
                'failed', 'state-file-isolation-nondeterministic: '
                'the sink left its lagging state before restore')
        if row is None or row['tick'] - submit_tick < SINK_HOLD_TICKS:
            return case.finish(
                'failed', 'state-file-isolation-failed: the served '
                'tick stopped advancing across the parked admission')
        case.observe('the window admission stood unanswered while '
                     'the drain was stalled')
        # Phase 4: restore the mount — the drain catches up, the
        # parked admission answers, and the pair reconverges.
        try:
            restore(active)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the state-file restore action never '
                'completed: ' + str(exc)[:200])
        impeded = False
        case.observe('the mount lever released the stall')
        thread.join(SINK_ANSWER_DEADLINE)
        if not answer['done']:
            return case.finish(
                'failed', 'state-file-isolation-failed: the window '
                'admission never answered after the mount restored')
        if answer.get('receipt') is None:
            return case.finish(
                'failed', 'state-file-isolation-failed: the window '
                'admission erred after the mount restored: '
                + str(answer.get('error')))
        receipt = answer['receipt']
        if _outcome_key(receipt) not in ('accepted', 'applied'):
            return case.finish(
                'failed', 'state-file-isolation-failed: the window '
                'admission settled '
                + str(_outcome_key(receipt)) + ' rather than '
                'accepted')
        deadline = time.monotonic() + SINK_DEADLINE
        settled = wait_for(lambda: _settled_outcome(ctx, base, index),
                           deadline, interval=SINK_POLL)
        if settled != 'applied':
            return case.finish(
                'failed', 'state-file-isolation-failed: the window '
                'admission settled ' + str(settled)
                + ' rather than applied')
        case.observe('the window admission settled applied once the '
                     'file caught up')
        final = wait_for(lambda: _sink_healthy(ctx, base), deadline,
                         interval=SINK_POLL)
        if not final:
            return case.finish(
                'failed', 'state-file-isolation-failed: the sink '
                'never drained back to healthy after the mount '
                'restored')
        if final.get('lost'):
            return case.finish(
                'failed', 'state-file-isolation-failed: captures '
                'were lost across the stall')
        # The durability audit the ordered 200 attested: the file on
        # the mount covers the window admission's capture.
        audit = {'admission': index}
        state_file = (ctx.get('state_files') or {}).get(active)
        if state_file:
            try:
                persisted = json.loads(Path(state_file).read_text())
            except Exception as exc:
                return case.finish(
                    'failed', 'state-file-isolation-nondeterministic:'
                    ' the durable file was unreadable after the '
                    'answered admission: ' + str(exc)[:200])
            admission = persisted.get('command_admission') or {}
            audit['file_attempts'] = admission.get('attempts')
            if not isinstance(audit['file_attempts'], int) \
                    or audit['file_attempts'] <= index:
                return case.finish(
                    'failed', 'state-file-isolation-nondeterministic:'
                    ' the answered 200 did not attest the file '
                    'covering the admission — durable attempts %r '
                    'at admission index r%d'
                    % (audit['file_attempts'], index))
        ref = save_evidence(ctx['evidence_dir'],
                            'state-file-command.json',
                            {'index': index, 'settled': settled,
                             'answered_while_lagging': False,
                             'audit': audit})
        case.evidence('file', ref, 'the window admission\'s ordering '
                      'and the durable-file audit')
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SINK_POLL)
        if owner != active:
            return case.finish(
                'failed', 'state-file-isolation-failed: the pair did '
                'not reconverge — the field owner moved to %r'
                % owner)
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=SINK_POLL) is None:
            return case.finish(
                'failed', 'state-file-isolation-failed: no tracking '
                'standby after the mount restored')
        ref = save_evidence(ctx['evidence_dir'],
                            'state-file-window.json',
                            {'lag': 'lagging',
                             'ticks': SINK_TICKS + SINK_HOLD_TICKS,
                             'io_failures_added': 0,
                             'scan_overruns_added': 0,
                             'answered_while_lagging': False})
        case.evidence('file', ref, 'the impeded window — cadence and '
                      'ordering outcomes')
        ref = save_evidence(ctx['evidence_dir'],
                            'state-file-restored.json',
                            {'state': final.get('state'),
                             'depth': final.get('depth'),
                             'lost': final.get('lost'),
                             'owner': owner, 'peer': peer,
                             'peer_sync': 'tracking'})
        case.evidence('file', ref, 'the post-restore sink health and '
                      'reconverged pair')
        case.observe('the sink drained to healthy and the pair '
                     'reconverged with launch roles restored')
        return case.finish(
            'passed', 'the stalled --state-file mount held inside '
            'the contract: lagging surfaced while cadence and the '
            'served surface held, the window admission kept its '
            'admission-durability ordering, and restore reconverged '
            'the pair')
    finally:
        if impeded:
            try:
                restore(active)
            except Exception:
                pass
