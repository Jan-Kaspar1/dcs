"""The usurped_foreign_claim acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg drives the lane's keyed probe pair — its driven
# seat, its relaunch lever, and its bridge-placed plant's claim
# surface — and restores the pair's launch layout every pass, so it
# runs after the keyed announced-source leg whose probe-pair coverage
# it extends, and inside the claim-lifecycle cluster ahead of the
# revision legs that need every seat as it launched.
RUNS_AFTER = frozenset({'scenario_keyed_announced_source'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The usurped verdict and foreign-claim reclaim contract — the
# per-revision lane evidence for the fix #1410 pins (decision 108's
# keyed-pair self-service recovery, WW-FND-002's redundant failover
# integrity). A --pair-token pair whose field an unkeyed writer takes
# must tell a foreign held claim from true ownerlessness: the field's
# own arbitration names the standing writer's declared monitor, the
# keyed monitoring surface pulls that endpoint under a fresh ?prove=
# nonce, and an endpoint outside the line cannot prove the key. The
# verdict it serves is `usurped` — "the tracked line has no field
# owner and the writer the field names is not this line's" — not the
# ordinary `orphaned`, because the two want opposite answers from
# POST /promote: the conditional orphan grant refuses a live
# incumbent, while the usurped diagnosis arms the unconditional
# claim_writer that takes the field back — and journals
# foreign_claim_preempted naming the endpoint the field was taken
# from. The pair-fault surface carries the `standby_usurped` kind
# under PAIR_FAULT_KINDS_VERSION 4.
#
# The honest-absence halves are the contract's other edge: no
# preemption is armed on evidence the pair key never produced. An
# unkeyed run has no key to ask under — the same foreign-held field
# reads `orphaned` to it, and its promote meets the conditional
# grant's refusal. A monitor-less claim names no endpoint to
# diagnose, and a claim whose declared endpoint is dead cannot be
# convicted — both keep `orphaned` and the refused conditional
# grant while the claim's holder lives. And once a keyed peer owns
# the field again, the surviving sibling's verdict clears to
# tracking: the writer proves the line's key on every pull, so the
# pair never reads its own member as foreign — the reclaim must
# not turn a keyed peer into an unconditional preemptor of its own
# members.
#
# The subject is the lane's keyed probe pair (ctx['probe'], #1058 —
# the deployed pair stays whichever posture the run config gives it).
# The probe pair's plant is bridge-placed: no host socket reaches it,
# so the claim ops the shipped dcs-plant-ctl does not expose ride the
# runner's raw field-attachment seam — `field_request` for the
# read-only probe_writer verdict, `hold_field_claim`/
# `drop_field_claim` for the live held claims the honest-absence
# halves stage (a monitor-less claim and a dead declared endpoint are
# shapes no launched controller can raise — a real --listen always
# declares its own live monitor). The live foreign writer itself is a
# real unkeyed driven controller — ctx['probe']['start_driven'] with
# keyed=False — whose promotion runs the same unconditionally
# preempting claim_writer the foreign-claim legs stage, paced through
# POST /scan while the pair's own free-running scans fence, demote,
# diagnose, and reclaim.
#
# Named diagnostics: usurped-foreign-claim-failed for a contract
# miss, usurped-foreign-claim-nondeterministic for instability the
# contract does not answer for (a dropped probe, a refused staging
# claim, an unsettled restore, two passes' digests diverging), and
# usurped-foreign-claim-unchecked for the self-check's planted
# negatives slipping the judge. Inconclusive when the staged run
# predates the contract — no probe pair, no keyed posture, no
# driven/unkeyed launch lever, no raw claim seam, no claim surface —
# or the probe pair never settles into its launch layout.

USURPED_SETTLE = 30    # bound on each converge/demote/diagnose wait
USURPED_POLL = 0.4     # cadence polling the peers mid-episode
USURPED_PASSES = 2     # identical-digest consecutive passes
USURPED_ROUNDS = 4     # polls each held-claim window spans
DRIVEN_SCANS = 4       # scans per POST /scan pacing batch
# The raw held-claim halves' foreign owner token — never a rig pin,
# never the tool's: "qa-usurp".
FOREIGN_OWNER = 0x7161_2d75_7375_7270
# The dead declared monitor's port on the holder's own bridge address:
# a routable IP nothing listens on, so the diagnosis' ?prove= pull is
# refused outright — the dead-declaration shape that keeps orphaned.
DEAD_MONITOR_PORT = 9
# page.html's PAIR_FAULT_KINDS_VERSION — the pair-fault vocabulary
# the usurped verdict lands under.
FAULT_KINDS_VERSION = 4

DIAG_FAILED = 'usurped-foreign-claim-failed'
DIAG_NONDET = 'usurped-foreign-claim-nondeterministic'
DIAG_UNCHECKED = 'usurped-foreign-claim-unchecked'


def _try_role(subject, name):
    """GET /role on the named subject peer, or None when the monitor
    does not answer — a dropped read is never the leg's verdict."""
    try:
        return _role(subject, subject[name])
    except Exception:
        return None


def _sync_kind(report):
    """The report's sync verdict name — 'tracking', 'orphaned',
    'usurped', 'unsynchronized', 'degraded', 'diverged',
    'reinitialized' — or None on a dropped/absent report."""
    if not isinstance(report, dict):
        return None
    sync = report.get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict):
        for kind in ('tracking', 'orphaned', 'usurped', 'degraded',
                     'diverged', 'reinitialized'):
            if kind in sync:
                return kind
        return 'empty'
    return 'none'


def _claim_probe(subject):
    """The probe field's standing claim read through the leg's raw
    seam — `probe_writer` on a fresh attachment: 'fenced' naming the
    standing claim's owner and declared monitor while one stands,
    'unclaimed' while none does. None on a dropped probe or an
    unparsed reply — one lost read, never a staged verdict."""
    request = subject.get('field_request')
    if request is None:
        return None
    try:
        result = request({'op': 'probe_writer'})
    except Exception:
        return None
    if getattr(result, 'returncode', 1) != 0 \
            or not getattr(result, 'stdout', None):
        return None
    try:
        body = json.loads(result.stdout)
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _verdict_owner(response):
    return ((response or {}).get('error') or {}).get('owner')


def _verdict_monitor(response):
    return ((response or {}).get('error') or {}).get('monitor')


def _verdict_declares_monitor(response):
    """Whether the verdict's error carries the monitor field at all —
    a monitor-less claim's answer has no such field."""
    return 'monitor' in ((response or {}).get('error') or {})


