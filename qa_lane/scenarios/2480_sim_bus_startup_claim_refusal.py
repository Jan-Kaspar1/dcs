"""The sim_bus_startup_claim_refusal acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seats 'revised'/'foreign'/
# 'driven' and the lane's own sim-bus device server — it needs the
# ownerless-backoff leg's seat released, runs after the sim-cyclic
# fencing-loss leg that shares the driven/foreign seats and the same
# device server, and must be done before the revision legs take the
# born seats over.
RUNS_AFTER = frozenset({'scenario_ownerless_remote_backoff',
                        'scenario_sim_cyclic_fencing_loss_demote'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The born-active startup-claim refusal over a sim-bus field — the
# per-revision lane evidence for the contract #1350's fix establishes
# (WW-FND-002's failover continuity and the one-active role
# invariant): a controller starting without `--peer` over a sim-bus
# field whose write-ownership claim is held by a live controller
# refuses startup by name instead of preempting the incumbent. The
# defect the contract answers: the born-active's startup claim had no
# conditional grant on the register protocol, so it fell back to the
# *unconditional* `claim_writer` and seized the field out from under a
# live incumbent — the incumbent's next write fenced, it demoted itself
# in place, the second run went active unpaired, and the line's
# tracking standby was orphaned onto a source that had lost the field.
# On the fixed revision the launch's conditional
# `claim_writer_unless_held` ask meets the standing claim, answers
# `fenced`, and the pairless run settles to the recorded disposition:
# a nonzero exit naming the live-holder refusal and the `--standby`
# remedy. The refusal is the sim-tcp verdict verbatim — 'a live peer
# holds the field's write-ownership claim' — and it arrives on the
# register protocol because a bus claim dies with its last holder's
# connection, so a standing claim there always has a live incumbent to
# name.
#
# The leg stages the finding's own reproduction on the lane's shipped
# `dcs-sim-bus-device` protocol server: `start_sim_bus_device` binds
# the run config's bus model with the device's `__BUS_ADDR__`
# placeholder resolved to the device container's bridge name and hands
# back both the bridge address and the staged document, which the leg
# mounts into each controller it launches through the born launcher's
# `document` seam — a document-addressed launch with no `--remote` at
# all, so both ends of the register protocol read one declaration. The
# pass then runs the three seats in order:
#
# - the incumbent (`revised`): a born-active declaring no pair over
#   the unclaimed bus field. Its conditional grant lands and it scans
#   as the field's writer — the served proof the claim is live is the
#   `probe_writer` hook the register protocol's claim-introspection
#   surface installs, so `/role` answers `field_claim: held`.
# - the refused launch (`foreign`): the same born-active shape over the
#   same model with no `--peer`. It must exit nonzero inside the
#   bound carrying the live-holder verdict, the missing-pair wording,
#   and the `--standby` remedy; its own journal must record the
#   refusal beside a `field_claim_observed` naming the incumbent's
#   owner token.
# - the positive control (`driven`): the same launch shape declaring
#   `--standby <incumbent>` instead. It must converge `tracking` behind
#   the incumbent, own nothing, keep its command gate closed, and
#   journal no tracking-source refusal — the launch shape's rejoin
#   half, and the arm of the arm's failure the defect orphaned.
#
# Across the refused launch the incumbent must be untouched: still the
# field's writer, still `active`, still scanning, its journal carrying
# no `field_claim_lost` and no fenced `active → standby` demotion. The
# deployed pair never enters the staging — the leg's device server is
# a different container on a different claim token, and the rig's
# launched roles are asserted undisturbed before, after, and once the
# leg's own seats are gone. Each pass ends with the rig swept: the
# three seats and the device server removed, and the sweep itself is
# audited back over the rig — a born seat or a device server that
# outlived it is a claim the legs behind this one would inherit. Two
# consecutive passes must produce identical outcome digests.
#
# Named diagnostics: sim-bus-claim-refusal-failed tags the contract
# clauses — the named verdict's launch landing the claim or going
# active anyway, a refusal that exits zero or exits unnamed, an exit
# that never names the missing pair or the `--standby` remedy, an
# incumbent the refusal disturbs (the claim flipped, a fenced
# demotion journaled, the scan stopped), a refusal that never
# journaled its own verdict or attributed the standing claim to the
# incumbent's token, a `--standby` control that orphans — converging
# then journaling a tracking-source refusal — or that promotes onto
# the field behind a live incumbent or admits a command behind its
# closed gate — while
# sim-bus-claim-refusal-nondeterministic tags the instability the
# contract does not answer for: refused staging calls, a starved
# watch or monitor, an unread verdict, a control that never converges,
# a control whose SignalIndex never named the point its closed gate
# had to refuse, a moved or wedged deployed pair, a rig the sweep did
# not restore, or two passes whose digests diverge. A rig that cannot
# stage the leg — no device server, no born-seat staging levers, no
# pinned claim token for the incumbent seat, no settled deployed pair
# — and a staged revision predating the contract, whose second
# born-active claims the bus field and goes active with no named
# verdict, report inconclusive. The unchecked-diagnostic self-check
# replays the judge over planted negatives and reports
# sim-bus-claim-refusal-unchecked for any that slip through.

BUS_SETTLE = 45    # bound on each launch settling and the refusal wait
BUS_POLL = 0.4     # cadence polling a seat's verdict mid-stage
BUS_WATCH = 4      # served reads the control's converged window spans
INCUMBENT_SEAT = 'revised'   # the first controller — takes the claim
REFUSED_SEAT = 'foreign'     # the born-active declaring no --peer
CONTROL_SEAT = 'driven'      # the --standby control launch
CLAUSE = 'sim-bus-claim-refusal-failed'
NONDET = 'sim-bus-claim-refusal-nondeterministic'
UNCHECKED = 'sim-bus-claim-refusal-unchecked'

# The refusal's three named texts — the sim-tcp verdict the sim-bus
# path must reproduce exactly, the missing-pair sentence that says why
# this launch has no rejoin, and the remedy flag an operator types.
LIVE_HOLDER = "a live peer holds the field's write-ownership claim"
UNDECLARED = 'no --peer was declared'
REMEDY = '--standby ADDRESS'

# The runner's own words for a rig that stages no device server: an
# absent capability the leg declines on, never a mid-run flake.
UNSTAGED_DEVICE = 'stages no sim-bus device server'


def _bus_unstaged(exc):
    """Whether a device-server staging failure names an unstaged run
    config — the absent capability the leg reports inconclusive on,
    as distinct from a docker failure mid-stage."""
    return UNSTAGED_DEVICE in str(exc)


def _bus_role(ctx, base):
    """One normalized role read — role, scan tick, tracking posture —
    or None when the endpoint dropped it."""
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _bus_view(ctx, seat):
    """One monitor read of a born seat: role, the field's claim
    posture, and the served scan tick — the evidence tuple the
    settle and watch loops collect."""
    base = ctx.get(seat)
    if not base:
        return None
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _bus_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever — the refusal's exit evidence."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _bus_journal(ctx, seat):
    """The seat's durable journal records — its runner-owned
    --journal-file. An unreadable or absent file reads as no records:
    the judge distinguishes a silent journal from an unread one
    through the specific clause it looks for."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return []
    try:
        return _journal_entries(path)
    except Exception:
        return []


