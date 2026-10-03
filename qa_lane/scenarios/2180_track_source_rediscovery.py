"""The track_source_rediscovery leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — the address-move relaunch
# lands the field owner back on its launch flags and the tracking peer
# reconverges on its own — so it only owes the launch layout to the
# failover case behind it.
RUNS_BEFORE = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The tracking-source address-rediscovery contract — the per-revision
# lane evidence for #1202's fix (WW-LCM-001's continuity clause and
# the TrackTarget::Name rendezvous rule): a DNS-named --standby/--peer
# source must keep rendezvousing across the peer's address changes.
# The defect #1202 removed resolved the configured name once at
# startup and pinned the TrackTarget::Addr for the process lifetime,
# so one routine container restart onto a new IP permanently degraded
# the unarmed standby and manufactured a phantom failover on an armed
# peer against the still-healthy owner.
#
# Staging (the finding's own reproduction): with the deployed pair
# settled and tracking, the runner's address-move lever removes the
# tracking source's container, holds its freed bridge address on a
# placeholder — the address stays occupied exactly like the finding
# staged it — and recreates the launch so the configured container
# name resolves to a NEW address. The tracking peer — one process
# lifetime throughout — must then:
#
# - keep aiming its pulls at the configured name: while the name is
#   unresolvable or still booting, the served sync.degraded detail
#   names the configured `host:port` (the fallback journaled by
#   name), never the stale startup-resolved address — on a
#   pre-contract build the detail names the pinned IP forever;
# - re-discover the source at its new address and reconverge to a
#   tracking verdict without a restart — the durable journal opens
#   no new run_boundary — never degraded on the stale address
#   indefinitely;
# - never fire the armed failover against the still-healthy owner on
#   stale-pin evidence: no role_changed, no promotion_refused, and
#   no field_claim_lost on the recreated owner.
#
# Named diagnostics: track-source-rediscovery-failed tags the
# contract clauses — the pinned-on-stale reconvergence claim, a
# failover the armed peer fired inside its documented budget, the
# by-name fallback evidence that never landed, a reconvergence that
# never came (or came only through a peer restart), unrestored
# launch roles — and track-source-rediscovery-nondeterministic tags
# the instability the contract does not answer for: a refused
# staging call, a starved watch, a move that outlived the armed
# budget, a source that produced no new lifetime. A peer stranded
# degraded on the stale address without reconverging or firing is
# the pre-contract shape itself and reports inconclusive, as do
# rigs that cannot stage the move at all. The unchecked-diagnostic
# self-check replays the judge over planted negatives and reports
# track-source-rediscovery-unchecked for any that slip through.

REDISC_SETTLE = 45   # bound on each settle/reconverge/restore wait
REDISC_POLL = 0.4    # cadence polling the peer mid-move
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DIAG_FAILED = 'track-source-rediscovery-failed'
DIAG_NONDET = 'track-source-rediscovery-nondeterministic'
DIAG_UNCHECKED = 'track-source-rediscovery-unchecked'


def _sync_of(report):
    sync = (report or {}).get('sync')
    return sync if isinstance(sync, dict) else {}


def _degraded_detail(report):
    """The pull target a `degraded` report's detail names — the
    'fetch from <target>: <error>' a produced-nothing pull reports:
    the configured name on a contract build, the pinned
    startup-resolved address on the defect."""
    detail = (_sync_of(report).get('degraded') or {}).get('detail')
    return detail if isinstance(detail, str) else None


def _durable_events(ctx, name, floor, kind):
    """The `kind` event bodies the peer's durable --journal-file
    carries since `floor` records."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _run_boundaries(ctx, name):
    """The process lifetimes the peer's durable journal records — one
    run_boundary per resumed run; a rediscovery that needed a
    restart leaves a new one behind."""
    return sum(1 for item in _journal_entries(ctx['journal_files'][name])
               if 'run_boundary' in item)