def _pair_faults(subject):
    """The page's pairHealth rule applied to the subject pair's last
    /role polls — the pair-fault surface the leg asserts names
    standby_usurped, mirrored from page.html's fault_kinds naming
    (peer_unreachable, standby_unsynchronized_past_grace,
    standby_degraded/diverged/orphaned/usurped, field_unclaimed,
    no_active_peer, dual_active) under PAIR_FAULT_KINDS_VERSION 4.
    The unsynchronized-past-grace clock is wall-clock on the page and
    not the leg's assertion here, so it is omitted rather than
    approximated."""
    kinds, actives = [], []
    for name in ('active', 'standby'):
        report = _try_role(subject, name)
        if report is None:
            kinds.append('peer_unreachable')
            continue
        if report.get('role') == 'active':
            actives.append(name)
        sync = report.get('sync')
        if isinstance(sync, dict):
            for kind in ('degraded', 'diverged', 'orphaned',
                         'usurped'):
                if kind in sync:
                    kinds.append('standby_' + kind)
        if report.get('field_claim') == 'unclaimed':
            kinds.append('field_unclaimed')
    if not actives:
        kinds.append('no_active_peer')
    elif len(actives) > 1:
        kinds.append('dual_active')
    return {'version': FAULT_KINDS_VERSION, 'actives': actives,
            'fault_kinds': kinds}


def _scan_batch(subject, scans=DRIVEN_SCANS):
    """One POST /scan batch on the driven attachment — the paced
    peer's every checkpoint pull, promote claim, and field write runs
    inside it. Returns (status, body) with a refused call's named
    outcome decoded, or (None, None) on a dropped request."""
    try:
        return http_json('POST', subject['driven'] + '/scan',
                         {'scans': scans}, timeout=scans * 2 + 15)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body
    except Exception:
        return None, None


def _pace_driven(subject, check, deadline):
    """Pace the driven attachment through /scan batches until `check`
    accepts its /role report or the deadline passes. Returns the last
    report served."""
    report = None
    while time.monotonic() < deadline:
        _scan_batch(subject)
        report = _try_role(subject, 'driven')
        if report is not None and check(report):
            return report
    return report


def _served_journal(subject, name, floor):
    """The named peer's served journal entries since the captured
    `?since=` cursor — None when the read dropped."""
    try:
        _, body = http_json('GET', subject[name] + '/journal?since='
                            + str(floor))
    except Exception:
        return None
    return _journal_list(body)


def _preemptions(subject, name, floor):
    """The foreign_claim_preempted records the named peer's served
    journal carries since `floor` — their payloads, each naming the
    writer endpoint the unconditional claim took the field from."""
    entries = _served_journal(subject, name, floor)
    if entries is None:
        return None
    return [entry['event']['foreign_claim_preempted']
            for entry in entries
            if 'foreign_claim_preempted'
            in (entry.get('event') or {})]


def _durable_preemptions(subject, name, floor):
    """The foreign_claim_preempted payloads in the named peer's
    --journal-file records since `floor` records — the durable mirror
    the episode's audit reads — or None when the file can't be
    read."""
    try:
        records = _journal_entries(subject['journal_files'][name])
    except Exception:
        return None
    return [event['foreign_claim_preempted']
            for record in records[floor:]
            for event in [(record.get('entry') or {}).get('event')
                          or {}]
            if 'foreign_claim_preempted' in event]


def _await_verdicts(subject, want, names=('active', 'standby')):
    """Poll the named peers' /role until every one reports sync kind
    `want` or the bound passes. Returns (last verdict map, reached) —
    the map is always the most recent readings, so the judge can name
    the wrong verdict rather than shrug at a timeout. The
    free-running pair members scan on their own cadence — the driven
    attachment is paced by its caller, never here."""
    deadline = time.monotonic() + USURPED_SETTLE
    verdicts = {}
    while True:
        verdicts = {name: _sync_kind(_try_role(subject, name))
                    for name in names}
        if all(kind == want for kind in verdicts.values()):
            return verdicts, True
        if time.monotonic() >= deadline:
            return verdicts, False
        time.sleep(USURPED_POLL)


def _watch_kinds(subject, rounds, names=('active', 'standby')):
    """USURPED_ROUNDS polls of the named peers' sync kinds through the
    held-claim window — the verdict set the honest-absence halves
    assert orphaned-only over: `usurped` observed on a peer that
    cannot have convicted the writer is the unconditional-preemption
    defect."""
    rows, seen = [], {name: set() for name in names}
    for index in range(rounds):
        row = {}
        for name in names:
            kind = _sync_kind(_try_role(subject, name))
            row[name] = kind
            seen[name].add(kind)
        rows.append(row)
        if index + 1 < rounds:
            time.sleep(USURPED_POLL)
    return rows, {name: sorted(k for k in kinds if k)
                  for name, kinds in seen.items()}


def _promote(subject, name):
    """POST /promote on the named peer — (status, body): 200 with the
    post-change RoleReport on the granted promotion, 409 naming the
    SwitchError on a refusal."""
    return _settle_call(subject[name] + '/promote')


def _answer_role(body):
    """The role a switchover answer carries — the granted RoleReport's
    field; a refusal's SwitchError body carries none."""
    return (body or {}).get('role') if isinstance(body, dict) else None


def _restore_layout(subject):
    """Best-effort launch-layout restore on the probe pair: demote
    whichever member still owns the field when it is not 'active',
    promote 'active' back over the holderless standing claim — the
    conditional grant answers a holderless claim — and wait the
    tracking sibling in. Every step is retried inside the bound and
    swallowed on refusal; returns True only once the pair reports
    the layout."""
    deadline = time.monotonic() + USURPED_SETTLE
    while time.monotonic() < deadline:
        owner = _pair_active(subject)
        if owner == 'standby':
            _settle_call(subject['standby'] + '/demote')
        elif owner is None:
            _promote(subject, 'active')
        if _pair_active(subject) == 'active' \
                and _tracking_standby(subject, 'standby') is not None:
            return True
        time.sleep(USURPED_POLL)
    return _pair_active(subject) == 'active' \
        and _tracking_standby(subject, 'standby') is not None


