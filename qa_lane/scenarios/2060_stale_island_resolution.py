"""The stale_island_resolution acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg shares the launch-layout window behind the
# announced-source legs — it needs the settled tracking pair, launches
# the run's driven third controller on it, induces the demoted-pair
# island per pass, and restores the launch roles before the tune
# case's a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The stale-island re-resolution contract — decision 91's landed #831
# behavior pinned as per-run lane evidence on the deployed rig for
# WW-LCM-001's takeover-continuity clause and WW-FND-002's pair
# semantics (the workspace fixture
# crates/dcs-controller/tests/stale_island.rs pins the mechanism): a
# field owner that never configured a peer demotes onto a verified
# announced hint — the sibling standby, a checkpoint source that
# merely replays the demoted run's own line back to it — and the
# demoted pair then islands, each tracking a peer that owns nothing.
# The contract's two halves then apply on the live rig's real peer
# discovery, announces, and claim state: the orphan-resolution probe
# follows the propagated `line_owner` and the recorded announcers onto
# the endpoint that verifiably serves the line *as its field owner*
# (keyed `line_proof` on this run), so the islanded pair reconverges
# on the promoted third controller; and an islanded `POST /promote`
# runs the conditional orphan claim — refused `field_claim_failed`
# while the incumbent's live unyielded controller claim stands, never
# preempting it.
#
# The leg launches the run's driven third controller through
# ctx['start_driven'] — `--standby <owner> --driven`, its checkpoint
# pulls and scans gated on POST /scan — converges it onto the field
# owner, then per pass demotes the owner so the demoted ex-owner and
# the sibling standby resolve onto each other while the driven peer's
# promote takes the field. The islanded sibling's promote must answer
# the named `field_claim_failed` verdict, the orphan probes must
# re-resolve both islanded peers onto the live owner (the journaled
# tracking_source_adopted plus the propagated line_owner the served
# checkpoints carry are the evidence), and the three must settle to
# exactly one active plus tracking standbys — no mutual-standby
# remnants. The pair's entry roles restore before the next pass and
# the teardown removes the driven peer. Named diagnostics
# island-resolution-failed for a contract miss and
# island-resolution-nondeterministic when the passes disagree or the
# rig reports an impossible state; inconclusive when the rig is
# unreachable, the third-controller seam is absent, the run is
# unkeyed, or the served surface predates the contract.

ISLAND_SETTLE = 45     # bound on each switch, the re-resolution, and
                       # the role restore
ISLAND_FORM = 20       # bound on the island's orphaned verdicts — the
                       # sibling's failover budget (~12s at 120 misses
                       # on the 100 ms cadence) never reaches inside it
ISLAND_POLL = 0.4      # wait cadence inside the leg
DRIVEN_SCANS = 4       # driven scans per batch — the announce pulls the
                       # keyed probe needs plus convergence margin
DRIVEN_PORT = 8082     # the driven peer's --listen port inside the rig
PAIR_PORTS = {'active': 8080, 'standby': 8081}


def _orphaned(ctx, name):
    """The endpoint's /role report while it is a standby reporting the
    ownerless-line verdict — the island member's state — else None."""
    report = _try_role(ctx, ctx[name])
    sync = (report or {}).get('sync')
    if (report or {}).get('role') == 'standby' \
            and isinstance(sync, dict) and 'orphaned' in sync:
        return report
    return None


def _tracking(report):
    """Whether a /role report shows a tracking standby — the
    reconverged posture the re-resolution owes."""
    return (report or {}).get('role') == 'standby' \
        and 'tracking' in ((report or {}).get('sync') or {})


def _drive(ctx, scans=DRIVEN_SCANS):
    """One POST /scan batch on the driven peer — every pull it ever
    performs happens inside the request."""
    return http_json('POST', ctx['driven'] + '/scan',
                     {'scans': scans}, timeout=scans * 2 + 15)


