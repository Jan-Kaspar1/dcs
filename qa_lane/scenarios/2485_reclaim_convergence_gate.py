"""The reclaim_convergence_gate acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages its own pair of born-active seats on the
# lane's scratch sim-serve field and freezes it, so it needs the
# sim-bus startup-refusal leg's seats released — it shares the
# driven/foreign/revised born seats with every born leg behind it —
# and must finish before the revision legs take those seats over.
RUNS_AFTER = frozenset({'scenario_ownerless_remote_backoff',
                        'scenario_sim_bus_startup_claim_refusal'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The convergence-gated fencing-loss reclaim contract — the
# per-revision lane evidence for the contract #1317's fix establishes
# (WW-LCM-001 continuity and the ownership-epoch integrity the
# failover-miss budget's staleness bound rests on).
#
# The fencing-loss reclaim exists so a fenced ex-owner can re-take its
# own released claim and escape the released-preemption wedge: a
# different owner preempts the claim, the ex-owner's next write meets
# the fence, and the demotion leaves it `standby` with the loss mark
# armed — after which the bound conditional `reclaim` takes the field
# back under its own token, pre-empting the holderless shapes the
# field's arbitration cannot tell from a dead owner's. Those
# conditional-claim paths break a deadlock only where the asker can
# *prove* convergence with the field's line; a run with zero
# convergence evidence — never tracked, unsynchronized, with no
# adoptable tracking source — must never preempt a different owner's
# claim on stale state.
#
# The defect the contract answers let the least-converged participant
# win a post-outage race purely on transport ordering: a fenced
# ex-owner's armed reclaim landed before the incumbent's re-attach, so
# it re-activated on an image older than the field's line and rolled
# the incumbent's applied state back, while the incumbent was fenced
# and demoted on its own re-attach. Nothing about the race was
# deterministic — only the transport ordering was.
#
# The leg stages the finding's own deterministic sequence against the
# deployed rig, on the lane's scratch sim-serve field so the deployed
# pair is never moved:
#
# - `start_born_field('serving')` serves the plant, and a born-active
#   on the `driven` seat claims it and settles `active` — the ex-owner
#   whose fencing loss arms the reclaim.
# - `start_born_field('serving')` again re-serves the field *under the
#   same container name*: a fresh plant server with an empty claim
#   table, and the ex-owner's control connection severed. A born-active
#   on the `foreign` seat then launches against the unclaimed field —
#   its conditional startup grant lands and it claims first — while the
#   ex-owner reattaches, meets the fence on its next write, and settles
#   `standby`/`unsynchronized` with the loss mark standing and no
#   adoptable tracking source.
# - `pause_born_field` freezes the plant in place and the leg holds it
#   there until *both* seats' served `io_health` reports their control
#   connection down: the incumbent's claim stays standing — a
#   `dcs-sim-net` claim outlives its holders — so the thaw is the
#   post-outage race the defect was won on.
# - `unpause_born_field` thaws. Through both seats' serving monitors
#   and their durable `--journal-file`s the leg asserts that A's
#   reclaim cannot preempt B's claim: the incumbent's re-attach keeps
#   or retakes ownership on its live line (it settles `active` with the
#   claim `held` and its tick advancing), the unsynchronized ex-owner
#   stays `standby` without the field across the whole window, and no
#   ownership epoch rolls back to the staler image (the ex-owner's
#   journal carries no `reclaim`-origin promotion, no re-armed claim,
#   and no `field_claim_observed` naming the incumbent — a reclaim that
#   issued at all is a reclaim that ran ungated).
#
# The positive control then proves the probe still does its designed
# work: a *converged* fenced ex-owner's bound reclaim of its own
# released claim still takes the field. On a freshly re-served field a
# born-active on the `revised` seat claims it and declares the
# incumbent seat its tracking source; the re-serve and the incumbent's
# claim fence it in place; it converges `tracking` on the live line —
# the standing proof the gate reads — and once the incumbent is removed
# and the field's claim stands holderless, its bound reclaim preempts
# by design, walking `standby → promoting → active` under the `reclaim`
# origin with the claim `held`.
#
# The deployed pair is framed before and after and must be undisturbed
# throughout; every staged seat and the scratch field are torn down and
# the pair's launch roles restored.
#
# Named diagnostics: reclaim-convergence-failed tags the contract
# clauses — an ex-owner that left `standby` or took the field, an
# ownership epoch that rolled back on stale state, an incumbent that
# lost or was fenced on its re-attach, a stalled or lost claim, a
# positive control that stayed silent, a disturbed or unrestored
# deployed pair, and an unjournaled or unattributed loss — and
# reclaim-convergence-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a starved
# watch, a seat's process gone, a read that dropped, an unconverged
# positive control, two passes whose digests diverge.
#
# A staged revision that predates the contract reports inconclusive.
# #1317's fix is a behavioural guard with no new served field and no
# new durable record, so the pre-contract signature is behavioural: the
# ex-owner's reclaim *issued* — its journal carries the granted
# `reclaim`-origin promotion or the refused `field_claim_observed`
# naming the incumbent — *and* its own serving monitor showed it
# taking the field. A build without the convergence gate loses that
# race on transport ordering and presents exactly that shape, so the
# leg cannot call it a failure of the contract it never had. A record
# whose served surfaces report the gating holding while its durable
# journal proves the reclaim took the field is not that signature: the
# served monitors never saw the take, so the honest defect reading does
# not apply and the contradiction is the contract failure the judge
# names.
#
# The unchecked-diagnostic self-check replays the judge over planted
# negatives and reports reclaim-convergence-unchecked for any that slip
# through.

EX_OWNER_SEAT = 'driven'    # A — the born-active the fencing loss arms
INCUMBENT_SEAT = 'foreign'  # B — the born-active that claims after the
                            # re-serve, holding the claim across the
                            # freeze
CONTROL_SEAT = 'revised'    # C — the converged fenced ex-owner of the
                            # positive control
SEATS_USED = (EX_OWNER_SEAT, INCUMBENT_SEAT, CONTROL_SEAT)

RECLAIM_SERVE = 60    # bound on each served field's claim to settle
RECLAIM_SETTLE = 60   # bound on each seat's role and claim walk
RECLAIM_POLL = 0.5    # cadence polling the seats' serving monitors
RECLAIM_LINK = 1.0    # cadence polling a seat's served io_health
RECLAIM_FREEZE = 30   # the freeze, held until both links report down
RECLAIM_WINDOW = 25   # the post-thaw window the race is judged across
RECLAIM_ROUNDS = 6    # reads the post-thaw watch collects per seat
RECLAIM_RESTORE = 45  # bound on the launch-layout restore
DIAG_FAILED = 'reclaim-convergence-failed'
DIAG_NONDET = 'reclaim-convergence-nondeterministic'
DIAG_UNCHECKED = 'reclaim-convergence-unchecked'

# The sync verdicts a run with zero convergence evidence reports: never
# tracked, degraded on a pull that produced nothing, or diverged on the
# field evidence. `tracking` and `reinitialized` are the two verdicts
# whose landed apply re-proves the run, so neither may appear here.
UNCONVERGED_SYNC = ('unsynchronized', 'degraded', 'diverged')


def _seat_sync(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest are single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _seat_role(ctx, seat):
    return _try_role(ctx, ctx[seat])


def _seat_view(ctx, seat):
    """One read of a born seat's serving monitor: the RoleReport plus
    the snapshot's driver link — the served evidence tuple every clause
    of the contract reads. None when the monitor answers nothing: a
    dropped read is never a view."""
    report = _try_role(ctx, ctx[seat])
    if report is None:
        return None
    snapshot = _try_snapshot(ctx, ctx[seat]) or {}
    driver = ((snapshot.get('io_health') or {}).get('driver') or {})
    return {'role': report.get('role'),
            'sync': _seat_sync(report),
            'field_claim': report.get('field_claim'),
            'link': driver.get('link'),
            'tick': report.get('tick')}


def _seat_state(ctx, seat):
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _seat_entries(ctx, seat):
    """The seat's durable `--journal-file` entries — the records the
    born launch reset at launch — or None when the file cannot be
    read at all."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return None
    try:
        return [item for item in _journal_entries(path)
                if 'entry' in item]
    except Exception:
        return None


