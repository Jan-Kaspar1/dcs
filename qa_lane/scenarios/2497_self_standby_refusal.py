"""The self_standby_refusal acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stands one self-addressed `--standby` launch and
# one legitimate `--standby` control on the labeled born seats, never
# touching the deployed pair's roles or its field claim — so it runs
# inside the claim-lifecycle cluster, after the released-claim leg that
# restores the pair's launch layout, and before the revision legs that
# need the pair and the born seats as they launched them.
RUNS_AFTER = frozenset({'scenario_demote_release_stays_released'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The self-standby startup-claim refusal contract — the per-revision
# lane evidence for the contract #1340's fix establishes (the
# startup-claim arbitration WW-FND-002's redundant failover depends
# on). A tracking source must name a *different* instance: a
# `--standby` (or `--peer`) target resolving to this run's own
# `--listen` socket is a usage error at launch, refused before the run
# exists.
#
# The condition the leg stages is the one the gate can only catch by
# resolving, not by string-comparing: a `--standby` launch whose
# tracking seat is the launch's *own announced address*. On the rig
# that is a labeled born seat launched `--standby` at its own
# rig-bridge container name and monitor port — the very address the
# seat's own checkpoint pulls announce through `?peer=` to every
# participant that pulls from it, the seat included, and the address
# the field sees it announce itself as under its own pinned
# `--owner-token`. Nothing in the launch spells "me": the name is a
# different string from the wildcard `--listen` bind, and it resolves
# to the container's bridge IP rather than loopback. Only resolution
# tells the two apart.
#
# The defect the contract answers: that self-reference passed every
# check. Each pull returned the run's own checkpoint, which a
# standby's document always stamps `source_owns_field: false`, so
# every apply scored a heartbeat miss — the unarmed run reported
# `standby` covering no peer forever, and the armed one manufactured a
# failover against itself at every budget. Nothing refused it: the run
# lived as a legitimate-looking seat in the pair, indistinguishable on
# the monitor from a standby that tracks a real peer, where the same
# class of verdict at boot — a startup claim a live peer holds, a
# `--peer` naming this run, a persistence alias — is a nonzero exit
# carrying the named refusal. The gate's fix resolves both spellings —
# an equal socket, a wildcard listen reached through any of this
# host's addresses, a wildcard target the loopback stack answers — and
# refuses the self-addressed declaration at parse, like the other
# startup-claim refusal classes.
#
# The clauses, staged on the deployed rig:
#
# - the self-addressed launch is refused inside the bound: a nonzero
#   exit whose stderr names the verdict — the offending `--standby`
#   flag, the resolution that made the target this run's own socket,
#   that `--listen` socket, the "must be a different instance" reason,
#   and the staged address itself;
# - it never occupies a seat: the process is gone, its monitor never
#   answered once across the refusal window, and its durable journal
#   carries no run boundary and no entry — a boot refusal lands where
#   the refusal classes' contract carries it, on the process's own
#   stderr, precisely because the run never existed to journal;
# - the field never moved: a read-only `probe_writer` still names the
#   incumbent's own pinned owner token, never the refused seat's;
# - a legitimate `--standby` launch naming a *different* instance —
#   the same seat kind, the same flag, the same launch — is not
#   refused: it converges `tracking` on the incumbent and observes the
#   incumbent's held claim, so the gate cannot pass by refusing every
#   tracking source;
# - the deployed pair is undisturbed throughout: its field owner
#   `active` with the claim held and its tick advancing, its member
#   `standby` and tracking, and every staged seat removed afterwards.
#
# Named diagnostics: self-standby-refusal-failed tags the contract
# clauses — a self-addressed seat that survives as a live standby, a
# zero exit, an unnamed, resolution-free, socket-free or address-free
# refusal, a monitor that answered, a journal that claims a run
# existed, a field whose claim moved off the incumbent, a legitimate
# standby that was refused or never tracked, a disturbed pair — and
# self-standby-refusal-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a vanished
# container, a lost claim probe, a seat left behind. A run context
# carrying no born-seat staging levers, no published monitor for a
# born seat or the pair, no per-seat journal files, no published
# plant endpoint, or a deployed pair that never settled on its launch
# layout reports inconclusive, as does a staging whose own-address
# identity cannot be proven — the leg never judges a launch it could
# not stage. The unchecked-diagnostic self-check replays the judge
# over planted negatives and reports self-standby-refusal-unchecked
# for any that slip through.

SR_REFUSE = 45     # bound on the self-addressed launch's refusal
SR_CONVERGE = 45   # bound on the legitimate standby converging
SR_WATCH = 4       # seat reads the refusal window spans at minimum
SR_POLL = 0.4      # cadence polling a seat's monitor and container
DIAG_FAILED = 'self-standby-refusal-failed'
DIAG_NONDET = 'self-standby-refusal-nondeterministic'
DIAG_UNCHECKED = 'self-standby-refusal-unchecked'

# The self-addressed seat: the run whose `--standby` names its own
# announced address. The control seat: the same launch kind naming a
# different instance, so the gate's over-refusal is caught too.
SELF_SEAT = 'driven'
CONTROL_SEAT = 'foreign'
SEATS = (SELF_SEAT, CONTROL_SEAT)

# The refusal's own words on the refusing process's stderr — the flag
# it refused, the resolution that made the target this run's own
# socket, that socket, the reason a tracking source is a different
# instance, and the staged address itself. The usage block the shell
# prints below the refusal repeats the flag and the reason, so the
# resolution clause and the address echo are what distinguish the
# verdict line itself from the boilerplate around it. A boot usage
# error carries no durable record, so this text is the whole
# contract's evidence surface and the leg reads all of it.
REFUSAL_FLAG = '--standby'
REFUSAL_SELF = 'resolves to this instance'
REFUSAL_SOCKET = "own --listen socket"
REFUSAL_VERDICT = 'must be a different instance'


def _seat_view(ctx, seat):
    """One read of a born seat's serving monitor: role, sync verdict,
    served claim observation, and tick. None when the monitor answers
    nothing — a refused read is evidence of no served monitor, which
    is the self-addressed seat's expected surface."""
    report = _try_role(ctx, ctx.get(seat) or '')
    if report is None:
        return None
    return {'role': report.get('role'),
            'sync': _seat_sync(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _seat_sync(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _tracking(report):
    return (report or {}).get('role') == 'standby' \
        and _seat_sync(report) == 'tracking'


def _member_view(ctx, name):
    """One deployed pair member's served surface — the
    undisturbed-pair evidence: role, tick, and the observed field
    claim."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    return {'role': report.get('role'),
            'tick': report.get('tick'),
            'field_claim': report.get('field_claim')}


def _seat_state(ctx, seat):
    """The seat container's process verdict — running flag, exit code,
    and its log tail. None where the read itself failed."""
    state = ctx.get('born_controller_state')
    if state is None:
        return None
    try:
        return state(seat)
    except Exception:
        return None


def _seat_census(ctx, seat):
    """The seat's durable journal census: whether the runner-owned file
    exists at all, and how many run-boundary markers and entries it
    carries. A boot refusal predates the run, so the honest count is
    an absent or empty file — any record at all is a run that existed,
    which a usage error cannot have."""
    path = (ctx.get('journal_files') or {}).get(seat)
    if not path or not Path(path).is_file():
        return {'present': False, 'boundaries': 0, 'entries': 0}
    try:
        items = _journal_entries(path)
    except ValueError:
        return {'present': True, 'boundaries': 0, 'entries': -1}
    return {'present': True,
            'boundaries': sum(1 for item in items
                              if 'run_boundary' in item),
            'entries': sum(1 for item in items if 'entry' in item)}


def _claim_probe(ctx):
    """The lane's claim-aware seam: a dedicated attachment's read-only
    `probe_writer` on the plant protocol — the standing claim's verdict
    with its owner token named, asserting, joining, and releasing
    nothing, so a field whose owner the leg audits is read without
    seizing it. None is a lost read, never a verdict."""
    response = _try_plant(ctx, {'op': 'probe_writer'})
    if response is None:
        return None
    error = response.get('error') or {}
    kind = error.get('kind')
    if kind == 'fenced':
        return {'claim': 'held', 'owner': error.get('owner')}
    if kind == 'unclaimed':
        return {'claim': 'unclaimed', 'owner': None}
    if response.get('result') == 'done':
        return {'claim': 'held', 'owner': None}
    return {'claim': 'unknown', 'owner': None}


def _wait_refusal(ctx, seat):
    """The refusal window: the seat's process verdict and every view
    its monitor served while the window stood. Polls until the process
    is gone and SR_WATCH reads have been attempted, so a monitor that
    served briefly and died is still caught; the last verdict read is
    returned when the bound passes, so a seat still standing at the
    bound is audited as standing."""
    served = []
    attempts = 0
    state = None
    deadline = time.monotonic() + SR_REFUSE
    while time.monotonic() < deadline:
        attempts += 1
        state = _seat_state(ctx, seat)
        view = _seat_view(ctx, seat)
        if view is not None:
            served.append(view)
        gone = state is not None and (state.get('absent')
                                      or not state.get('running'))
        if gone and attempts >= SR_WATCH:
            break
        time.sleep(SR_POLL)
    return state, served


def _wait_tracking(ctx, seat, deadline=None):
    """The seat's report once it serves standby/tracking, else the last
    report (or None) when the bound passed — an unconverged seat's own
    served surface is the evidence of what it reported instead."""
    latest = []
    wait_for(lambda: (lambda report: latest.append(report) or
                      (report if _tracking(report) else None))
             (_seat_view(ctx, seat)),
             deadline or time.monotonic() + SR_CONVERGE,
             interval=SR_POLL)
    return latest[-1] if latest else None


def _self_pass(ctx, number):
    """One pass over the self-standby refusal contract: settle the
    deployed pair, stand the self-addressed `--standby` launch on a
    labeled seat and prove the target really is that seat's own
    announced address, watch its refusal window, then stand a
    legitimate `--standby` launch on a second seat and collect both
    seats' verdicts beside the pair's undisturbedness. Returns
    (record, evidence); an aborted stage leaves its later keys absent."""
    record = {'pass': number}
    evidence = {'pass': number}
    incumbent = _pair_active(ctx)
    if incumbent is None:
        evidence['inconclusive'] = (
            'the deployed pair reports no field-owning member — the '
            'incumbent the self-addressed seat would track as its own '
            'never settled')
        return record, evidence
    member = 'standby' if incumbent == 'active' else 'active'
    tokens = ctx.get('plant_owner') or {}
    record['remote'] = ctx['plant_remote']
    record['incumbent'] = incumbent
    record['incumbent_token'] = tokens.get(incumbent)
    record['self_token'] = tokens.get(SELF_SEAT)
    record['control_token'] = tokens.get(CONTROL_SEAT)
    record['incumbent_before'] = _member_view(ctx, incumbent)
    record['member_before'] = _member_view(ctx, member)
    record['claim_before'] = _claim_probe(ctx)
    if (record['incumbent_before'] or {}).get('field_claim') != 'held' \
            or (record['member_before'] or {}).get('role') != 'standby':
        evidence['inconclusive'] = (
            'the deployed pair is not in the settled shape the '
            'staging needs — the incumbent must hold the field with '
            'its member tracking: '
            + json.dumps({'incumbent': record['incumbent_before'],
                          'member': record['member_before']})[:200])
        return record, evidence
    try:
        try:
            launch = ctx['start_born_controller'](
                SELF_SEAT, record['remote'], standby=SELF_SEAT)
            record['self_target'] = (launch or {}).get('standby')
            record['self_address'] = (launch or {}).get('address')
            # The staging's own identity check: the refused target must
            # be the seat's own announced address. A rig that resolved
            # a seat key to anything else would stage a legitimate
            # cross-seat standby, and every verdict below would be
            # reading the wrong launch.
            if not record['self_target'] \
                    or record['self_target'] != record['self_address']:
                evidence['inconclusive'] = (
                    'the self-addressed staging did not name the '
                    'seat\'s own announced address: the launch carried '
                    '--standby ' + str(record['self_target'])
                    + ' against its own announced '
                    + str(record['self_address'])
                    + ', so the self-referential condition was never '
                      'staged')
                return record, evidence
            record['refused'], record['served'] = _wait_refusal(
                ctx, SELF_SEAT)
            record['self_census'] = _seat_census(ctx, SELF_SEAT)
            control = ctx['start_born_controller'](
                CONTROL_SEAT, record['remote'], standby=incumbent)
            record['control_target'] = (control or {}).get('standby')
        except Exception as exc:
            record['stage_error'] = str(exc)[:300]
            return record, evidence
        record['control'] = _wait_tracking(ctx, CONTROL_SEAT)
        record['control_state'] = _seat_state(ctx, CONTROL_SEAT)
        record['control_census'] = _seat_census(ctx, CONTROL_SEAT)
        record['claim_after'] = _claim_probe(ctx)
        record['incumbent_after'] = _member_view(ctx, incumbent)
        record['member_after'] = _member_view(ctx, member)
    finally:
        record['removed'] = _sweep(ctx)
    return record, evidence


def _sweep(ctx):
    """The teardown every seat the pass stood, in order — the leg's own
    cleanup, reported so a seat left behind is visible rather than
    inherited by the legs behind this one."""
    swept = []
    for seat in SEATS:
        try:
            ctx['stop_born_controller'](seat)
            swept.append(seat)
        except Exception:
            pass
    return swept


def _refusal_named(logs, target):
    """Whether the refusing process's own output carries the named
    verdict: the flag it refused, the resolution that made the target
    this run's own socket, that socket, the reason a tracking source
    is a different instance, and the staged address itself."""
    if not logs:
        return False
    return all(fragment in logs for fragment in
               (REFUSAL_FLAG, REFUSAL_SELF, REFUSAL_SOCKET,
                REFUSAL_VERDICT, str(target)))


def _incumbent_undisturbed(before, after):
    """The deployed incumbent was never disturbed: still active, still
    observing its held claim, its tick advancing across the pass."""
    return (after or {}).get('role') == 'active' \
        and (after or {}).get('field_claim') == 'held' \
        and isinstance((before or {}).get('tick'), int) \
        and isinstance((after or {}).get('tick'), int) \
        and after['tick'] > before['tick']


def _judge_self(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses
    and DIAG_NONDET the instability the contract does not answer for.
    """
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the self-standby staging never completed: '
               + str(record['stage_error']))
        return
    refused = record.get('refused') or {}
    target = record.get('self_target')
    logs = refused.get('logs') or ''
    if refused.get('absent'):
        nondet('refusal-vanished', 'the self-addressed seat\'s '
               'container vanished — no process verdict exists to '
               'audit')
    elif refused.get('running'):
        failed('refusal-standing', 'the self-addressed --standby '
               'launch is still standing as the pair\'s live standby — '
               'the silently-legitimate seat the refusal exists to '
               'prevent: ' + json.dumps(refused)[:200])
    elif not refused.get('exit'):
        failed('refusal-exit', 'the self-addressed --standby launch '
               'did not exit nonzero: ' + json.dumps(refused)[:200])
    elif not _refusal_named(logs, target):
        failed('refusal-named', 'the self-standby refusal\'s stderr '
               'never named the verdict — ' + REFUSAL_FLAG + ' '
               + str(target) + ' must answer "' + REFUSAL_SELF + "' "
               + REFUSAL_SOCKET + '" and "' + REFUSAL_VERDICT + '": '
               + logs[-240:])
    if record.get('served'):
        failed('refusal-served', 'the self-addressed seat\'s monitor '
               'answered during the refusal window — the run bound a '
               'monitor and lived a seat: '
               + json.dumps(record['served'])[:240])
    census = record.get('self_census') or {}
    if census.get('boundaries') or census.get('entries'):
        failed('refusal-journaled', 'the self-addressed seat\'s '
               'durable journal carries run records — a boot usage '
               'error refuses before the run exists, so nothing may '
               'claim it served: ' + json.dumps(census)[:200])

    token = record.get('incumbent_token')
    for label in ('claim_before', 'claim_after'):
        claim = record.get(label)
        if claim is None:
            nondet('claim-probe', 'the field\'s claim observation '
                   + label + ' never answered — the read-only probe '
                   'lost a round trip')
            continue
        if claim.get('claim') != 'held':
            failed('claim-open', 'the field\'s claim stood '
                   + str(claim.get('claim')) + ' at ' + label
                   + ' — the pair\'s write ownership is not the '
                     'incumbent\'s to hold')
            continue
        if record.get('self_token') is not None \
                and claim.get('owner') == record['self_token']:
            failed('claim-seized', 'the refused self-standby seat\'s '
                   'own token holds the field\'s claim at ' + label
                   + ' — a refused launch must never take the field')
            continue
        if token is not None and claim.get('owner') not in (None,
                                                              token):
            failed('claim-owner', 'the field\'s claim at ' + label
                   + ' is not the incumbent\'s own pinned token '
                   + str(token) + ': ' + json.dumps(claim)[:200])

    control = record.get('control')
    state = record.get('control_state') or {}
    if state.get('running') is not True:
        logs = state.get('logs') or ''
        if _refusal_named(logs, record.get('control_target')):
            failed('control-overrefused', 'the legitimate --standby '
                   'launch naming a different instance was refused with '
                   'the self-standby verdict — the gate may not pass by '
                   'refusing every tracking source: ' + logs[-240:])
        else:
            failed('control-exit', 'the legitimate --standby launch '
                   'exited — a tracking source naming a different '
                   'instance must run: ' + json.dumps(state)[:200])
    elif not _tracking(control):
        failed('control-unconverged', 'the legitimate --standby seat '
               'never converged tracking on the incumbent: '
               + json.dumps(control)[:200])
    elif control.get('field_claim') != 'held':
        failed('control-claim', 'the legitimate standby does not '
               'observe the incumbent\'s held claim: '
               + json.dumps(control)[:200])
    if not (record.get('control_census') or {}).get('boundaries'):
        failed('control-unjournaled', 'the legitimate --standby seat '
               'carries no run boundary in its durable journal — the '
               'contrast the pre-run refusal rests on: '
               + json.dumps(record.get('control_census'))[:200])
    if record.get('control_target') == record.get('self_target'):
        failed('control-identity', 'the legitimate --standby launch '
               'was staged on the self-addressed target '
               + str(record.get('control_target')) + ' — the '
               'false-positive guard never named a different instance')

    if not _incumbent_undisturbed(record.get('incumbent_before'),
                                  record.get('incumbent_after')):
        failed('incumbent', 'the deployed pair\'s incumbent was '
               'disturbed — a refused self-standby must leave the '
               'standing claim\'s owner active, held, and advancing: '
               + json.dumps({'before': record.get('incumbent_before'),
                             'after': record.get('incumbent_after')}
                            )[:300])
    member = record.get('member_after') or {}
    if member.get('role') != 'standby':
        failed('member', 'the deployed pair\'s tracking member left '
               'standby — the staged launches disturbed the pair\'s '
               'second member: ' + json.dumps(member)[:200])
    if sorted(record.get('removed') or []) != sorted(SEATS):
        nondet('swept', 'not every staged seat was removed: '
               + json.dumps({'removed': record.get('removed'),
                             'seats': list(SEATS)}))


def _digest_self(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each word carries the recorded disposition only while no
    violation names it."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'self-standby': 'refused-at-boot'
            if clean('refusal-standing', 'refusal-exit',
                     'refusal-named', 'refusal-journaled')
            else 'defect',
        'self-seat': 'never-served'
            if clean('refusal-served', 'refusal-vanished')
            else 'defect',
        'field-claim': 'incumbent-held'
            if clean('claim-open', 'claim-owner', 'claim-seized',
                     'claim-probe')
            else 'defect',
        'declared-standby': 'tracking'
            if clean('control-exit', 'control-overrefused',
                     'control-unconverged', 'control-claim',
                     'control-unjournaled', 'control-identity')
            else 'defect',
        'deployed-pair': 'undisturbed'
            if clean('incumbent', 'member')
            else 'disturbed',
        'seats': 'swept' if clean('swept') else 'left-behind'}


def _self_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative — every recorded failure shape must name
    DIAG_FAILED; the instability shapes must report DIAG_NONDET.
    Returns the negative names the judge let through."""
    slipped = []

    def clean_record():
        logs = ('error: --standby dcs-hw-qa-1-d:8082 resolves to this '
                "instance's own --listen socket 0.0.0.0:8082: the "
                'tracking source must be a different instance — a run '
                'pulling its own checkpoints tracks no peer')
        return {
            'pass': 1,
            'remote': 'dcs-hw-qa-1-plant:9001',
            'incumbent': 'active',
            'incumbent_token': 424243,
            'self_token': 424247,
            'control_token': 424246,
            'self_target': 'dcs-hw-qa-1-d:8082',
            'self_address': 'dcs-hw-qa-1-d:8082',
            'control_target': 'dcs-hw-qa-1-a:8080',
            'incumbent_before': {'role': 'active', 'tick': 10,
                                 'field_claim': 'held'},
            'member_before': {'role': 'standby', 'tick': 8,
                              'field_claim': 'held'},
            'claim_before': {'claim': 'held', 'owner': 424243},
            'refused': {'running': False, 'exit': 1, 'absent': False,
                        'logs': logs},
            'served': [],
            'self_census': {'present': False, 'boundaries': 0,
                            'entries': 0},
            'control': {'role': 'standby',
                        'sync': {'tracking': {'aligned': 42}},
                        'field_claim': 'held', 'tick': 3},
            'control_state': {'running': True, 'exit': None,
                              'absent': False, 'logs': ''},
            'control_census': {'present': True, 'boundaries': 1,
                               'entries': 4},
            'claim_after': {'claim': 'held', 'owner': 424243},
            'incumbent_after': {'role': 'active', 'tick': 30,
                                'field_claim': 'held'},
            'member_after': {'role': 'standby', 'tick': 20,
                             'field_claim': 'held'},
            'removed': ['driven', 'foreign']}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_self(record,
                    lambda key, diag, detail:
                    found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negatives the issue names: the self-standby
    # refusing nothing and living the pair's live standby, plus the
    # refusal that never named itself, the seat that served anyway,
    # the run that journaled itself into existence, the field that
    # moved, the legitimate standby the gate over-refused, and the
    # pair it disturbed.
    expect('self-seat-legitimate', lambda record:
           record['refused'].update({'running': True, 'exit': None}))
    expect('self-exit-zero', lambda record:
           record['refused'].update({'exit': 0}))
    expect('self-exit-unnamed', lambda record:
           record['refused'].update({'logs': 'error: the tracking '
                                      'source must be a different '
                                      'instance'}))
    expect('self-exit-no-resolution', lambda record:
           record['refused'].update({'logs': 'error: --standby '
                                      'dcs-hw-qa-1-d:8082 is this run\'s '
                                      "own --listen socket 0.0.0.0:8082: "
                                      'the tracking source must be a '
                                      'different instance'}))
    expect('self-exit-no-socket', lambda record:
           record['refused'].update({'logs': 'error: --standby '
                                      'dcs-hw-qa-1-d:8082 resolves to '
                                      "this instance's tracking source; "
                                      'the tracking source must be a '
                                      'different instance'}))
    expect('self-exit-no-target', lambda record:
           record['refused'].update({'logs': 'error: --standby '
                                      'dcs-hw-qa-1-d:9999 resolves to '
                                      "this instance's own --listen "
                                      'socket 0.0.0.0:8082: the '
                                      'tracking source must be a '
                                      'different instance'}))
    expect('self-exit-silent', lambda record:
           record['refused'].update({'logs': ''}))
    expect('self-seat-served', lambda record:
           record.update({'served': [{'role': 'standby',
                                      'sync': 'unsynchronized',
                                      'field_claim': None,
                                      'tick': 1}]}))
    expect('self-seat-journaled', lambda record:
           record['self_census'].update({'present': True,
                                        'boundaries': 1, 'entries': 2}))
    expect('self-seat-claimed', lambda record:
           record['claim_after'].update({'owner': 424247}))
    expect('field-unclaimed', lambda record:
           record['claim_after'].update({'claim': 'unclaimed',
                                        'owner': None}))
    expect('field-owner-foreign', lambda record:
           record['claim_after'].update({'owner': 424246}))
    expect('field-unknown', lambda record:
           record['claim_before'].update({'claim': 'unknown'}))
    expect('control-exited', lambda record:
           record['control_state'].update({'running': False,
                                           'exit': 1}))
    expect('control-over-refused', lambda record:
           record['control_state'].update(
               {'running': False, 'exit': 1,
                'logs': 'error: --standby dcs-hw-qa-1-a:8080 resolves '
                        "to this instance's own --listen socket "
                        '0.0.0.0:8082: the tracking source must be a '
                        'different instance'}))
    expect('control-never-tracks', lambda record:
           record.update({'control': {'role': 'standby',
                                      'sync': 'unsynchronized'}}))
    expect('control-claim-unobserved', lambda record:
           record['control'].update({'field_claim': None}))
    expect('control-unjournaled', lambda record:
           record['control_census'].update({'present': False,
                                            'boundaries': 0,
                                            'entries': 0}))
    expect('control-staged-self', lambda record:
           record.update({'control_target': 'dcs-hw-qa-1-d:8082'}))
    expect('incumbent-demoted', lambda record:
           record['incumbent_after'].update({'role': 'standby',
                                             'field_claim': None}))
    expect('incumbent-stalled', lambda record:
           record['incumbent_after'].update({'tick': 10}))
    expect('member-promoted', lambda record:
           record['member_after'].update({'role': 'active'}))
    expect('seat-left-behind', lambda record:
           record.update({'removed': ['driven']}), DIAG_NONDET)
    # The instability shapes must report nondeterministic: a refused
    # staging call, a vanished container, a lost claim probe.
    expect('stage-refused', lambda record:
           record.update({'stage_error': 'docker run failed'}),
           DIAG_NONDET)
    expect('refusal-container-vanished', lambda record:
           record['refused'].update({'running': False, 'exit': None,
                                     'absent': True}),
           DIAG_NONDET)
    expect('claim-probe-lost', lambda record:
           record.update({'claim_after': None}), DIAG_NONDET)
    return slipped


def scenario_self_standby_refusal(ctx):
    """Exercise the self-standby startup-claim refusal contract on the
    deployed rig: stand a `--standby` launch whose tracking seat is
    its own announced address — the run every participant, the seat
    included, sees announced as the pair's standby — and prove the
    gate refuses it at boot by name: a nonzero exit carrying the
    named verdict on the refusing process's own stderr, no monitor
    ever served, no durable run record, and the field's claim still
    under the incumbent's own token. A legitimate `--standby` launch
    naming a different instance still converges and tracks, so the
    gate cannot pass by refusing every tracking source; the deployed
    pair runs undisturbed and every staged seat is removed afterward.
    Two consecutive passes must produce identical digests."""
    case = Case(
        'self-standby-refusal',
        'A self-addressed tracking source is refused at boot',
        'a --standby launch whose tracking seat is its own announced '
        'address is refused inside the bound — a nonzero exit whose '
        "stderr names the offending flag, the resolution that made the "
        'target this instance, the own --listen socket it reaches, the '
        'different-instance reason, and the staged address — never '
        'occupying a seat: the monitor never answers, the durable '
        "journal carries no run record, and the field's claim still "
        "names the incumbent's own pinned token. A legitimate "
        '--standby launch naming a different instance is not refused '
        'and converges tracking on the incumbent, the deployed pair is '
        'undisturbed with every staged seat removed afterward, and two '
        'passes produce identical digests')
    try:
        missing = [key for key in (
            'plant_remote', 'plant', 'start_born_controller',
            'stop_born_controller',
            'born_controller_state') if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context '
                               'carries no self-standby staging '
                               'levers: ' + ', '.join(missing))
        if not all(ctx.get(seat) for seat in SEATS):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'born seats')
        if not all(ctx.get(member) for member in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no published monitor for the '
                               'deployed pair')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in SEATS):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-seat journal files — '
                               'the pre-run half of the audit cannot '
                               'run')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _self_pass(ctx, number)
            evidence['record'] = record
            if not evidence.get('inconclusive'):
                _judge_self(record, note)
            digest = _digest_self(record, violations)
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'self-standby-refusal-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref,
                          'self-standby-refusal pass ' + str(number)
                          + ' — the staged own-address target, the '
                          'refusal window\'s served reads and process '
                          'verdict, the seat\'s durable census, the '
                          'field\'s claim observations, the '
                          'legitimate standby\'s convergence, the '
                          'incumbent checks, and the normalized '
                          'digest')
            if evidence.get('inconclusive'):
                return case.finish('inconclusive',
                                   evidence['inconclusive'])
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
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two self-standby-refusal passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _self_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
