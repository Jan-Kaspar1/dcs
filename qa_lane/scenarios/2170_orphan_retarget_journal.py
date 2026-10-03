"""The orphan_retarget_journal leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg closes the keyed announced-source cluster's
# island window — the demoted-pair island and the orphan-probe
# re-resolution it stages are the same machinery the announced-source
# and stale-island legs prove can form — and it restores the pair's
# launch roles for the failover case behind it.
RUNS_AFTER = frozenset({'scenario_orphan_episode_bound'})
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The journaled orphan-resolution pull-target switch — #1137's landed
# fix pinned as per-run lane evidence for WW-FND-004's named-evidence
# journal clause (the workspace fixture
# crates/dcs-monitor/tests/tracking.rs's
# an_orphaned_peers_resolved_pull_source_journals_the_adoption pins
# the mechanism): an orphaned standby's resolution probe re-targets
# its pulls through the `resolved` slot — the same pull-target
# authority the announced- and claimed-source adoptions carry,
# outranking the configured tracking source — so the switch must
# journal a `tracking_source_adopted` record naming the verified
# owner, never a silent re-point. Before the fix the switch landed
# unjournaled: `field_orphaned` bracketed it and nothing attributed
# when or where the pulls moved.
#
# The leg stages the island the announced-source and stale-island
# legs already run: the run's driven third controller converges on
# the field owner through ctx['start_driven'], the owner demotes onto
# the sibling standby's verified announced hint — the journaled
# :8081 adoption being the parity baseline the re-target is compared
# against — and the demoted pair islands, each tracking a peer that
# owns nothing. The driven peer then promotes over the yielded
# claim, and each islanded peer's orphan-resolution probe follows
# the propagated line_owner, the recorded announcers, and the
# field's claim verdict onto the endpoint that verifiably serves the
# line as its field owner — the resolved pin that used to land
# silent. The durable --journal-file is the audit surface: each
# islanded peer must carry a tracking_source_adopted naming the
# driven peer's :8082 listener, landing after the field_orphaned
# transition that explains why the pulls moved — the same named,
# attributed record the announced and claimed paths produce — and
# the peers must reconverge onto the live owner before the pair's
# launch roles restore. Named diagnostics retarget-journal-failed
# for a contract miss — the silent re-point, the misattributed or
# misordered record, the missing orphan bracket, the wedged
# reconvergence, the unrestored roles — and
# retarget-journal-nondeterministic when the passes disagree or the
# rig answers with instability instead of a verdict — a refused
# control-plane call, a driven peer that never converges, a
# dual-active settle — and retarget-journal-unchecked when the
# self-check's planted negatives slip the leg's own audits.
# Inconclusive when the rig is unreachable, the keyed subject or
# the third-controller seam is absent, or the served surface
# predates the contract.

RETARGET_SETTLE = 45   # bound on each switch, re-resolution, restore
RETARGET_FORM = 20     # bound on the island's orphaned verdicts —
                       # the sibling's failover budget (~12 s at 120
                       # misses on the 100 ms cadence) never reaches
                       # inside it
RETARGET_POLL = 0.4    # wait cadence inside the leg
DRIVEN_SCANS = 4       # driven scans per batch — the announce pulls
                       # the keyed probe needs plus convergence margin
DRIVEN_PORT = 8082     # the driven peer's --listen port inside the rig
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DIAG_FAILED = 'retarget-journal-failed'
DIAG_NONDET = 'retarget-journal-nondeterministic'
DIAG_UNCHECKED = 'retarget-journal-unchecked'


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


def _durable_records(ctx, name, floor):
    """The durable --journal-file's event records since `floor` items
    — [{'seq', 'kind', 'body'}] over the kinds the retarget audit
    reads: the tracking_source_adopted records the re-target owes and
    the field_orphaned transitions that bracket it. The durable file
    is the audit surface the named-evidence clause is asserted on —
    the served tail would show the same records one consumer later."""
    out = []
    for index, item in enumerate(
            _journal_entries(ctx['journal_files'][name])):
        if index < floor:
            continue
        entry = item.get('entry') or {}
        event = entry.get('event') or {}
        for kind in ('tracking_source_adopted', 'field_orphaned'):
            if isinstance(event.get(kind), dict):
                out.append({'seq': entry.get('seq', index + 1),
                            'kind': kind,
                            'body': event[kind]})
    return out


def _checkpoint_owner(ctx, name):
    """The line_owner stamp the peer's served checkpoint propagates —
    the line's own word for its owner — or None when the read drops."""
    try:
        _, doc = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    return (doc or {}).get('line_owner')