def _usurped_half(subject, record, evidence, floors):
    """The live-usurper half: the unkeyed driven attachment converges
    on the keyed owner through the public checkpoint pulls its
    tokenless posture still allows, promotes through the same
    unconditional claim_writer the foreign-claim legs stage, and
    holds the field while the fenced keyed pair demotes, diagnoses,
    and reports `usurped` beside field_claim 'held'. The demoted
    owner's own POST /promote must then reclaim the field against the
    live writer — foreign_claim_preempted naming the usurper's
    endpoint — and the sibling's verdict must clear to tracking under
    the keyed writer, never usurped again. Returns False when the
    staging itself never landed (the record says where)."""
    half = {'foreign': None}
    record['usurped'] = half
    evidence['usurper'] = half
    foreign = subject['start_driven']('active', keyed=False)
    half['foreign'] = foreign
    # The unkeyed attachment's paced convergence on the keyed owner —
    # the public, unkey-gated checkpoint pulls the finding reports.
    converged = _pace_driven(
        subject,
        lambda report: _sync_kind(report) == 'tracking',
        time.monotonic() + USURPED_SETTLE)
    half['converged'] = _sync_kind(converged)
    if _sync_kind(converged) != 'tracking':
        return False
    # The preemption: the converged unkeyed run's promote runs the
    # unconditional claim — no foreign-writer diagnosis exists for a
    # run with no key to ask under, so its own takeover is the
    # deliberate-claim path every tracking standby's promote takes.
    status, body = _settle_call(subject['driven'] + '/promote')
    half['promote'] = {'status': status, 'body': body}
    if status != 200:
        return False
    activated = _pace_driven(
        subject,
        lambda report: report.get('role') == 'active',
        time.monotonic() + USURPED_SETTLE)
    half['usurper_role'] = (activated or {}).get('role')
    # The demotion watch: the keyed owner's first fenced write
    # demotes it in place — its monitor keeps answering while the
    # tracking sibling holds standby.
    walked = wait_for(
        lambda: (_try_role(subject, 'active') or {})
        .get('role') == 'standby' or None,
        time.monotonic() + USURPED_SETTLE, interval=USURPED_POLL)
    half['demoted'] = bool(walked)
    if not walked:
        return False
    half['losses'] = None
    if floors.get('active_served') is not None:
        losses = _served_journal(subject, 'active',
                                 floors['active_served'])
        half['losses'] = [entry['event']['field_claim_lost']
                          for entry in losses or []
                          if 'field_claim_lost'
                          in (entry.get('event') or {})]
    # The diagnosis window: both keyed peers must reach the narrower
    # verdict — the field's arbitration names the usurper's monitor,
    # and neither declared endpoint proves the line's key.
    verdicts, reached = _await_verdicts(subject, 'usurped')
    half['verdicts'] = verdicts
    half['verdicts_reached'] = reached
    owner_report = _try_role(subject, 'active')
    half['field_claim'] = (owner_report or {}).get('field_claim')
    half['faults'] = _pair_faults(subject)
    claim = _claim_probe(subject)
    half['claim_names'] = {'verdict': claim,
                           'owner': _verdict_owner(claim),
                           'monitor': _verdict_monitor(claim)}
    if not reached:
        return False
    # The reclaim on the pair's own surface: an operator promote on
    # the demoted keyed owner routes to the unconditional claim the
    # usurped verdict arms — granted against the live foreign writer
    # where the orphaned conditional grant refused.
    status, body = _promote(subject, 'active')
    half['reclaim'] = {'status': status,
                       'role': _answer_role(body)}
    if status != 200:
        return False
    recovered = wait_for(
        lambda: (_try_role(subject, 'active') or {})
        .get('role') == 'active' or None,
        time.monotonic() + USURPED_SETTLE, interval=USURPED_POLL)
    half['recovered'] = bool(recovered)
    if floors.get('active_served') is not None:
        half['preempted'] = _preemptions(
            subject, 'active', floors['active_served'])
    if floors.get('active_durable') is not None:
        half['durable_preempted'] = _durable_preemptions(
            subject, 'active', floors['active_durable'])
    half['claim_after'] = _claim_probe(subject)
    # The fenced usurper: demoted like any superseded writer but
    # still serving — the claim was reclaimed while it lived, which
    # is exactly what the contract requires the gate to answer.
    half['usurper_after'] = _driven_role_report(subject)
    # The narrowness half: the keyed writer's claim declares the
    # owner's own monitor, which proves the line's key — so the
    # sibling's diagnosis drops the verdict and the pair is no
    # unconditional preemptor of its own members. Poll the
    # reconvergence and record every sync kind seen.
    seen = set()
    deadline = time.monotonic() + USURPED_SETTLE
    cleared = False
    while time.monotonic() < deadline and not cleared:
        report = _try_role(subject, 'standby')
        kind = _sync_kind(report)
        seen.add(kind)
        if kind == 'tracking':
            cleared = True
        else:
            time.sleep(USURPED_POLL)
    half['cleared'] = {'tracking': cleared,
                       'kinds': sorted(k for k in seen if k)}
    return bool(recovered)


def _driven_role_report(subject):
    """The usurper's post-reclaim /role — role plus whether its
    monitor still answers at all, the alive-while-reclaimed evidence.
    The driven peer only demotes inside a paced scan, so the report
    paces it until its fenced verdict lands (or the bound passes —
    the judge names a still-active report the fencing miss)."""
    report = None
    deadline = time.monotonic() + USURPED_SETTLE
    while time.monotonic() < deadline:
        _scan_batch(subject)
        report = _try_role(subject, 'driven')
        if report is not None and report.get('role') != 'active':
            break
    return {'alive': report is not None,
            'role': (report or {}).get('role'),
            'sync': _sync_kind(report)}


def _absence_half(subject, record, evidence, key, request):
    """One honest-absence half: `request` holds a foreign claim on the
    probe field whose declared-monitor shape (absent, or a dead
    address) leaves the keyed pair no endpoint a diagnosis can
    convict — the surviving keyed peers must keep `orphaned`, never
    `usurped`, and the promotion gate must keep the conditional
    grant's refusal while the holder lives. Dropping the holder
    leaves the claim standing holderless, and the pair's own promote
    must then take the field back through the ordinary conditional
    grant. Returns the held-claim seam's answer record (also stamped
    into `record`/`evidence` under `key`), or None when the staging
    never landed."""
    half = {}
    record[key] = half
    evidence[key] = half
    hold = subject['hold_field_claim'](request)
    half['hold'] = hold
    if not isinstance(hold, dict) \
            or (hold.get('reply') or {}).get('result') \
            not in ('done', 'claimed_shared'):
        return None
    # The held window: the keyed owner's fenced write demotes it, and
    # every keyed peer's verdict stays orphaned across the window —
    # the claim's evidence never armed a preemption.
    walked = wait_for(
        lambda: (_try_role(subject, 'active') or {})
        .get('role') == 'standby' or None,
        time.monotonic() + USURPED_SETTLE, interval=USURPED_POLL)
    half['demoted'] = bool(walked)
    rows, seen = _watch_kinds(subject, USURPED_ROUNDS)
    half['window'] = rows
    half['seen'] = seen
    half['claim_names'] = _verdict_fields(_claim_probe(subject))
    # The refused gate: promote on the demoted keyed owner must meet
    # the conditional grant's refusal while the live foreign holder
    # stands — no preemption armed on evidence the pair key never
    # produced.
    status, body = _promote(subject, 'active')
    half['promote'] = {'status': status, 'body': body}
    # Release the holder: the claim stands holderless — preemptable
    # through the ordinary conditional grant, which the pair's own
    # promote now runs.
    subject['drop_field_claim']()
    half['dropped_claim'] = _verdict_fields(_claim_probe(subject))
    status, body = _promote(subject, 'active')
    half['repromote'] = {'status': status,
                         'role': _answer_role(body)}
    deadline = time.monotonic() + USURPED_SETTLE
    half['restored'] = wait_for(
        lambda: (_pair_active(subject) == 'active'
                 and _tracking_standby(subject, 'standby') is not None)
        or None,
        deadline, interval=USURPED_POLL) is not None
    return half