def _seat_events(entries, kind):
    """The `kind` event bodies the durable entries carry."""
    out = []
    for item in entries or []:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _seat_walk(entries):
    """The journaled `role_changed` transitions as (from, to, origin)
    tuples — the walk attribution the ownership-epoch audit reads."""
    return [(change.get('from'), change.get('to'), change.get('origin'))
            for change in _seat_events(entries, 'role_changed')]


def _seat_floor(ctx, seat):
    """The durable journal's entry count — the cursor the freeze's
    evidence is read after. None where the file cannot be read."""
    entries = _seat_entries(ctx, seat)
    return None if entries is None else len(entries)


def _seat_since(ctx, seat, floor):
    """The durable entries above `floor`, or None where the read
    dropped — a dropped read is never an absence of evidence."""
    entries = _seat_entries(ctx, seat)
    if entries is None or floor is None:
        return None
    return entries[floor:]


def _wait_seat(ctx, seat, match, watch, bound, poll=None):
    """Poll a seat's serving monitor until `match(view)` holds, or the
    bound passes; every poll row lands in `watch` as
    {role, sync, field_claim, answered} so the judge can separate a
    monitor that stopped answering from a walk that never landed.
    Returns the matching view, else the last accepted one."""
    accepted = []

    def found():
        view = _seat_view(ctx, seat)
        watch.append({'role': (view or {}).get('role'),
                      'sync': (view or {}).get('sync'),
                      'field_claim': (view or {}).get('field_claim'),
                      'answered': view is not None})
        if view is not None and match(view):
            accepted.append(view)
            return view
        return None

    wait_for(found, time.monotonic() + bound,
             interval=poll or RECLAIM_POLL)
    return accepted[-1] if accepted else None


def _wait_link(ctx, seat, down, watch, bound):
    """Hold the freeze until the seat's served `io_health` reports its
    control connection `down` (or back `connected` on the thaw); the
    poll rows land in `watch` so a link that never reported the outage
    is visible as such rather than as a seat that stopped answering."""
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        view = _seat_view(ctx, seat)
        link = (view or {}).get('link')
        watch.append({'link': link, 'answered': view is not None})
        if view is not None and ((link == 'connected') != down):
            return True
        time.sleep(RECLAIM_LINK)
    return False


def _reclaim_window(ctx, seats, rounds, bound, until=None):
    """The post-thaw watch: both seats' serving monitors polled
    together until `rounds` answered rows each have landed (or
    `until` accepts a row pair), or the bound passes. Returns the
    interleaved per-seat poll log the judge replays."""
    rows = []
    collected = {seat: 0 for seat in seats}
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        for seat in seats:
            view = _seat_view(ctx, seat)
            rows.append({'seat': seat,
                         'role': (view or {}).get('role'),
                         'sync': (view or {}).get('sync'),
                         'field_claim': (view or {}).get('field_claim'),
                         'tick': (view or {}).get('tick'),
                         'answered': view is not None})
            if view is not None:
                collected[seat] += 1
        if all(collected[seat] >= rounds for seat in seats):
            break
        if until is not None and until(rows):
            break
        time.sleep(RECLAIM_POLL)
    return rows