def _retarget_pass(ctx, number):
    """One orphan-retarget pass: converge the driven third peer on the
    field owner, demote the owner so the demoted pair islands on each
    other, promote the driven peer so the islanded peers' orphan
    probes re-target their pulls onto the live owner, then restore the
    pair's launch roles. Returns (record, evidence): the record is
    what the judge replays — raw staged facts only; an aborted stage
    simply leaves its later keys absent for the judge to name."""
    record = {}
    evidence = {'entry_owner': 'active', 'pass': number}
    owner, peer = 'active', 'standby'

    # The settled gate: the entry layout holds — the unconfigured
    # owner holds the field and the sibling tracks it.
    record['settled'] = wait_for(
        lambda: _pair_active(ctx) == owner
        and _tracking_standby(ctx, peer) or None,
        time.monotonic() + RETARGET_SETTLE, interval=RETARGET_POLL)
    if record['settled'] is None:
        return record, evidence

    # Converge the driven peer: each batched scan is a checkpoint pull
    # that announces its monitor on the owner — the recorded hint the
    # islanded peers' orphan probes later dial.
    converged = None
    deadline = time.monotonic() + RETARGET_SETTLE
    while converged is None and time.monotonic() < deadline:
        try:
            _drive(ctx)
        except Exception:
            pass
        converged = _tracking_standby(ctx, 'driven')
        if converged is None:
            time.sleep(RETARGET_POLL)
    record['driven'] = converged
    if converged is None:
        return record, evidence

    # The post-converge layout check — the sibling's paced pulls keep
    # its announce the freshest hint the demote's verify adopts, so
    # the island forms on the demoted pair, not the driven peer.
    record['settled'] = wait_for(
        lambda: _pair_active(ctx) == owner
        and _tracking_standby(ctx, peer) or None,
        time.monotonic() + RETARGET_SETTLE, interval=RETARGET_POLL)
    if record['settled'] is None:
        return record, evidence

    floors = {}
    for name in (owner, peer):
        floors[name] = len(_journal_entries(ctx['journal_files'][name]))

    # Induce the island: demote the unconfigured field owner — the
    # announced-source verify adopts the newest tracking hint, the
    # sibling standby, and journals it: the parity baseline the
    # re-target's record is compared against.
    status, body = _settle_call(ctx[owner] + '/demote')
    record['demote'] = {'status': status, 'body': body}
    record['adoptions'] = [item['body'] for item in
                           _durable_records(ctx, owner, floors[owner])
                           if item['kind'] == 'tracking_source_adopted']
    if status != 200:
        return record, evidence

    # The island forms: the demoted owner tracks the sibling and the
    # sibling tracks it back — neither serves the line as its owner,
    # so both report the orphaned verdict and journal the transition.
    def islanded():
        pair = {name: _orphaned(ctx, name) for name in (owner, peer)}
        return pair if all(pair.values()) else None

    record['island'] = wait_for(islanded,
                                time.monotonic() + RETARGET_FORM,
                                interval=RETARGET_POLL)
    if not isinstance(record['island'], dict):
        return record, evidence
    # The orphan transitions' durable records — the resolution
    # evidence the re-target's attribution record must land behind.
    def orphans(name):
        return [item['body'] for item in
                _durable_records(ctx, name, floors[name])
                if item['kind'] == 'field_orphaned']

    for name in (owner, peer):
        wait_for(lambda: orphans(name) or None,
                 time.monotonic() + RETARGET_SETTLE,
                 interval=RETARGET_POLL)
    record['orphans'] = {name: orphans(name)
                         for name in (owner, peer)}

    # The driven peer observes the ownerless line, then takes the
    # field: its promotion's claim preempts the demoted owner's
    # yielded claim.
    try:
        _drive(ctx, 1)
    except Exception:
        pass
    record['driven_islanded'] = _orphaned(ctx, 'driven')
    promote_status, promoted = _settle_call(ctx['driven'] + '/promote')
    record['driven_promote'] = {'status': promote_status,
                                'body': promoted}
    if promote_status != 200:
        return record, evidence

    # The re-resolution: each islanded peer's orphan probe verifies
    # the live owner and re-targets its pulls through the resolved
    # slot — the switch the durable journal must attribute. Drive the
    # new owner so its scans keep the line advancing, then wait for
    # one active plus two tracking standbys.
    resolved = None
    reports = {}
    deadline = time.monotonic() + RETARGET_SETTLE
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
            time.sleep(RETARGET_POLL)
    record['resolved'] = resolved
    # The durable trail since the pass's floors — every journaled
    # adoption and orphan transition the re-resolution left: the
    # audit surface the attribution clauses read.
    record['trail'] = {name: _durable_records(ctx, name, floors[name])
                       for name in (owner, peer)}
    record['line_owner'] = {name: _checkpoint_owner(ctx, name)
                            for name in (owner, peer, 'driven')}
    if resolved is None:
        return record, evidence

    # Restore the pair's entry roles: demote the driven owner onto its
    # configured --standby source (the entry owner), re-promote the
    # owner over the yielded claim, and reconverge every peer — the
    # driven peer's pulls again inside its scans.
    restore_status, restore = _settle_call(ctx['driven'] + '/demote')
    record['restore_demote'] = {'status': restore_status,
                                'body': restore}
    if restore_status != 200:
        return record, evidence
    promoted_back, last = None, None
    deadline = time.monotonic() + RETARGET_SETTLE
    while promoted_back is None and time.monotonic() < deadline:
        promote_status, body = _settle_call(ctx[owner] + '/promote')
        if promote_status == 200:
            promoted_back = body
        else:
            last = (promote_status, body)
            time.sleep(RETARGET_POLL)
    record['restore_promote'] = {'body': promoted_back,
                                 'last_refusal': last}
    if promoted_back is None:
        return record, evidence
    restored = None
    deadline = time.monotonic() + RETARGET_SETTLE
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
            time.sleep(RETARGET_POLL)
    record['restored'] = restored
    return record, evidence