def _failover_fired(record):
    """Whether the recorded evidence says the armed tracking peer
    fired its failover gate — promoted out of standby, journaled the
    role change, or preempted the still-healthy owner's claim."""
    failover = record.get('failover') or {}
    return bool(failover.get('promoted') or failover.get('role_changed')
                or failover.get('claim_lost'))


def _pre_contract(record):
    """Whether the run's own evidence is the pre-contract shape: the
    peer stayed pinned on the stale startup-resolved address — the
    defect's signature — without reconverging and without the armed
    failover firing. The staged run predates the #1202 contract and
    classifies inconclusive rather than failed."""
    return bool(record.get('stale_evidence')) \
        and record.get('reconverged') is None \
        and not _failover_fired(record)


def _judge_rediscovery(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    contract clauses — a reconvergence asserted while the pull
    evidence stays pinned on the stale address, a failover the armed
    peer fired inside its documented budget, the by-name fallback
    that never landed, a reconvergence that never came, the restart
    the contract never needs, unrestored launch roles — and
    DIAG_NONDET tags the instability the contract does not answer
    for: a refused staging call, a starved watch, a failover gate
    reached only past its documented bound, a source that produced
    no new lifetime. An aborted stage ends the audit where the pass
    ended — the later keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('move_error') is not None:
        nondet('move', 'the address-move staging never completed: '
               + str(record['move_error']))
        return
    if record.get('move') is None or record.get('same_address'):
        # The inconclusive shapes — no staged move, a recreate onto
        # the same address, the pre-contract strand — are classified
        # by the pass itself; a record this thin ends the audit here.
        return
    if (record.get('reads') or 0) == 0:
        nondet('watch', 'the tracking peer\'s monitor answered '
               'nothing across the move window — the starved watch '
               'gave the audit no served verdict to read')
        return          # a lost observation, never the audit's verdict
    failover = record.get('failover') or {}
    armed_window = False
    if _failover_fired(record):
        if failover.get('past_budget'):
            armed_window = True
            nondet('armed-window', 'the armed tracking peer fired '
                   'failover only past its documented miss budget '
                   '(elapsed ' + json.dumps(failover.get('elapsed'))
                   + 's) — the move window outlived the failover '
                   'gate rather than tripping it on stale evidence')
        else:
            failed('failover', 'the armed tracking peer fired '
                   'failover against the still-healthy owner on '
                   'stale-pin evidence — the phantom promotion the '
                   'contract exists to prevent: '
                   + json.dumps(failover)[:240])
    if failover.get('refused'):
        armed_window = True
        nondet('armed-window', 'the armed failover gate reached its '
               'budget and was refused by the live incumbent — the '
               'move window outlived the armed budget')
    reconverged = record.get('reconverged')
    if reconverged is not None and record.get('stale_evidence'):
        failed('pinned-stale-verdict', 'the tracking peer asserts a '
               'reconverged verdict while its pull evidence stayed '
               'pinned on the stale address '
               + str(record.get('old_address')) + ' — the served '
               'verdict contradicts the degraded record the move '
               'window collected')
    if reconverged is not None \
            and not record.get('by_name_evidence'):
        if not record.get('degraded'):
            nondet('by-name-window', 'the miss window produced no '
                   'degraded record to audit — the by-name fallback '
                   'evidence was never sampled')
        else:
            failed('by-name', 'the fallback never journaled by name '
                   '— the peer\'s degraded records name no '
                   'configured source '
                   + str(record.get('expected_name')) + ': '
                   + json.dumps((record.get('degraded') or [])[:3])[:240])
    if reconverged is None and not record.get('stale_evidence') \
            and not _failover_fired(record):
        failed('reconverge', 'the tracking peer never reconverged '
               'onto the restarted source and is not stranded on '
               'the stale address either — last served state '
               + json.dumps(record.get('last_role'))[:240])
    boundaries = record.get('boundaries') or {}
    if boundaries.get('peer_after', 0) > boundaries.get('peer_before', 0):
        failed('restart', 'the tracking peer\'s durable journal '
               'opened a new run boundary across the move — the '
               'rediscovery needed a restart the contract says is '
               'never required')
    if boundaries.get('source_after', 0) <= boundaries.get(
            'source_before', 0):
        nondet('source-restart', 'the recreated source\'s durable '
               'journal opened no new run boundary — the relaunch '
               'produced no new process lifetime to rediscover')
    # An unrestored layout inside the armed-window instability's
    # blast radius — a promoted or stranded peer that can no longer
    # reconverge — is the same instability, not a second clause.
    if not record.get('restored') and not armed_window:
        failed('roles', 'the pair never settled back to its launch '
               'roles — ' + str(record.get('owner')) + ' active '
               'with ' + str(record.get('peer'))
               + ' tracking behind it')


def _digest_rediscovery(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause. Named for the leg so
    no sibling's patch.object(scenarios, '_digest', ...) seam ever
    rewrites it through the facade."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'moved': 'new-address' if clean('move') else 'none',
        'degraded': 'by-name'
            if clean('by-name', 'by-name-window', 'watch')
            else 'unproven',
        'reconverged': 'tracking'
            if clean('reconverge', 'pinned-stale-verdict')
            else 'stranded',
        'failover': 'none'
            if clean('failover', 'armed-window') else 'fired',
        'restart': 'none' if clean('restart') else 'restarted',
        'roles': 'restored' if clean('roles') else 'unrestored'}


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay the
    rediscovery judge over each planted negative the issue names —
    a reconvergence asserted while pinned on the stale address, a
    failover the armed peer fired on the stale evidence, the by-name
    fallback that never landed, a restart doing the work, unrestored
    roles — and require the judge to note each; the instability
    shapes must report nondeterministic, not failed. A silent judge
    returns the negative names it let through."""
    slipped = []

    def clean_record():
        return {'owner': 'active', 'peer': 'standby',
                'move': {'container': 'dcs-hw-qa-a',
                         'placeholder': 'dcs-hw-qa-placeholder',
                         'old_address': '172.22.0.3',
                         'new_address': '172.22.0.6'},
                'old_address': '172.22.0.3',
                'new_address': '172.22.0.6',
                'expected_name': 'dcs-hw-qa-a:8080',
                'reads': 8,
                'degraded': ['fetch from dcs-hw-qa-a:8080: '
                             'cannot resolve: name or service '
                             'not known'],
                'stale_evidence': False,
                'by_name_evidence': True,
                'reconverged': {'role': 'standby',
                                'sync': {'tracking': {'aligned': 130}}},
                'aligned_before': 100, 'aligned_after': 130,
                'last_role': {'role': 'standby'},
                'failover_seen': None,
                'failover': {'promoted': False, 'role_changed': False,
                             'refused': False, 'claim_lost': False,
                             'past_budget': False, 'elapsed': None},
                'boundaries': {'peer_before': 1, 'peer_after': 1,
                               'source_before': 1, 'source_after': 2},
                'restored': True}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_rediscovery(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negatives the issue names: the peer asserted as
    # reconverged while pinned on the stale address, and failover
    # asserted as unfired while it fires on the stale evidence —
    # both must surface the named diagnostic on the honest record.
    expect('pinned-stale-verdict', lambda record:
           record.update({'stale_evidence': True}))
    expect('failover-on-stale', lambda record: record.update(
        {'stale_evidence': True,
         'reconverged': None,
         'failover': {'promoted': True, 'role_changed': True,
                      'refused': False, 'claim_lost': True,
                      'past_budget': False, 'elapsed': 0.4}}))
    # The named fallback evidence never landed — the peer's degraded
    # records name a foreign endpoint, not the configured source.
    expect('by-name-missing', lambda record: record.update(
        {'by_name_evidence': False,
         'degraded': ['fetch from 172.22.0.9:8080: refused']}))
    # The peer never reconverged and is not stale-pinned either.
    expect('never-reconverged', lambda record:
           record.update({'reconverged': None}))
    # A new run boundary — the rediscovery cost a peer restart.
    expect('restart-needed', lambda record:
           record['boundaries'].update({'peer_after': 2}))
    # The launch roles never restored.
    expect('roles-unrestored', lambda record:
           record.update({'restored': False}))
    # The instability the contract does not answer for must report
    # nondeterministic: a refused staging call, a starved watch, a
    # failover gate reached only past its documented bound or
    # refused by the live incumbent, a miss window with no degraded
    # record to audit, a source that produced no new lifetime.
    expect('move-refused', lambda record:
           record.update({'move_error': 'docker rm failed'}),
           DIAG_NONDET)
    expect('watch-starved', lambda record:
           record.update({'reads': 0}), DIAG_NONDET)
    expect('failover-past-budget', lambda record: record.update(
        {'reconverged': None,
         'failover': {'promoted': True, 'role_changed': True,
                      'refused': False, 'claim_lost': False,
                      'past_budget': True, 'elapsed': 30.0}}),
        DIAG_NONDET)
    expect('promotion-refused', lambda record: record.update(
        {'failover': {'promoted': False, 'role_changed': False,
                      'refused': True, 'claim_lost': False,
                      'past_budget': False, 'elapsed': None}}),
        DIAG_NONDET)
    expect('by-name-window', lambda record: record.update(
        {'by_name_evidence': False, 'degraded': []}), DIAG_NONDET)
    expect('source-not-restarted', lambda record:
           record['boundaries'].update({'source_after': 1}),
           DIAG_NONDET)
    return slipped


def _rediscovery_pass(ctx, number, owner, peer):
    """One address-move pass: capture the peer's journal floor and
    stream alignment, run the runner's move lever — the source's
    container removed, its freed address held by a placeholder, the
    launch recreated onto a NEW address — and watch the peer's
    served verdict across the window. The audit reads the served
    monitor (degraded detail must name the configured source by
    name, the verdict must reconverge tracking) and both durable
    journals (no new boundary on the peer, a new one on the source,
    no armed-failover trace). Returns (record, evidence): the
    record is what the judge replays; an aborted stage simply
    leaves its later keys absent for the judge to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    floors = {name: len(_journal_entries(ctx['journal_files'][name]))
              for name in (owner, peer)}
    boundaries = {'peer_before': _run_boundaries(ctx, peer),
                  'source_before': _run_boundaries(ctx, owner)}
    record['aligned_before'] = _tracking_aligned(
        _try_role(ctx, ctx[peer]))

    # The address move: docker rm -f on the tracking source, a
    # placeholder pinned onto its freed bridge address, and the
    # launch recreated — the configured DNS name resolves to a NEW
    # address from here on.
    try:
        move = ctx['move_controller_address'](owner)
    except Exception as exc:
        record['move_error'] = str(exc)[:300]
        return record, evidence
    record['move'] = move
    record['old_address'] = move.get('old_address')
    record['new_address'] = move.get('new_address')
    record['same_address'] = (move.get('new_address')
                              == move.get('old_address'))
    expected_name = (move.get('container') or '') \
        + ':' + str(PAIR_PORTS['active'])
    record['expected_name'] = expected_name
    budget_secs = (ctx.get('failover_misses') or 0) * 0.1
    moved_at = time.monotonic()

    # The watch: every served report on the tracking peer until the
    # tracking verdict lands or the armed failover fires. The
    # degraded detail is the leg's central evidence — it names the
    # pull target verbatim: the configured name on a contract
    # build, the pinned startup-resolved address on the defect.
    reads = 0
    degraded = []
    reconverged = None
    failover_seen = None
    last_role = None
    deadline = time.monotonic() + REDISC_SETTLE
    while time.monotonic() < deadline:
        report = _try_role(ctx, ctx[peer])
        if report is not None:
            reads += 1
            last_role = report
            role = report.get('role')
            if role in ('promoting', 'active') \
                    and failover_seen is None:
                failover_seen = {
                    'role': role,
                    'elapsed': time.monotonic() - moved_at,
                    'report': report}
            detail = _degraded_detail(report)
            if detail is not None:
                degraded.append(detail)
            sync = _sync_of(report)
            if role == 'standby' and 'tracking' in sync:
                reconverged = report
                break
            if failover_seen is not None:
                break
        time.sleep(REDISC_POLL)

    record['reads'] = reads
    record['degraded'] = degraded[:50]
    # The stale-pin evidence is the pin holding at the window's end —
    # a learned-pin release may legitimately name the stale address
    # across its first produced-nothing pulls before the configured
    # name re-resolves; pinned is what never lets go of it.
    record['stale_evidence'] = bool(degraded) and any(
        str(record['old_address']) in detail
        for detail in degraded[-2:])
    record['by_name_evidence'] = any(
        expected_name in detail for detail in degraded)
    record['reconverged'] = reconverged
    record['aligned_after'] = _tracking_aligned(reconverged)
    record['last_role'] = last_role
    record['failover_seen'] = failover_seen

    # The durable half: the peer's journal must open no new run
    # boundary (the rediscovery needed no restart), the source's
    # must open one (the recreate produced a new lifetime), and the
    # armed gate must leave no trace — no role_changed, no
    # promotion_refused on the peer, no field_claim_lost on the
    # still-healthy owner.
    peer_roles = _durable_events(ctx, peer, floors[peer],
                                 'role_changed')
    peer_refused = _durable_events(ctx, peer, floors[peer],
                                   'promotion_refused')
    source_claim_lost = _durable_events(ctx, owner, floors[owner],
                                        'field_claim_lost')
    boundaries['peer_after'] = _run_boundaries(ctx, peer)
    boundaries['source_after'] = _run_boundaries(ctx, owner)
    record['boundaries'] = boundaries
    elapsed = (failover_seen or {}).get('elapsed')
    record['failover'] = {
        'promoted': failover_seen is not None,
        'role_changed': bool(peer_roles),
        'refused': bool(peer_refused),
        'claim_lost': bool(source_claim_lost),
        'past_budget': bool(budget_secs and elapsed is not None
                            and elapsed > budget_secs * 1.25),
        'elapsed': elapsed}

    # The pre-contract classification runs before the restore wait —
    # a peer stranded on the stale pin can never converge the wait,
    # so the bound would be spent proving what the evidence already
    # says.
    if _pre_contract(record):
        evidence['inconclusive'] = (
            'the tracking peer stayed pinned on the stale '
            'startup-resolved address ' + str(record['old_address'])
            + ' through the whole bound — never reconverged, never '
            'fired the armed gate — the staged run predates the '
            'tracking-source rediscovery contract')
        return record, evidence

    # The launch layout for the judge and the next pass: the named
    # owner active with the tracking peer behind it — repairing
    # first when a fired failover or a slipping owner left the
    # layout off its launch shape.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        _restore_layout(ctx, owner, peer)
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer) is not None,
        time.monotonic() + REDISC_SETTLE, interval=REDISC_POLL)
    return record, evidence


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore after a pass: free the
    placeholder's held address, relaunch the owner when its
    container never came back (an aborted move's recreate half),
    demote a promoted peer, re-promote the named owner, and
    converge the pair — every step retried inside the bound and
    swallowed on refusal."""
    try:
        ctx['release_address_placeholder']()
    except Exception:
        pass
    try:
        if _try_role(ctx, ctx[owner]) is None \
                and ctx.get('relaunch_controller') is not None:
            try:
                ctx['relaunch_controller'](owner)
            except Exception:
                pass
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + REDISC_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(REDISC_POLL)
    except Exception:
        pass


def scenario_track_source_rediscovery(ctx):
    """Exercise the tracking-source address-rediscovery contract on
    the deployed pair: with the pair settled and tracking, the
    runner's address-move lever removes the tracking source's
    container, holds its freed bridge address on a placeholder —
    the staging the finding records — and recreates the launch so
    the configured DNS name resolves to a NEW address; the tracking
    peer must keep pulling the configured name (its degraded
    evidence names it, never the stale startup-resolved address),
    re-discover the source at its new address, and reconverge to a
    tracking verdict inside one process lifetime — no new run
    boundary — while the armed peer never fires failover against
    the still-healthy owner on stale-pin evidence; the launch roles
    restore and two passes produce identical digests."""
    case = Case(
        'track-source-rediscovery',
        'Tracking peer re-discovers its source on a new container '
        'address',
        'with the deployed pair settled — the launched-active owner '
        'and its standby tracking the configured container name — '
        'each pass removes the source container, holds its freed '
        'bridge address on a placeholder, and recreates the launch '
        'so the name resolves to a NEW address; the tracking peer '
        'must keep aiming pulls at the configured name (the '
        'degraded record names it by name, never the stale '
        'startup-resolved address), re-discover the source at its '
        'new address, and reconverge to a tracking verdict inside '
        'one process lifetime — never degraded on the stale '
        'address indefinitely, never restarted — while the armed '
        'peer never fires failover against the still-healthy owner '
        'on stale-pin evidence; the launch roles restore and two '
        'passes produce identical digests')
    owner = peer = None
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the rediscovery leg needs is absent')
        if ctx.get('move_controller_address') is None \
                or ctx.get('release_address_placeholder') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no address-move lever — the '
                               'runner cannot stage the container '
                               'address change the leg exercises')
        if ctx.get('relaunch_controller') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no relaunch_controller '
                               'action — an aborted move could not '
                               'be restored')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the durable half of the '
                               'rediscovery audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the passes stage from: the
        # launched-active peer holds the field (it is the container
        # name the sibling's --standby resolves) with the configured
        # standby tracking it.
        deadline = time.monotonic() + REDISC_SETTLE
        if _pair_active(ctx) != 'active':
            _restore_layout(ctx, 'active', 'standby')
        owner_ok = wait_for(
            lambda: _pair_active(ctx) == 'active' and 'active' or None,
            deadline, interval=REDISC_POLL)
        if owner_ok != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — the '
                               'launched-active peer must hold the '
                               'field for the address move the leg '
                               'stages')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=REDISC_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg moves inside was never reached')
        # The contract substrate on the staged run: the served
        # checkpoint's field-ownership stamps — the machinery the
        # tracking-source contract builds on; a run without them
        # predates the substrate the leg exercises.
        try:
            _, checkpoint = http_json('GET', ctx['standby']
                                      + '/checkpoint')
        except Exception as exc:
            return case.finish('inconclusive', 'the tracking peer\'s '
                               'checkpoint never answered: '
                               + str(exc)[:200])
        if not isinstance(checkpoint, dict) \
                or 'source_owns_field' not in checkpoint \
                or 'line_owner' not in checkpoint:
            return case.finish('inconclusive', 'the served '
                               'checkpoint carries no '
                               'source_owns_field/line_owner stamps '
                               '— the staged run predates the '
                               'tracking-source contract the leg '
                               'exercises')
        owner, peer = 'active', 'standby'
        case.observe('field owner / tracking source: ' + owner
                     + ' (' + ctx[owner] + '); tracking peer: '
                     + peer)

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _rediscovery_pass(
                    ctx, number, owner, peer)
                evidence['record'] = record
                if not evidence.get('inconclusive'):
                    _judge_rediscovery(record, note)
                digest = _digest_rediscovery(violations)
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'track-source-rediscovery-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref,
                              'track-source-rediscovery pass '
                              + str(number) + ' — the address move, '
                              'the served degraded and verdict '
                              'watch, the durable journal audit, '
                              'the armed-failover check, the '
                              'restore, and the normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish('inconclusive',
                                       evidence['inconclusive'])
                if record.get('move') is not None \
                        and record.get('same_address'):
                    return case.finish(
                        'inconclusive', 'the recreated source '
                        'landed on the same bridge address '
                        + str(record.get('new_address')) + ' — the '
                        'move staged no address change')
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
            # clean pass restores it by construction; an aborted
            # pass gets the placeholder freed, a missing owner
            # relaunched, a promoted peer demoted, and the launch
            # roles converged, best-effort.
            _restore_layout(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two address-move passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the rediscovery judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