def _journaled(ctx, name, floor, kind):
    """The event bodies of `kind` the peer's served journal carries
    since `floor` — the durable evidence the demotion's adoption and
    the orphan transitions owe."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return []
    out = []
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _checkpoint_owner(ctx, name):
    """The line_owner stamp the peer's served checkpoint propagates —
    the line's own word for its owner — or None when the read drops."""
    try:
        _, doc = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    return (doc or {}).get('line_owner')


def _island_pass(ctx, number, owner):
    """One stale-island pass: converge the driven third peer on the
    field owner, demote the owner so the demoted pair islands on each
    other, promote the driven peer so its live claim stands while an
    islanded promote refuses, then watch the islanded pair re-resolve
    onto the real owner and restore the pair's entry roles. Returns
    (digest, violations, evidence): digest is the pass's normalized
    verdict record, identical across clean passes; violations is
    {key: (diagnostic, detail)} in first-seen order."""
    violations = {}
    evidence = {'entry_owner': owner, 'pass': number}
    digest = {'adopted': 'none', 'island': 'absent',
              'islanded_promote': 'none', 'resolution': 'wedged',
              'roles': 'unrestored'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'island-resolution-failed', detail)

    peer = 'standby' if owner == 'active' else 'active'
    evidence.update({'owner': owner, 'peer': peer})

    # The settled gate: the entry owner holds the field and the other
    # launched peer tracks it — the layout the pass's restore owes.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # Converge the driven peer: each batched scan is a checkpoint pull
    # that announces its monitor on the owner — the recorded hint the
    # orphan probe later dials.
    converged = None
    deadline = time.monotonic() + ISLAND_SETTLE
    while converged is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception as exc:
            failed('driven-scan', 'the driven peer\'s /scan batch '
                   'never answered: ' + str(exc)[:200])
            return None, violations, evidence
        converged = _tracking_standby(ctx, 'driven')
        if converged is None:
            time.sleep(ISLAND_POLL)
    evidence['driven_converged'] = converged
    if converged is None:
        failed('driven-converge', 'the driven peer never reported a '
               'tracking standby on ' + owner)
        return None, violations, evidence

    floors = {}
    for name in (owner, peer, 'driven'):
        floors[name] = _journal_cursor(ctx, ctx[name])

    # Induce the island: demote the field owner — the announced-source
    # verify adopts the newest tracking hint, the sibling standby.
    demote_status, demote = _settle_call(ctx[owner] + '/demote')
    evidence['demote'] = {'status': demote_status, 'body': demote}
    if demote_status != 200:
        failed('demote', 'POST /demote on the field owner answered '
               + str(demote_status) + ' ' + json.dumps(demote)[:200])
        return None, violations, evidence
    adoptions = _journaled(ctx, owner, floors[owner],
                           'tracking_source_adopted')
    evidence['adoptions'] = adoptions
    if owner == 'active':
        # ctrl-a configured no tracking source: its demotion owes the
        # journaled announced-source adoption — and the island the
        # leg induces needs it to name the sibling standby's port,
        # not the driven peer's.
        sources = [body.get('source') for body in adoptions]
        if len(sources) != 1:
            failed('adoption', 'the demotion journaled '
                   + str(len(sources)) + ' tracking-source adoptions '
                   'instead of one naming the sibling standby')
            return None, violations, evidence
        if not str(sources[0]).endswith(':' + str(PAIR_PORTS[peer])):
            failed('adoption', 'the demotion adopted '
                   + str(sources[0]) + ' instead of the sibling '
                   'standby (:' + str(PAIR_PORTS[peer])
                   + ') — the demoted-pair island never formed')
            return None, violations, evidence
        digest['adopted'] = 'sibling'
    else:
        # ctrl-b's configured --standby covers its demotion — no
        # adoption is owed.
        digest['adopted'] = 'configured' if not adoptions else 'sibling'

    # The island forms: the demoted owner tracks the sibling and the
    # sibling tracks it back — neither serves the line as its owner,
    # so both report the orphaned verdict and journal the transition.
    def islanded():
        pair = {name: _orphaned(ctx, name) for name in (owner, peer)}
        return pair if all(pair.values()) else None

    island = wait_for(islanded, time.monotonic() + ISLAND_FORM,
                      interval=ISLAND_POLL)
    evidence['island'] = island
    if island is None:
        failed('island', 'the demoted pair never reported the '
               'orphaned verdict on each other — the island never '
               'formed')
        return None, violations, evidence
    digest['island'] = 'formed'
    orphans = {name: _journaled(ctx, name, floors[name],
                                'field_orphaned')
               for name in (owner, peer)}
    evidence['field_orphaned'] = orphans
    for name in (owner, peer):
        if not orphans[name]:
            failed('orphan-journal', name + ' reported orphaned but '
                   'never journaled field_orphaned — the durable '
                   'evidence the island owes is absent')

    # The driven peer observes the ownerless line, then takes the
    # field: its promotion's claim preempts the demoted owner's
    # yielded claim. The claim lands inside the promote request, so
    # the islanded refusal below is exercised while the incumbent's
    # live unyielded claim already stands — the driven peer stays
    # 'promoting' until its next scan.
    try:
        _drive(ctx, 1)
    except Exception as exc:
        failed('driven-scan', 'the driven peer\'s island-observing '
               'scan never answered: ' + str(exc)[:200])
        return None, violations, evidence
    evidence['driven_islanded'] = _orphaned(ctx, 'driven')
    promote_status, promoted = _settle_call(ctx['driven'] + '/promote')
    evidence['driven_promote'] = {'status': promote_status,
                                  'body': promoted}
    if promote_status != 200:
        failed('driven-promote', 'POST /promote on the driven peer '
               'answered ' + str(promote_status) + ' '
               + json.dumps(promoted)[:200])
        return None, violations, evidence

    # The islanded promote refusal: the sibling's conditional orphan
    # claim meets the incumbent's live unyielded controller claim —
    # the named field_claim_failed, never a preemption.
    islanded_report = _orphaned(ctx, peer)
    evidence['islanded_at_refusal'] = islanded_report
    refuse_status, refused = _settle_call(ctx[peer] + '/promote')
    evidence['islanded_promote'] = {'status': refuse_status,
                                    'body': refused}
    named = refused == 'field_claim_failed' or (
        isinstance(refused, dict) and 'field_claim_failed' in refused)
    if islanded_report is None:
        note('islanded-window', 'island-resolution-nondeterministic',
             'the islanded sibling re-resolved before its promote '
             'probe landed — the refusal leg never ran')
    if refuse_status == 409 and named:
        digest['islanded_promote'] = 'field_claim_failed'
    else:
        failed('islanded-promote', 'POST /promote on the islanded '
               + peer + ' answered ' + str(refuse_status) + ' '
               + json.dumps(refused)[:200] + ' — not the named '
               'field_claim_failed refusal while a live incumbent '
               'holds the unyielded claim')
    incumbent = _try_role(ctx, ctx['driven'])
    evidence['incumbent'] = incumbent
    if (incumbent or {}).get('role') not in ('promoting', 'active'):
        failed('incumbent', 'the incumbent lost its promoting role '
               'on the islanded promote: ' + json.dumps(incumbent)[:200])

    # Re-resolution: the demoted peer's orphan probe dials the
    # recorded announcer now serving the line as its owner; the
    # sibling follows the line_owner the demoted peer's checkpoints
    # propagate. Drive the new owner so its scans keep the line
    # advancing, then wait for one active plus two tracking standbys.
    resolved = None
    reports = {}
    deadline = time.monotonic() + ISLAND_SETTLE
    while resolved is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception:
            pass
        reports = {name: _try_role(ctx, ctx[name])
                   for name in (owner, peer, 'driven')}
        if (reports['driven'] or {}).get('role') == 'active' \
                and _tracking(reports[owner]) \
                and _tracking(reports[peer]):
            resolved = reports
        else:
            time.sleep(ISLAND_POLL)
    evidence['resolved'] = resolved
    if resolved is None:
        failed('resolution', 'the islanded pair never reconverged '
               'onto the live driven owner: '
               + json.dumps(reports)[:300])
        return None, violations, evidence
    digest['resolution'] = 'resolved'
    actives = [name for name, report in resolved.items()
               if (report or {}).get('role') == 'active']
    if actives != ['driven']:
        note('dual-active', 'island-resolution-nondeterministic',
             'the settle reports ' + json.dumps(sorted(actives))
             + ' owning the field — the island left more than one '
             'active')
    for name in (owner, peer):
        sync = (resolved.get(name) or {}).get('sync')
        if isinstance(sync, dict) and 'orphaned' in sync:
            note('remnant-' + name,
                 'island-resolution-nondeterministic',
                 name + ' still reports orphaned after the settle — '
                 'a mutual-standby remnant the re-resolution was '
                 'meant to clear')
    # The propagated ownership evidence: each reconverged peer's
    # checkpoint names the driven peer's listen port as the line's
    # owner.
    owners = {name: _checkpoint_owner(ctx, name)
              for name in (owner, peer, 'driven')}
    evidence['line_owner'] = owners
    for name in (owner, peer):
        if not str(owners.get(name) or '').endswith(':'
                                                   + str(DRIVEN_PORT)):
            failed('line-owner', name + ' propagates line_owner '
                   + json.dumps(owners.get(name)) + ' — not the live '
                   'owner\'s :' + str(DRIVEN_PORT))

    # Restore the pair's entry roles: demote the driven owner onto its
    # configured --standby source (the entry owner), re-promote the
    # owner over the yielded claim, and reconverge every peer — the
    # driven peer's pulls again inside its scans.
    restore_status, restore = _settle_call(ctx['driven'] + '/demote')
    evidence['restore_demote'] = {'status': restore_status,
                                  'body': restore}
    if restore_status != 200:
        failed('restore', 'POST /demote on the driven owner answered '
               + str(restore_status) + ' ' + json.dumps(restore)[:200])
        return None, violations, evidence
    promoted_back, last = None, None
    deadline = time.monotonic() + ISLAND_SETTLE
    while promoted_back is None and time.monotonic() < deadline:
        promote_status, body = _settle_call(ctx[owner] + '/promote')
        if promote_status == 200:
            promoted_back = body
        else:
            last = (promote_status, body)
            time.sleep(ISLAND_POLL)
    evidence['restore_promote'] = {'body': promoted_back,
                                   'last_refusal': last}
    if promoted_back is None:
        failed('restore', 'the entry owner\'s promote never '
               'succeeded: ' + json.dumps(last)[:300])
        return None, violations, evidence
    restored = None
    deadline = time.monotonic() + ISLAND_SETTLE
    while restored is None and time.monotonic() < deadline:
        try:
            _drive(ctx, 2)
        except Exception:
            pass
        reports = {name: _try_role(ctx, ctx[name])
                   for name in (owner, peer, 'driven')}
        if (reports[owner] or {}).get('role') == 'active' \
                and _tracking(reports[peer]) \
                and _tracking(reports['driven']):
            restored = reports
        else:
            time.sleep(ISLAND_POLL)
    evidence['restored'] = restored
    if restored is None:
        failed('restore', 'the rig never settled back to the entry '
               'role layout: ' + json.dumps(reports)[:300])
    else:
        digest['roles'] = 'restored'
    evidence['digest'] = dict(digest)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    return digest, violations, evidence