def _bus_events(ctx, seat, kind):
    """The `kind` event bodies the seat's durable journal carries."""
    out = []
    for item in _bus_journal(ctx, seat):
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _bus_fenced(ctx, seat):
    """Whether the seat's journal records the field taking its claim
    away — the `field_claim_lost` a preempted owner's first fenced
    write lands, or the fenced `active → standby` demotion that
    follows it. Either is the claim flipping."""
    if _bus_events(ctx, seat, 'field_claim_lost'):
        return True
    return any(event.get('from') == 'active'
               and event.get('to') == 'standby'
               and event.get('origin') == 'fenced'
               for event in _bus_events(ctx, seat, 'role_changed'))


def _bus_claimants(ctx, seat):
    """The owner tokens the seat's journaled `field_claim_observed`
    records attribute the standing claim to."""
    return sorted({event.get('claimant')
                   for event in _bus_events(ctx, seat,
                                            'field_claim_observed')
                   if event.get('claimant') is not None})


def _bus_launch(ctx, seat, model, peer=None, standby=None):
    """Launch one controller on the leg's own bus field: the staged
    document mounted in place of the run's own model through the born
    launcher's `document` seam, and no `--remote`, so the attachment
    dials the device server's bridge address straight out of the
    model. `peer`/`standby` carry the launch shape's tracking wiring;
    neither is the reproduction's second launch."""
    try:
        launched = ctx['start_born_controller'](
            seat, None, peer=peer, standby=standby, document=model)
    except Exception as exc:
        return {'seat': seat, 'stage_error': str(exc)[:300]}
    return {'seat': seat, 'launch': launched}


def _bus_incumbent(ctx, model):
    """The live claim holder: a born-active declaring no pair over the
    unclaimed bus field. Its conditional startup grant must land and
    it must scan as the field's writer — the served claim posture the
    whole pass's evidence is read against."""
    incumbent = _bus_launch(ctx, INCUMBENT_SEAT, model)
    if incumbent.get('stage_error') is not None:
        return incumbent
    incumbent['granted'] = wait_for(
        lambda: (lambda view: view if view is not None
                 and view.get('role') == 'active'
                 and view.get('field_claim') == 'held'
                 else None)(_bus_view(ctx, INCUMBENT_SEAT)),
        time.monotonic() + BUS_SETTLE, interval=BUS_POLL)
    incumbent['before'] = _bus_view(ctx, INCUMBENT_SEAT)
    return incumbent


