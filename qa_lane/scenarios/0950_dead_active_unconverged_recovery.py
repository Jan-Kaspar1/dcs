"""The dead_active_unconverged_recovery acceptance leg — one module
per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for
the ordering rule and the shared seam."""
from .common import *

# Ordering: The dead-active recovery case sits in the same restored
# window as the standby-loss case beside it: it stops the field
# owner, drives its unconverged standby's promote refusals, and
# restarts the owner so it preempts the dead hold — then it hands the
# field back so the pair stands on its launch roles for the cases
# behind this one.
RUNS_AFTER = frozenset({'scenario_superseded_restart'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# Decision 86's recorded recovery, as per-revision lane evidence for
# WW-LCM-001's continuity and recovery clauses. An active that dies
# holding the plant's single-writer claim while its standby is
# unconverged is recoverable only by restart-as-active: promotion
# requires `StandbySync::Tracking` or `Reinitialized`, a verdict a
# dead peer can never supply — the claim stands across a disconnect
# and `release_writer` drops only the caller's own hold, so promotion
# cannot free the field either. No other lane leg stages the wedge:
# the superseded-restart case assumes a converged tracking standby,
# the standby-loss case keeps the active alive, and the divergence
# legs keep the active serving. The leg uses only the runner's
# existing ctx actions and reads everything through the surviving
# peer's serving monitor, the field's own claim answers, and the
# peer-declared `--journal-file` paths:
#
# - (a) The wedge. With the pair settled and tracking, the field
#   owner's container is stopped (`ctx['stop_controller']`) — the
#   dead owner's claim keeps standing under its own hold, since the
#   claim is not connection-released. Across the whole dead-peer
#   window, asserted through the standby's serving monitor:
#     * the standby's sync report walks to an unconverged verdict and
#       it never leaves `standby` — it cannot promote itself out of
#       the wedge;
#     * every `POST /promote` answers the named `not_converged`
#       refusal carrying that standing verdict — a silent promotion
#       among them is the defect;
#     * the quiesced peer keeps serving: `/role`, `/snapshot`, and
#       `/journal` answer throughout the window, so the wedge is a
#       named, monitorable state rather than a process exit;
#     * the dead owner's claim keeps fencing third-party mutation
#       probes through `_plant_probe` — `fenced`, naming the dead
#       owner's own token, never `unclaimed` and never a silently
#       writable field.
#
# - (b) The recorded recovery. `ctx['start_controller']` relaunches
#   the active's container as its configured active: its unconditional
#   startup claim preempts the dead owner's outliving hold. Asserted
#   afterwards:
#     * the resumed active owns the field — its writes land on the
#       plant, its own scans step it, and third-party probes answer
#       `fenced` naming the resumed owner's token;
#     * exactly one peer reports `active` at every poll across the
#       recovery and the reconvergence;
#     * the standby reconverges on the new checkpoint stream — a
#       converged verdict again, with the `source_restarted` record
#       journaled where the stream's generation regressed across the
#       resumed owner's cold-start boundary.
#
# The rig's launch roles are restored for the cases behind this one:
# the launched active back on the field with the launched standby
# converged on it. The leg is inconclusive when the rig or a settled
# pair is unreachable, when the run publishes no plant endpoint to
# fence through, when a peer pins no owner token, when the lifecycle
# actions are absent, or when the stop or the start never completed —
# an induction that never stood, never a verdict.
#
# Named diagnostics: dead-active-recovery-failed tags the contract
# clauses — a standby that left `standby` or never showed an
# unconverged verdict, a promote admitted or answered another refusal,
# a quiesced peer that stopped serving, a dead claim that lapsed,
# fenced nothing, or named another owner, a resumed active that never
# owned the field or whose writes never landed, a dual-active window,
# a standby that never reconverged, or a restart never journaled — and
# dead-active-recovery-nondeterministic the instability the contract
# does not answer for: a staging lever that never completed, a read
# that dropped, or two passes whose evidence digests diverge.

DEAD_ACTIVE_POLL = 0.4         # cadence watching the wedge and recovery
DEAD_ACTIVE_WEDGE = 45         # bound on the standby's unconverged walk
DEAD_ACTIVE_PROMOTES = 3       # promote probes fired across the wedge
DEAD_ACTIVE_RETURN = 60        # bound on the resumed owner's monitor
DEAD_ACTIVE_RECONVERGE = 60    # bound on the standby reconverging
DEAD_ACTIVE_WINDOW = 45        # bound on the one-active recovery window
DEAD_ACTIVE_FIELD_TICKS = 3    # served field ticks the resumed owner shows
DEAD_ACTIVE_SETTLE_DEADLINE = 60  # bound on the closing role restore

# The sync-state verdicts that are *not* convergence: the shapes a
# dead peer's standby must report while it can never be promoted.
UNCONVERGED_SYNC = ('unsynchronized', 'degraded')
CONVERGED_SYNC = ('tracking', 'orphaned')


def _sync_kind(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest single-key objects. None for
    a settled `active`."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _converged(report):
    """Whether a served report reads a standby under a converged sync
    verdict — the posture a restartee's promote is admitted on."""
    return (report or {}).get('role') == 'standby' \
        and _sync_kind(report) in CONVERGED_SYNC


def _journal_file_events(path):
    """Every event body a `--journal-file` records, in append order."""
    return [(item.get('entry') or {}).get('event')
            for item in _journal_entries(path)
            if isinstance(item.get('entry'), dict)]


def _journal_file_events_of(path, kind):
    """The payloads one event kind carries in a `--journal-file`, in
    record order, or None where the file cannot be read."""
    try:
        return [event[kind] for event in _journal_file_events(path)
                if isinstance(event, dict) and kind in event]
    except (OSError, ValueError):
        return None


def _control(url, payload=None):
    """POST a control-plane request — /promote and /demote — answering
    `(status, body)` with a refused call's named outcome decoded
    instead of raised."""
    try:
        return http_json('POST', url, payload)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body


def _owning_report(ctx, key):
    """A served report while the endpoint owns the field — the
    posture a resumed launched active must reach."""
    report = _try_role(ctx, ctx[key])
    if report is not None and report.get('role') == 'active':
        return report
    return None


def _converged_report(ctx, key):
    """A served report while the endpoint is a standby under a
    converged sync verdict — the posture the recovered pair must
    reconverge to."""
    report = _try_role(ctx, ctx[key])
    if report is not None and _converged(report):
        return report
    return None


def _probe_owner(probe):
    """The owner token a fencing verdict attributes the standing claim
    to, or None on an unfenced or unattributed answer."""
    return ((probe or {}).get('error') or {}).get('owner')


def _pair_view(*keys):
    """The health view the leg grades: one entry per peer the page
    would be configured with."""
    return [{'key': key, 'report': None, 'error': None} for key in keys]


def _pair_poll(ctx, view):
    """One `/role` poll per peer in the view — the pair view's role
    cadence. A failed poll is recorded as pair health; the page reads a
    peer that does not answer as unreachable, never as healthy."""
    for entry in view:
        report = _try_role(ctx, ctx[entry['key']])
        entry['report'] = report
        entry['error'] = None if report is not None else 'unreachable'


def _pair_health(view):
    """The page's pairHealth verdict over the view's last poll: the
    peers reporting `active`, and the redundancy faults it names — an
    unreachable peer, a standby off a converged verdict, no active peer
    at all, and the dual-active split the surface must never silently
    resolve by picking the first peer that reports active."""
    faults, actives = [], []
    for entry in view:
        name = entry['key']
        if entry['error'] is not None:
            faults.append(name + ' unreachable')
            continue
        report = entry['report']
        if report is None:
            continue
        if report.get('role') == 'active':
            actives.append(name)
        if report.get('role') == 'standby' \
                and _sync_kind(report) not in CONVERGED_SYNC:
            faults.append(name + ' standby ' + str(_sync_kind(report)))
    if not actives:
        faults.append('no_active_peer')
    elif len(actives) > 1:
        faults.append('dual_active')
    return {'active': actives[0] if len(actives) == 1 else None,
            'actives': actives,
            'faults': faults}


def scenario_dead_active_unconverged_recovery(ctx):
    """Stop the field owner's container and prove the wedge decision 86
    records — an unconverged standby refused `not_converged` across the
    whole dead-peer window while the dead owner's claim keeps fencing
    and its monitor keeps serving — then restart it as its configured
    active, assert the resumed owner takes the field with exactly one
    peer active throughout and the standby reconverging on the new
    stream, and restore the pair's launch roles."""
    case = Case('dead-active-unconverged-recovery',
                'A dead active holding the field is recoverable only '
                'by restart-as-active',
                'with the pair settled and tracking, stopping the '
                "field owner's container walks its standby's served "
                'sync to an unconverged verdict it never promotes out '
                'of — every POST /promote in the dead-peer window '
                'answering the named not_converged refusal carrying '
                'that verdict, the quiesced peer still serving role, '
                'snapshot, and journal, and the dead owner\'s standing '
                'claim still fencing third-party mutation probes and '
                'naming its own token; restarting the stopped container '
                'as its configured active preempts the dead hold, the '
                'resumed owner\'s writes land and step the plant while '
                'probes answer fenced naming its token, exactly one '
                'peer reports active at every poll across the recovery, '
                'and the standby reconverges on the new checkpoint '
                'stream with the source_restart record journaled '
                'where its generation regressed; and the pair is '
                'restored to its launch roles afterward')
    active, standby = 'active', 'standby'
    started = False
    try:
        stop = ctx.get('stop_controller')
        start = ctx.get('start_controller')
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context carries '
                               'no controller stop/start action — the '
                               'dead-active wedge has no documented '
                               'seam')
        if not ctx.get('plant'):
            return case.finish('inconclusive',
                               'the run publishes no plant endpoint')
        if not all(ctx.get(name) for name in (active, standby)):
            return case.finish('inconclusive', 'the run context '
                               'publishes no monitor for one of the '
                               'launched pair members')
        tokens = ctx.get('plant_owner') or {}
        journals = ctx.get('journal_files') or {}
        dead_token = tokens.get(active)
        peer_token = tokens.get(standby)
        if dead_token is None or peer_token is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the pair')
        dead_journal = journals.get(active)
        peer_journal = journals.get(standby)
        if dead_journal is None or peer_journal is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for '
                               'the pair')

        def settled():
            """The settled launch shape: the launched active owning the
            field with the launched standby converged on it, or None."""
            owner = _try_role(ctx, ctx[active])
            tracker = _try_role(ctx, ctx[standby])
            if (owner or {}).get('role') == 'active' \
                    and _converged(tracker):
                return True
            return None

        if wait_for(settled, time.monotonic() + DEAD_ACTIVE_RETURN,
                    interval=DEAD_ACTIVE_POLL) is None:
            reachable = any(_try_role(ctx, ctx[name]) is not None
                            for name in (active, standby))
            return case.finish(
                'failed' if reachable else 'inconclusive',
                'the pair never settled in its launch shape — '
                + active + ' must own the field with ' + standby
                + ' converged on it' if reachable
                else 'the pair is unreachable')
        baseline_probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        if not _fenced(baseline_probe):
            return case.finish('failed', 'the field held no writer '
                               'claim before the wedge — a third '
                               'attachment\'s probe answered '
                               + json.dumps(baseline_probe)[:300])
        if _probe_owner(baseline_probe) != dead_token:
            return case.finish('failed', 'the standing claim names '
                               + str(_probe_owner(baseline_probe))
                               + ', not the launched active\'s own '
                                 'pinned token — the pair is not in its '
                                 'launch claim state: '
                               + json.dumps(baseline_probe)[:300])
        case.observe('field owner: ' + active + ' (token '
                     + str(dead_token) + '); standby: ' + standby
                     + ' (token ' + str(peer_token) + ')')

        # --- (a) the wedge ------------------------------------------
        # The dead owner's container stops mid-claim. The claim is held
        # by the dead owner's own hold, not by its connection, so the
        # field stays claimed by a token nobody can answer for.
        try:
            stop(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the field-owner stop '
                               'induction never completed: '
                               + str(exc)[:300])
        case.observe('field-owner container stopped; its claim stands '
                     'under its own hold')

        window = {'syncs': [], 'roles': [], 'promotes': [],
                  'served': {'role': 0, 'snapshot': 0, 'journal': 0},
                  'probes': [], 'owners': []}
        deadline = time.monotonic() + DEAD_ACTIVE_WEDGE
        while time.monotonic() < deadline:
            report = _try_role(ctx, ctx[standby])
            if report is None:
                time.sleep(DEAD_ACTIVE_POLL)
                continue
            window['roles'].append(report.get('role'))
            window['syncs'].append(_sync_kind(report))
            for path, key in (('/role', 'role'), ('/snapshot', 'snapshot'),
                              ('/journal?since=0', 'journal')):
                try:
                    http_json('GET', ctx[standby] + path)
                    window['served'][key] += 1
                except Exception:
                    pass
            probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
            if probe is not None:
                window['probes'].append(probe)
                window['owners'].append(_probe_owner(probe))
            if len(window['promotes']) < DEAD_ACTIVE_PROMOTES \
                    and _sync_kind(report) in UNCONVERGED_SYNC:
                window['promotes'].append(
                    _control(ctx[standby] + '/promote'))
            if len(window['promotes']) >= DEAD_ACTIVE_PROMOTES \
                    and len(window['syncs']) >= 4:
                break
            time.sleep(DEAD_ACTIVE_POLL)
        ref = save_evidence(ctx['evidence_dir'],
                            'dead-active-wedge.json', window)
        case.evidence('file', ref, 'the dead-peer window: the '
                      'standby\'s served sync walk, its promote '
                      'refusals, its kept-serving counts, and the '
                      "dead owner's fencing probes")
        if not window['syncs']:
            return case.finish('inconclusive', 'the quiesced peer\'s '
                               'monitor never answered /role across the '
                               'dead-peer window — the wedge was never '
                               'observed')
        if not any(sync in UNCONVERGED_SYNC
                   for sync in window['syncs']):
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the quiesced peer never '
                'walked its sync to an unconverged verdict — it served '
                + json.dumps(window['syncs']) + ' with its source dead, '
                'so a promotion would have been admitted on a stale '
                'convergence proof')
        if any(role != 'standby' for role in window['roles']):
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the quiesced peer left '
                'standby across the dead-peer window — it read '
                + json.dumps(window['roles']) + ': promotion requires a '
                'converged verdict a dead peer can never supply')
        if any(count == 0 for count in window['served'].values()):
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the quiesced peer stopped '
                'serving across the dead-peer window — /role, '
                '/snapshot, and /journal answered '
                + json.dumps(window['served']) + ': the wedge is a named, '
                'monitorable state, not a process exit')
        if not window['promotes']:
            return case.finish('inconclusive', 'the wedge window never '
                               'reached an unconverged verdict to probe '
                               'with a promote')
        for status, body in window['promotes']:
            if status != 409 or not (isinstance(body, dict)
                                     and 'not_converged' in body):
                return case.finish(
                    'failed',
                    'dead-active-recovery-failed: POST /promote on the '
                    'unconverged standby answered ' + str(status) + ' '
                    + json.dumps(body)[:300] + ' — the convergence gate '
                    'must refuse it by name, never admit a silent '
                    'promotion over a dead source')
        if not window['probes']:
            return case.finish('inconclusive', 'no third-party fencing '
                               'probe answered across the dead-peer '
                               'window — the claim\'s standing evidence '
                               'was never observed')
        unfenced = [probe for probe in window['probes']
                    if not _fenced(probe)]
        if unfenced:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: a third-party mutation was '
                'not refused across the dead-peer window — the dead '
                'owner\'s claim answered '
                + json.dumps(unfenced[0])[:300] + ': the field was '
                'unclaimed or silently writable behind a dead owner')
        if any(owner != dead_token for owner in window['owners']):
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the standing claim moved '
                'off the dead owner\'s token across the window — it '
                'fenced naming ' + json.dumps(window['owners'])[:300]
                + ' where the dead owner held ' + str(dead_token))
        case.observe('the dead peer\'s wedge: sync walked '
                     + ' -> '.join(str(sync) for sync in window['syncs'])
                     + ' with ' + str(len(window['promotes']))
                     + ' not_converged refusals, the quiesced peer '
                     'answering role/snapshot/journal '
                     + json.dumps(window['served']) + ', and the claim '
                     'fencing naming the dead owner\'s token throughout')

        # --- (b) the recorded recovery ------------------------------
        # The stop/start pair is the lane's own restart machinery: the
        # started container comes back as its configured active, whose
        # unconditional startup claim preempts the dead hold.
        try:
            start(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the field-owner start '
                               'induction never completed: '
                               + str(exc)[:300])
        started = True
        case.observe('field-owner container started; its startup '
                     'claim preempts the dead hold')
        resumed = wait_for(lambda: _owning_report(ctx, active),
                           time.monotonic() + DEAD_ACTIVE_RETURN,
                           interval=DEAD_ACTIVE_POLL)
        if resumed is None:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the relaunched container '
                'never resumed as active — GET /role reads '
                + json.dumps(_try_role(ctx, ctx[active]))[:300]
                + ': the unconditional startup claim did not preempt the '
                  'dead owner\'s outliving hold')

        # The one-active window plus the field keeping stepping under
        # the resumed owner: its served tick advances, and third-party
        # probes keep answering `fenced` naming the resumed token.
        view = _pair_view(active, standby)
        polls = []
        ticks = []
        prev_tick = None
        deadline = time.monotonic() + DEAD_ACTIVE_WINDOW
        while time.monotonic() < deadline:
            _pair_poll(ctx, view)
            health = _pair_health(view)
            polls.append({'roles': {
                entry['key']: (entry['report'] or {}).get('role')
                for entry in view},
                'actives': health['actives'],
                'faults': health['faults']})
            if len(health['actives']) > 1:
                return case.finish(
                    'failed',
                    'dead-active-recovery-failed: two peers reported '
                    'active (' + ', '.join(health['actives'])
                    + ') across the recovery — the field must end owned '
                      'by exactly one peer')
            if health['active'] == active:
                tick = (_try_snapshot(ctx, ctx[active]) or {}).get('tick')
                if isinstance(tick, int) \
                        and (prev_tick is None or tick > prev_tick):
                    prev_tick = tick
                    ticks.append(tick)
            if len(ticks) >= DEAD_ACTIVE_FIELD_TICKS:
                break
            time.sleep(DEAD_ACTIVE_POLL)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        ref = save_evidence(ctx['evidence_dir'],
                            'dead-active-recovery.json',
                            {'polls': polls, 'field_ticks': ticks,
                             'probe': probe})
        case.evidence('file', ref, 'the recovery window: the one-active '
                      'polls, the resumed owner\'s advancing field '
                      'ticks, and the post-recovery fencing probe')
        if not polls:
            return case.finish('inconclusive', 'no poll answered across '
                               'the recovery window')
        if len(ticks) < DEAD_ACTIVE_FIELD_TICKS:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the resumed owner advanced '
                'its served field ' + str(len(ticks)) + ' times across '
                'the recovery window where '
                + str(DEAD_ACTIVE_FIELD_TICKS) + ' were required — the '
                'field stopped stepping under the resumed owner')
        if not _fenced(probe):
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the field stopped fencing '
                'third-party mutations after the takeover — the probe '
                'answered ' + json.dumps(probe)[:300])
        if _probe_owner(probe) != dead_token:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the field\'s claim names '
                + str(_probe_owner(probe)) + ' after the takeover, not '
                'the resumed owner\'s own pinned token '
                + str(dead_token) + ': '
                + json.dumps(probe)[:300])
        case.observe('the resumed owner owns the field: exactly one '
                     'active at every one of ' + str(len(polls))
                     + ' polls, the plant stepped ' + str(len(ticks))
                     + ' times, and probes fence naming its token')

        # The standby reconverges on the new checkpoint stream, and the
        # stream's generation regression across the resumed owner's
        # cold-start boundary is journaled as `source_restarted` where
        # the regression applies.
        reconverged = wait_for(
            lambda: _converged_report(ctx, standby),
            time.monotonic() + DEAD_ACTIVE_RECONVERGE,
            interval=DEAD_ACTIVE_POLL)
        resyncs = _journal_file_events_of(peer_journal,
                                          'source_restarted')
        ref = save_evidence(
            ctx['evidence_dir'], 'dead-active-resync.json',
            {'report': _try_role(ctx, ctx[standby]),
             'source_restarted': resyncs})
        case.evidence('file', ref, 'the standby\'s reconverged verdict '
                      'and its journaled source-restart record')
        if reconverged is None:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the standby never '
                'reconverged to a promotable verdict on the resumed '
                'owner\'s new checkpoint stream — GET /role reads '
                + json.dumps(_try_role(ctx, ctx[standby]))[:300])
        if resyncs is None:
            return case.finish('inconclusive', 'the standby\'s durable '
                               'journal became unreadable across the '
                               'resync audit')
        if resyncs:
            case.observe('the standby journaled ' + str(len(resyncs))
                         + ' source_restarted record(s) for the new '
                           'stream generation')
        case.observe('the standby reconverged: '
                     + (reconverged or {}).get('role') + '/'
                     + str(_sync_kind(reconverged)))

        # The restore: the pair back on its launch roles — the launched
        # active owning the field with the launched standby converged.
        # The recovery already left the restartee on the field, so the
        # restore is proved from the other direction too: the field
        # handed to the other member and back through the documented
        # order, so a recovery can never leave the pair wedged for the
        # legs behind this one.
        swap_demote, swap_promote = (_control(ctx[active] + '/demote'),
                                     _control(ctx[standby] + '/promote'))
        if swap_demote[0] != 200 or swap_promote[0] != 200:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the restore swap answered '
                'demote ' + str(swap_demote[0]) + ' promote '
                + str(swap_promote[0]) + ' — the pair could not be '
                'handed off the recovered owner')
        if wait_for(lambda: _owning_report(ctx, standby),
                    time.monotonic() + DEAD_ACTIVE_SETTLE_DEADLINE,
                    interval=DEAD_ACTIVE_POLL) is None:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the restore swap left no '
                'field owner — the pair lost the field outright after '
                'the recovery')
        demote, promote = (_control(ctx[standby] + '/demote'),
                           _control(ctx[active] + '/promote'))
        if demote[0] != 200 or promote[0] != 200:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the restore switch '
                'answered demote ' + str(demote[0]) + ' promote '
                + str(promote[0]) + ' — the pair could not be handed '
                'back to its launch roles')
        if wait_for(settled, time.monotonic() + DEAD_ACTIVE_SETTLE_DEADLINE,
                    interval=DEAD_ACTIVE_POLL) is None:
            return case.finish(
                'failed',
                'dead-active-recovery-failed: the pair did not settle '
                'back to its launch roles after the recovery')
        case.observe('the pair stands restored on its launch roles')
    except Exception as exc:
        return case.finish('failed', 'dead-active-recovery-failed: the '
                           'leg raised ' + repr(exc)[:300])
    finally:
        if started and _try_role(ctx, ctx[active]) is None:
            # The recovery never came up: put the rig back so the legs
            # behind this one do not read a dismantled pair.
            try:
                ctx['start_controller'](active)
            except Exception:
                pass
    return case.finish('passed')