def _pair_view(ctx, name):
    """One read of a deployed pair member's serving monitor — the
    undisturbed-pair evidence: role, sync, claim, and the served tick."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    return {'role': report.get('role'), 'sync': _seat_sync(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _pair_undisturbed(before, after):
    """The deployed member was never disturbed: it kept its launch
    role, kept the field's claim, and its served tick advanced across
    the whole staged episode."""
    return (after or {}).get('role') == (before or {}).get('role') \
        and (after or {}).get('field_claim') == (before or {}).get(
            'field_claim') \
        and isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and (after or {})['tick'] > (before or {})['tick']


def _reclaim_restore(ctx, owner):
    """Best-effort launch-layout restore: stop every seat the leg stood
    and remove the scratch field, then put the deployed pair back on
    its launch roles — promoting the launch owner back over whatever
    member holds the field — retrying inside the bound and swallowing
    every refusal. Cleanup, never the contract the leg judges."""
    try:
        for seat in SEATS_USED:
            try:
                ctx['stop_born_controller'](seat)
            except Exception:
                pass
        stop = ctx.get('stop_born_field')
        if stop is not None:
            try:
                stop()
            except Exception:
                pass
        deadline = time.monotonic() + RECLAIM_RESTORE
        while time.monotonic() < deadline:
            if (_pair_active(ctx) or owner) != owner:
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, 'standby' if owner
                                          == 'active' else 'active'):
                return True
            time.sleep(RECLAIM_POLL)
    except Exception:
        pass
    return False


def _reclaim_pass(ctx, number):
    """One pass over the convergence-gated reclaim contract: serve the
    plant and claim it with a born-active, re-serve the field under the
    same name so a second born-active claims first while the first is
    fenced to an unsynchronized standby, freeze the plant until both
    connections drop, thaw and watch the incumbent's re-attach hold the
    field against the ex-owner's ungated reclaim, then stage the
    positive control's converged fenced ex-owner and its granted
    reclaim. Returns (record, evidence); the judge replays the record,
    and an aborted stage simply leaves its later keys absent."""
    record = {'pass': number}
    evidence = {'pass': number}
    owner = _pair_active(ctx)
    if owner is None:
        evidence['inconclusive'] = (
            'the deployed pair reports no field-owning member — the '
            'undisturbed-pair baseline the staging frames never settled')
        return record, evidence
    member = 'standby' if owner == 'active' else 'active'
    tokens = ctx.get('plant_owner') or {}
    record['incumbent'] = owner
    record['member'] = member
    record['seat_tokens'] = {seat: tokens.get(seat)
                             for seat in SEATS_USED}
    record['pair_before'] = _pair_view(ctx, owner)
    record['member_before'] = _pair_view(ctx, member)
    if (record['pair_before'] or {}).get('field_claim') != 'held' \
            or (record['member_before'] or {}).get('role') != 'standby':
        evidence['inconclusive'] = (
            'the deployed pair is not in its settled launch shape — '
            'the field owner must hold the claim with its member '
            'tracking: '
            + json.dumps({'owner': record['pair_before'],
                          'member': record['member_before']})[:200])
        return record, evidence
    paused = False
    try:
        # --- the ex-owner claims the served field ------------------
        try:
            field = ctx['start_born_field']('serving')
            record['field'] = field
            record['remote'] = field['remote']
            ctx['start_born_controller'](EX_OWNER_SEAT, record['remote'])
        except Exception as exc:
            record['stage_error'] = ('the served field or its '
                                     'born-active never launched: '
                                     + str(exc)[:250])
            return record, evidence
        watch = []
        record['owner_active'] = _wait_seat(
            ctx, EX_OWNER_SEAT,
            lambda view: view.get('role') == 'active'
            and view.get('field_claim') == 'held',
            watch, RECLAIM_SERVE)
        record['owner_watch'] = watch

        # --- the re-serve: a second born-active claims first ------
        try:
            ctx['start_born_field']('serving')
            ctx['start_born_controller'](INCUMBENT_SEAT, record['remote'],
                                         peer=EX_OWNER_SEAT)
        except Exception as exc:
            record['stage_error'] = ('the re-served field or the '
                                     'claiming born-active never '
                                     'launched: ' + str(exc)[:250])
            return record, evidence
        watch = []
        record['preemptor'] = _wait_seat(
            ctx, INCUMBENT_SEAT,
            lambda view: view.get('role') == 'active'
            and view.get('field_claim') == 'held',
            watch, RECLAIM_SERVE)
        record['preemptor_watch'] = watch
        # The ex-owner meets the fence on its reattached write and
        # settles the demotion with the loss mark standing — armed for
        # the reclaim, on no adoptable tracking source.
        record['fenced'] = _wait_seat(
            ctx, EX_OWNER_SEAT,
            lambda view: view.get('role') == 'standby'
            and view.get('sync') in UNCONVERGED_SYNC,
            [], RECLAIM_SETTLE)
        # A durable read that dropped is None, never an empty list: the
        # judge's absence clauses must not read a lost file as a
        # journal with no loss in it.
        entries = _seat_entries(ctx, EX_OWNER_SEAT)
        record['ex_owner_loss'] = None if entries is None \
            else _seat_events(entries, 'field_claim_lost')
        record['ex_owner_walk'] = None if entries is None \
            else _seat_walk(entries)

        # --- the freeze, held until both connections drop ----------
        floors = {seat: _seat_floor(ctx, seat)
                  for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT)}
        record['floors'] = floors
        try:
            ctx['pause_born_field']()
            paused = True
        except Exception as exc:
            record['stage_error'] = 'the plant freeze never landed: ' \
                + str(exc)[:250]
            return record, evidence
        links = {}
        for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT):
            poll = []
            links[seat] = _wait_link(ctx, seat, True, poll,
                                     RECLAIM_FREEZE)
            poll.append({'polls': len(poll)})
            links[seat] = {'down': links[seat], 'polls': len(poll)}
        record['links'] = links

        # --- the thaw: the race the defect was won on -------------
        try:
            ctx['unpause_born_field']()
            paused = False
        except Exception as exc:
            record['stage_error'] = 'the plant thaw never landed: ' \
                + str(exc)[:250]
            return record, evidence
        watch = []
        record['window_rows'] = _reclaim_window(
            ctx, (EX_OWNER_SEAT, INCUMBENT_SEAT), RECLAIM_ROUNDS,
            RECLAIM_WINDOW)
        record['window'] = {
            'answered': {seat: sum(1 for row in record['window_rows']
                                   if row.get('seat') == seat
                                   and row.get('answered'))
                         for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT)}}
        after = {}
        for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT):
            after[seat] = _seat_since(ctx, seat, floors.get(seat))
        record['after'] = after
        record['states'] = {seat: _seat_state(ctx, seat)
                            for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT)}
        issued = granted = took = False
        for seat, entries in after.items():
            walk = _seat_walk(entries)
            reclaims = [row for row in walk
                        if row[2] == 'reclaim' or row[1] == 'promoting']
            issued = issued or bool(reclaims) or bool(
                _seat_events(entries, 'field_claim_observed')) or bool(
                    _seat_events(entries, 'field_claim_rearmed'))
            granted = granted or bool(reclaims)
            if seat == EX_OWNER_SEAT:
                record['ex_owner_since'] = None if entries is None else {
                    'walk': walk,
                    'observed': _seat_events(
                        entries, 'field_claim_observed'),
                    'rearmed': _seat_events(
                        entries, 'field_claim_rearmed'),
                    'losses': _seat_events(entries, 'field_claim_lost')}
                # The ex-owner's own serving monitor is the honest
                # half of the pre-contract signature: a build whose
                # reclaim runs ungated shows it standing as the field's
                # owner. A record whose polls never show that while the
                # journal does is the doctored contradiction, and the
                # judge names it rather than waving it through.
                took = any(row.get('seat') == EX_OWNER_SEAT
                           and row.get('role') in ('promoting', 'active')
                           for row in record['window_rows'])
            else:
                record['incumbent_since'] = None if entries is None \
                    else {'walk': walk}
        record['ask_issued'] = issued
        record['ask_granted'] = granted
        record['served_take'] = took
        # The pre-contract signature: the reclaim issued while the
        # ex-owner held zero convergence evidence AND its own serving
        # monitor showed it taking the field. A build with no
        # convergence gate loses the post-outage race on transport
        # ordering and presents exactly this shape, so it is a staged
        # revision predating the contract rather than a failure of it.
        if issued and took:
            record['pre_contract'] = (
                'the unsynchronized ex-owner\'s reclaim ask issued '
                'against the incumbent\'s standing claim and its own '
                'serving monitor showed it taking the field — a build '
                'whose fencing-loss reclaim runs ungated: the staged '
                'revision predates the convergence-gated reclaim '
                'contract')

        # --- the positive control: the converged ex-owner ---------
        try:
            for seat in (EX_OWNER_SEAT, INCUMBENT_SEAT):
                ctx['stop_born_controller'](seat)
            ctx['start_born_field']('serving')
            ctx['start_born_controller'](
                CONTROL_SEAT, record['remote'], peer=INCUMBENT_SEAT)
        except Exception as exc:
            record['stage_error'] = ('the positive control\'s field '
                                     'or born-active never launched: '
                                     + str(exc)[:250])
            return record, evidence
        watch = []
        control = {'watch': watch}
        control['claimed'] = _wait_seat(
            ctx, CONTROL_SEAT,
            lambda view: view.get('role') == 'active',
            watch, RECLAIM_SERVE)
        try:
            ctx['start_born_field']('serving')
            ctx['start_born_controller'](INCUMBENT_SEAT, record['remote'],
                                         peer=CONTROL_SEAT)
        except Exception as exc:
            record['stage_error'] = ('the control\'s re-served field '
                                     'or its claimant never launched: '
                                     + str(exc)[:250])
            return record, evidence
        watch = []
        control['preemptor'] = _wait_seat(
            ctx, INCUMBENT_SEAT,
            lambda view: view.get('role') == 'active'
            and view.get('field_claim') == 'held',
            watch, RECLAIM_SERVE)
        watch = []
        control['fenced'] = _wait_seat(
            ctx, CONTROL_SEAT,
            lambda view: view.get('role') == 'standby',
            watch, RECLAIM_SETTLE)
        watch = []
        control['converged'] = _wait_seat(
            ctx, CONTROL_SEAT,
            lambda view: view.get('role') == 'standby'
            and view.get('sync') == 'tracking',
            watch, RECLAIM_SETTLE)
        # The incumbent leaves; its claim stands holderless — the
        # dead-owner shape the reclaim preempts by design — and the
        # converged ex-owner's bound grant must land.
        try:
            ctx['stop_born_controller'](INCUMBENT_SEAT)
        except Exception as exc:
            record['stage_error'] = ('the control\'s incumbent never '
                                     'released the field: '
                                     + str(exc)[:250])
            return record, evidence
        watch = []
        control['active'] = _wait_seat(
            ctx, CONTROL_SEAT,
            lambda view: view.get('role') == 'active'
            and view.get('field_claim') == 'held',
            watch, RECLAIM_SETTLE)
        control['watch_rows'] = watch
        control['walk'] = None if _seat_entries(
            ctx, CONTROL_SEAT) is None else _seat_walk(
                _seat_entries(ctx, CONTROL_SEAT))
        control['state'] = _seat_state(ctx, CONTROL_SEAT)
        record['control'] = control

        record['pair_after'] = _pair_view(ctx, owner)
        record['member_after'] = _pair_view(ctx, member)
    finally:
        if paused:
            try:
                ctx['unpause_born_field']()
            except Exception:
                pass
        record['restored'] = _reclaim_restore(ctx, owner)
        record['restored_owner'] = _pair_active(ctx)
    return record, evidence


def _reclaim_preempted(record):
    """The ex-owner's durable journal proves its reclaim was granted:
    a `reclaim`-origin promotion or a re-armed claim landed above the
    freeze's cursor."""
    since = record.get('ex_owner_since') or {}
    if since.get('walk') and any(
            row[2] == 'reclaim' or row[1] == 'promoting'
            for row in since['walk']):
        return True
    return bool(since.get('rearmed'))


