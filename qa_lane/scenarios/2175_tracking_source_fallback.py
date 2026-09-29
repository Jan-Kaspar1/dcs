"""The tracking_source_fallback leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg extends the keyed announced-source cluster's
# staging — the successor pin it kills is the demote/announce/
# orphan-resolution machinery the island and orphan-episode legs
# already prove — and it restores the pair's launch roles for the
# failover case behind it.
RUNS_AFTER = frozenset({'scenario_orphan_episode_bound'})
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The dead-successor tracking-source fallback contract — #1136's
# landed fix pinned as per-run lane evidence for WW-LCM-001's
# continuity clause (the mechanism-level pin release lives in
# crates/dcs-monitor's PIN_MISS_BUDGET bookkeeping): a tracking peer
# pinned onto a learned source — the `resolved` or `adopted`
# successor the orphan probe's own verification earned — keeps that
# pin only while the endpoint answers. PIN_MISS_BUDGET consecutive
# produced-nothing pulls against it release the pin and re-run the
# source resolution inside the same cycle, so a dead learned
# successor drops the pulls back onto the declared --standby source
# instead of stranding the peer on a dead endpoint until restart.
#
# The leg stages the pin through the demote/announce path the
# adoption legs already prove: the keyed pair's field owner demotes
# onto the announced hints so the demoted pair islands; the run's
# driven third controller then promotes onto the field, and both
# island members' orphan-resolution probes pin the standby onto it —
# the journaled tracking_source_adopted naming :8082 is the learned
# pin's own evidence. The standby is then frozen through the rig's
# pause action, the launch owner promoted back over the field — a
# tracking peer's unconditional claim preempts the successor's
# standing one — and the pinned container removed, so the peer's
# first pulls on resume already meet a dead source: the frozen
# window guarantees no orphaned apply can re-resolve the pin onto
# the configured endpoint before the release itself fires, making
# the post-kill adoption journal attributable to the pin-release
# path alone. On resume the produced-nothing pulls count to the
# budget, the pin releases, the same-cycle resolution re-proves the
# configured endpoint — now serving owner checkpoints again — and
# journals the retarget by name. The served sync must reconverge to
# a tracking verdict, the fallback must journal its named evidence —
# a tracking_source_adopted naming the configured :8080 endpoint —
# inside the peer's one process lifetime, and the pair's launch
# roles restore. Named diagnostics source-fallback-failed for a
# contract miss — a fallback that never journaled, a tracking
# verdict asserted while the journal shows the pin never retargeted
# off the dead successor, a reconvergence that never lands, a run
# boundary proving a restart did the work, unrestored launch roles —
# source-fallback-nondeterministic when two passes disagree or the
# rig answers with instability instead of a verdict — a staging call
# refused, a pin that never landed, a starved watch, the peer
# promoting out from under the leg — and
# source-fallback-unchecked when the self-check's planted negatives
# slip the leg's own audits.

FALLBACK_SETTLE = 45   # bound on each switch/converge/restore wait
FALLBACK_FORM = 25     # bound on the island forming and the pin
                       # landing — inside the armed failover budget
FALLBACK_POLL = 0.15   # wait cadence inside the leg
DRIVEN_SCANS = 4       # driven scans per batch — the pulls the keyed
                       # probe's announces and convergence need
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DRIVEN_PORT = 8082     # the driven peer's --listen port inside the rig
DIAG_FAILED = 'source-fallback-failed'
DIAG_NONDET = 'source-fallback-nondeterministic'
DIAG_UNCHECKED = 'source-fallback-unchecked'


def _orphaned(ctx, name):
    """The endpoint's /role report while it is a standby reporting the
    ownerless-line verdict — the island member's state — else None."""
    report = _try_role(ctx, ctx[name])
    sync = (report or {}).get('sync')
    if (report or {}).get('role') == 'standby' \
            and isinstance(sync, dict) and 'orphaned' in sync:
        return report
    return None


def _drive(ctx, scans=DRIVEN_SCANS):
    """One POST /scan batch on the driven peer — every pull it ever
    performs happens inside the request."""
    return http_json('POST', ctx['driven'] + '/scan',
                     {'scans': scans}, timeout=scans * 2 + 15)