def _judge_retarget(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    named-evidence contract clauses — the island's orphaned verdicts
    and their journaled transitions, the re-target's attributed
    tracking_source_adopted naming the live owner landing after the
    orphan record that explains it, the reconvergence onto the
    driven owner and its propagated line_owner, the restored launch
    roles — and DIAG_NONDET tags the instability the contract does
    not answer for: a driven peer that never converged, refused
    control-plane calls that leave the staging short, a parity
    baseline the demotion never journaled, a dual-active settle. An
    aborted stage ends the audit where the pass ended — the later
    keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('settled') is None:
        nondet('settled', 'the pair never held its launch layout — '
               'the unconfigured owner and its tracking sibling the '
               'island stages from were never both reported')
        return
    if not isinstance(record.get('driven'), dict):
        nondet('driven', 'the driven peer never reported a tracking '
               'standby — the live owner the re-target resolves '
               'onto never staged')
        return
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        nondet('demote', 'the island-inducing POST /demote answered '
               + str(demote.get('status')) + ' '
               + json.dumps(demote.get('body'))[:160] + ' — the '
               'staging never opened an orphan episode')
        return
    adoptions = record.get('adoptions')
    if not isinstance(adoptions, list) or len(adoptions) != 1 \
            or not str((adoptions[0] or {}).get('source', '')) \
            .endswith(':' + str(PAIR_PORTS['standby'])):
        nondet('adoption', 'the demotion journaled '
               + json.dumps(adoptions)[:200] + ' instead of one '
               'tracking-source adoption naming the sibling standby '
               'on :' + str(PAIR_PORTS['standby']) + ' — the '
               'announced-path parity baseline the audit compares '
               'the re-target against is absent')
    if not isinstance(record.get('island'), dict):
        failed('island', 'the demoted pair never reported the '
               'orphaned verdict on each other — the island never '
               'formed')
        return
    orphans = record.get('orphans') or {}
    for name in ('active', 'standby'):
        if not orphans.get(name):
            failed('orphan-' + name, name + ' reported orphaned but '
                   'never journaled field_orphaned — the resolution '
                   'evidence the durable trail owes beside the '
                   're-target is absent')
    promote = record.get('driven_promote') or {}
    if promote.get('status') != 200:
        nondet('driven-promote', 'POST /promote on the driven peer '
               'answered ' + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160] + ' — the '
               'live owner the orphan probes re-target onto never '
               'took the field')
        return
    # The attribution clause itself: each islanded peer's durable
    # trail must carry a tracking_source_adopted naming the driven
    # peer's listen port — the same named record the announced and
    # claimed paths journal for an identical pull-target switch —
    # landing after the field_orphaned transition that says why the
    # pulls moved.
    trail = record.get('trail') or {}
    for name in ('active', 'standby'):
        entries = trail.get(name) or []
        retargets = [item for item in entries
                     if item.get('kind') == 'tracking_source_adopted'
                     and str((item.get('body') or {})
                             .get('source', ''))
                     .endswith(':' + str(DRIVEN_PORT))]
        if not retargets:
            failed('retarget-' + name, name + '\'s durable journal '
                   'carries no tracking_source_adopted naming the '
                   'driven owner\'s :' + str(DRIVEN_PORT) + ' '
                   'listener — the orphan-resolution re-target of '
                   'the pull source landed unjournaled, the silent '
                   're-point the contract forbids')
            continue
        orphan_seqs = [item.get('seq') for item in entries
                       if item.get('kind') == 'field_orphaned'
                       and isinstance(item.get('seq'), int)]
        first = retargets[0]
        if orphan_seqs and isinstance(first.get('seq'), int) \
                and first['seq'] <= max(orphan_seqs):
            failed('retarget-order-' + name, name + ' journaled the '
                   're-target at seq ' + str(first['seq']) + ' ahead '
                   'of the field_orphaned transition at seq '
                   + str(max(orphan_seqs)) + ' — the durable trail '
                   'cannot attribute why the pulls moved')
    resolved = record.get('resolved')
    if not isinstance(resolved, dict):
        failed('resolution', 'the islanded pair never reconverged '
               'onto the live driven owner: '
               + json.dumps(record.get('resolved'))[:200])
        return
    actives = [name for name, report in resolved.items()
               if (report or {}).get('role') == 'active']
    if actives != ['driven']:
        nondet('dual-active', 'the settle reports '
               + json.dumps(sorted(actives)) + ' owning the field — '
               'the re-resolution left more than one active')
    for name in ('active', 'standby'):
        sync = (resolved.get(name) or {}).get('sync')
        if not isinstance(sync, dict) or 'tracking' not in sync:
            failed('reconverge-' + name, name + ' never reported a '
                   'tracking standby after the re-target: '
                   + json.dumps(resolved.get(name))[:200])
    owners = record.get('line_owner') or {}
    for name in ('active', 'standby'):
        if not str(owners.get(name) or '') \
                .endswith(':' + str(DRIVEN_PORT)):
            failed('line-owner-' + name, name + ' propagates '
                   'line_owner ' + json.dumps(owners.get(name))
                   + ' — not the live owner\'s :' + str(DRIVEN_PORT)
                   + ' the re-targeted pulls verified')
    demote_back = record.get('restore_demote') or {}
    if demote_back.get('status') != 200:
        nondet('restore-demote', 'POST /demote on the driven owner '
               'answered ' + str(demote_back.get('status')) + ' '
               + json.dumps(demote_back.get('body'))[:160] + ' — '
               'the launch-role restore never started')
        return
    if record.get('restore_promote', {}).get('body') is None:
        nondet('restore-promote', 'the entry owner\'s promote never '
               'succeeded: '
               + json.dumps((record.get('restore_promote') or {})
                            .get('last_refusal'))[:200])
        return
    if not isinstance(record.get('restored'), dict):
        failed('restored', 'the pair never settled back to its '
               'launch roles — the layout the cases behind this one '
               'meet is unrestored')