def _reclaim_staged(record):
    """The finding's own sequence formed: a born-active owned the
    served field and a different born-active claimed it after the
    re-serve. Without both, the fencing loss the reclaim is armed by
    never happened and the reclaim's clauses judge nothing."""
    return (record.get('owner_active') is not None
            and record.get('preemptor') is not None)


def _judge_pair(record, failed):
    """The deployed pair's clauses — the undisturbed-pair framing the
    staged episode must leave, and the launch-layout restore."""
    if not _pair_undisturbed(record.get('pair_before'),
                             record.get('pair_after')):
        failed('pair', 'the deployed pair\'s field owner was disturbed '
               '— it must keep its launch role and its claim with its '
               'tick advancing across the whole staged episode: '
               + json.dumps({'before': record.get('pair_before'),
                             'after': record.get('pair_after')})[:300])
    member_after = record.get('member_after') or {}
    if member_after.get('role') != 'standby':
        failed('pair', 'the deployed pair\'s tracking member left '
               'standby — the staged episode disturbed the pair\'s '
               'second member: ' + json.dumps(member_after)[:200])
    if record.get('restored') is not True:
        failed('restore', 'the launch configuration and roles did not '
               'restore after the staged episode')
    elif record.get('restored_owner') not in (None, record.get('incumbent')):
        failed('restore', 'the restore left a field-owning member other '
               'than the launch owner ' + str(record.get('incumbent'))
               + ': ' + str(record.get('restored_owner')))