def _durable_events(ctx, name, floor, kind):
    """The `kind` event bodies the peer's durable --journal-file
    carries since `floor` records — the audit surface the learned
    pin's adoption and the fallback retarget are asserted on."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _run_boundaries(ctx, name):
    """The process lifetimes the peer's durable journal records — one
    run_boundary per resumed run; a reconvergence that needed a
    restart leaves a new one behind."""
    return sum(1 for item in _journal_entries(ctx['journal_files'][name])
               if 'run_boundary' in item)


def _adoptions_naming(ctx, name, floor, port):
    """The tracking_source_adopted events the peer journaled since
    `floor` whose source names the given listen port."""
    return [event for event in _durable_events(
        ctx, name, floor, 'tracking_source_adopted')
        if str(event.get('source') or '').endswith(':' + str(port))]


def _pinned(ctx, name, floor, port):
    """The peer's learned pin landed: a journaled tracking_source
    adoption naming the port plus the tracking verdict the pulls it
    moved onto produce — {'journal', 'report'} or None."""
    journal = _adoptions_naming(ctx, name, floor, port)
    report = _tracking_standby(ctx, name)
    if journal and report is not None:
        return {'journal': journal, 'report': report}
    return None


def _judge_fallback(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    dead-successor contract clauses — the named fallback evidence
    the retarget owes, the tracking verdict that must not be
    asserted while the journal shows the pin still on the dead
    successor, the reconvergence, the restart the contract never
    needs, the restored launch roles — and DIAG_NONDET tags the
    instability the contract does not answer for: a refused staging
    call, a pin that never landed, a starved watch, the peer
    promoting out from under the leg. An aborted stage ends the
    audit where the pass ended — the later keys it never wrote are
    not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('driven_scan_error') is not None \
            or not isinstance(record.get('converged'), dict):
        nondet('driven-converge', 'the driven peer never converged a '
               'tracking standby on the field owner — the successor '
               'staging never began: '
               + json.dumps(record.get('driven_scan_error'))[:200])
        return
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        nondet('demote', 'the island-inducing POST /demote answered '
               + str(demote.get('status')) + ' '
               + json.dumps(demote.get('body'))[:160] + ' — the '
               'staging never opened the island')
        return
    if not isinstance(record.get('island'), dict):
        nondet('island', 'the demoted pair never reported the '
               'orphaned verdict on each other — the island never '
               'formed')
        return
    driven_promote = record.get('driven_promote') or {}
    if driven_promote.get('status') != 200:
        nondet('driven-promote', 'POST /promote on the driven peer '
               'answered ' + str(driven_promote.get('status')) + ' '
               + json.dumps(driven_promote.get('body'))[:160] + ' — '
               'the successor never took the field')
        return
    if not isinstance(record.get('driven_active'), dict):
        nondet('driven-active', 'the driven peer never settled '
               'active on the field — the successor the pin targets '
               'never stood')
        return
    if not isinstance(record.get('pinned'), dict):
        nondet('pin', 'the peer never pinned the successor — no '
               'tracking_source_adopted naming :' + str(DRIVEN_PORT)
               + ' journaled, or the tracking verdict it moves the '
               'pulls onto never landed — the learned source the '
               'contract releases was never staged')
        return
    promote = record.get('promote') or {}
    if promote.get('status') != 200:
        nondet('promote', 'the launch owner\'s re-claim POST '
               '/promote answered ' + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160] + ' — the '
               'configured source never re-earned the field')
        return
    if not isinstance(record.get('owner_active'), dict):
        nondet('owner-active', 'the launch owner never settled '
               'active again — the configured source cannot serve '
               'owner checkpoints')
        return
    if record.get('reads') == 0:
        nondet('watch', 'the peer\'s monitor answered nothing '
               'through the fallback window — the starved watch gave '
               'the audit no served verdict to read')
        return
    last = record.get('last_role') or {}
    if last.get('role') is not None and last.get('role') != 'standby':
        nondet('failover', 'the pinned peer reported '
               + json.dumps(last)[:200] + ' inside the fallback '
               'window — it promoted out from under the leg rather '
               'than reconverging as a standby')
        return
    fallback = record.get('fallback_journal') or []
    reconverged = record.get('reconverged')
    if not fallback:
        if reconverged is not None:
            failed('pinned-dead', 'the peer asserts a tracking '
                   'reconvergence its durable journal contradicts — '
                   'no tracking_source_adopted naming the configured '
                   ':' + str(PAIR_PORTS['active']) + ' endpoint '
                   'landed after the pinned successor died, so the '
                   'pin still targets the dead source the verdict '
                   'claims to have left')
        else:
            failed('fallback', 'the dead pin never produced the '
                   'named fallback evidence — no '
                   'tracking_source_adopted naming :'
                   + str(PAIR_PORTS['active']) + ' journaled on the '
                   'peer after the successor died; the peer is '
                   'stranded on the dead learned source')
        return
    if reconverged is None:
        failed('reconverge', 'the configured-source fallback '
               'journaled its retarget but the peer never '
               'reconverged to a tracking verdict — the declared '
               '--standby source was retried without converging')
        return
    boundaries = record.get('boundaries') or {}
    if boundaries.get('after', 0) > boundaries.get('before', 0):
        failed('restart', 'the peer\'s durable journal opened a new '
               'run boundary across the fallback — the reconvergence '
               'needed a restart the contract says is never '
               'required')
    if not record.get('restored'):
        failed('roles', 'the pair never settled back to its launch '
               'roles — ' + str(record.get('owner')) + ' active '
               'with ' + str(record.get('peer')) + ' tracking '
               'behind it')


def _digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'pinned': 'successor'
            if clean('driven-converge', 'demote', 'island',
                     'driven-promote', 'driven-active', 'pin')
            else 'none',
        'fallback': 'journaled'
            if clean('pinned-dead', 'fallback', 'promote',
                     'owner-active', 'watch', 'failover')
            else 'absent',
        'reconverged': 'tracking'
            if clean('reconverge', 'pinned-dead') else 'stranded',
        'restart': 'none' if clean('restart') else 'restarted',
        'roles': 'restored' if clean('roles') else 'unrestored'}


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay the fallback
    judge over each planted negative the issue names — a tracking
    verdict asserted while the pin stands dead, the fallback that
    never journaled, a reconvergence that never landed, a restart
    doing the work, unrestored roles — and require the judge to note
    each. A silent judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        return {'converged': {'role': 'standby',
                              'sync': {'tracking': {'aligned': 40}}},
                'demote': {'status': 200, 'body': {'role': 'demoting'}},
                'island': {'active': {'role': 'standby'},
                           'standby': {'role': 'standby'}},
                'driven_promote': {'status': 200,
                                   'body': {'role': 'promoting'}},
                'driven_active': {'role': 'active'},
                'pinned': {'journal': [{'source': 'probe-d:8082'}],
                           'report': {'role': 'standby'}},
                'promote': {'status': 200, 'body': {'role': 'promoting'}},
                'owner_active': {'role': 'active'},
                'reads': 6,
                'last_role': {'role': 'standby'},
                'fallback_journal': [{'source': 'probe-a:8080'}],
                'reconverged': {'role': 'standby',
                                'sync': {'tracking': {'aligned': 60}}},
                'boundaries': {'before': 0, 'after': 0},
                'owner': 'active', 'peer': 'standby',
                'restored': True}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_fallback(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: the peer asserted
    # as reconverged while the pin stands on the dead source — the
    # served tracking verdict contradicting the durable journal.
    expect('pinned-dead-verdict', lambda record:
           record.update({'fallback_journal': []}))
    # The dead pin strands the peer entirely — no retarget, no
    # reconvergence.
    expect('fallback-silent', lambda record: record.update(
        {'fallback_journal': [], 'reconverged': None}))
    # The named evidence landed but the tracking verdict never did.
    expect('reconverge-never', lambda record:
           record.update({'reconverged': None}))
    # A new run boundary — the reconvergence cost a restart.
    expect('restart-needed', lambda record:
           record['boundaries'].update({'after': 1}))
    # The launch roles never restored.
    expect('roles-unrestored', lambda record:
           record.update({'restored': False}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a pin that never landed, refused
    # staging calls, a starved watch, the peer promoting out.
    expect('pin-never-staged', lambda record:
           record.update({'pinned': None}), DIAG_NONDET)
    expect('demote-refused', lambda record:
           record.update({'demote': {'status': 409, 'body':
                                     'no_tracking_source'}}),
           DIAG_NONDET)
    expect('promote-refused', lambda record:
           record.update({'promote': {'status': 409, 'body':
                                      'not_converged'}}),
           DIAG_NONDET)
    expect('watch-starved', lambda record:
           record.update({'reads': 0}), DIAG_NONDET)
    expect('failover-fired', lambda record:
           record['last_role'].update({'role': 'promoting'}),
           DIAG_NONDET)
    return slipped


def _fallback_pass(ctx, number, owner, peer):
    """One dead-successor pass: island the demoted pair, promote the
    driven peer so the standby's orphan probe pins it, freeze the
    standby, re-claim the field for the launch owner, kill the pinned
    container, and thaw — the produced-nothing pulls must release the
    learned pin inside the budget and re-resolve onto the configured
    endpoint, journaled by name. Returns (record, evidence): the
    record is what the judge replays; an aborted stage simply leaves
    its later keys absent for the judge to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    floors = {name: len(_journal_entries(ctx['journal_files'][name]))
              for name in (owner, peer)}

    # Converge the driven peer: each batched scan is a checkpoint pull
    # that announces its monitor on the field owner — the recorded
    # hint the orphan probe dials — and earns its own convergence.
    converged = None
    deadline = time.monotonic() + FALLBACK_SETTLE
    while converged is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception as exc:
            record['driven_scan_error'] = str(exc)[:200]
            return record, evidence
        converged = _tracking_standby(ctx, 'driven')
        if converged is None:
            time.sleep(FALLBACK_POLL)
    record['converged'] = converged
    if converged is None:
        return record, evidence

    # Induce the island: demote the field owner — the announced-source
    # verify adopts the newest tracking hint — so the demoted pair
    # tracks a peer that owns nothing.
    status, body = _settle_call(ctx[owner] + '/demote')
    record['demote'] = {'status': status, 'body': body}
    if status != 200:
        return record, evidence

    def islanded():
        pair = {name: _orphaned(ctx, name) for name in (owner, peer)}
        return pair if all(pair.values()) else None

    record['island'] = wait_for(islanded,
                                time.monotonic() + FALLBACK_FORM,
                                interval=FALLBACK_POLL)
    if not isinstance(record['island'], dict):
        return record, evidence

    # The successor takes the field: the driven peer observes the
    # ownerless line, then its promotion's conditional claim preempts
    # the demoted owner's yielded claim. Drive its scans through the
    # settle.
    try:
        _drive(ctx, 1)
    except Exception as exc:
        record['driven_scan_error'] = str(exc)[:200]
        return record, evidence
    status, body = _settle_call(ctx['driven'] + '/promote')
    record['driven_promote'] = {'status': status, 'body': body}
    if status != 200:
        return record, evidence
    driven_active = None
    deadline = time.monotonic() + FALLBACK_SETTLE
    while driven_active is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception:
            pass
        report = _try_role(ctx, ctx['driven'])
        if (report or {}).get('role') == 'active':
            driven_active = report
        else:
            time.sleep(FALLBACK_POLL)
    record['driven_active'] = driven_active
    if driven_active is None:
        return record, evidence

    # The learned pin: the standby's orphan-resolution probe verifies
    # the successor serving the line as its owner and journals the
    # retarget by name; the tracking verdict follows the moved pulls.
    record['pinned'] = wait_for(
        lambda: _pinned(ctx, peer, floors[peer], DRIVEN_PORT),
        time.monotonic() + FALLBACK_FORM, interval=FALLBACK_POLL)
    record['owner_pinned'] = wait_for(
        lambda: _adoptions_naming(ctx, owner, floors[owner],
                                  DRIVEN_PORT) or None,
        time.monotonic() + FALLBACK_POLL * 4, interval=FALLBACK_POLL)
    if not isinstance(record['pinned'], dict):
        return record, evidence

    # Freeze the standby: the paused window guarantees no orphaned
    # apply can re-resolve the pin onto the configured endpoint
    # before the release itself fires — the post-kill journal is
    # attributable to the pin-release path alone.
    paused = False
    try:
        ctx['pause_controller'](peer)
        paused = True
        # Re-claim the field for the launch owner: a tracking peer's
        # promotion runs the unconditional claim, preempting the
        # successor's standing claim — the configured source earns
        # its owner checkpoints back before the pin dies.
        promoted, last = None, None
        deadline = time.monotonic() + FALLBACK_SETTLE
        while promoted is None and time.monotonic() < deadline:
            status, body = _settle_call(ctx[owner] + '/promote')
            if status == 200:
                promoted = body
            else:
                last = (status, body)
                time.sleep(FALLBACK_POLL)
        record['promote'] = {'status': 200 if promoted is not None
                             else last[0] if last else None,
                             'body': promoted if promoted is not None
                             else last[1] if last else None}
        record['owner_active'] = wait_for(
            lambda: (_try_role(ctx, ctx[owner]) or {})
            .get('role') == 'active'
            and _try_role(ctx, ctx[owner]) or None,
            time.monotonic() + FALLBACK_SETTLE,
            interval=FALLBACK_POLL)
        if promoted is None or record['owner_active'] is None:
            return record, evidence
        # Kill the pinned successor outright — the learned pin now
        # names a dead endpoint the produced-nothing pulls must out-
        # live inside the miss budget.
        ctx['stop_driven']()
        record['killed'] = True
        kill_floor = len(_journal_entries(ctx['journal_files'][peer]))
    finally:
        if paused:
            try:
                ctx['unpause_controller'](peer)
            except Exception:
                pass

    # The fallback: the resumed peer's pulls meet the dead source,
    # the miss run reaches the budget, the pin releases, and the
    # same-cycle re-resolution re-proves the configured endpoint —
    # journaled by name — ahead of the tracking verdict it earns.
    reads = 0
    fallback, reconverged, last_role = [], None, None
    deadline = time.monotonic() + FALLBACK_SETTLE
    while time.monotonic() < deadline:
        fallback = _adoptions_naming(
            ctx, peer, kill_floor, PAIR_PORTS['active'])
        report = _try_role(ctx, ctx[peer])
        if report is not None:
            reads += 1
            last_role = report
            sync = report.get('sync') or {}
            if report.get('role') == 'standby' \
                    and 'tracking' in sync and fallback:
                reconverged = report
                break
            if report.get('role') == 'standby' \
                    and 'tracking' in sync:
                reconverged = report
        time.sleep(FALLBACK_POLL)
    record['reads'] = reads
    record['last_role'] = last_role
    record['fallback_journal'] = fallback
    record['reconverged'] = reconverged
    entries = _journal_entries(ctx['journal_files'][peer])
    record['boundaries'] = {
        'before': sum(1 for item in entries[:floors[peer]]
                      if 'run_boundary' in item),
        'after': sum(1 for item in entries
                     if 'run_boundary' in item)}

    # The launch roles for the next pass and the cases behind this
    # one: the owner active with the peer tracking behind it.
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer) is not None,
        time.monotonic() + FALLBACK_SETTLE, interval=FALLBACK_POLL)
    return record, evidence


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore on the exercised pair: the
    documented switchover order run again — the named owner promoted
    back over the field with its sibling tracking behind it. Every
    step is retried inside the bound and swallowed on refusal."""
    try:
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + FALLBACK_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(FALLBACK_POLL)
    except Exception:
        pass


def scenario_tracking_source_fallback(ctx):
    """Exercise the dead-successor tracking-source fallback contract
    on the run's keyed pair: with the pair settled and tracking,
    island the demoted pair so the driven third controller's promote
    pins the standby onto it through the orphan-resolution probe —
    the journaled tracking_source_adopted naming :8082 — then freeze
    the standby, promote the launch owner back over the field, kill
    the pinned container, and thaw: the produced-nothing pulls must
    release the learned pin inside the budget, the same-cycle
    re-resolution must journal its retarget onto the configured
    :8080 endpoint by name, the served sync must reconverge tracking,
    and no run boundary may open — no restart. The subject is the
    deployed pair while the run config keys it, else the lane-staged
    keyed probe pair."""
    case = Case(
        'tracking-source-fallback',
        'Dead learned tracking source falls back to the configured '
        'source',
        'with the keyed pair settled — the deployed pair, or the '
        'lane-staged probe pair while the deployed pair runs '
        'unkeyed — each pass drives the standby\'s pull source onto '
        'a successor pin through the same demote/announce/'
        'orphan-resolution path the adoption legs stage (the island '
        'demote, the driven peer\'s field claim, the journaled '
        'tracking_source_adopted naming :8082), then freezes the '
        'standby, re-claims the field for the launch owner, and '
        'removes the pinned container so the resumed pulls meet a '
        'dead source; the produced-nothing miss run must release the '
        'pin inside the budget, re-resolve onto the configured '
        ':8080 endpoint journaled by name, reconverge the served '
        'sync to a tracking verdict inside one process lifetime, '
        'and restore the pair\'s launch roles; two passes produce '
        'identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the fallback leg needs is absent')
        # The keyed subject: the deployed pair while the run config
        # keys it, else the lane-staged probe pair — the learned pin
        # the leg kills is keyed-only machinery, so an unkeyed
        # deployment with no staged probe pair is inconclusive on
        # capability, not on the contract.
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no keyed '
                               'probe pair is staged — the learned '
                               'tracking-source pin the leg kills is '
                               'off')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed probe '
                         'pair — the deployed pair runs unkeyed')
            ctx = subject
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the keyed subject '
                               'carries only one endpoint — the pair '
                               'the leg needs is absent')
        for action in ('start_driven', 'stop_driven',
                       'pause_controller', 'unpause_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the successor staging and the '
                                   'frozen-pull induction cannot be '
                                   'driven')
        if ctx.get('driven') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven endpoint — the '
                               'successor has no monitor')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the named fallback evidence '
                               'audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the passes stage from: the unconfigured
        # peer — launched without a tracking source — must hold the
        # field so its demote owes the announced adoption the island
        # forms on; a swapped layout is restored before the leg
        # reports.
        deadline = time.monotonic() + FALLBACK_SETTLE
        if _pair_active(ctx) != 'active':
            _restore_layout(ctx, 'active', 'standby')
        owner = wait_for(lambda: _pair_active(ctx) == 'active'
                         and 'active' or None, deadline,
                         interval=FALLBACK_POLL)
        if owner != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — the '
                               'unconfigured peer must hold the '
                               'field for the announced-source '
                               'island the leg induces')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=FALLBACK_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg restores to was never reached')
        # The contract surface: the field owner's served checkpoint
        # must carry the ownership stamps — source_owns_field and
        # line_owner — the pin machinery reads; a run predating the
        # contract cannot stage the leg.
        try:
            _, checkpoint = http_json('GET', ctx[owner]
                                      + '/checkpoint')
        except Exception as exc:
            return case.finish('inconclusive', 'the field owner\'s '
                               'checkpoint never answered: '
                               + str(exc)[:200])
        if not isinstance(checkpoint, dict) \
                or 'source_owns_field' not in checkpoint \
                or 'line_owner' not in checkpoint:
            return case.finish('inconclusive', 'the served '
                               'checkpoint carries no '
                               'source_owns_field/line_owner stamps '
                               '— the staged run predates the '
                               'tracking-source fallback contract')
        owner, peer = 'active', 'standby'
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); pinned tracking peer: ' + peer)

        digests = []
        driven_up = False
        try:
            for number in (1, 2):
                try:
                    launched = ctx['start_driven'](owner)
                    driven_up = True
                except Exception as exc:
                    return case.finish('inconclusive',
                                       'the driven-peer launch '
                                       'never completed: '
                                       + str(exc)[:300])
                case.observe('driven peer up: '
                             + str((launched or {}).get('container')))
                if wait_for(lambda: _try_role(ctx, ctx['driven']),
                            time.monotonic() + FALLBACK_SETTLE,
                            interval=FALLBACK_POLL) is None:
                    return case.finish('inconclusive', 'the driven '
                                       'monitor never answered '
                                       '/role')
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _fallback_pass(
                    ctx, number, owner, peer)
                driven_up = record.get('killed') is not True
                _judge_fallback(record, note)
                digest = _digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'tracking-source-fallback-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'tracking-source-fallback '
                              'pass ' + str(number) + ' — the driven '
                              'convergence, the island demote, the '
                              'successor claim, the journaled '
                              'successor pin, the frozen kill '
                              'window, the named fallback retarget '
                              'onto the configured endpoint, the '
                              'reconverged verdict, the run-boundary '
                              'count, the restore, and the '
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
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted pass
            # gets the peer thawed, the successor removed, and the
            # documented role order run again, best-effort.
            try:
                ctx['unpause_controller'](peer)
            except Exception:
                pass
            if driven_up:
                try:
                    ctx['stop_driven']()
                except Exception:
                    pass
            _restore_layout(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two dead-successor passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the fallback judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
