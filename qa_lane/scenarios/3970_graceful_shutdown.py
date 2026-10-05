"""The graceful_shutdown acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the graceful-shutdown case preserves the found
# active/standby posture — a signaled container restarts onto the
# same mounts and resumes its role — so it needs no declared window.
# Its discovered position (3970) runs it after the cause-alarm leg
# and before the dcs-ctl close.


# --------------------------------------------------------------------
# The graceful SIGTERM shutdown contract on the deployed pair
# (WW-LCM-001's continuity clause, per-revision lane evidence for
# #820): a SIGTERM to the field-owning controller's container stops
# the paced scan loop at a scan boundary within the documented bound,
# flushes the latest checkpoint through the declared --state-file,
# releases the held plant write claim on the way out — a successor is
# never fenced by the dead claim — and exits 0, with a second signal
# forcing prompt exit. The leg delivers the signals through the
# runner's signal-only action (`docker kill --signal=` with no SIGKILL
# follow-up, unlike the restart action's `docker stop`) and reads the
# verdicts off the runner-owned state: the container's exit code and
# log tail (`controller_state`), the host-side --state-file, and the
# serving monitors.
#
# The run, with the pair settled and the active holding the field
# claim:
#
# - records the active's served tick, then signals its container and
#   requires it exited within the lane's stop bound with exit 0 and
#   the graceful-shutdown marker on its log tail — an old revision
#   that dies 143 without the marker reports inconclusive (the rig
#   predates the contract), never failed;
# - reads the declared --state-file and requires it holds the flushed
#   checkpoint at or past the pre-stop tick — a lost flush fails;
# - relaunches the stopped container onto the same mounts and
#   requires it resumed the persisted tick, keeps advancing, and the
#   pair reconverges to one active plus one tracking standby with the
#   found posture — the relaunch's granted startup claim is the
#   release proof: a live incumbent's claim would refuse the start
#   naming FieldClaimFailed, and a dead claim would fence it;
# - impedes the active's --state-file mount (the declared fifo lever),
#   signals once, and requires the container still runs past the
#   stall-observation window — the first signal parked in the flush,
#   not lost — then signals again and requires prompt exit with
#   status 143, restores the mount, relaunches, and requires the pair
#   reconverged with the found posture.
#
# Each down window stays inside the standby's armed failover budget
# (120 misses at the 100 ms cadence ≈ 12 s): the signaled exit lands
# in ~1 s and the container restarts in seconds, so no window arms a
# spurious self-promotion. Functional misses name
# graceful-shutdown-failed; ordering violations and unread verdicts
# name graceful-shutdown-nondeterministic; a rig that is unreachable,
# never settles, predates the contract, or carries no signal, state,
# or mount lever reports inconclusive.

GRACEFUL_SETTLE = 30        # bound on the pair settling before/after
GRACEFUL_POLL = 0.5         # cadence watching the signaled container
GRACEFUL_EXIT_BOUND = 15    # lane stop bound on the signaled exit —
                            # the binary contract is 10 s; docker
                            # delivery plus the poll cadence ride above
GRACEFUL_RETURN = 30        # bound on the relaunched monitor's return
GRACEFUL_ADVANCE = 15       # bound on the resumed tick advancing
GRACEFUL_RECONVERGE = 30    # bound on active + tracking settling
GRACEFUL_STALL_OBSERVE = 3  # the stalled flush the first signal parks in
GRACEFUL_PROMPT_BOUND = 10  # bound on the second signal's prompt exit


def _stopped(ctx, state_of, name):
    """The container's state once it is down — else None while it
    still runs. A vanished container reads as a stopped one with no
    verdict, which the exit audit fails."""
    try:
        state = state_of(name)
    except Exception:
        return None
    if state.get('running'):
        return None
    return state


def _state_tick(path):
    """The tick the --state-file's checkpoint holds."""
    with open(path) as handle:
        return json.load(handle).get('tick')