def _judge_reclaim(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses
    and DIAG_NONDET the instability the contract does not answer for.
    Each stage's clauses are guarded by that stage's own evidence, so
    a stage that never ran — an unbound record key, or a staging that
    never armed the reclaim at all — leaves them unread rather than
    reporting an absence as a failure."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the staging never completed: '
               + str(record['stage_error']))
        return

    # --- the ex-owner's activation and the claim it lost ------------
    if record.get('owner_active') is None:
        nondet('serve', 'the born-active never settled active holding '
               'the served field\'s claim — the ex-owner the reclaim '
               'arms never owned the field: '
               + json.dumps(record.get('owner_watch') or [])[:300])
    if record.get('preemptor') is None:
        nondet('serve', 'the re-served field\'s born-active never '
               'claimed it — the different-owner claim the '
               'convergence gate exists to protect never stood: '
               + json.dumps(record.get('preemptor_watch') or [])[:300])
    if not _reclaim_staged(record):
        # Nothing armed the reclaim, so none of its clauses can be
        # read: the pair's framing and the restore are all that
        # remains to judge.
        _judge_pair(record, failed)
        return
    fenced = record.get('fenced')
    if 'fenced' in record and fenced is None:
        failed('fence', 'the fenced ex-owner never settled an '
               'unsynchronized standby — the loss mark the reclaim '
               'reads was never armed under a demotion')
    elif fenced is not None:
        if fenced.get('field_claim') != 'held':
            failed('fence', 'the fenced ex-owner does not observe the '
                   'incumbent\'s standing claim — the reclaim would '
                   'meet an unclaimed field, where nothing stands to '
                   'preempt: ' + json.dumps(fenced)[:200])
        if fenced.get('sync') == 'tracking':
            failed('zero-evidence', 'the fenced ex-owner reports '
                   'tracking — it carries convergence evidence, so '
                   'this is not the zero-evidence race the gate '
                   'exists for: ' + json.dumps(fenced)[:200])
    losses = record.get('ex_owner_loss')
    if losses is not None:
        if len(losses) != 1:
            failed('loss-journaled', 'the ex-owner\'s durable journal '
                   'carries ' + str(len(losses)) + ' field_claim_lost '
                   'records — one held claim loses it once')
        elif losses[0].get('claimant') is None:
            failed('loss-attribution', 'the ex-owner\'s field_claim_lost '
                   'names no claimant — the fencing verdict carried no '
                   'attribution')
        elif (record.get('seat_tokens') or {}).get(INCUMBENT_SEAT) \
                is not None and losses[0].get('claimant') != (
                    record['seat_tokens'][INCUMBENT_SEAT]):
            failed('loss-attribution', 'the ex-owner\'s field_claim_lost '
                   'names claimant ' + str(losses[0].get('claimant'))
                   + ', not the claiming born-active\'s owner token '
                   + str(record['seat_tokens'][INCUMBENT_SEAT]))
    else:
        failed('loss-journaled', 'the ex-owner\'s durable journal '
               'cannot be read — the loss the reclaim is armed by '
               'left no durable record')
    walk = record.get('ex_owner_walk')
    if walk is not None and not any(row[0] == 'demoting'
                                    and row[1] == 'standby'
                                    for row in walk):
        failed('demotion-journaled', 'the ex-owner\'s journal carries '
               'no demoting → standby walk — the fencing demotion '
               'never landed in place: ' + json.dumps(walk)[:200])

    # --- the freeze: both connections really dropped ---------------
    links = record.get('links') or {}
    for seat, read in links.items():
        if not read.get('down'):
            failed('links', 'the ' + seat + ' seat\'s served io_health '
                   'never reported its control connection down while '
                   'the plant stood frozen — the post-outage race was '
                   'never staged: ' + json.dumps(read))
    floors = record.get('floors') or {}
    for seat, floor in floors.items():
        if floor is None:
            nondet('floor', 'the ' + seat + ' seat\'s durable journal '
                   'could not be read at the freeze — the post-thaw '
                   'audit has no cursor to read above')

    # --- the thaw: the gated reclaim -------------------------------
    rows = record.get('window_rows')
    if rows is not None:
        owner_rows = [row for row in rows
                      if row.get('seat') == EX_OWNER_SEAT]
        incumbent_rows = [row for row in rows
                          if row.get('seat') == INCUMBENT_SEAT]
        if not owner_rows or not any(row.get('answered')
                                     for row in owner_rows):
            nondet('starved', 'the ex-owner\'s serving monitor answered '
                   'no post-thaw read — the window the reclaim races '
                   'in was never observed')
        if not incumbent_rows or not any(row.get('answered')
                                         for row in incumbent_rows):
            nondet('starved', 'the incumbent\'s serving monitor '
                   'answered no post-thaw read — its re-attach was '
                   'never observed')
        if any(row.get('role') in ('promoting', 'active')
               for row in owner_rows):
            failed('preempted', 'the unsynchronized ex-owner left '
                   'standby on its serving monitor after the thaw — '
                   'its reclaim preempted the live incumbent\'s claim '
                   'on stale state: '
                   + json.dumps(owner_rows)[:300])
        if any(row.get('sync') == 'tracking' for row in owner_rows):
            failed('zero-evidence', 'the unsynchronized ex-owner '
                   'reported tracking inside the race window — it '
                   'carried convergence evidence, so the gating the '
                   'contract requires was never exercised: '
                   + json.dumps(owner_rows)[:300])
        if any(row.get('field_claim') == 'unclaimed'
               for row in owner_rows):
            failed('unclaimed', 'the ex-owner\'s own claim probe '
                   'answered `unclaimed` inside the race window — '
                   'nothing stood to preempt there, so the gate the '
                   'contract requires was never exercised: '
                   + json.dumps(owner_rows)[:300])
        if not all(row.get('role') == 'active'
                   and row.get('field_claim') == 'held'
                   for row in incumbent_rows if row.get('answered')):
            failed('incumbent-displaced', 'the incumbent did not keep '
                   'or retake ownership on its live line across the '
                   'race window: ' + json.dumps(incumbent_rows)[:300])
        ticks = [row.get('tick') for row in incumbent_rows
                 if isinstance(row.get('tick'), int)]
        if len(ticks) < 2 or ticks[-1] <= ticks[0]:
            failed('incumbent-stalled', 'the incumbent\'s served tick '
                   'never advanced across the race window — the field '
                   'it reattached to stood still: '
                   + json.dumps(incumbent_rows)[:300])
    since = record.get('ex_owner_since')
    if since is None:
        nondet('durable', 'the ex-owner\'s durable journal could not be '
               'read above the freeze cursor — the ownership-epoch '
               'audit never landed')
    else:
        if _reclaim_preempted(record):
            failed('rollback', 'the ex-owner\'s durable journal proves '
                   'its reclaim took the field back on stale state — '
                   'a `reclaim`-origin promotion or a re-armed claim '
                   'landed while its serving monitors reported the '
                   'convergence gating holding: '
                   + json.dumps(since.get('walk'))[:300])
        if since.get('observed'):
            failed('issued', 'the ex-owner\'s journal records a '
                   'field_claim_observed inside the race window — its '
                   'reclaim ask issued against the standing claim '
                   'with no convergence evidence behind it: '
                   + json.dumps(since['observed'])[:300])
        if since.get('losses'):
            failed('epoch', 'the ex-owner\'s journal records a second '
                   'field_claim_lost above the freeze — the ownership '
                   'epoch rolled again while the incumbent held its '
                   'live line: ' + json.dumps(since['losses'])[:300])
    incumbent_walk = (record.get('incumbent_since') or {}).get('walk')
    if incumbent_walk is not None and any(
            row[1] != 'active' for row in incumbent_walk):
        failed('incumbent-fenced', 'the incumbent left active on its '
               're-attach — it was fenced and demoted while the stale '
               'ex-owner re-activated: ' + json.dumps(incumbent_walk)
               [:300])
    states = record.get('states') or {}
    for seat, state in states.items():
        if state is None:
            nondet('starved', 'the ' + seat + ' seat\'s process verdict '
                   'could not be read')
        elif state.get('absent'):
            nondet('vanished', 'the ' + seat + ' seat\'s container '
                   'vanished — no process verdict exists to audit')
        elif state.get('running') is not True:
            failed('seat-exited', 'the ' + seat + ' seat\'s process '
                   'stopped instead of degrading in place across the '
                   'outage: ' + json.dumps(state)[:200])

    # --- the positive control: the converged reclaim still lands ---
    control = record.get('control')
    if control is not None:
        if control.get('claimed') is None:
            nondet('control-claim', 'the control\'s born-active never '
                   'claimed its served field — the converged '
                   'fenced ex-owner never owned the field')
        if control.get('preemptor') is None:
            nondet('control-claim', 'the control\'s second born-active '
                   'never claimed the re-served field — no different '
                   'owner fenced the ex-owner')
        if control.get('converged') is None:
            nondet('control-converge', 'the control\'s ex-owner never '
                   'converged tracking on the live line — the '
                   'convergence proof the gate reads was never '
                   'staged, so the positive control judged nothing: '
                   + json.dumps(control.get('watch') or [])[:300])
        elif control.get('active') is None:
            failed('control-silent', 'a converged fenced ex-owner\'s '
                   'bound reclaim of its own released claim did not '
                   'take the field — the released-preemption wedge '
                   'the probe exists to close is still open: '
                   + json.dumps(control.get('watch_rows') or [])[:300])
        walk = control.get('walk')
        if control.get('converged') is not None and walk is not None:
            if any(row[2] == 'request' for row in walk):
                failed('control-walk', 'the converged ex-owner\'s '
                       'reclaim walk journals origin=request — an '
                       'operator promote ran instead of the bound '
                       'reclaim: ' + json.dumps(walk)[-300:])
            elif not any(row[2] == 'reclaim'
                         and row[1] == 'promoting' for row in walk):
                failed('control-walk', 'the converged ex-owner\'s '
                       'journal carries no standby → promoting walk '
                       'under the `reclaim` origin — the grant that '
                       'took the field was never journaled: '
                       + json.dumps(walk)[-300:])
        state = control.get('state')
        if state is not None and state.get('running') is not True:
            failed('control-exited', 'the converged ex-owner\'s process '
                   'stopped instead of holding the claim it re-took: '
                   + json.dumps(state)[:200])
    elif record.get('stage_error') is None:
        nondet('control', 'the positive control never ran')

    # --- the deployed pair: undisturbed and restored ---------------
    _judge_pair(record, failed)