def _bus_refusal(ctx, model):
    """The reproduction's second launch and its verdict: 'claimed' —
    a role read showed it owning the field or promoting into it, the
    grant landed; 'refused' — the process exited nonzero inside the
    bound; 'clean-exit' — it exited zero, which is no disposition the
    born-active contract records; 'standing' — a served standby
    surface that neither took the claim nor ended the launch;
    'unread' — nothing verdict-shaped arrived inside the bound. The
    three refusal texts are read off the exit's own log tail.

    A launch's own `field_claim: held` is never the landing signal: a
    run that never took the claim still *observes* the incumbent's
    through the register protocol's `probe_writer`, exactly as the
    tracking standby does. Only the role says who owns the field."""
    verdict = _bus_launch(ctx, REFUSED_SEAT, model)
    if verdict.get('stage_error') is not None:
        return verdict
    views = []
    state = None
    deadline = time.monotonic() + BUS_SETTLE
    while time.monotonic() < deadline:
        seen = _bus_state(ctx, REFUSED_SEAT)
        if seen is not None:
            state = seen
            if seen.get('exit') is not None and not seen.get('running'):
                break
        view = _bus_view(ctx, REFUSED_SEAT)
        if view is not None:
            views.append(view)
            if view.get('role') in ('active', 'promoting'):
                break
        time.sleep(BUS_POLL)
    if state is None:
        state = _bus_state(ctx, REFUSED_SEAT)
    state = state or {}
    exit_code = state.get('exit')
    logs = str(state.get('logs') or '')
    landed = [view for view in views
              if view.get('role') in ('active', 'promoting')]
    if landed:
        disposition = 'claimed'
    elif not state.get('running') \
            and isinstance(exit_code, int) \
            and not isinstance(exit_code, bool):
        disposition = 'refused' if exit_code else 'clean-exit'
    elif views and all(view.get('role') == 'standby'
                       for view in views):
        disposition = 'standing'
    else:
        disposition = 'unread'
    verdict.update({
        'disposition': disposition, 'running': state.get('running'),
        'exit': exit_code, 'views': views,
        'named': LIVE_HOLDER in logs,
        'undeclared': UNDECLARED in logs,
        'remedy': REMEDY in logs,
        'logs_tail': logs[-400:]})
    return verdict


def _bus_control_point(ctx, seat):
    """A writable bool in-point the control's closed gate must refuse
    — out of the seat's own served SignalIndex, so the probe follows
    whatever bus model the run config declares rather than a pinned
    id. None when the index cannot be read."""
    base = ctx.get(seat)
    if not base:
        return None
    try:
        _, signals = http_json('GET', base + '/signals')
    except Exception:
        return None
    entry = _writable_bool_point(signals or {})
    return entry.get('point') if entry else None


def _bus_command_refused(ctx, seat, point):
    """POST the control's declared writable command: a tracking
    standby must receipt it `not_active` — the closed gate's served
    evidence that the control owns nothing. The rejection reason's
    kind, or None on any other verdict."""
    if point is None:
        return None
    try:
        _, receipt = http_json('POST', ctx[seat] + '/command', {
            'command': {'write_value': {'point': point,
                                        'kind': 'bool',
                                        'value': {'bool': True}}},
            'actor': 'qa-sim-bus-claim'})
    except Exception:
        return None
    reason = ((receipt or {}).get('outcome', {}).get('rejected', {})
              .get('reason') or {})
    return next(iter(reason), None)


def _bus_control(ctx, model):
    """The positive control — the same launch shape declaring
    `--standby <incumbent>` instead of no pair at all. The declared
    tracking member must converge on the incumbent, hold no claim of
    its own, keep its command gate closed, and journal no
    tracking-source refusal: the arm the defect orphaned."""
    control = _bus_launch(ctx, CONTROL_SEAT, model,
                          standby=INCUMBENT_SEAT)
    if control.get('stage_error') is not None:
        return control
    control['converged'] = wait_for(
        lambda: (lambda report: report if report is not None
                 and report.get('role') == 'standby'
                 and 'tracking' in (report.get('sync') or {})
                 else None)(_try_role(ctx, ctx[CONTROL_SEAT])),
        time.monotonic() + BUS_SETTLE, interval=BUS_POLL)
    point = _bus_control_point(ctx, CONTROL_SEAT)
    views = []
    for _ in range(BUS_WATCH):
        view = _bus_view(ctx, CONTROL_SEAT)
        if view is not None:
            views.append(view)
        time.sleep(BUS_POLL)
    control['watch'] = views
    control['command_point'] = point
    control['refused_command'] = _bus_command_refused(ctx, CONTROL_SEAT,
                                                      point)
    control['source_refusals'] = _bus_events(ctx, CONTROL_SEAT,
                                             'tracking_source_refused')
    return control