def _retarget_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'settled': 'held' if clean('settled') else 'unsettled',
        'driven': 'tracking' if clean('driven') else 'absent',
        'adopted': 'sibling'
            if clean('demote', 'adoption') else 'none',
        'island': 'formed' if clean('island') else 'absent',
        'orphans': 'journaled'
            if clean('orphan-active', 'orphan-standby')
            else 'silent',
        'retarget': 'attributed'
            if clean('retarget-active', 'retarget-standby',
                     'retarget-order-active',
                     'retarget-order-standby') else 'silent',
        'resolution': 'reconverged'
            if clean('resolution', 'reconverge-active',
                     'reconverge-standby', 'dual-active')
            else 'wedged',
        'line_owner': 'propagated'
            if clean('line-owner-active', 'line-owner-standby')
            else 'stale',
        'roles': 'restored'
            if clean('restore-demote', 'restore-promote', 'restored')
            else 'unrestored'}


def _retarget_self_check():
    """The leg's unchecked-diagnostic self-test: replay the retarget
    judge over each planted negative the issue names — the re-target
    asserted as journaled while the durable journal stays silent, the
    record naming the wrong source or landing ahead of the orphan
    transition it must follow, the missing orphan bracket, the wedged
    reconvergence, the unrestored roles — and require the judge to
    note each. A silent judge returns the negative names it let
    through."""
    slipped = []

    def clean_record():
        def trail(active_peer):
            # The owner's trail: the announced sibling adoption, the
            # orphan transition, then the resolved re-target — the
            # parity pair the audit compares. The standby's trail:
            # the orphan transition, then its own re-target.
            entries = [
                {'seq': 11, 'kind': 'field_orphaned',
                 'body': {'aligned': 140}},
                {'seq': 23, 'kind': 'tracking_source_adopted',
                 'body': {'source': 'ctrl-d:8082'}}]
            if active_peer:
                entries.insert(0, {'seq': 5,
                                   'kind': 'tracking_source_adopted',
                                   'body': {'source':
                                            'ctrl-b:8081'}})
            return entries
        return {'settled': {'role': 'standby',
                            'sync': {'tracking': {'aligned': 100}}},
                'driven': {'role': 'standby',
                           'sync': {'tracking': {'aligned': 120}}},
                'demote': {'status': 200,
                           'body': {'role': 'demoting'}},
                'adoptions': [{'source': 'ctrl-b:8081'}],
                'island': {'active': {'role': 'standby'},
                           'standby': {'role': 'standby'}},
                'orphans': {'active': [{'aligned': 140}],
                            'standby': [{'aligned': 140}]},
                'driven_promote': {'status': 200,
                                   'body': {'role': 'promoting'}},
                'resolved': {'driven': {'role': 'active'},
                             'active': {'role': 'standby',
                                        'sync': {'tracking':
                                                 {'aligned': 160}}},
                             'standby': {'role': 'standby',
                                         'sync': {'tracking':
                                                  {'aligned': 160}}}},
                'trail': {'active': trail(True),
                          'standby': trail(False)},
                'line_owner': {'active': 'ctrl-d:8082',
                               'standby': 'ctrl-d:8082',
                               'driven': 'ctrl-d:8082'},
                'restore_demote': {'status': 200,
                                   'body': {'role': 'demoting'}},
                'restore_promote': {'body': {'role': 'promoting'},
                                    'last_refusal': None},
                'restored': {'driven': {'role': 'standby'},
                             'active': {'role': 'active'},
                             'standby': {'role': 'standby'}}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_retarget(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: the re-target
    # asserted as journaled while the durable journal stays silent —
    # the resolved pair tracking while no attribution record exists.
    expect('retarget-silent', lambda record:
           record['trail']['standby'].pop())
    # ... and the same silence on the demoted owner's trail.
    expect('retarget-silent-owner', lambda record:
           record['trail'].update(
               {'active': [item for item in record['trail']['active']
                           if item['body'].get('source')
                           != 'ctrl-d:8082']}))
    # The journaled re-target names the wrong endpoint — the sibling
    # standby's port instead of the verified owner's.
    expect('retarget-misattributed', lambda record:
           record['trail']['standby'][-1]['body'].update(
               {'source': 'ctrl-a:8080'}))
    # The re-target journaled ahead of the orphan transition that
    # explains it — attribution without the why.
    expect('retarget-misordered', lambda record:
           record['trail']['standby'][-1].update({'seq': 9}))
    # The orphan transition never journaled — the resolution evidence
    # the re-target rides on is absent.
    expect('orphan-evidence-absent', lambda record:
           record.update({'orphans': {'active': [{'aligned': 140}],
                                      'standby': []}}))
    # The island's orphaned verdicts never reported.
    expect('island-absent', lambda record:
           record.update({'island': None}))
    # The peer never reconverged onto the re-targeted owner.
    expect('reconverge-absent', lambda record:
           record['resolved']['standby'].update(
               {'sync': {'orphaned': {'aligned': 150}}}))
    # The propagated line_owner keeps naming the dead source.
    expect('line-owner-stale', lambda record:
           record['line_owner'].update({'standby': 'ctrl-a:8080'}))
    # The launch roles never restored.
    expect('roles-unrestored', lambda record:
           record.update({'restored': None}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a driven peer that never
    # converged, refused control-plane calls, a parity baseline the
    # demotion never journaled, a dual-active settle.
    expect('settle-lost', lambda record:
           record.update({'settled': None}), DIAG_NONDET)
    expect('driven-unconverged', lambda record:
           record.update({'driven': None}), DIAG_NONDET)
    expect('demote-refused', lambda record:
           record.update({'demote': {'status': 409,
                                     'body': 'no_tracking_source'}}),
           DIAG_NONDET)
    expect('adoption-absent', lambda record:
           record.update({'adoptions': []}), DIAG_NONDET)
    expect('driven-promote-refused', lambda record:
           record.update({'driven_promote': {
               'status': 409,
               'body': {'field_claim_failed': {}}}}), DIAG_NONDET)
    expect('dual-active', lambda record:
           record['resolved']['standby'].update({'role': 'active'}),
           DIAG_NONDET)
    return slipped


def _retarget_restore_layout(ctx):
    """Best-effort launch-layout restore on the exercised pair while a
    pass aborts mid-island: demote whichever peer still owns the
    field — the driven peer included — promote the entry owner, and
    drive the driven peer's scans until the launch roles report back.
    Every step is retried inside the bound and swallowed on refusal."""
    try:
        reports = {name: _try_role(ctx, ctx[name])
                   for name in ('active', 'standby', 'driven')}
        for name, report in reports.items():
            if name != 'active' \
                    and (report or {}).get('role') == 'active':
                _settle_call(ctx[name] + '/demote')
        deadline = time.monotonic() + RETARGET_SETTLE
        while time.monotonic() < deadline:
            try:
                _drive(ctx, 2)
            except Exception:
                pass
            if _pair_active(ctx) == 'active' \
                    and _tracking_standby(ctx, 'standby') is not None:
                return
            if (_try_role(ctx, ctx['active']) or {}) \
                    .get('role') == 'standby':
                _settle_call(ctx['active'] + '/promote')
            time.sleep(RETARGET_POLL)
    except Exception:
        pass


def scenario_orphan_retarget_journal(ctx):
    """Exercise the journaled orphan-resolution pull-target contract
    on the run's keyed pair: launch the driven third controller
    tracking the field owner, demote the owner so the demoted pair
    islands on each other — the journaled announced-source adoption
    the parity baseline — then promote the driven peer; each
    islanded peer's orphan probe must re-target its pulls onto the
    live owner through the resolved slot, and the durable
    --journal-file must carry the switch as a named
    tracking_source_adopted record naming the driven peer's
    listener, landing after the field_orphaned transition that
    explains it, before the peer reconverges and the pair's launch
    roles restore. The subject is the deployed pair while the run
    config keys it, else the lane-staged keyed probe pair."""
    case = Case(
        'orphan-retarget-journal',
        'Orphan-resolution pull-source re-target journals the '
        'attributed adoption',
        'with the keyed pair settled — the deployed pair, or the '
        'lane-staged probe pair while the deployed pair runs '
        'unkeyed — launch the driven third controller tracking the '
        'field owner, demote the owner so the demoted ex-owner and '
        'the sibling standby report the orphaned verdict tracking '
        'each other — the demotion\'s announced-source adoption '
        'journaled as the parity baseline — then promote the '
        'driven peer onto the field; each islanded peer\'s '
        'orphan-resolution probe verifies and re-targets its pulls '
        'onto the live owner, and the durable journal must carry '
        'that switch as a tracking_source_adopted record naming '
        'the driven peer\'s listener after the field_orphaned '
        'transition — the same attributed record the announced and '
        'claimed paths produce, never a silent re-point — before '
        'the peers reconverge, the pair\'s entry roles restore, '
        'and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the retarget leg needs is absent')
        # The keyed subject: the deployed pair while the run config
        # keys it, else the lane-staged probe pair — the island's
        # announced-source demote is keyed-only, so an unkeyed
        # deployment with no staged probe pair is inconclusive on
        # capability, not on the contract.
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no keyed '
                               'probe pair is staged — the '
                               'announced-source island the leg '
                               'induces is off')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed '
                         'probe pair — the deployed pair runs '
                         'unkeyed')
            ctx = subject
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the keyed subject '
                               'carries only one endpoint — the pair '
                               'the leg needs is absent')
        for action in ('start_driven', 'stop_driven'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the third-controller launch '
                                   'the re-target resolves onto is '
                                   'absent')
        if ctx.get('driven') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no driven endpoint — the '
                               'third controller has no monitor')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the durable attribution '
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
        deadline = time.monotonic() + RETARGET_SETTLE
        if _pair_active(ctx) != 'active':
            _retarget_restore_layout(ctx)
        owner = wait_for(lambda: _pair_active(ctx) == 'active'
                         and 'active' or None, deadline,
                         interval=RETARGET_POLL)
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
                    deadline, interval=RETARGET_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg restores to was never reached')

        # The contract surface: the field owner's served checkpoint
        # must carry the ownership stamps — source_owns_field and
        # line_owner — the orphan-resolution probe reads; a rig
        # predating the contract cannot run the leg.
        try:
            _, checkpoint = http_json('GET', ctx['active']
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
                               'retarget-journal contract')
        case.observe('field owner: active (' + ctx['active']
                     + '); orphaned standby: standby')
        try:
            launched = ctx['start_driven']('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the driven-peer '
                               'launch never completed: '
                               + str(exc)[:300])
        case.observe('driven peer up: '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, ctx['driven']),
                    time.monotonic() + RETARGET_SETTLE,
                    interval=RETARGET_POLL) is None:
            return case.finish('inconclusive', 'the driven monitor '
                               'never answered /role')
        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _retarget_pass(ctx, number)
                _judge_retarget(record, note)
                digest = _retarget_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'orphan-retarget-journal-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'orphan-retarget pass '
                              + str(number) + ' — the driven '
                              'convergence, the demote\'s journaled '
                              'parity adoption, the island\'s '
                              'orphaned verdicts, the durable '
                              'trail\'s re-target records and their '
                              'orphan brackets, the reconvergence '
                              'onto the live owner, the restore, '
                              'and the normalized digest')
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
            # The launch layout for the cases behind this one: a
            # clean pass restores it by construction; an aborted pass
            # gets the documented role order run again, best-effort —
            # then the driven container is removed.
            _retarget_restore_layout(ctx)
            try:
                ctx['stop_driven']()
            except Exception:
                pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two orphan-retarget passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the retarget judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _retarget_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