def _verdict_fields(response):
    """The claim verdict's named fields flattened for the record —
    owner token, declared monitor, whether the field carried a
    monitor key at all."""
    return {'raw': response,
            'owner': _verdict_owner(response),
            'monitor': _verdict_monitor(response),
            'declared': _verdict_declares_monitor(response)}


def _unkeyed_half(subject, record, evidence, foreign):
    """The unkeyed-run half: the same live foreign claim — this one
    declaring the still-serving unkeyed usurper's monitor, an
    endpoint a keyed peer convicts — read by a probe member
    relaunched without the pair token. It must report `orphaned`
    where its keyed sibling reports `usurped` on the same claim, and
    its promote must meet the conditional refusal: nothing armed on
    evidence a key it does not hold would have produced. The member
    is relaunched keyed again, the holder dropped, and the pair
    restored — the half's layout is the launch one."""
    half = {}
    record['unkeyed'] = half
    evidence['unkeyed'] = half
    hold = subject['hold_field_claim']({
        'op': 'claim_writer', 'owner': FOREIGN_OWNER,
        'controller': True, 'monitor': foreign['address']})
    half['hold'] = hold
    if not isinstance(hold, dict) \
            or (hold.get('reply') or {}).get('result') \
            not in ('done', 'claimed_shared'):
        return None
    walked = wait_for(
        lambda: (_try_role(subject, 'active') or {})
        .get('role') == 'standby' or None,
        time.monotonic() + USURPED_SETTLE, interval=USURPED_POLL)
    half['demoted'] = bool(walked)
    # The keyed member convicts the declared endpoint first — the
    # contrast the unkeyed reading is staged against.
    keyed_map, keyed_ok = _await_verdicts(subject, 'usurped',
                                        names=('active',))
    half['keyed_verdict'] = keyed_map
    half['keyed_reached'] = keyed_ok
    # The unkeyed run: the standby member relaunched without the pair
    # token reads the same claim and reaches orphaned — no key to ask
    # under — and its promote meets the conditional refusal.
    subject['relaunch_controller']('standby', keyed=False)
    unkeyed_map, unkeyed_ok = _await_verdicts(
        subject, 'orphaned', names=('standby',))
    half['unkeyed_verdict'] = unkeyed_map
    half['unkeyed_reached'] = unkeyed_ok
    status, body = _promote(subject, 'standby')
    half['promote'] = {'status': status, 'body': body}
    # Restore the launch posture: the member relaunched keyed, the
    # held claim dropped to holderless, the pair's own promote
    # reclaiming the field, the sibling reconverged.
    subject['relaunch_controller']('standby', keyed=True)
    subject['drop_field_claim']()
    half['dropped_claim'] = _verdict_fields(_claim_probe(subject))
    status, body = _promote(subject, 'active')
    half['repromote'] = {'status': status,
                         'role': _answer_role(body)}
    deadline = time.monotonic() + USURPED_SETTLE
    half['restored'] = wait_for(
        lambda: (_pair_active(subject) == 'active'
                 and _tracking_standby(subject, 'standby') is not None)
        or None,
        deadline, interval=USURPED_POLL) is not None
    return half


def _usurp_pass(subject, number):
    """One full pass over the probe pair: the live unkeyed writer's
    usurp-and-reclaim, then the three honest-absence halves — the
    monitor-less claim, the dead declared endpoint, and the unkeyed
    run — each asserting `orphaned` and the conditional refusal while
    the foreign holder lives, and the ordinary grant restoring the
    pair once the claim goes holderless. Returns (record, evidence):
    the record is what the judge replays."""
    record = {'pass': number}
    evidence = {'pass': number, 'foreign_owner': FOREIGN_OWNER}
    floors = {}
    for name in ('active', 'standby'):
        try:
            floors[name + '_served'] = _journal_cursor(
                subject, subject[name])
        except Exception:
            floors[name + '_served'] = None
        try:
            floors[name + '_durable'] = len(_journal_entries(
                subject['journal_files'][name]))
        except Exception:
            floors[name + '_durable'] = None
    record['floors'] = floors
    record['baseline'] = _verdict_fields(_claim_probe(subject))
    evidence['baseline'] = record['baseline']

    foreign = {}
    usurper_started = False
    try:
        landed = _usurped_half(subject, record, evidence, floors)
        foreign = (record.get('usurped') or {}).get('foreign') or {}
        usurper_started = bool(foreign)
        # The honest-absence halves: the held-claim seam stages the
        # declared-monitor shapes no launched controller can raise.
        # The dead-declared half names the still-alive usurper's
        # bridge IP on a port nothing serves — a routable address
        # whose ?prove= pull is refused outright — while the unkeyed
        # half declares the usurper's live monitor itself, so it
        # runs last while the usurper still serves.
        if landed and foreign.get('address'):
            dead_monitor = foreign['address'].rpartition(':')[0] \
                + ':' + str(DEAD_MONITOR_PORT)
            _absence_half(subject, record, evidence, 'monitorless', {
                'op': 'claim_writer', 'owner': FOREIGN_OWNER,
                'controller': True})
            _absence_half(subject, record, evidence, 'dead', {
                'op': 'claim_writer', 'owner': FOREIGN_OWNER,
                'controller': True, 'monitor': dead_monitor})
            _unkeyed_half(subject, record, evidence, foreign)
        record['restored'] = _restore_layout(subject)
    finally:
        if usurper_started:
            try:
                subject['stop_driven']()
            except Exception:
                pass
        try:
            subject['drop_field_claim']()
        except Exception:
            pass
    return record, evidence