def _bus_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's bus staging, the peer still
    a tracking standby — in every framing the record carries, the
    `before` and `after` of the staging and the `final` one the leg
    reads once its own seats are gone."""
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
        if not isinstance(seen_tick, int) or isinstance(seen_tick, bool) \
                or (tick is not None and seen_tick <= tick):
            return False
        tick = seen_tick
    return tick is not None


def _bus_incumbent_held(incumbent):
    """The incumbent was never disturbed by the refusal: still the
    field's writer, still active, its scan advancing across the
    window, and its journal carrying no fenced demotion. Only
    meaningful where the incumbent ever reported itself the writer —
    a run that never claimed the field has nothing to lose."""
    if not incumbent.get('granted'):
        return True
    before = incumbent.get('before') or {}
    after = incumbent.get('after') or {}
    if after.get('role') != 'active' or after.get('field_claim') != 'held':
        return False
    if incumbent.get('fenced') is True:
        return False
    tick0, tick1 = before.get('tick'), after.get('tick')
    return isinstance(tick0, int) and not isinstance(tick0, bool) \
        and isinstance(tick1, int) and not isinstance(tick1, bool) \
        and tick1 > tick0


def _bus_control_holds(control):
    """The --standby control stayed what it declared across the watch:
    a tracking standby the whole way, never promoting onto the field
    behind a live incumbent. Its served `field_claim` reads `held`
    throughout — that is the live incumbent's claim observed, not one
    of its own — so the ownership evidence is its role, read apart from
    the closed command gate the judge audits on its own."""
    watch = control.get('watch') or []
    if not watch:
        return False
    return all(view.get('role') == 'standby' for view in watch)


def _bus_verdict_audit(verdict):
    """The refusal's durable-evidence summary, for a violation detail —
    the recorded journal flag and the attributed claimants."""
    return {'journaled': verdict.get('journaled'),
            'claimants': verdict.get('claimants')}


def _judge_bus(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: CLAUSE tags the contract clauses and
    NONDET the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, CLAUSE, detail)

    def nondet(key, detail):
        note(key, NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the sim-bus device server never staged: '
               + str(record['stage_error']))
        return
    incumbent = record.get('incumbent') or {}
    verdict = record.get('refused') or {}
    control = record.get('control') or {}

    if incumbent.get('stage_error') is not None:
        nondet('incumbent-stage', 'the incumbent launch never staged: '
               + str(incumbent['stage_error']))
    elif not incumbent.get('granted'):
        nondet('incumbent-claim', 'the first launch never reported '
               'itself the field\'s writer inside the bound — the live '
               'claim holder the refusal must meet never existed: '
               + json.dumps(incumbent.get('before'))[:200])

    if verdict.get('stage_error') is not None:
        nondet('refused-stage', 'the second launch never staged: '
               + str(verdict['stage_error']))
    elif not incumbent.get('granted'):
        # The refusal clauses mean nothing without a live holder to
        # refuse: with no incumbent claiming the field, the second
        # launch taking it is correct, not the recorded defect.
        pass
    else:
        disposition = verdict.get('disposition')
        if disposition == 'claimed':
            failed('refused-claimed', 'the second born-active claimed '
                   'the bus field and went active — the named refusal '
                   'must land before the claim: '
                   + json.dumps(verdict.get('views'))[:300])
        elif disposition == 'clean-exit':
            failed('refused-exit', 'the refused launch exited zero — a '
                   'claim refused with no declared pair has no '
                   'recorded disposition but a nonzero exit: '
                   + (verdict.get('logs_tail') or '')[-200:])
        elif disposition == 'standing':
            nondet('refused-standing', 'the second launch stood a '
                   'claimless standby surface without ever claiming or '
                   'exiting — the register protocol answers its '
                   'conditional grant, so no verdict arrived: '
                   + json.dumps(verdict.get('views'))[:300])
        elif disposition != 'refused':
            nondet('refused-unread', 'the second launch produced no '
                   'readable verdict inside the bound: '
                   + json.dumps(verdict)[:300])
        else:
            if verdict.get('named') is not True:
                failed('refused-unnamed', 'the refusal exited without '
                       'naming the live holder\'s claim — the verdict '
                       'is the only record a stranded run leaves: '
                       + (verdict.get('logs_tail') or '')[-200:])
            if verdict.get('undeclared') is not True:
                failed('refused-undeclared', 'the refusal did not name '
                       'the missing --peer that leaves it no pair to '
                       'rejoin: '
                       + (verdict.get('logs_tail') or '')[-200:])
            if verdict.get('remedy') is not True:
                failed('refused-remedy', 'the refusal did not name the '
                       '--standby relaunch an operator can act on: '
                       + (verdict.get('logs_tail') or '')[-200:])
            if verdict.get('read') is not True:
                # The durable half is unreadable: the refusal's own
                # verdict cannot be audited, which is instability, not
                # a missing record.
                nondet('refused-journal', 'the refused launch\'s '
                       'journal file could not be read — the durable '
                       'attribution cannot be audited')
            else:
                if verdict.get('journaled') is not True:
                    failed('refused-unjournaled', 'the refusal '
                           'journaled no startup_claim_refused verdict '
                           'of its own — the durable half of the '
                           'born-active contract: '
                           + json.dumps(_bus_verdict_audit(verdict)))
                claimants = verdict.get('claimants')
                token = record.get('incumbent_token')
                if token is not None and claimants != [token]:
                    failed('refused-claimant', 'the refusal\'s observed '
                           'claimant is not the incumbent\'s owner '
                           'token ' + str(token) + ': '
                           + json.dumps(claimants))

    if incumbent.get('granted') and not _bus_incumbent_held(incumbent):
        failed('incumbent', 'the live incumbent was disturbed by the '
               'refused launch — the conditional grant must never '
               'preempt it: ' + json.dumps(incumbent.get('after'))[:200])

    if control.get('stage_error') is not None:
        nondet('control-stage', 'the --standby control launch never '
               'staged: ' + str(control['stage_error']))
    else:
        watch = control.get('watch') or []
        refusals = control.get('source_refusals') or []
        if refusals:
            failed('control-orphan-refusal', 'the --standby control '
                   'journaled a tracking-source refusal — the orphan '
                   'the preemption produced, on the arm that declares '
                   'its source: '
                   + json.dumps(refusals)[:300])
        if not watch:
            nondet('control-watch', 'the control\'s monitor never '
                   'answered the watch reads')
        elif not control.get('converged'):
            nondet('control-launch', 'the --standby control never '
                   'converged tracking on the live incumbent: '
                   + json.dumps(watch)[:300])
        else:
            if not _bus_control_holds(control):
                failed('control-promoted', 'the control left the '
                       'tracking posture it declared — a role it '
                       'cannot hold behind a live incumbent: '
                       + json.dumps(watch)[:300])
            elif control.get('command_point') is None:
                # The gate's evidence is unreadable, not crossed: a
                # SignalIndex that never named a writable bool
                # in-point leaves the leg nothing to submit, which is
                # the rig's staging surface rather than the control's
                # declared posture.
                nondet('control-signals', 'the control\'s SignalIndex '
                       'never named a writable bool in-point — the '
                       'closed gate\'s served evidence could not be '
                       'read')
            elif control.get('refused_command') != 'not_active':
                failed('control-admitted-a-command', 'the control '
                       'crossed its closed command gate — a tracking '
                       'standby must receipt the write not_active: '
                       + json.dumps({'point': control.get(
                           'command_point'),
                           'verdict': control.get(
                               'refused_command')})[:300])

    if not _bus_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the bus staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])

    rig = record.get('rig') or {}
    seats = rig.get('seats') or {}
    standing = sorted(seat for seat, absent in seats.items()
                      if absent is not True)
    if standing or rig.get('device_error'):
        nondet('rig-not-restored', 'the leg left the rig\'s claim state '
               'standing — a seat or the device server outlived the '
               'sweep the legs behind this one inherit: '
               + json.dumps({'standing': standing,
                             'device': rig.get('device_error')},
                            sort_keys=True)[:300])


def _bus_digest(record, violations):
    """The pass's normalized verdict set — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    verdict = record.get('refused') or {}
    disposition = verdict.get('disposition')
    refused = 'exits-named' \
        if disposition == 'refused' \
        and verdict.get('named') is True \
        and verdict.get('undeclared') is True \
        and verdict.get('remedy') is True \
        and clean('refused-claimed', 'refused-exit', 'refused-unnamed',
                  'refused-undeclared', 'refused-remedy',
                  'refused-unjournaled', 'refused-claimant') \
        else 'defect'
    return {
        'incumbent': 'holds-and-scans'
            if clean('incumbent', 'incumbent-stage', 'incumbent-claim')
            else 'defect',
        'refused': refused,
        'audit': 'attributed' if clean('refused-unjournaled',
                                       'refused-claimant',
                                       'refused-journal')
                  else 'defect',
        'control': 'tracks-nothing-owned'
            if clean('control-stage', 'control-watch',
                     'control-launch', 'control-promoted',
                     'control-signals', 'control-admitted-a-command',
                     'control-orphan-refusal')
            else 'defect',
        'pair': 'held' if clean('pair-disturbed') else 'disturbed',
        'rig': 'restored' if clean('rig-not-restored') else 'dirty'}