def scenario_stale_island_resolution(ctx):
    """Exercise decision 91's stale-island contract on the deployed
    rig: launch the driven third controller tracking the field owner,
    demote the owner so the demoted pair islands on each other, then
    promote the driven peer — the islanded sibling's promote must
    refuse field_claim_failed against the live incumbent claim, and
    the islanded pair must re-resolve onto the real owner through the
    propagated line_owner and the keyed owner-verify probes."""
    case = Case(
        'stale-island-resolution',
        'Demoted-pair island re-resolves onto the promoted third '
        'controller',
        'with the deployed pair settled and the run keyed, launch the '
        'driven third controller tracking the field owner, demote the '
        'owner so the demoted ex-owner and the sibling standby report '
        'the orphaned verdict tracking each other — the journaled '
        'tracking_source_adopted and field_orphaned entries their '
        'evidence — then promote the driven peer onto the field; an '
        'islanded POST /promote answers the named field_claim_failed '
        'while the incumbent\'s live unyielded claim stands, the '
        'islanded peers\' orphan probes verify and adopt the live '
        'owner the served checkpoints name through line_owner, and '
        'the three settle to exactly one active plus tracking '
        'standbys with no mutual-standby remnants; the pair\'s entry '
        'roles restore, and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the island leg needs is absent')
        for action in ('start_driven', 'stop_driven'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the third-controller launch '
                                   'the island needs is absent')
        if ctx.get('driven') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven endpoint — the '
                               'third controller has no monitor')
        if not ctx.get('pair_token'):
            return case.finish('inconclusive', 'the run carries no '
                               '--pair-token — the keyed '
                               'announced-source and orphan-probe '
                               'contract the leg exercises is off')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + ISLAND_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ISLAND_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ISLAND_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg induces the island from was '
                               'never reached')
        # The contract surface: the field owner's served checkpoint
        # must carry the ownership stamps — source_owns_field and
        # line_owner — the island's probes read; a rig predating the
        # contract cannot run the leg.
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
                               '— the rig predates the stale-island '
                               'contract')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer)
        try:
            launched = ctx['start_driven'](owner)
        except Exception as exc:
            return case.finish('inconclusive', 'the driven-peer '
                               'launch never completed: '
                               + str(exc)[:300])
        case.observe('driven peer up: '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, ctx['driven']),
                    time.monotonic() + ISLAND_SETTLE,
                    interval=ISLAND_POLL) is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _island_pass(
                    ctx, number, owner)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'stale-island-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'stale-island pass '
                              + str(number) + ' — the driven '
                              'convergence, the demote\'s journaled '
                              'adoption, the island\'s orphaned '
                              'verdicts, the islanded promote\'s '
                              'refusal, the re-resolution onto the '
                              'live owner, the restore, and the '
                              'normalized digest')
                if violations or digest is None:
                    diagnostic = 'island-resolution-failed' \
                        if digest is None or any(
                            name == 'island-resolution-failed'
                            for name, _ in violations.values()) \
                        else 'island-resolution-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted pass
            # gets the documented role order run again, best-effort —
            # demote whichever peer still owns the field (the driven
            # peer included), promote the entry owner — then the
            # driven container is removed.
            try:
                reports = {name: _try_role(ctx, ctx[name])
                           for name in (owner, peer, 'driven')}
                for name, report in reports.items():
                    if name != owner \
                            and (report or {}).get('role') == 'active':
                        _settle_call(ctx[name] + '/demote')
                deadline = time.monotonic() + ISLAND_SETTLE
                while time.monotonic() < deadline:
                    try:
                        _drive(ctx, 2)
                    except Exception:
                        pass
                    if _pair_active(ctx) == owner \
                            and _tracking_standby(ctx, peer) \
                            is not None:
                        break
                    if (_try_role(ctx, ctx[owner]) or {}) \
                            .get('role') == 'standby':
                        # A mid-transition refusal heals on retry.
                        _settle_call(ctx[owner] + '/promote')
                    time.sleep(ISLAND_POLL)
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
            try:
                ctx['stop_driven']()
            except Exception:
                pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'island-resolution-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two stale-island passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