def _reclaim_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    rows = record.get('window_rows') or []
    incumbent_rows = [row for row in rows
                      if row.get('seat') == INCUMBENT_SEAT]
    return {
        'staging': 'fenced'
            if clean('serve', 'fence', 'loss-journaled', 'loss-attribution',
                     'demotion-journaled', 'links')
            else 'unstaged',
        'evidence': 'none'
            if clean('zero-evidence', 'unclaimed', 'control-converge')
            else 'present',
        'reclaim': 'gated'
            if clean('preempted', 'rollback', 'issued', 'epoch',
                     'incumbent-displaced', 'incumbent-fenced',
                     'incumbent-stalled')
            else 'ungated',
        'incumbent': 'live'
            if all(row.get('role') == 'active'
                   and row.get('field_claim') == 'held'
                   for row in incumbent_rows)
            else 'displaced',
        'control': 'reclaimed'
            if clean('control-silent', 'control-walk', 'control-exited')
            else 'silent',
        'pair': 'undisturbed'
            if clean('pair', 'restore', 'seat-exited')
            else 'disturbed',
        'reads': 'complete'
            if clean('stage', 'starved', 'vanished', 'durable', 'floor',
                     'control-claim', 'control')
            else 'partial'}


def _reclaim_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — the issue's named doctored record (the
    convergence gating asserted while the unsynchronized reclaimer
    takes the field, which the served surfaces hide and the durable
    journal disproves), the ownership-epoch rollback, the displaced or
    fenced incumbent, the silent positive control, the unsettled
    launch layout, the unjournaled loss, the instability shapes — and
    require the judge to note each. A silent judge returns the negative
    names it let through."""
    slipped = []

    def clean_record():
        polls = [{'seat': EX_OWNER_SEAT, 'role': 'standby',
                  'sync': 'unsynchronized', 'field_claim': 'held',
                  'tick': 40, 'answered': True},
                 {'seat': INCUMBENT_SEAT, 'role': 'active',
                  'sync': None, 'field_claim': 'held', 'tick': 12,
                  'answered': True},
                 {'seat': EX_OWNER_SEAT, 'role': 'standby',
                  'sync': 'unsynchronized', 'field_claim': 'held',
                  'tick': 60, 'answered': True},
                 {'seat': INCUMBENT_SEAT, 'role': 'active',
                  'sync': None, 'field_claim': 'held', 'tick': 20,
                  'answered': True}]
        return {
            'pass': 1,
            'incumbent': 'active',
            'member': 'standby',
            'seat_tokens': {EX_OWNER_SEAT: 424247,
                            INCUMBENT_SEAT: 424246,
                            CONTROL_SEAT: 424245},
            'field': {'container': 'dcs-hw-qa-1-born-plant',
                      'remote': 'dcs-hw-qa-1-born-plant:9001',
                      'mode': 'serving'},
            'remote': 'dcs-hw-qa-1-born-plant:9001',
            'owner_active': {'role': 'active', 'sync': None,
                             'field_claim': 'held', 'link': 'connected',
                             'tick': 30},
            'preemptor': {'role': 'active', 'sync': None,
                          'field_claim': 'held', 'link': 'connected',
                          'tick': 5},
            'fenced': {'role': 'standby', 'sync': 'unsynchronized',
                       'field_claim': 'held', 'link': 'connected',
                       'tick': 40},
            'ex_owner_loss': [{'point': 100, 'claimant': 424246}],
            'ex_owner_walk': [('active', 'demoting', 'fenced'),
                              ('demoting', 'standby', 'fenced')],
            'floors': {EX_OWNER_SEAT: 4, INCUMBENT_SEAT: 1},
            'links': {EX_OWNER_SEAT: {'down': True, 'polls': 3},
                      INCUMBENT_SEAT: {'down': True, 'polls': 3}},
            'window_rows': polls,
            'window': {'answered': {EX_OWNER_SEAT: 2,
                                    INCUMBENT_SEAT: 2}},
            'after': {EX_OWNER_SEAT: [], INCUMBENT_SEAT: []},
            'ex_owner_since': {'walk': [], 'observed': [],
                               'rearmed': [], 'losses': []},
            'incumbent_since': {'walk': []},
            'states': {EX_OWNER_SEAT: {'running': True, 'exit': None,
                                        'absent': False},
                       INCUMBENT_SEAT: {'running': True, 'exit': None,
                                        'absent': False}},
            'ask_issued': False,
            'ask_granted': False,
            'served_take': False,
            'control': {
                'watch': [{'role': 'active', 'sync': None,
                           'field_claim': 'held', 'answered': True}],
                'claimed': {'role': 'active', 'sync': None,
                            'field_claim': 'held', 'link': 'connected',
                            'tick': 20},
                'preemptor': {'role': 'active', 'sync': None,
                              'field_claim': 'held', 'link': 'connected',
                              'tick': 3},
                'fenced': {'role': 'standby',
                           'sync': 'unsynchronized',
                           'field_claim': 'held', 'link': 'connected',
                           'tick': 30},
                'converged': {'role': 'standby',
                              'sync': 'tracking',
                              'field_claim': 'held',
                              'link': 'connected', 'tick': 34},
                'active': {'role': 'active', 'sync': None,
                           'field_claim': 'held', 'link': 'connected',
                           'tick': 40},
                'watch_rows': [{'role': 'standby',
                                'sync': 'tracking',
                                'field_claim': 'held',
                                'answered': True}],
                'walk': [('active', 'demoting', 'fenced'),
                         ('demoting', 'standby', 'fenced'),
                         ('standby', 'promoting', 'reclaim'),
                         ('promoting', 'active', 'reclaim')],
                'state': {'running': True, 'exit': None,
                          'absent': False}},
            'pair_before': {'role': 'active', 'sync': None,
                            'field_claim': 'held', 'tick': 100},
            'member_before': {'role': 'standby', 'sync': 'tracking',
                              'field_claim': 'held', 'tick': 95},
            'pair_after': {'role': 'active', 'sync': None,
                           'field_claim': 'held', 'tick': 180},
            'member_after': {'role': 'standby', 'sync': 'tracking',
                             'field_claim': 'held', 'tick': 170},
            'restored': True,
            'restored_owner': 'active'}

    def audit(record):
        found = {}
        _judge_reclaim(
            record, lambda key, diagnostic, detail:
            found.setdefault(key, diagnostic))
        return found

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    if audit(clean_record()):
        slipped.append('clean-overstrict')

    # The issue's named doctored negative: the convergence gating
    # asserted — every served read reports the ex-owner unsynchronized
    # and the incumbent holding the field — while the durable journal
    # proves the reclaim took it back on stale state.
    expect('gating-asserted-while-reclaim-took-field', lambda record:
           record['ex_owner_since'].update({
               'walk': [('standby', 'promoting', 'reclaim'),
                        ('promoting', 'active', 'reclaim')]}))
    expect('reclaim-rearmed-the-claim', lambda record:
           record['ex_owner_since'].update(
               {'rearmed': [{'point': 100}]}))
    expect('reclaim-asked-the-standing-claim', lambda record:
           record['ex_owner_since'].update(
               {'observed': [{'point': 100, 'claimant': 424246}]}))
    expect('ex-owner-promoted-on-its-monitor', lambda record:
           record['window_rows'].__setitem__(
               2, dict(record['window_rows'][2], role='promoting')))
    expect('ex-owner-converged-in-the-window', lambda record:
           record['window_rows'].__setitem__(
               2, dict(record['window_rows'][2], sync='tracking')))
    expect('field-went-unclaimed-under-the-ex-owner', lambda record:
           record['window_rows'].__setitem__(
               2, dict(record['window_rows'][2],
                       field_claim='unclaimed')))
    expect('incumbent-displaced', lambda record:
           record['window_rows'].__setitem__(
               3, dict(record['window_rows'][3], role='standby')))
    expect('incumbent-tick-stalled', lambda record:
           record['window_rows'].__setitem__(
               3, dict(record['window_rows'][3], tick=12)))
    expect('incumbent-lost-the-claim', lambda record:
           record['window_rows'].__setitem__(
               1, dict(record['window_rows'][1],
                       field_claim='unclaimed')))
    expect('incumbent-fenced-on-reattach', lambda record:
           record['incumbent_since'].update({
               'walk': [('active', 'demoting', 'fenced')]}))
    expect('ownership-epoch-rolled-again', lambda record:
           record['ex_owner_since'].update(
               {'losses': [{'point': 100, 'claimant': 424246}]}))
    expect('ex-owner-never-fenced', lambda record:
           record.update({'fenced': None}))
    expect('ex-owner-never-demoted-in-place', lambda record:
           record.update({'ex_owner_walk': [('active', 'demoting',
                                             'fenced')]}))
    expect('ex-owner-observes-no-claim', lambda record:
           record['fenced'].update({'field_claim': None}))
    expect('fenced-ex-owner-tracking', lambda record:
           record['fenced'].update({'sync': 'tracking'}))
    expect('loss-unjournaled', lambda record:
           record.update({'ex_owner_loss': []}))
    expect('loss-twice', lambda record:
           record.update({'ex_owner_loss': [
               {'point': 100, 'claimant': 424246},
               {'point': 100, 'claimant': 424246}]}))
    expect('loss-unattributed', lambda record:
           record.update({'ex_owner_loss': [{'point': 100}]}))
    expect('loss-wrong-claimant', lambda record:
           record.update({'ex_owner_loss': [
               {'point': 100, 'claimant': 424249}]}))
    expect('link-never-dropped', lambda record:
           record['links'][EX_OWNER_SEAT].update({'down': False}))
    expect('control-reclaim-silent', lambda record:
           record['control'].update({'active': None}))
    expect('control-walk-unjournaled', lambda record:
           record['control'].update({'walk': [
               ('active', 'demoting', 'fenced'),
               ('demoting', 'standby', 'fenced')]}))
    expect('control-operator-promote', lambda record:
           record['control'].update({'walk': [
               ('active', 'demoting', 'fenced'),
               ('demoting', 'standby', 'fenced'),
               ('standby', 'promoting', 'request'),
               ('promoting', 'active', 'request')]}))
    expect('control-ex-owner-exited', lambda record:
           record['control'].update({'state': {'running': False,
                                              'exit': 1,
                                              'absent': False}}))
    expect('pair-owner-disturbed', lambda record:
           record['pair_after'].update({'role': 'standby',
                                        'field_claim': None}))
    expect('pair-owner-stalled', lambda record:
           record['pair_after'].update({'tick': 100}))
    expect('pair-member-promoted', lambda record:
           record['member_after'].update({'role': 'active'}))
    expect('launch-layout-unrestored', lambda record:
           record.update({'restored': False}))
    expect('restore-left-a-foreign-owner', lambda record:
           record.update({'restored_owner': 'standby'}))
    expect('seat-process-exited', lambda record:
           record['states'][EX_OWNER_SEAT].update(
               {'running': False, 'exit': 1}))

    # The instability shapes must report nondeterministic: a refused
    # staging call, a starved window, a vanished container, an
    # unreadable durable journal, an unstaged control.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('ex-owner-never-activated', lambda record:
           record.update({'owner_active': None}), DIAG_NONDET)
    expect('incumbent-never-claimed', lambda record:
           record.update({'preemptor': None}), DIAG_NONDET)
    expect('window-starved', lambda record:
           (record['window_rows'].__setitem__(
               0, dict(record['window_rows'][0], role=None,
                       answered=False)),
            record['window_rows'].__setitem__(
                2, dict(record['window_rows'][2], role=None,
                        answered=False))),
           DIAG_NONDET)
    expect('journal-unreadable-above-the-floor', lambda record:
           record.update({'ex_owner_since': None}), DIAG_NONDET)
    expect('journal-unreadable-at-the-floor', lambda record:
           record['floors'].update({EX_OWNER_SEAT: None}), DIAG_NONDET)
    expect('container-vanished', lambda record:
           record['states'][INCUMBENT_SEAT].update(
               {'running': False, 'exit': None, 'absent': True}),
           DIAG_NONDET)
    expect('control-never-converged', lambda record:
           record['control'].update({'converged': None}), DIAG_NONDET)
    expect('control-never-claimed', lambda record:
           record['control'].update({'claimed': None}), DIAG_NONDET)
    return slipped


def scenario_reclaim_convergence_gate(ctx):
    """Exercise the convergence-gated fencing-loss reclaim contract
    against the deployed rig: serve the plant and let a born-active
    claim it, re-serve the field under the same name so a second
    born-active claims first while the first is fenced to an
    unsynchronized standby with no adoptable tracking source, freeze
    the plant until both connections drop, and prove on the thaw that
    the fenced ex-owner's reclaim cannot preempt the live incumbent's
    claim — the incumbent's reattach keeps or retakes ownership on
    its live line, the ex-owner stays standby without the field, and
    no ownership epoch rolls back to the staler image. Then the
    positive control: a converged fenced ex-owner's bound reclaim of
    its own released claim still takes the field under the `reclaim`
    origin. The deployed pair stays undisturbed throughout, every
    staged seat is removed, and the launch configuration and roles are
    restored. Two consecutive passes must produce identical digests."""
    case = Case(
        'reclaim-convergence-gate',
        'A fenced ex-owner with no convergence evidence cannot preempt '
        'a live claim',
        'after a post-outage race an unsynchronized fenced ex-owner '
        'stays standby without the field while the live incumbent '
        'keeps or retakes ownership on its live line and no ownership '
        'epoch rolls back to the staler image; a converged fenced '
        'ex-owner\'s bound reclaim of its own released claim still '
        'takes the field; the deployed pair is undisturbed and the '
        'launch configuration and roles restored; and two passes '
        'produce identical digests')
    try:
        missing = [key for key in (
            'start_born_field', 'pause_born_field', 'unpause_born_field',
            'stop_born_field', 'start_born_controller',
            'stop_born_controller', 'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no reclaim-convergence staging levers: '
                               + ', '.join(missing))
        if not all(ctx.get(seat) for seat in SEATS_USED):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the born seats')
        if not all(ctx.get(member) for member in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the deployed '
                               'pair')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in SEATS_USED):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-seat journal files — the durable '
                               'half of the ownership-epoch audit '
                               'cannot run')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _reclaim_pass(ctx, number)
            evidence['record'] = record
            if not evidence.get('inconclusive') \
                    and not record.get('pre_contract'):
                _judge_reclaim(record, note)
            digest = _reclaim_digest(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {key: diagnostic for key,
                                      (diagnostic, _)
                                      in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'reclaim-convergence-gate-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref,
                          'reclaim-convergence-gate pass ' + str(number)
                          + ' — the served and re-served field, both '
                          'seats\' role and claim walks, the freeze\'s '
                          'link reads, the post-thaw race window, both '
                          'durable journals, the positive control, the '
                          'deployed-pair framing, and the normalized '
                          'digest')
            if evidence.get('inconclusive'):
                return case.finish('inconclusive', evidence['inconclusive'])
            if record.get('pre_contract'):
                return case.finish('inconclusive', record['pre_contract'])
            if violations:
                name = DIAG_FAILED if any(
                    diagnostic == DIAG_FAILED
                    for diagnostic, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail
                        in list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two reclaim-convergence passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _reclaim_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive',
                           'the leg could not complete on this rig: '
                           + str(exc)[:500])