def _bus_pass(ctx, number, launch):
    """One pass over the reproduction: frame the deployed pair's
    roles, stage the device server, launch the first controller and
    read its claim, stage the second launch and read its verdict and
    the incumbent's fate across the refusal, run the --standby
    control, then frame the pair again. The `final` framing and the
    rig's restoration read belong to the caller: they only mean
    anything once the sweep has run."""
    record = {'pass': number, 'launch': dict(launch), 'roles': {},
              'field': {}, 'incumbent': {}, 'refused': {},
              'control': {}, 'rig': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _bus_role(ctx, ctx[name]) for name in (owner, peer)}
    try:
        field = ctx['start_sim_bus_device']()
    except Exception as exc:
        record['stage_error'] = str(exc)[:300]
        record['unstaged'] = _bus_unstaged(exc)
    else:
        record['field'] = {'address': field.get('address'),
                           'device': field.get('device'),
                           'model': field.get('model')}
        model = field.get('model')
        record['incumbent'] = _bus_incumbent(ctx, model)
        record['refused'] = _bus_refusal(ctx, model)
        verdict = record['refused']
        if verdict.get('stage_error') is None:
            path = (ctx.get('journal_files') or {}).get(REFUSED_SEAT)
            verdict['read'] = bool(path) and Path(path).is_file()
            verdict['journaled'] = bool(_bus_events(
                ctx, REFUSED_SEAT, 'startup_claim_refused'))
            verdict['claimants'] = _bus_claimants(ctx, REFUSED_SEAT)
        incumbent = record['incumbent']
        if incumbent.get('stage_error') is None:
            incumbent['fenced'] = _bus_fenced(ctx, INCUMBENT_SEAT)
            incumbent['after'] = _bus_view(ctx, INCUMBENT_SEAT)
        # The narrow inconclusive gate: only a launch that took the
        # claim from a live holder, with no named verdict, is the
        # pre-contract shape — the register protocol's missing
        # conditional grant, the unconditional claim seizing the field.
        # A named verdict that claims the field anyway is the
        # contract's own breach and stays the judge's to report, and a
        # claim taken over a field nobody held is not a preemption at
        # all — that shape is the judge's instability class.
        if verdict.get('disposition') == 'claimed' \
                and verdict.get('named') is not True \
                and incumbent.get('granted'):
            record['inconclusive'] = (
                'the staged revision predates the sim-bus born-active '
                'claim-refusal contract — the second born-active '
                'claimed the bus field and went active, precluding the '
                'live incumbent')
        else:
            record['control'] = _bus_control(ctx, model)
        # The staged document every seat mounted: the audit's proof
        # that both ends of the register protocol read one
        # declaration — the server's register map and the attachment's
        # dialed address come out of this one file.
        record['field']['mounted'] = sorted({
            str((part.get('launch') or {}).get('model'))
            for part in (record['incumbent'], record['refused'],
                         record['control'])
            if (part.get('launch') or {}).get('model') is not None})
    record['roles']['after'] = {
        name: _bus_role(ctx, ctx[name]) for name in (owner, peer)}
    return record