def _judge_usurped(record, note):
    """The usurped-verdict and foreign-claim reclaim contract: the
    live unkeyed writer fenced the keyed owner into the `usurped`
    verdict beside field_claim 'held' — never `orphaned` — the
    pair-fault surface named standby_usurped, POST /promote reclaimed
    the field through the unconditional claim and journaled
    foreign_claim_preempted naming the usurper's endpoint, the fenced
    usurper stayed up, and the sibling's verdict cleared under the
    keyed writer. The absence halves kept `orphaned` and the
    conditional refusal while each staged foreign holder lived, and
    the holderless claim restored through the ordinary grant.
    Contract misses report usurped-foreign-claim-failed; instability
    the contract does not answer for reports
    usurped-foreign-claim-nondeterministic."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    half = record.get('usurped') or {}
    foreign = half.get('foreign') or {}
    baseline = record.get('baseline') or {}
    if baseline.get('raw') is None:
        nondet('baseline', 'the probe field\'s claim surface '
               'answered no probe — the pass never saw the standing '
               'claim')
        return
    if not _fenced(baseline['raw']) \
            or baseline.get('owner') != record.get('owner_token'):
        nondet('baseline', 'the standing claim did not name the '
               'probe owner at pass start — the staging precondition '
               'was lost: ' + json.dumps(baseline['raw'],
                                         sort_keys=True)[:160])
        return
    if not foreign:
        nondet('foreign-launch', 'the unkeyed driven attachment never '
               'launched — the staging never ran')
        return
    if foreign.get('owner') != record.get('driven_token'):
        nondet('foreign-owner', 'the launched attachment pinned '
               + str(foreign.get('owner'))
               + ' — the probe driven token expected: '
               + str(record.get('driven_token')))
        return
    if half.get('converged') != 'tracking':
        nondet('usurper-converge', 'the unkeyed attachment never '
               'converged on the keyed owner — the staging never '
               'reached the promotion: ' + str(half.get('converged')))
        return
    if (half.get('promote') or {}).get('status') != 200:
        nondet('usurper-promote', 'the unkeyed attachment\'s promote '
               'was refused — the staging never preempted: '
               + json.dumps(half.get('promote'), sort_keys=True)[:160])
        return
    if half.get('usurper_role') != 'active':
        failed('usurper-active', 'the unkeyed attachment never took '
               'the field — its claim_writer preemption is the '
               'premise the pair\'s verdict must answer: '
               + str(half.get('usurper_role')))
        return
    if not half.get('demoted'):
        failed('demotion', 'the fenced keyed owner never demoted — '
               'the field\'s own arbitration must fence the '
               'preempted writer')
        return
    losses = half.get('losses')
    if losses is None:
        nondet('loss', 'the fenced owner\'s served journal was '
               'unreadable — the loss-attribution audit never ran')
        return
    if not losses:
        failed('loss', 'the fenced owner\'s served journal carries '
               'no field_claim_lost for the preemption — the loss '
               'attribution the episode reconstructs from')
        return
    verdicts = half.get('verdicts') or {}
    if not half.get('verdicts_reached'):
        if any(kind is None for kind in verdicts.values()):
            nondet('verdicts', 'a keyed peer\'s monitor stopped '
                   'answering mid-diagnosis — the verdict never '
                   'settled: '
                   + json.dumps(verdicts, sort_keys=True)[:160])
            return
        kind = 'orphaned' if 'orphaned' in verdicts.values() \
            else 'the served states'
        failed('verdicts', 'the surviving keyed peers reported '
               + json.dumps(verdicts, sort_keys=True)
               + ' while a live writer outside the line held the '
               'field — the verdict must be usurped, not ' + kind)
        return
    if half.get('field_claim') != 'held':
        failed('field-claim', 'the demoted keyed owner reported '
               'field_claim '
               + str(half.get('field_claim'))
               + ' — the field\'s arbitration held the whole time; '
               'the narrower verdict names exactly that')
        return
    faults = half.get('faults') or {}
    if faults.get('version') != FAULT_KINDS_VERSION \
            or 'standby_usurped' not in (faults.get('fault_kinds')
                                         or []):
        failed('fault-surface', 'the pair-fault surface named '
               + json.dumps(faults, sort_keys=True)[:200]
               + ' — standby_usurped under kind version 4 expected')
        return
    names = half.get('claim_names') or {}
    if names.get('owner') != record.get('driven_token'):
        failed('claim-owner', 'the field\'s arbitration named '
               + str(names.get('owner'))
               + ' — the unkeyed attachment\'s token '
               + str(record.get('driven_token')) + ' expected')
        return
    if names.get('monitor') != foreign.get('address'):
        failed('claim-monitor', 'the field\'s arbitration named '
               'monitor ' + str(names.get('monitor'))
               + ' — the usurper\'s declared endpoint '
               + str(foreign.get('address')) + ' expected: the '
               'dialable substitution the diagnosis dials')
        return
    reclaim = half.get('reclaim') or {}
    if reclaim.get('status') != 200:
        failed('reclaim', 'the demoted keyed owner\'s promote '
               'answered ' + str(reclaim.get('status'))
               + ' — the usurped verdict must route the promotion '
               'through the unconditional claim while the foreign '
               'writer lives: '
               + json.dumps(reclaim, sort_keys=True)[:160])
        return
    if not half.get('recovered'):
        failed('recovered', 'the reclaimed promotion never settled '
               'the keyed owner active again')
        return
    preempted = half.get('preempted')
    if not preempted:
        failed('journal-served', 'the reclaiming peer\'s served '
               'journal carries no foreign_claim_preempted — the '
               'audit record the contract names')
        return
    writers = [entry.get('writer') for entry in preempted]
    if foreign.get('address') not in writers:
        failed('journal-writer', 'foreign_claim_preempted named '
               + json.dumps(writers)[:160]
               + ' — the usurper\'s endpoint '
               + str(foreign.get('address'))
               + ' is the writer the field was taken from')
        return
    durable = half.get('durable_preempted')
    if not durable:
        nondet('journal-durable', 'the durable --journal-file '
               'carries no foreign_claim_preempted record')
        return
    if foreign.get('address') not in \
            [entry.get('writer') for entry in durable]:
        failed('durable-writer', 'the durable '
               'foreign_claim_preempted named '
               + json.dumps([entry.get('writer')
                             for entry in durable])[:160]
               + ' — the usurper\'s endpoint expected')
        return
    after = half.get('usurper_after') or {}
    if not after.get('alive'):
        nondet('usurper-alive', 'the usurper stopped answering '
               'after the reclaim — alive-through-preemption is '
               'the staged shape, and its loss here is the rig\'s '
               'not the contract\'s')
        return
    if after.get('role') == 'active':
        failed('fenced', 'the usurper still reports active after '
               'the reclaim — the keyed peer\'s claim must fence '
               'the superseded writer')
        return
    cleared = half.get('cleared') or {}
    if 'usurped' in (cleared.get('kinds') or []):
        failed('cleared', 'the keyed sibling reported usurped while '
               'a keyed peer held the field — the writer proves the '
               'line\'s key, so no pair member may read as foreign')
        return
    if not cleared.get('tracking'):
        failed('cleared', 'the keyed sibling never reconverged '
               'tracking on the reclaimed owner — the usurped '
               'verdict must clear once the line\'s writer proves '
               'its key')
        return
    # The honest-absence halves: each staged claim kept orphaned and
    # the conditional refusal while its holder lived, and the
    # holderless claim restored through the ordinary grant.
    for key, label in (('monitorless', 'monitor-less'),
                       ('dead', 'dead-declared')):
        absence = record.get(key)
        if absence is None:
            return
        hold = absence.get('hold') or {}
        if (hold.get('reply') or {}).get('result') \
                not in ('done', 'claimed_shared'):
            nondet(key + '-staging', 'the ' + label + ' claim never '
                   'landed — the raw held-claim seam never answered '
                   'a grant: '
                   + json.dumps(hold.get('reply'),
                                sort_keys=True)[:160])
            continue
        if absence.get('demoted') is not True:
            nondet(key + '-demotion', 'the keyed owner never '
                   'demoted under the ' + label
                   + ' claim — the held window never stood')
            continue
        seen = absence.get('seen') or {}
        for name in ('active', 'standby'):
            kinds = seen.get(name) or []
            if 'usurped' in kinds:
                failed(key + '-verdict', 'the ' + label
                       + ' claim left ' + name
                       + ' reporting usurped — a claim declaring no '
                       'dialable monitor convicts nothing, and no '
                       'preemption may arm on evidence the pair key '
                       'never produced')
                break
            if not kinds:
                nondet(key + '-verdict', name + '\'s monitor '
                       'answered nothing inside the held window — '
                       'the verdict never settled')
                break
            if 'orphaned' not in kinds:
                nondet(key + '-verdict', 'the ' + label
                       + ' claim left ' + name + ' reporting only '
                       + json.dumps(kinds)
                       + ' — orphaned never surfaced inside the '
                       'window, so the ownerless reading never '
                       'settled')
                break
        promote = absence.get('promote') or {}
        body = promote.get('body')
        refused = promote.get('status') == 409 \
            and isinstance(body, dict) \
            and 'field_claim_failed' in body
        if promote.get('status') == 200:
            failed(key + '-preempted', 'the keyed peer\'s promote '
                   'was granted against the live ' + label
                   + ' foreign claim — the conditional orphan grant '
                   'must refuse a live incumbent it cannot convict')
            continue
        if not refused:
            nondet(key + '-promote', 'the keyed peer\'s promote '
                   'answered ' + str(promote.get('status')) + ' '
                   + json.dumps(body, sort_keys=True)[:120]
                   + ' — the named conditional refusal '
                   'field_claim_failed expected')
            continue
        if absence.get('restored') is not True:
            failed(key + '-restore', 'the pair never restored to '
                   'the launch layout after the ' + label
                   + ' claim went holderless — the ordinary '
                   'conditional grant must take a holderless '
                   'standing claim')
            continue
    unkeyed = record.get('unkeyed')
    if unkeyed is None:
        return
    uhold = unkeyed.get('hold') or {}
    if (uhold.get('reply') or {}).get('result') \
            not in ('done', 'claimed_shared'):
        nondet('unkeyed-staging', 'the live-monitor foreign claim '
               'never landed for the unkeyed-run half: '
               + json.dumps(uhold.get('reply'), sort_keys=True)[:160])
        return
    if unkeyed.get('demoted') is not True:
        nondet('unkeyed-demotion', 'the keyed owner never demoted '
               'under the live-monitor foreign claim')
        return
    keyed = unkeyed.get('keyed_verdict') or {}
    if not unkeyed.get('keyed_reached'):
        if any(kind is None for kind in keyed.values()):
            nondet('unkeyed-contrast', 'the keyed member\'s monitor '
                   'never answered inside the diagnosis window')
            return
        failed('unkeyed-contrast', 'the keyed member reported '
               + json.dumps(keyed, sort_keys=True)
               + ' on a claim declaring the live unkeyed monitor — '
               'the diagnosis must still convict it before the '
               'unkeyed member\'s orphaned reading can mean '
               'anything')
        return
    reading = unkeyed.get('unkeyed_verdict') or {}
    if not unkeyed.get('unkeyed_reached'):
        if any(kind is None for kind in reading.values()):
            nondet('unkeyed-verdict', 'the relaunched unkeyed '
                   'member\'s monitor never answered inside the '
                   'bound')
            return
        failed('unkeyed-verdict', 'the member relaunched without '
               'the pair token reported '
               + json.dumps(reading, sort_keys=True)
               + ' — an unkeyed run has no key to ask under and '
               'must keep orphaned')
        return
    promote = unkeyed.get('promote') or {}
    body = promote.get('body')
    if promote.get('status') == 200:
        failed('unkeyed-preempted', 'the unkeyed member\'s promote '
               'was granted against the live foreign claim — the '
               'conditional grant must refuse while the incumbent '
               'lives')
        return
    if not (promote.get('status') == 409
            and isinstance(body, dict)
            and 'field_claim_failed' in body):
        nondet('unkeyed-promote', 'the unkeyed member\'s promote '
               'answered ' + str(promote.get('status')) + ' '
               + json.dumps(body, sort_keys=True)[:120]
               + ' — the named conditional refusal expected')
        return
    if unkeyed.get('restored') is not True:
        failed('unkeyed-restore', 'the pair never restored its '
               'launch layout after the unkeyed member rejoined '
               'keyed and the claim went holderless')
        return
    if record.get('restored') is not True:
        nondet('restored', 'the pair never settled back to the '
               'launch layout at pass end')


def _usurped_digest(record, violations):
    """The normalized verdict digest the two passes must produce
    identically — every clause reduced to the verdict it names. The
    diagnostics are the complaint's names; the digest is the rig's
    answer."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    half = record.get('usurped') or {}
    absence_keys = ('monitorless', 'dead', 'unkeyed')
    return {
        'preempted': 'usurped' if clean(
            'demotion', 'verdicts', 'field-claim', 'fault-surface',
            'claim-owner', 'claim-monitor', 'usurper-active')
        and half else 'unproven',
        'reclaim': 'granted' if clean(
            'reclaim', 'recovered', 'journal-served',
            'journal-writer', 'durable-writer', 'fenced')
        else 'broken',
        'cleared': 'tracking' if clean('cleared') else 'latched',
        'absent': 'refused' if all(
            isinstance(record.get(key), dict)
            and clean(key + '-verdict', key + '-preempted',
                      key + '-restore', key + '-promote',
                      key + '-staging')
            for key in absence_keys)
        and clean('unkeyed-contrast', 'unkeyed-verdict',
                  'unkeyed-preempted', 'unkeyed-promote',
                  'unkeyed-restore', 'unkeyed-staging')
        else 'armed',
        'restored': 'layout' if record.get('restored') is True
        else 'stranded'}