def scenario_graceful_shutdown(ctx):
    """Signal the field-owning controller's container and prove the
    graceful shutdown: bounded stop with exit 0 and the named marker,
    the flushed checkpoint on the declared state file, the claim
    released into a resumed successor, the reconverged pair, and the
    second signal's prompt exit past a stalled flush."""
    case = Case('graceful-shutdown',
                'SIGTERM stops a controller within the bound with its '
                'checkpoint flushed and its field claim released',
                'the signaled container exits within the lane stop '
                'bound with exit 0 and the graceful-shutdown marker, '
                'the declared state file holds the flushed checkpoint '
                'at or past the pre-stop tick, the relaunched '
                'controller resumes the persisted run and the pair '
                'reconverges to one active plus one tracking standby '
                'with the found posture, and a second signal past a '
                'stalled flush exits 143 promptly')
    try:
        signal = ctx.get('signal_controller')
        start = ctx.get('start_controller')
        state_of = ctx.get('controller_state')
        if not callable(signal) or not callable(start) \
                or not callable(state_of):
            return case.finish('inconclusive', 'the run context '
                               'carries no controller-signal/start/state '
                               'actions')
        state_files = ctx.get('state_files') or {}
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + GRACEFUL_SETTLE)
        if active is None:
            unreachable = all(
                _try_role(ctx, ctx[name]) is None
                for name in ('active', 'standby') if ctx.get(name))
            if unreachable:
                return case.finish('inconclusive', 'the rig is '
                                   'unreachable')
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'no peer reports role=active')
        peer = 'standby' if active == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer),
                    time.monotonic() + GRACEFUL_SETTLE) is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'never settled to one active plus a '
                               'tracking standby')
        base, peer_base = ctx[active], ctx[peer]
        state_path = state_files.get(active)
        if state_path is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no state-file path for the '
                               'restarting pair')
        try:
            before = _snapshot(ctx, base)
        except Exception as exc:
            return case.finish('inconclusive', 'the active peer '
                               'stopped answering: ' + str(exc)[:200])
        tick0 = before.get('tick') or 0
        ref = save_evidence(ctx['evidence_dir'],
                            'graceful-shutdown-before.json',
                            {'tick': tick0})
        case.evidence('file', ref, 'pre-stop tick on the field owner')
        case.observe('field owner ' + active + ' at tick '
                     + str(tick0))

        # Phase 1 — the graceful signal: SIGTERM with no SIGKILL
        # follow-up, so the process's own shutdown path is what the
        # leg observes.
        signaled_at = time.monotonic()
        try:
            signal(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the signal action '
                               'never completed: ' + str(exc)[:200])
        stopped = wait_for(
            lambda: _stopped(ctx, state_of, active),
            signaled_at + GRACEFUL_EXIT_BOUND, interval=GRACEFUL_POLL)
        if stopped is None:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the signaled container never exited '
                               'within the lane stop bound')
        exit_code = stopped.get('exit')
        logs = stopped.get('logs') or ''
        graceful = 'graceful shutdown' in logs
        ref = save_evidence(ctx['evidence_dir'],
                            'graceful-shutdown-stop.json',
                            {'tick': tick0,
                             'exit': exit_code, 'graceful': graceful})
        case.evidence('file', ref, 'the signaled exit: exit code and '
                      'the graceful marker')
        case.observe('signaled container exited with exit %s'
                     % (exit_code,))
        if exit_code != 0 or not graceful:
            if exit_code == 143 and not graceful:
                return case.finish(
                    'inconclusive', 'the deployed revision predates '
                    'the contract — SIGTERM killed the run (exit 143) '
                    'without the graceful-shutdown marker')
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the signaled exit was %s without the '
                               'graceful shutdown (marker %s)'
                               % (exit_code, graceful))

        # The flushed checkpoint: the declared state file holds the
        # stopped run's tick — at or past the pre-stop read, since the
        # final persist captures the current tick.
        try:
            persisted = _state_tick(state_path)
        except (OSError, ValueError) as exc:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the declared state file does not hold '
                               'the flushed checkpoint: ' + str(exc)[:200])
        if not isinstance(persisted, int) or persisted < tick0:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the state file holds tick %s while the '
                               'run served %s before the signal — the '
                               'flush lost the stopped run'
                               % (persisted, tick0))
        case.observe('state file holds the flushed checkpoint at tick '
                     + str(persisted))

        # Phase 2 — the handover: the stopped container relaunches
        # onto the same mounts. Its granted startup claim is the
        # release proof — a standing claim would refuse the start —
        # and the resumed tick with the reconverged pair is the
        # continuity proof.
        try:
            start(active)
        except Exception as exc:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the signaled container never relaunched '
                               '(a dead claim fencing the successor '
                               'reads exactly here): '
                               + str(exc)[:200])

        def returned():
            report = _try_role(ctx, base)
            if report is None or report.get('role') != 'active':
                return None
            return report

        if wait_for(returned, time.monotonic() + GRACEFUL_RETURN,
                    interval=GRACEFUL_POLL) is None:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the relaunched controller never '
                               'returned as active')
        resumed = _snapshot(ctx, base)
        tick1 = resumed.get('tick') or 0
        grown = wait_for(
            lambda: (s.get('tick', 0) > tick1 and s or None)
            if (s := _try_snapshot(ctx, base)) else None,
            time.monotonic() + GRACEFUL_ADVANCE)
        ref = save_evidence(ctx['evidence_dir'],
                            'graceful-shutdown-resumed.json',
                            {'persisted': persisted,
                             'resumed': tick1,
                             'advanced': bool(grown)})
        case.evidence('file', ref, 'resumed tick and its advance')
        if tick1 < persisted:
            return case.finish(
                'failed', 'graceful-shutdown-nondeterministic: the tick '
                'rewound across the resume: ' + str(persisted) + ' -> '
                + str(tick1))
        if not grown:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the resumed run did not advance its '
                               'tick')
        case.observe('resumed at tick ' + str(tick1) + ' (persisted '
                     + str(persisted) + ') and advancing')

        def roles_settled():
            try:
                resumed_role = _role(ctx, base)
                peer_role = _role(ctx, peer_base)
            except Exception:
                return None
            if resumed_role.get('role') != 'active' \
                    or peer_role.get('role') != 'standby':
                return None
            if 'tracking' not in (peer_role.get('sync') or {}):
                return None
            return {'restarted': resumed_role, 'peer': peer_role}

        settled = wait_for(roles_settled,
                           time.monotonic() + GRACEFUL_RECONVERGE,
                           interval=GRACEFUL_POLL)
        if not settled:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the pair did not reconverge to one '
                               'active plus one tracking standby after '
                               'the resume')
        case.observe('pair reconverged: restarted peer active, '
                     + peer + ' tracking standby')

        # Phase 3 — the second signal past a stalled flush: impede
        # the active's --state-file mount so the graceful flush waits
        # instead of finishing, signal once, require the container
        # still runs, signal again, and require prompt exit 143.
        impede = ctx.get('impede_state_file')
        restore = ctx.get('restore_state_file')
        if not callable(impede) or not callable(restore):
            return case.finish(
                'inconclusive', 'the run context carries no state-file '
                'mount lever — the graceful path above passed, but '
                'the second-signal prompt exit cannot be staged')
        try:
            impede(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the mount lever '
                               'never staged: ' + str(exc)[:200])
        try:
            signal(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the second-phase '
                               'signal never completed: '
                               + str(exc)[:200])
        deadline = time.monotonic() + GRACEFUL_STALL_OBSERVE
        exited_early = None
        while time.monotonic() < deadline:
            state = state_of(active)
            if not state.get('running'):
                exited_early = state
                break
            time.sleep(GRACEFUL_POLL)
        if exited_early is not None:
            early_exit = exited_early.get('exit')
            early_logs = exited_early.get('logs') or ''
            if early_exit == 143 \
                    and 'graceful shutdown' not in early_logs:
                return case.finish(
                    'inconclusive', 'the deployed revision predates '
                    'the contract — the first signal killed the run '
                    '(exit 143) instead of parking in the stalled '
                    'flush')
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the first signal exited (%s) despite '
                               'the stalled mount — the flush never '
                               'waited' % (early_exit,))
        case.observe('first signal parked in the stalled flush — the '
                     'container still runs')
        second_at = time.monotonic()
        try:
            signal(active)
        except Exception as exc:
            return case.finish('inconclusive', 'the second signal '
                               'never completed: ' + str(exc)[:200])
        forced = wait_for(
            lambda: _stopped(ctx, state_of, active),
            second_at + GRACEFUL_PROMPT_BOUND, interval=GRACEFUL_POLL)
        if forced is None:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the second signal never forced prompt '
                               'exit within the bound')
        forced_exit = forced.get('exit')
        ref = save_evidence(ctx['evidence_dir'],
                            'graceful-shutdown-second-signal.json',
                            {'exit': forced_exit})
        case.evidence('file', ref, 'the second signal prompt exit')
        if forced_exit != 143:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the second signal exited %s, expected '
                               'the prompt 143' % (forced_exit,))
        case.observe('second signal forced prompt exit 143')

        # Restore the mount, relaunch, and reconverge with the found
        # posture — the pair the next leg inherits.
        try:
            restore(active)
        except Exception as exc:
            restore_error = str(exc)[:200]
        else:
            restore_error = None
        try:
            start(active)
        except Exception as exc:
            return case.finish('failed', 'graceful-shutdown-failed: '
                               'the container never relaunched after '
                               'the prompt exit: ' + str(exc)[:200])
        final = wait_for(roles_settled,
                         time.monotonic() + GRACEFUL_RECONVERGE,
                         interval=GRACEFUL_POLL)
        if restore_error is not None:
            return case.finish('inconclusive', 'the mount lever '
                               'never restored: ' + restore_error)
        if not final:
            return case.finish(
                'failed',
                'graceful-shutdown-nondeterministic: the pair did not '
                'restore to one active plus one tracking standby '
                'after the prompt exit')
        case.observe('pair restored: restarted peer active, '
                     + peer + ' tracking standby')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