def _bus_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — the refusal asserted while the second launch claims
    the field and goes active, a refusal that exits zero or unnamed,
    an exit naming neither the missing pair nor the remedy, a refusal
    that never journaled or attributed the standing claim, an
    incumbent the refusal disturbs, a --standby control that orphans
    or promotes onto the field or admits a command, a moved pair — and
    report every one let slip."""
    refusal = ("error: startup: a live peer holds the field's "
               "write-ownership claim — a controller restarting into a "
               "pair cannot prove its resumed state is current with "
               "the incumbent's and must not preempt it; relaunch with "
               "--standby ADDRESS to rejoin as the incumbent's tracking "
               "standby instead — no --peer was declared, so there is "
               "no pair to rejoin: relaunch with --standby ADDRESS to "
               "track the field's live owner")

    def clean_record():
        return {'pass': 1,
                'launch': {'owner': 'active', 'peer': 'standby'},
                'field': {'address': 'dcs-hw-qa-1-bus:9005',
                          'device': 1,
                          'model': '/run/sim-bus/model.json'},
                'incumbent': {'seat': INCUMBENT_SEAT,
                              'granted': {'role': 'active',
                                          'field_claim': 'held',
                                          'tick': 5},
                              'before': {'role': 'active',
                                         'field_claim': 'held',
                                         'tick': 10},
                              'after': {'role': 'active',
                                        'field_claim': 'held',
                                        'tick': 40},
                              'fenced': False},
                'refused': {'seat': REFUSED_SEAT,
                            'disposition': 'refused',
                            'running': False, 'exit': 1,
                            'views': [], 'logs_tail': refusal,
                            'named': True, 'undeclared': True,
                            'remedy': True, 'journaled': True,
                            'claimants': [424243], 'read': True},
                'control': {'seat': CONTROL_SEAT,
                            'watch': [{'role': 'standby',
                                       'field_claim': 'held',
                                       'tick': 40},
                                      {'role': 'standby',
                                       'field_claim': 'held',
                                       'tick': 41}],
                            'converged': {'role': 'standby'},
                            'command_point': 51,
                            'refused_command': 'not_active',
                            'source_refusals': []},
                'incumbent_token': 424243,
                'rig': {'seats': {INCUMBENT_SEAT: True,
                                  REFUSED_SEAT: True,
                                  CONTROL_SEAT: True},
                        'device_error': None},
                'roles': {
                    'before': {
                        'active': {'role': 'active', 'tick': 10,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 10,
                                    'tracking': True}},
                    'after': {
                        'active': {'role': 'active', 'tick': 12,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 12,
                                    'tracking': True}},
                    'final': {
                        'active': {'role': 'active', 'tick': 14,
                                   'tracking': False},
                        'standby': {'role': 'standby', 'tick': 14,
                                    'tracking': True}}}}

    def audit(record):
        found = {}
        _judge_bus(record,
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

    # The doctored negative the issue names — the refusal asserted
    # while the second launch claims the field and goes active.
    expect('refusal-asserted-but-active', lambda r:
           r['refused'].update(disposition='claimed', running=True,
                               exit=None, named=False,
                               logs_tail='', undeclared=False,
                               remedy=False, views=[
                                   {'role': 'active',
                                    'field_claim': 'held', 'tick': 5}]))
    expect('refusal-asserted-but-named-and-claimed', lambda r:
           r['refused'].update(disposition='claimed', running=True,
                               exit=None,
                               views=[{'role': 'active',
                                       'field_claim': 'held',
                                       'tick': 5}]))
    expect('refusal-exits-zero', lambda r:
           r['refused'].update(disposition='clean-exit', exit=0))
    expect('refusal-exits-unnamed', lambda r:
           r['refused'].update(named=False))
    expect('refusal-names-no-missing-pair', lambda r:
           r['refused'].update(undeclared=False))
    expect('refusal-names-no-remedy', lambda r:
           r['refused'].update(remedy=False))
    expect('refusal-unjournaled', lambda r:
           r['refused'].update(journaled=False))
    expect('refusal-wrong-claimant', lambda r:
           r['refused'].update(claimants=[424244]))
    expect('incumbent-lost-its-claim', lambda r:
           r['incumbent']['after'].update(field_claim=None))
    expect('incumbent-demoted-fenced', lambda r:
           r['incumbent'].update(fenced=True))
    expect('incumbent-scan-wedged', lambda r:
           r['incumbent']['after'].update(tick=10))
    expect('control-orphaned', lambda r:
           r['control'].update(source_refusals=[
               {'source': 'dcs-hw-qa-1-c:8082', 'detail': 'no_tracking'}]))
    expect('control-promoted-onto-the-field', lambda r:
           r['control']['watch'][1].update(role='active'))
    expect('control-admitted-a-command', lambda r:
           r['control'].update(refused_command=None))
    # The instability shapes must report nondeterministic: refused
    # staging calls, a starved watch or monitor, an unread verdict, a
    # control that never converges, a gate whose evidence never
    # arrived, a moved pair, a rig left standing.
    expect('control-signals-unreadable', lambda r:
           r['control'].update(command_point=None), NONDET)
    expect('device-stage-refused', lambda r:
           r.update(stage_error='docker run failed'), NONDET)
    expect('incumbent-launch-refused', lambda r:
           r['incumbent'].update(stage_error='docker run failed'),
           NONDET)
    expect('incumbent-never-claimed', lambda r:
           r['incumbent'].update(granted=None), NONDET)
    expect('refused-launch-refused', lambda r:
           r['refused'].update(stage_error='docker run failed'),
           NONDET)
    expect('refusal-unread', lambda r:
           r['refused'].update(disposition='unread', running=True,
                               exit=None), NONDET)
    expect('refusal-pending-unverdicted', lambda r:
           r['refused'].update(disposition='pending', running=True,
                               exit=None, views=[
                                   {'role': 'standby',
                                    'field_claim': None, 'tick': 1}]),
           NONDET)
    expect('refusal-journal-unread', lambda r:
           r['refused'].update(read=False), NONDET)
    expect('control-launch-refused', lambda r:
           r['control'].update(stage_error='docker run failed'),
           NONDET)
    expect('control-watch-starved', lambda r:
           r['control'].update(watch=[]), NONDET)
    expect('control-never-converged', lambda r:
           r['control'].update(converged=None), NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'),
           NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['after']['standby'].update(tracking=False),
           NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=10), NONDET)
    expect('pair-final-owner-moved', lambda r:
           r['roles']['final']['active'].update(role='standby'), NONDET)
    expect('pair-final-scan-wedged', lambda r:
           r['roles']['final']['active'].update(tick=12), NONDET)
    expect('rig-left-standing', lambda r:
           r['rig']['seats'].update({CONTROL_SEAT: False}), NONDET)
    expect('rig-presence-unreadable', lambda r:
           r['rig']['seats'].update({INCUMBENT_SEAT: None}), NONDET)
    expect('device-server-still-serving', lambda r:
           r['rig'].update(device_error='device is busy'), NONDET)
    return slipped


def _bus_teardown(ctx):
    """Best-effort teardown: the leg's three seats and the device
    server — a clean pass leaves nothing standing and the rig's claim
    state free, and an aborted pass gets the same sweep so the legs
    behind this one see free seats and a free device-server name.
    Returns the device server's own removal error, or None when it
    came down: the one half of the claim state the read-only state
    lever cannot confirm, since the device container is not a born
    seat."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        for seat in (INCUMBENT_SEAT, REFUSED_SEAT, CONTROL_SEAT):
            try:
                lever(seat)
            except Exception:
                pass
    try:
        if ctx.get('stop_sim_bus_device') is None:
            return None
        ctx['stop_sim_bus_device']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _bus_rig_state(ctx, device_error):
    """The rig's claim state after the sweep: every born seat's own
    presence read back through the read-only state lever — a seat
    still standing is a claim the legs behind this one would inherit —
    beside the device server's removal error. `absent` None is a read
    the lever could not answer, never a seat proven gone."""
    return {'seats': {seat: (_bus_state(ctx, seat) or {}).get('absent')
                      for seat in (INCUMBENT_SEAT, REFUSED_SEAT,
                                   CONTROL_SEAT)},
            'device_error': device_error}