def _self_check():
    """The unchecked-diagnostic self-check: replay the judge over
    each planted negative — every clause the leg asserts — and
    require the judge to note each through the same path the rig
    evidence takes. A silent judge returns the negative names it let
    through."""
    slipped = []

    def clean_record():
        """The record of a clean pass: the unkeyed attachment
        converged, promoted, and fenced the keyed owner into the
        usurped verdict beside field_claim 'held'; the reclaim
        granted, the preemption journaled naming the usurper's
        endpoint, the usurper alive and fenced, the sibling's
        verdict cleared to tracking; and each absence half refused
        its promote and restored on the holderless claim."""
        absent = {'hold': {'reply': {'result': 'done'}},
                  'demoted': True,
                  'seen': {'active': ['orphaned'],
                           'standby': ['orphaned']},
                  'promote': {'status': 409,
                              'body': {'field_claim_failed': {
                                  'detail': 'held'}}},
                  'repromote': {'status': 200, 'role': 'promoting'},
                  'restored': True}
        return {
            'owner_token': 424248, 'driven_token': 424250,
            'baseline': {'owner': 424248,
                         'monitor': '172.18.0.2:8080',
                         'declared': True,
                         'raw': {'result': 'error', 'error': {
                             'kind': 'fenced', 'owner': 424248,
                             'monitor': '172.18.0.2:8080'}}},
            'usurped': {
                'foreign': {'container': 'dcs-hw-qa-1-probe-d',
                            'owner': 424250,
                            'address': '172.18.0.5:8092'},
                'converged': 'tracking',
                'promote': {'status': 200, 'body': {
                    'role': 'promoting'}},
                'usurper_role': 'active',
                'demoted': True,
                'losses': [{'point': 200, 'owner': 424248,
                            'claimant': 424250}],
                'verdicts': {'active': 'usurped',
                             'standby': 'usurped'},
                'verdicts_reached': True,
                'field_claim': 'held',
                'faults': {'version': 4, 'actives': [],
                           'fault_kinds': ['standby_usurped',
                                           'standby_usurped',
                                           'no_active_peer']},
                'claim_names': {'owner': 424250,
                                'monitor': '172.18.0.5:8092'},
                'reclaim': {'status': 200, 'role': 'promoting'},
                'recovered': True,
                'preempted': [{'writer': '172.18.0.5:8092'}],
                'durable_preempted': [{'writer': '172.18.0.5:8092'}],
                'usurper_after': {'alive': True, 'role': 'standby',
                                  'sync': 'tracking'},
                'cleared': {'tracking': True, 'kinds': ['tracking']}},
            'monitorless': dict(absent), 'dead': dict(absent),
            'unkeyed': {'hold': {'reply': {'result': 'done'}},
                        'demoted': True,
                        'keyed_verdict': {'active': 'usurped'},
                        'keyed_reached': True,
                        'unkeyed_verdict': {'standby': 'orphaned'},
                        'unkeyed_reached': True,
                        'promote': {'status': 409,
                                    'body': {'field_claim_failed': {
                                        'detail': 'held'}}},
                        'repromote': {'status': 200,
                                      'role': 'promoting'},
                        'restored': True},
            'restored': True}

    def replay(record):
        noted = {}

        def note(key, diagnostic, detail):
            noted[key] = (diagnostic, detail)

        _judge_usurped(record, note)
        return noted

    checks = [
        # The defect itself: the pair latches plain orphaned while
        # the live unkeyed writer holds the field.
        ('verdicts-orphaned', lambda r: r['usurped'].update(
            verdicts={'active': 'orphaned', 'standby': 'orphaned'},
            verdicts_reached=False)),
        ('verdicts-partial', lambda r: r['usurped'].update(
            verdicts={'active': 'usurped', 'standby': 'orphaned'},
            verdicts_reached=False)),
        ('field-claim-unclaimed', lambda r: r['usurped'].update(
            field_claim='unclaimed')),
        ('fault-surface', lambda r: r['usurped'].update(
            faults={'version': 4, 'actives': [],
                    'fault_kinds': ['standby_orphaned']})),
        ('claim-owner-wrong', lambda r: r['usurped'].update(
            claim_names={'owner': 424243,
                         'monitor': '172.18.0.5:8092'})),
        ('claim-monitor-wrong', lambda r: r['usurped'].update(
            claim_names={'owner': 424250,
                         'monitor': '172.18.0.9:8092'})),
        ('reclaim-refused', lambda r: r['usurped'].update(
            reclaim={'status': 409,
                     'body': {'field_claim_failed': {}}})),
        ('journal-silent', lambda r: r['usurped'].update(
            preempted=[])),
        ('journal-wrong-writer', lambda r: r['usurped'].update(
            preempted=[{'writer': '172.18.0.9:8092'}])),
        ('durable-wrong-writer', lambda r: r['usurped'].update(
            durable_preempted=[{'writer': '172.18.0.9:8092'}])),
        ('usurper-still-active', lambda r: r['usurped'].update(
            usurper_after={'alive': True, 'role': 'active'})),
        # The unconditional-preemptor defect: the sibling reads the
        # keyed owner as foreign.
        ('sibling-still-usurped', lambda r: r['usurped'].update(
            cleared={'tracking': True,
                     'kinds': ['tracking', 'usurped']})),
        ('sibling-never-tracks', lambda r: r['usurped'].update(
            cleared={'tracking': False, 'kinds': ['orphaned']})),
        # The absence halves: the wrong verdict, or the armed
        # preemption, or the broken restore.
        ('monitorless-usurped', lambda r: r['monitorless'].update(
            seen={'active': ['usurped'],
                  'standby': ['orphaned']})),
        ('monitorless-granted', lambda r: r['monitorless'].update(
            promote={'status': 200, 'body': {'role': 'promoting'}})),
        ('dead-usurped', lambda r: r['dead'].update(
            seen={'active': ['orphaned'],
                  'standby': ['usurped']})),
        ('dead-granted', lambda r: r['dead'].update(
            promote={'status': 200, 'body': {'role': 'promoting'}})),
        ('unkeyed-usurped', lambda r: r['unkeyed'].update(
            unkeyed_verdict={'standby': 'usurped'},
            unkeyed_reached=False)),
        ('unkeyed-granted', lambda r: r['unkeyed'].update(
            promote={'status': 200, 'body': {'role': 'promoting'}})),
        ('unkeyed-contrast', lambda r: r['unkeyed'].update(
            keyed_verdict={'active': 'orphaned'},
            keyed_reached=False)),
    ]
    for name, edit in checks:
        record = clean_record()
        edit(record)
        if not replay(record):
            slipped.append(name)
    # The clean record itself must produce no complaint — a judge
    # that notes on the honest pass is as broken as a silent one.
    if replay(clean_record()):
        slipped.append('clean-record')
    return slipped


def scenario_usurped_foreign_claim(ctx):
    """Exercise the keyed pair's usurped verdict and foreign-claim
    reclaim on the simulated QA rig
    (qa-scenario-usurped-claim-reclaim, the contract #1410's fix
    pins — decision 108). On the lane's staged keyed probe pair, an
    unkeyed driven attachment converges through public checkpoint
    pulls and preempts the field with the unconditional claim_writer;
    the surviving keyed peers must report `usurped` beside
    field_claim 'held' — never plain `orphaned` — the pair-fault
    surface must name standby_usurped, and POST /promote on the
    demoted keyed owner must reclaim the field through the
    unconditional claim while the usurper lives, journaling
    foreign_claim_preempted naming the usurper's endpoint. The
    absence halves — a monitor-less held claim, a dead declared
    endpoint, and an unkeyed member relaunched — must keep
    `orphaned` and the conditional grant's refusal while the
    foreign holder lives, and a holderless foreign claim must hand
    the field back through the ordinary grant. Two consecutive
    passes produce identical outcome digests; the unchecked-
    diagnostic self-check reports usurped-foreign-claim-unchecked;
    the leg is inconclusive when the run stages no keyed probe pair
    or the claim seams it needs."""
    case = Case(
        'usurped-foreign-claim',
        'Keyed pair usurped verdict and live foreign-claim reclaim',
        'A live unkeyed writer\'s claim_writer against the keyed '
        'probe pair\'s field is answered by the surviving keyed '
        'peers reporting usurped beside field_claim held — not '
        'orphaned — with standby_usurped on the pair-fault '
        'surface; POST /promote on a keyed peer reclaims the field '
        'through the unconditional claim while the usurper lives '
        'and journals foreign_claim_preempted naming its endpoint; '
        'the monitor-less, dead-declared, and unkeyed shapes keep '
        'orphaned and the conditional refusal; and the sibling\'s '
        'verdict clears once a keyed peer owns the field again')
    try:
        subject = ctx.get('probe')
        if not isinstance(subject, dict):
            return case.finish(
                'inconclusive',
                'no probe pair is staged — the leg\'s subject is '
                'the lane\'s keyed probe pair (ctx[\'probe\'])')
        if not subject.get('pair_token'):
            return case.finish(
                'inconclusive',
                'the probe pair is unkeyed — the leg stages the '
                'keyed contract: the diagnosis is a reading only a '
                'run holding the pair key can make')
        if not subject.get('active') or not subject.get('standby'):
            return case.finish(
                'inconclusive',
                'the probe pair\'s monitor endpoints are absent — '
                'the leg needs both peers\' /role and /promote '
                'surfaces')
        missing = [key for key in
                   ('start_driven', 'stop_driven', 'field_request',
                    'hold_field_claim', 'drop_field_claim',
                    'relaunch_controller')
                   if subject.get(key) is None]
        if missing:
            return case.finish(
                'inconclusive',
                'the probe subject stages no ' + ', '.join(missing)
                + ' seam — the staged run predates the raw '
                'field-attachment and unkeyed-launch levers the leg '
                'stages its foreign writers through')
        if not subject.get('driven'):
            return case.finish(
                'inconclusive',
                'the probe pair\'s driven endpoint is absent — the '
                'usurper paces through it')
        tokens = subject.get('plant_owner') or {}
        if not all(tokens.get(name)
                   for name in ('active', 'standby', 'driven')):
            return case.finish(
                'inconclusive',
                'no probe claim-owner pins are staged — the leg '
                'needs each peer\'s --owner token to attribute the '
                'claims and the fencing verdicts')
        journal_files = subject.get('journal_files')
        if not isinstance(journal_files, dict) \
                or not all(journal_files.get(name)
                           for name in ('active', 'standby')):
            return case.finish(
                'inconclusive',
                'no probe journal files are staged — the leg '
                'audits the reclaim\'s durable record')
        if FOREIGN_OWNER in tokens.values():
            return case.finish(
                'inconclusive',
                'the staged foreign token collides with a rig pin '
                '— the induction would attribute to a rig peer')
        reports = {name: _try_role(subject, name)
                   for name in ('active', 'standby')}
        if all(report is None for report in reports.values()):
            return case.finish(
                'inconclusive',
                'the probe pair is unreachable — the leg needs '
                'the serving monitors to audit the verdict')
        # The claim surface gates the staging posture before the
        # pair's roles: an unreadable claim read is the rig never
        # reached the posture the leg stages on.
        baseline = _claim_probe(subject)
        if baseline is None:
            return case.finish(
                'inconclusive',
                'the probe field\'s claim surface answered no '
                'probe — the staged run predates the raw claim '
                'read the leg audits')
        if not _fenced(baseline):
            return case.finish(
                'inconclusive',
                'the probe claim surface answered no fenced '
                'verdict — the field is unclaimed or the claim '
                'read surface is absent: '
                + json.dumps(baseline, sort_keys=True)[:200])
        if not _verdict_declares_monitor(baseline):
            return case.finish(
                'inconclusive',
                'the probe claim verdict names no declared '
                'monitor — the staged run predates the '
                'field-arbitrated monitor contract the usurped '
                'diagnosis dials: '
                + json.dumps(baseline, sort_keys=True)[:200])
        owner = _pair_active(subject)
        if owner != 'active':
            _restore_layout(subject)
            owner = _pair_active(subject)
            if owner != 'active':
                return case.finish(
                    'inconclusive',
                    'the probe pair never settled to its launch '
                    'layout — the leg stages each pass on '
                    'active-owning plus standby-tracking')
        if _verdict_owner(baseline) != tokens['active']:
            return case.finish(
                'inconclusive',
                'the standing claim names '
                + str(_verdict_owner(baseline))
                + ' — the probe active\'s pin '
                + str(tokens['active']) + ' expected: '
                + json.dumps(baseline, sort_keys=True)[:200])
        if wait_for(
                lambda: _tracking_standby(subject, 'standby')
                or None,
                time.monotonic() + USURPED_SETTLE,
                interval=USURPED_POLL) is None:
            return case.finish(
                'inconclusive',
                'the probe pair never reported a settled tracking '
                'standby — the leg stages on a healthy '
                'owner-plus-tracker pair')
        case.observe('usurped-claim pass: unkeyed driven usurper '
                     'preempts the probe field, keyed peers must '
                     'report usurped and reclaim through promote; '
                     'monitor-less, dead-declared, and unkeyed-run '
                     'absence halves keep orphaned plus the '
                     'conditional refusal; foreign claim token '
                     + str(FOREIGN_OWNER))

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _usurp_pass(subject, number)
            record['owner_token'] = tokens['active']
            record['driven_token'] = tokens['driven']
            _judge_usurped(record, note)
            digest = _usurped_digest(record, violations)
            evidence['record'] = record
            evidence['digest'] = digest
            evidence['violations'] = {
                key: {'diagnostic': diagnostic, 'detail': detail}
                for key, (diagnostic, detail) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'usurped-foreign-claim-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref,
                          'the leg\'s normalized record of pass '
                          + str(number) + ' — the usurper\'s live '
                          'claim, the keyed peers\' usurped '
                          'verdicts and pair-fault surface, the '
                          'reclaim\'s journaled foreign_claim_'
                          'preempted naming the usurper\'s '
                          'endpoint, the absence halves\' verdicts '
                          'and refusals, and the normalized digest')
            digests.append(digest)
            if violations:
                diagnostic, detail = next(iter(violations.values()))
                return case.finish(
                    'failed', diagnostic + ': ' + detail)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'outcome digests diverged — '
                + json.dumps(digests, sort_keys=True)[:400])
        slipped = _self_check()
        if slipped:
            return case.finish(
                'failed', DIAG_UNCHECKED + ': ' + str(slipped)[:400])
        case.observe('usurped-claim episode: two passes identical; '
                     'the live unkeyed writer was fenced, the keyed '
                     'peers reported usurped beside field_claim '
                     'held, promote reclaimed the field journaling '
                     'the usurper\'s endpoint, the absence halves '
                     'refused, and the verdict cleared under the '
                     'keyed writer — '
                     + json.dumps(digests[0], sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish(
            'inconclusive',
            'the leg could not stage the usurped-claim pass: '
            + str(exc))