def scenario_sim_bus_startup_claim_refusal(ctx):
    """Exercise the born-active startup-claim refusal over a sim-bus
    field: launch a first controller onto the lane's shipped device
    server and assert it takes the field's write-ownership claim, then
    stage a second born-active declaring the same model with no
    `--peer` and assert it exits nonzero inside the bound carrying the
    live-holder refusal — the incumbent's claim and role undisturbed,
    no fenced demotion journaled on it, no claim_owner flip — while a
    `--standby` launch in the same shape converges behind it and
    journals no tracking-source refusal. The rig's claim state and
    launch roles are restored afterward, and two consecutive passes
    produce identical outcome digests."""
    case = Case(
        'sim-bus-startup-claim-refusal',
        'A born-active launch over a live-held sim-bus claim refuses '
        'to start',
        'over a rig-mounted sim-bus field whose write-ownership claim '
        'a live controller holds, a born-active launch declaring no '
        '--peer exits nonzero naming the live-holder refusal and the '
        '--standby remedy — never claiming the field and going active '
        '— the incumbent keeps its claim, its active role, and a '
        'moving scan with no fenced demotion journaled, a --standby '
        'launch in the same shape converges tracking while owning '
        'nothing and journaling no tracking-source refusal, the rig\'s '
        'claim state and launch roles are restored — audited back over '
        'the swept rig, a seat or device server that outlived the '
        'sweep fails the leg — and two passes produce identical '
        'digests')
    try:
        missing = [key for key in ('start_sim_bus_device',
                                   'stop_sim_bus_device',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no sim-bus staging levers: '
                               + ', '.join(missing))
        if not all(ctx.get(seat)
                   for seat in (INCUMBENT_SEAT, REFUSED_SEAT,
                                CONTROL_SEAT)):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the leg\'s '
                               'born seats')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in
                   (INCUMBENT_SEAT, REFUSED_SEAT, CONTROL_SEAT)):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-seat journal files — the durable '
                               'half of the audit cannot run')
        tokens = ctx.get('plant_owner') or {}
        if not tokens.get(INCUMBENT_SEAT):
            return case.finish('inconclusive', 'the run context records '
                               'no pinned --owner-token for the '
                               'incumbent seat — the refusal\'s '
                               'attributed claimant cannot be audited '
                               'against a live holder')
        deadline = time.monotonic() + BUS_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=BUS_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')
                       if ctx.get(name)}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is '
                    'unreachable — monitor endpoints '
                    + str(ctx.get('active')) + ' and '
                    + str(ctx.get('standby')))
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=BUS_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'posture the leg proves undisturbed '
                               'was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            try:
                record = _bus_pass(ctx, number, launch)
            finally:
                # Each pass ends with the rig swept — the three seats
                # and the device server removed so the next pass and
                # the legs behind this one start on a free claim state
                # and free seats, and the launch roles they find are
                # the ones the rig started from.
                device_error = _bus_teardown(ctx)
            # The restoration audit, read back over the swept rig: the
            # seats' own presence and the pair's launch roles once the
            # leg's claim is gone.
            record['rig'] = _bus_rig_state(ctx, device_error)
            record['roles']['final'] = {
                name: _bus_role(ctx, ctx[name]) for name in (owner, peer)}
            if record.get('unstaged'):
                return case.finish(
                    'inconclusive', record['stage_error'])
            record['incumbent_token'] = tokens.get(INCUMBENT_SEAT)
            # A pass the leg declines to judge gets no digest: the
            # determinism contract is over audited passes, and a
            # pseudo-digest on a pre-contract record would read as a
            # verdict the leg refused to reach.
            if record.get('inconclusive'):
                digest = None
            else:
                _judge_bus(record, note)
                digest = _bus_digest(record, violations)
                record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'sim-bus-startup-claim-refusal-pass-'
                + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'sim-bus-startup-claim-refusal pass '
                          + str(number) + ' — the staged device server, '
                          'the incumbent\'s claim posture, the refused '
                          'launch\'s exit and journaled attribution, '
                          'the --standby control\'s convergence, the '
                          'deployed pair\'s before/after/final framing, '
                          'the swept rig\'s restoration read, and the '
                          'normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations:
                name = CLAUSE if any(
                    diagnostic == CLAUSE
                    for diagnostic, _ in violations.values()) \
                    else NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two sim-bus claim-refusal passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _bus_self_check()
        if slipped:
            return case.finish('failed', UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))