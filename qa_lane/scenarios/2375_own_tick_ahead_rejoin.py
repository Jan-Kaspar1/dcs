"""The own_tick_ahead_rejoin acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg drives the deployed pair out of its launch layout
# and puts it back — the reconverged ex-owner re-promoted onto the
# field with its successor tracking it behind — so it needs the
# stranded-standby leg's own restore to have seated the launch layout
# it frames.
RUNS_AFTER = frozenset({'scenario_stranded_standby_no_resync'})


# --------------------------------------------------------------------
# The demoted ex-owner's ahead-bound line rejoin contract — the
# per-revision lane evidence for the fix contract #1269 establishes
# (WW-LCM-001's continuity clause): a fenced or demoted field owner
# that declares no configured `--standby` must re-join the line
# through the field-arbitrated claimed-monitor rendezvous
# (`adopt_claimed_source`, dcs-monitor lib.rs ~L3059 — the unkeyed
# pair's only provable rendezvous) *regardless of how far its own
# paced tick has drifted from the line's*. `verify_owner_checkpoint`
# and `verify_announced_checkpoint` must not treat the prober's own
# tick plus `MAX_ANNOUNCED_AHEAD` (32) as the line-membership
# reference: an ex-owner's own clock has no authority over where the
# line moved.
#
# The defect this leg stages is self-reinforcing and journal-empty. A
# tracking peer's paced clock keeps counting through every source
# outage it survives while the pulled stream stands still, so the lead
# it accrues is permanent and the promotion carries it. Once the
# promoted successor's served tick led the demoted ex-owner's own by
# more than the thirty-two-tick window, the ex-owner's verify read
# that lead as a forged position, refused the field's own answer
# `Ahead` on every unkeyed rejoin path, and — with no configured
# source, no proven announced hint, and no line proof to fall back on
# — stranded `standby`/`unsynchronized` with a silent journal: the
# conditional startup grant stood refused while the claim did, and the
# only recovery the operator had was a restart-as-standby. #1336
# retired the bound outright, because a detached prober's `tick` and
# `stream_tick` alike advance at its own scan cadence — the gap
# measures pace asymmetry, never line membership.
#
# The leg stages the defect's own shape on the deployed paced pair:
# `active` (ctrl-a) is the field owner declaring no pair at all, so the
# claim's declared monitor is the only candidate its re-join can
# resolve, and `standby` (ctrl-b) is the pair's `--standby` tracker.
#
# - the staging: `stop_controller` on the owner holds its tracking
#   source down while the tracker keeps its 100 ms cadence, so every
#   pull is a produced-nothing miss while the tracker's own run tick
#   advances a scan at a time and the line's stands frozen. The leg
#   waits for the *measured* separation to clear the retired window
#   plus a margin, brings the owner back, waits for the pair to
#   reconverge, and reads both members' own served run ticks again to
#   record the lead the promotion will carry. A promotion computed
#   inside the retired window would prove nothing about one computed
#   past it, so a staging that never separated reports
#   nondeterministic and the leg stops there.
# - the lead-carrying document: the successor's own `/checkpoint`
#   before the promote. It must carry the declared stream position
#   (#1269's own surface), and that declaration must sit at or behind
#   the run tick it rides — the document's own consistency, the one
#   positional check an honest run passes. A document declaring a
#   position ahead of its own tick is the nonsense shape and a
#   contract failure; a document declaring no position at all is a
#   pinned revision predating the contract, and the leg reports
#   inconclusive there instead of asserting.
# - the routine promote: `POST /promote` on the lead-carrying tracker
#   with no `POST /demote` on the owner first, so the field's
#   arbitration preempts the standing claim and the owner's first
#   fenced write demotes it in place. The promote gate's own named
#   refusals (`not_converged`, `already_active`, `no_tracking_source`)
#   answer before any switchover and name a race in the staging rather
#   than the contract, so they report nondeterministic.
# - the re-join, asserted through the demoted ex-owner's serving
#   monitor and its runner-owned `--journal-file`: the sync verdict
#   converging to `tracking` rather than parking `unsynchronized` past
#   the documented tick bound, the fenced-origin
#   `active → demoting → standby` walk beside exactly one
#   `field_claim_lost` attributed to the promoted claim, the field's
#   own post-promotion fencing verdict naming that claim and its
#   declared monitor, exactly one `tracking_source_adopted` naming the
#   successor's declared endpoint, no `tracking_source_refused` naming
#   the successor, the re-joined peer's served document stamping the
#   successor's ownership honestly, and no further `run_boundary` — the
#   operator's restart-as-standby must not be what converged it.
# - the documented switch back: `POST /promote` on the converged
#   ex-owner answers `promoting` and the pair returns to its launch
#   roles, framed again once the pass's own claim is gone.
#
# Named diagnostics: ahead-bound-rejoin-failed tags the contract
# clauses — a demoted ex-owner that never re-joins or re-joins past the
# documented bound, a re-join whose claimed-monitor adoption is absent,
# duplicated or names another endpoint, a journaled source refusal
# naming the successor, an adoption a restart performed, an
# unattributed loss or an unwalked demotion, a fencing verdict that
# does not name the promoted claim or its declared monitor, a
# re-joined peer whose served document does not stamp the successor's
# ownership honestly, a lead-carrying document whose own declaration
# is nonsense, a later promote that does not answer the converged path,
# or launch roles left unrestored — while
# ahead-bound-rejoin-nondeterministic tags the instability the
# contract does not answer for: a refused staging call, a lead that
# never separated past the retired window, a pair that never
# reconverged after the thaw, a promote the convergence gate answered
# before any switchover, an unanswered promote, a starved re-join
# watch, a journal floor or durable sink that never served, and two
# passes whose digests diverge.
#
# No staged revision predates this contract in a shape the leg can
# declare on the evidence: an ex-owner that stays unsynchronized is the
# defect the leg names, not an unread surface, so every inconclusive
# verdict here is rig-side or pre-contract — an absent seam, an
# unreachable endpoint, a pair off its launch layout, a fencing verdict
# or served document missing the claim-declared monitor and the
# ownership stamps the rendezvous rides, a role report with no sync
# vocabulary, or a lead-carrying document declaring no stream
# position. The unchecked-diagnostic self-check replays the judge over
# planted negatives — the issue's doctored case, a re-join asserted
# while the ex-owner stays unsynchronized past the bound, the adoption
# missing or foreign, a journaled refusal, a restart that performed
# the re-join, an unattributed loss, an unwalked demotion, a foreign
# verdict, a dishonest served document, a refused switch back, unrestored
# launch roles, and the instability shapes — and reports
# ahead-bound-rejoin-unchecked for any that slip through.

OWNER = 'active'         # the launch field owner: declares no pair, so
                          # the claim's declared monitor is the only
                          # candidate its re-join can resolve
PEER = 'standby'         # the pair's --standby tracker, promoted
PAIR_PORTS = {'active': 8080, 'standby': 8081}
AHEAD_BOUND = 32         # the retired MAX_ANNOUNCED_AHEAD window, in
                          # run ticks: the lead the staging must clear
                          # before the promote, so the demoted
                          # ex-owner's own position sits past the bound
                          # the defect refused on
LEAD_MARGIN = 16         # the extra ticks the outage accrues before the
                          # thaw, so the measured lead survives the
                          # re-convergence reads the restart costs
REJOIN_TICKS = 60        # the documented lane bound on the re-join, in
                          # the demoted ex-owner's own paced scans — the
                          # finding's indefinite unsynchronized wedge is
                          # the failure this bounds
SETTLE = 45              # bound on the launch layout settling, the
                          # demotion watch, and the pair's re-seat
LEAD_SETTLE = 25         # bound on the outage's lead accrual
OUTAGE_SETTLE = 30       # bound on the thawed owner resuming and the
                          # pair reconverging
REJOIN_SETTLE = 30       # bound on the claimed-monitor re-join
RESTORE_SETTLE = 30      # bound on the documented switch back
AHEAD_POLL = 0.4         # cadence watching the pair mid-episode
DIAG_FAILED = 'ahead-bound-rejoin-failed'
DIAG_NONDET = 'ahead-bound-rejoin-nondeterministic'
DIAG_UNCHECKED = 'ahead-bound-rejoin-unchecked'

# The promote gate's own named refusals — the answers `POST /promote`
# gives before a switchover is computed at all. A promote answered one
# of these never reached the field's arbitration, so it names a race in
# the staging rather than the contract, and the leg reports it
# nondeterministic.
GATE_CAUSES = ('not_converged', 'already_active', 'no_tracking_source')


def _ahead_posture(ctx, name):
    """One normalized `/role` read — the reported role, the sync
    verdict's name, and the served run tick — or None when the monitor
    dropped the read. One shape for every read the leg makes, so the
    judge and the digest read one vocabulary."""
    report = _try_role(ctx, ctx[name])
    if not isinstance(report, dict):
        return None
    sync = report.get('sync')
    if isinstance(sync, dict) and sync:
        kind = str(next(iter(sync)))
    elif isinstance(sync, str) and sync:
        kind = sync
    else:
        kind = 'missing'
    tick = report.get('tick')
    return {'role': report.get('role'), 'sync': kind,
            'tick': tick if isinstance(tick, int)
            and not isinstance(tick, bool) else None}


def _ahead_pair(ctx, owner, peer, bound):
    """Both members' normalized postures once the pair stands on the
    launch layout — the launch owner writing the field with the peer
    tracking it — or None inside `bound` seconds. Every settle the leg
    performs (the initial frame, the post-thaw reconvergence, the
    documented switch back) reads through here, so the layout the leg
    demands is one definition."""
    def seated():
        roles = {name: _ahead_posture(ctx, name)
                 for name in (owner, peer)}
        if (roles.get(owner) or {}).get('role') != 'active':
            return None
        if (roles.get(peer) or {}).get('sync') != 'tracking':
            return None
        return roles
    return wait_for(seated, time.monotonic() + bound,
                    interval=AHEAD_POLL)


def _ahead_lead(ctx, owner, peer):
    """The two members' own served run ticks and the promotion's
    measured lead — the prober's own clock against the line's. None
    while either side's tick is unreadable."""
    owner_tick = (_ahead_posture(ctx, owner) or {}).get('tick')
    peer_tick = (_ahead_posture(ctx, peer) or {}).get('tick')
    if not isinstance(owner_tick, int) or not isinstance(peer_tick, int):
        return None
    return {'owner': owner_tick, 'peer': peer_tick,
            'lead': peer_tick - owner_tick}


def _ahead_past(lead):
    """Whether a measured lead sits past the retired ahead bound — the
    staging's own gate. A promotion carried on a basis inside the bound
    says nothing about the strand the defect held."""
    return isinstance(lead, dict) and isinstance(lead.get('lead'), int) \
        and lead['lead'] > AHEAD_BOUND


def _ahead_endpoint(value, endpoint):
    """Whether a recorded monitor address resolves one pair member's
    declared endpoint — the address the field's own claim arbitration
    vouches for, and the one the adoption record must name."""
    return str(value or '').endswith(':' + str(PAIR_PORTS[endpoint]))


def _ahead_records(ctx, name):
    """The peer's durable `--journal-file` records, or None when the
    file cannot be read at all — an unreadable sink is the rig's
    staging surface, never a journal that recorded nothing."""
    path = (ctx.get('journal_files') or {}).get(name)
    if not path or not Path(path).is_file():
        return None
    try:
        return _journal_entries(path)
    except Exception:
        return None


def _ahead_durable(ctx, name, floor, kind):
    """The `kind` event bodies the peer's durable journal carries
    since `floor` records — the audit surface the claimed monitor's
    adoption, its refusals, the fenced loss, and the demote walk are
    read on. None when the file or the floor never served."""
    records = _ahead_records(ctx, name)
    if records is None or not isinstance(floor, int):
        return None
    out = []
    for item in records[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _ahead_boundaries(ctx, name):
    """The process lifetimes the peer's durable journal records — one
    `run_boundary` per resumed run. The strand this leg closes was
    recoverable only by an operator restart-as-standby, so a boundary
    landing inside the re-join window is that remedy showing up in the
    record instead of the contract converging."""
    records = _ahead_records(ctx, name)
    if records is None:
        return None
    return sum(1 for item in records if 'run_boundary' in item)


def _ahead_floors(ctx, names):
    """The served journal cursors the durable audit reads from — one
    per named peer, or None when that peer's journal never served."""
    out = {}
    for name in names:
        try:
            out[name] = _journal_cursor(ctx, ctx[name])
        except Exception:
            out[name] = None
    return out


def _ahead_verdict(response):
    """One plant-protocol answer's claim arbitration: the standing
    claim's owner token and its declared monitor, beside whether the
    mutation was fenced at all. None when the probe itself was lost —
    one dropped read, never a verdict."""
    if not isinstance(response, dict):
        return None
    error = response.get('error') or {}
    return {'fenced': error.get('kind') == 'fenced',
            'owner': error.get('owner'), 'monitor': error.get('monitor')}


def _ahead_probe(ctx):
    """One bare `step` probe on the published plant port — the field's
    own fencing answer naming the standing claim's owner and declared
    monitor. It never sends a claim op: claiming from here would
    preempt the owner it is reading about."""
    return _ahead_verdict(_try_plant(ctx, {'op': 'step', 'dt': 0}))


def _ahead_refusal(body):
    """The cause a switchover refusal names — a bare variant string
    (`"already_active"`) or the single key of the object form
    (`{"not_converged": {...}}`) — or None when it names none."""
    if isinstance(body, str):
        return body or None
    if isinstance(body, dict) and len(body) == 1:
        return next(iter(body)) or None
    return None


def _ahead_switch(ctx, name):
    """One `POST /promote` and its answer: `{'status', 'body',
    'cause'}`. A refusal's named cause is in the body, so it is read
    here rather than through a 2xx-only helper. None when the request
    itself went unanswered, which is the judge's instability class."""
    base = ctx.get(name)
    if not base:
        return None
    try:
        status, body = http_json('POST', base + '/promote')
    except urllib.error.HTTPError as exc:
        try:
            status, body = exc.code, json.loads(exc.read() or b'null')
        except ValueError:
            status, body = exc.code, None
        finally:
            exc.close()
    except Exception:
        return None
    return {'status': status, 'body': body, 'cause': _ahead_refusal(body)}


def _ahead_granted(answer):
    """Whether one switchover answer is the applied transition the
    documented switch owes — a 200 carrying the `promoting` report."""
    return isinstance(answer, dict) and answer.get('status') == 200 \
        and isinstance(answer.get('body'), dict) \
        and answer['body'].get('role') == 'promoting'


def _ahead_document(ctx, name):
    """The peer's served checkpoint document's own line membership: its
    run tick, its declared stream position and whether the field
    declares one at all, its field-ownership stamps, and its propagated
    line owner — the surface the verify checks and the re-joined peer's
    honest stamps are read on. None when the document never served."""
    try:
        _, body = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    tick = body.get('tick')
    stream = body.get('stream_tick')
    return {'tick': tick if isinstance(tick, int)
            and not isinstance(tick, bool) else None,
            'stream': stream if isinstance(stream, int)
            and not isinstance(stream, bool) else None,
            'declared': 'stream_tick' in body,
            'stamped': 'source_owns_field' in body
            and 'line_owner' in body,
            'owns': body.get('source_owns_field'),
            'line_owner': body.get('line_owner')}


def _ahead_surfaces(ctx, owner, peer):
    """The contract's own served surface on both members: the
    ownership stamps, an integer run tick, and the sync vocabulary the
    re-join is judged on. Returns a stable reason string when a pinned
    revision predates any of them, else None — the pre-contract
    signature the driver reports inconclusive on rather than
    asserting."""
    for name in (owner, peer):
        document = _ahead_document(ctx, name)
        if document is None:
            return ('the served checkpoint on ' + name + ' never '
                    'answered — the field-arbitrated monitor contract '
                    'has no document to verify')
        if not document['stamped']:
            return ('the served checkpoint on ' + name + ' carries no '
                    'field-ownership stamps — the pinned revision '
                    'predates the field-arbitrated monitor contract')
        if document['tick'] is None:
            return ('the served checkpoint on ' + name + ' carries no '
                    'integer run tick — the pinned revision predates '
                    'the tick-domain contract the lead rides')
        report = _ahead_posture(ctx, name)
        if report is None:
            return ('the monitor on ' + name + ' never answered /role '
                    '— the served surface the re-join is judged on is '
                    'absent')
        if report['role'] == 'standby' and report['sync'] == 'missing':
            return ('the role report on ' + name + ' carries no sync '
                    'vocabulary — the pinned revision predates the '
                    'convergence verdicts the re-join converges to')
    return None


def _ahead_reseat(ctx, owner, peer):
    """The documented re-seat for a pair whose roles an earlier leg
    moved: the current writer demotes, the launch owner promotes once
    it has converged, and the pair returns to the launch layout. The
    record carries what the re-seat did, so the evidence never reads a
    leg-performed frame as the rig's own settled posture."""
    taken = {'demoted': [], 'converged': None, 'promoted': None,
             'layout': None}

    def converged():
        report = _ahead_posture(ctx, owner)
        return report if report and report['role'] == 'standby' \
            and report['sync'] == 'tracking' else None

    for name in (peer, owner):
        report = _ahead_posture(ctx, name)
        if (report or {}).get('role') in ('active', 'promoting'):
            status, body = _settle_call(ctx[name] + '/demote')
            taken['demoted'].append({'peer': name, 'status': status,
                                     'body': body})
    taken['converged'] = wait_for(converged, time.monotonic() + SETTLE,
                                  interval=AHEAD_POLL)
    if taken['converged'] is not None:
        taken['promoted'] = _ahead_switch(ctx, owner)
    taken['layout'] = _ahead_pair(ctx, owner, peer, SETTLE)
    return taken


def _ahead_outage(ctx, owner, peer):
    """The ahead-bound staging: the launch owner's tracking source goes
    down while the tracker keeps its cadence, so its own run tick
    accrues the permanent lead the promotion carries; the owner comes
    back and the pair reconverges. Returns the record's `stage`
    section; a refused lever or a lead that never separated leaves the
    section's later keys absent for the judge to name."""
    stage = {'outage': None, 'before': _ahead_posture(ctx, owner),
             'frozen': None, 'accrued': None, 'reconverged': None,
             'lead': None, 'document': None}
    frozen = (stage['before'] or {}).get('tick')
    if not isinstance(frozen, int):
        stage['outage'] = ('unreadable: the owner\'s served tick never '
                           'answered, so the frozen line position is '
                           'unknown')
        return stage
    stage['frozen'] = frozen
    try:
        ctx['stop_controller'](owner)
    except Exception as exc:
        stage['outage'] = 'stop refused: ' + str(exc)[:160]
        return stage
    stage['outage'] = 'stopped'

    def accrued():
        report = _ahead_posture(ctx, peer)
        tick = (report or {}).get('tick')
        if not isinstance(tick, int) or \
                tick - frozen <= AHEAD_BOUND + LEAD_MARGIN:
            return None
        return {'owner': frozen, 'peer': tick, 'lead': tick - frozen}

    stage['accrued'] = wait_for(accrued, time.monotonic() + LEAD_SETTLE,
                                interval=AHEAD_POLL)
    if stage['accrued'] is None:
        return stage
    try:
        ctx['start_controller'](owner)
    except Exception as exc:
        stage['outage'] = 'restart refused: ' + str(exc)[:160]
        return stage
    stage['outage'] = 'restarted'
    stage['reconverged'] = _ahead_pair(ctx, owner, peer, OUTAGE_SETTLE)
    stage['lead'] = _ahead_lead(ctx, owner, peer)
    return stage


def _ahead_stream(ctx, peer):
    """The lead-carrying successor's own served document: the run tick
    its promotion carries and the stream position that tick occupies in
    the line's origin domain. `predates` on the section when the
    document declares no integer stream position at all — the pinned
    revision predating #1269's declared stream-lead contract, which the
    driver reports inconclusive on."""
    document = _ahead_document(ctx, peer)
    if document is None:
        return {'predates': 'the promoted successor serves no readable '
                            'checkpoint document — the declared '
                            'stream-lead contract has nothing to read'}
    if document['stream'] is None:
        return {'predates': 'the lead-carrying successor declares no '
                            'integer stream position — the pinned '
                            'revision predates the declared stream-lead '
                            'contract the re-join verifies on',
                'document': document}
    return {'document': document}


def _ahead_switchover(ctx, owner, peer):
    """The routine promote on the lead-carrying tracker and the
    supersede it forces: the field's arbitration preempts the owner's
    standing claim and the owner's first fenced write demotes it in
    place. Returns the record's `switch` section with the durable loss,
    the demote walk, and the field's own post-promotion verdict."""
    switch = {'floors': _ahead_floors(ctx, (owner, peer)),
              'boundaries': {name: _ahead_boundaries(ctx, name)
                             for name in (owner, peer)},
              'posture': _ahead_posture(ctx, peer),
              'promote': None, 'walk': [], 'demoted': None,
              'promoted_after': None, 'losses': None, 'roles': None,
              'verdict': None}
    answer = _ahead_switch(ctx, peer)
    switch['promote'] = answer
    if not _ahead_granted(answer):
        return switch
    watch = []

    def walked():
        report = _ahead_posture(ctx, owner)
        if report is None:
            return None
        watch.append(report)
        return report if report['role'] == 'standby' else None

    switch['demoted'] = wait_for(walked, time.monotonic() + SETTLE,
                                 interval=AHEAD_POLL)
    switch['walk'] = [row['role'] for row in watch]
    switch['promoted_after'] = _ahead_posture(ctx, peer)
    floor = (switch['floors'] or {}).get(owner)
    switch['losses'] = _ahead_durable(ctx, owner, floor,
                                      'field_claim_lost')
    walk = _ahead_durable(ctx, owner, floor, 'role_changed')
    switch['roles'] = [(event.get('from'), event.get('to'),
                        event.get('origin')) for event in walk or []]
    switch['verdict'] = _ahead_probe(ctx)
    return switch


def _ahead_rejoin(ctx, owner, switch):
    """The bounded re-join: the demoted ex-owner's sync converging to
    `tracking` rather than parking `unsynchronized`, and the durable
    evidence of the rendezvous it converged through — the claimed
    monitor's adoption, the refusals naming the successor, and the
    process lifetimes that must not have grown. Returns the record's
    `rejoin` and `adoption` sections."""
    demoted = switch.get('demoted') or {}
    rows, tracked = [], None
    if demoted:
        def rejoin():
            report = _ahead_posture(ctx, owner)
            if report is None:
                return None
            rows.append(report)
            return report if report['role'] == 'standby' \
                and report['sync'] == 'tracking' else None
        tracked = wait_for(rejoin, time.monotonic() + REJOIN_SETTLE,
                           interval=AHEAD_POLL)
    rejoin = {'rows': rows, 'tracked': tracked, 'ticks': None}
    settle = demoted.get('tick')
    landed = (tracked or {}).get('tick')
    if isinstance(settle, int) and isinstance(landed, int):
        rejoin['ticks'] = landed - settle
    floor = (switch.get('floors') or {}).get(owner)
    adoption = {'boundaries': _ahead_boundaries(ctx, owner),
                'adoptions': _ahead_durable(ctx, owner, floor,
                                            'tracking_source_adopted'),
                'refusals': _ahead_durable(ctx, owner, floor,
                                           'tracking_source_refused'),
                'document': _ahead_document(ctx, owner)}
    return rejoin, adoption


def _ahead_restore(ctx, owner, peer):
    """The documented switch back: the reconverged ex-owner's promote
    answers the converged path and the pair returns to its launch
    roles — the successor demoting in place behind the owner's own
    configured source."""
    return {'promote': _ahead_switch(ctx, owner),
            'layout': _ahead_pair(ctx, owner, peer, RESTORE_SETTLE)}


def _ahead_pass(ctx, number, owner, peer):
    """One pass over the contract: frame the pair, stage the
    successor's permanent ahead-of-the-bound lead through the lane's
    controller lifecycle, promote the lead-carrying tracker, assert the
    fenced ex-owner's claimed-monitor re-join, and put the pair back on
    its launch roles. The final framing belongs to the caller: the
    restore only means anything once the switch back has landed."""
    record = {'pass': number, 'launch': {'owner': owner, 'peer': peer},
              'roles': {}, 'stage': {}, 'switch': {}, 'rejoin': {},
              'adoption': {}, 'restore': {}}
    record['roles']['before'] = {
        name: _ahead_posture(ctx, name) for name in (owner, peer)}
    stage = _ahead_outage(ctx, owner, peer)
    if stage.get('outage') == 'restarted' and _ahead_past(stage.get('lead')):
        stage.update(_ahead_stream(ctx, peer))
    record['stage'] = stage
    if stage.get('predates'):
        record['predates'] = stage['predates']
        record['roles']['after'] = dict(record['roles']['before'])
        return record
    switch = _ahead_switchover(ctx, owner, peer)
    record['switch'] = switch
    rejoin, adoption = _ahead_rejoin(ctx, owner, switch)
    record['rejoin'] = rejoin
    record['adoption'] = adoption
    record['roles']['after'] = {
        name: _ahead_posture(ctx, name) for name in (owner, peer)}
    record['restore'] = _ahead_restore(ctx, owner, peer)
    return record


def _ahead_walked(roles):
    """Whether a demote walk is exactly the documented fenced-origin
    `active → demoting → standby` pair and nothing else."""
    return roles == [('active', 'demoting', 'fenced'),
                     ('demoting', 'standby', 'fenced')]


def _ahead_judge(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: DIAG_FAILED tags the contract clauses and
    DIAG_NONDET the instability the contract does not answer for. An
    aborted stage ends the audit where the pass ended; the keys it
    never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    launch = record.get('launch') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    tokens = record.get('tokens') or {}
    stage = record.get('stage') or {}

    # ---- the staging: a measured lead past the retired bound on a pair
    # that reconverged behind the thaw.
    outage = stage.get('outage')
    if outage != 'restarted':
        nondet('stage-outage', 'the tracking-source outage never stood: '
               + str(outage))
    elif stage.get('accrued') is None:
        nondet('stage-lead', 'the outage accrued no run-tick lead past '
               'the retired ahead bound of ' + str(AHEAD_BOUND)
               + ' ticks from the frozen position ' + str(stage.get(
                   'frozen')) + ' — a promotion carried on a basis '
                   'inside the bound proves nothing about the strand the '
                   'contract closed')
    elif stage.get('reconverged') is None:
        nondet('stage-converge', 'the pair never reconverged after the '
               'tracking source came back: '
               + json.dumps(stage.get('reconverged')))
    elif not _ahead_past(stage.get('lead')):
        nondet('stage-lead', 'the promoted successor\'s own tick read '
               + json.dumps(stage.get('lead')) + ' against the demoted '
               'ex-owner\'s own — short of the retired ahead bound of '
               + str(AHEAD_BOUND) + ' ticks, so the promotion never '
               'carried the lead the defect stranded on')

    document = stage.get('document')
    if outage == 'restarted' and isinstance(document, dict) \
            and document['stream'] is not None \
            and isinstance(document['tick'], int) \
            and document['stream'] > document['tick']:
        failed('stream-nonsense', 'the lead-carrying successor declares '
               'stream position ' + str(document['stream']) + ' against '
               'its own run tick ' + str(document['tick']) + ' — a '
               'declared position ahead of the tick it rides is the '
               'document shape the verify refuses, not an honest accrued '
               'lead')

    switch = record.get('switch') or {}
    answer = switch.get('promote')
    posture = switch.get('posture') or {}
    if outage != 'restarted':
        pass
    elif posture.get('sync') != 'tracking':
        nondet('stage-promote-posture', 'the successor reported '
               + json.dumps(posture) + ' immediately before its promote '
               '— the routine switchover rides on a converged tracker')
    elif answer is None:
        nondet('promote-unanswered', 'the routine POST /promote on the '
               'lead-carrying successor produced no readable answer')
    elif not _ahead_granted(answer):
        cause = answer.get('cause')
        if cause in GATE_CAUSES:
            nondet('promote-gated', 'the promote was answered by the '
                   'gate\'s own ' + str(cause) + ' refusal before any '
                   'switchover was computed: '
                   + json.dumps(answer.get('body'))[:300])
        else:
            failed('promote-refused', 'the routine POST /promote on the '
                   'converged lead-carrying successor answered '
                   + str(answer.get('status')) + ' '
                   + json.dumps(answer.get('body'))[:300]
                   + ' — the field\'s own arbitration takes the claim '
                     'from a converged tracker unconditionally')
    else:
        _ahead_supersede(record, switch, owner, peer, tokens,
                         failed, nondet)

    # ---- the documented switch back and the launch roles.
    restore = record.get('restore') or {}
    if not restore:
        return
    answer = restore.get('promote')
    if answer is None:
        nondet('restore-unanswered', 'the documented switch back — '
               'POST /promote on the converged ex-owner — produced no '
               'readable answer')
    elif not _ahead_granted(answer):
        failed('restore-refused', 'POST /promote on the converged '
               'ex-owner answered ' + str(answer.get('status')) + ' '
               + json.dumps(answer.get('body'))[:300]
               + ' — a re-joined line must answer the converged path')
    if restore.get('layout') is None:
        failed('layout-unrestored', 'the pair never returned to its '
               'launch roles after the documented switch: '
               + json.dumps(restore.get('layout'))[:300])
    final = (record.get('roles') or {}).get('final') or {}
    if final.get(owner, {}).get('role') != 'active' \
            or final.get(peer, {}).get('sync') != 'tracking':
        failed('layout-unrestored', 'the pair\'s launch roles did not '
               'hold once the pass\'s own claim was gone: '
               + json.dumps(final)[:300])


def _ahead_supersede(record, switch, owner, peer, tokens, failed, nondet):
    """The clauses the granted switchover owes: the fenced demote in
    place with its attributed loss and its fenced walk, the field's own
    post-promotion verdict naming the promoted claim and its declared
    monitor, the ex-owner's bounded re-join through that declared
    monitor, and the durable adoption evidence — with no restart and no
    refusal standing in for the rendezvous."""
    walk = switch.get('walk') or []
    unexpected = [role for role in walk
                  if role not in ('active', 'promoting', 'demoting',
                                  'standby')]
    if switch.get('demoted') is None:
        failed('demotion-held', 'the superseded ex-owner never demoted '
               'in place — its role walk stayed ' + json.dumps(walk))
    elif unexpected:
        failed('demotion-walk', 'the superseded ex-owner reported an '
               'unexpected role walk ' + json.dumps(walk)
               + ' — the demote-in-place walks only '
                 'active → demoting → standby')
    promoted = switch.get('promoted_after') or {}
    if promoted.get('role') != 'active':
        failed('successor-inactive', 'the promoted successor reports '
               + json.dumps(promoted) + ' after taking the claim — the '
               'field would stand unwritten')
    losses = switch.get('losses')
    if losses is None:
        nondet('loss-unreadable', 'the superseded ex-owner\'s durable '
               'journal never served, so the claim loss the demotion '
               'owes cannot be audited')
    elif not losses:
        failed('loss-silent', 'the superseded ex-owner journaled no '
               'field_claim_lost during the supersede — the fence fired '
               'and nothing recorded it')
    elif len(losses) > 1:
        failed('loss-duplicated', 'the superseded ex-owner journaled '
               + str(len(losses)) + ' field_claim_lost records for one '
               'supersede — one record per held claim, not one per '
               'fenced write')
    elif losses[0].get('claimant') != tokens.get(peer):
        failed('loss-unattributed', 'the journaled field_claim_lost '
               'names claimant ' + str(losses[0].get('claimant'))
               + ' — the promoted successor\'s own owner token '
               + str(tokens.get(peer)) + ' was expected')
    roles = switch.get('roles')
    if roles is None:
        nondet('walk-unreadable', 'the superseded ex-owner\'s durable '
               'journal never served, so the demote walk the fence owes '
               'cannot be audited')
    elif not _ahead_walked(roles):
        failed('demotion-unwalked', 'the superseded ex-owner\'s '
               'journaled role walk is ' + json.dumps(roles)
               + ' — the fenced active → demoting → standby walk the '
                 'supersede documents')
    verdict = switch.get('verdict')
    if verdict is None:
        nondet('verdict-unreadable', 'the field\'s own post-promotion '
               'fencing probe never answered — the claim arbitration '
               'the rendezvous rides cannot be audited')
    elif not verdict.get('fenced'):
        failed('verdict-open', 'a third-party mutation was not fenced '
               'after the promotion — the field\'s single-writer claim '
               'did not survive the switchover: '
               + json.dumps(verdict)[:200])
    elif verdict.get('owner') != tokens.get(peer):
        failed('verdict-foreign', 'the post-promotion fencing verdict '
               'names owner ' + str(verdict.get('owner')) + ' — the '
               'promoted successor\'s own token ' + str(tokens.get(peer))
               + ' was expected')
    elif verdict.get('monitor') is None:
        failed('verdict-undeclared', 'the promoted successor\'s claim '
               'declares no monitor — the field-arbitrated rendezvous '
               'the re-join resolves is missing')
    elif not _ahead_endpoint(verdict.get('monitor'), peer):
        failed('verdict-misnamed', 'the promoted claim declares monitor '
               + str(verdict.get('monitor')) + ' — it does not resolve '
               'the successor on :' + str(PAIR_PORTS[peer]))

    rejoin = record.get('rejoin') or {}
    adoption = record.get('adoption') or {}
    rows = rejoin.get('rows') or []
    tracked = rejoin.get('tracked')
    if not rows:
        nondet('rejoin-watch', 'the demoted ex-owner\'s monitor answered '
               'none of the re-join watch\'s reads — the bound the '
               'convergence owes was never observed')
    elif tracked is None:
        failed('rejoin-stranded', 'the demoted ex-owner never re-joined '
               'the line — the ahead-bound strand the contract closed: '
               'its sync readings stayed '
               + json.dumps([{'role': row.get('role'),
                              'sync': row.get('sync')} for row in rows])
               [:400])
    else:
        ticks = rejoin.get('ticks')
        if isinstance(ticks, int) and ticks > REJOIN_TICKS:
            failed('rejoin-late', 'the demoted ex-owner took '
                   + str(ticks) + ' of its own paced scans to re-join — '
                   'past the documented lane bound of '
                   + str(REJOIN_TICKS))
    boundaries = adoption.get('boundaries')
    was = (switch.get('boundaries') or {}).get(owner)
    if boundaries is None or was is None:
        nondet('adoption-unreadable', 'the demoted ex-owner\'s process '
               'lifetimes were never recorded — a restart cannot be '
               'ruled out of the re-join')
    elif boundaries != was:
        failed('restart-rejoined', 'a run boundary landed inside the '
               're-join window (' + str(was) + ' → ' + str(boundaries)
               + ') — the operator restart-as-standby the defect left '
                 'as its only recovery performed the convergence, not '
                 'the claimed-monitor rendezvous')
    adoptions = adoption.get('adoptions')
    refusals = adoption.get('refusals')
    if adoptions is None or refusals is None:
        nondet('adoption-unreadable', 'the demoted ex-owner\'s durable '
               'journal never served, so the claimed monitor\'s adoption '
               'and its refusals cannot be audited')
    else:
        named = [refusal for refusal in refusals
                 if _ahead_endpoint(refusal.get('source'), peer)]
        if named:
            failed('refusal-journaled', 'the demoted ex-owner journaled '
                   'a tracking-source refusal naming the successor it '
                   'owed an adoption — the pre-contract positional '
                   'refusal, durable evidence of the strand: '
                   + json.dumps(named)[:300])
        if not adoptions:
            failed('adoption-silent', 'the demoted ex-owner resolved no '
                   'verified tracking source — neither a journaled '
                   'adoption this re-join nor a durable pin naming the '
                   'declared monitor on :' + str(PAIR_PORTS[peer]))
        elif len(adoptions) > 1:
            failed('adoption-duplicated', 'the demoted ex-owner '
                   'journaled ' + str(len(adoptions))
                   + ' tracking_source_adopted records for one re-join — '
                     'the claimed-monitor rendezvous journals once')
        elif not _ahead_endpoint(adoptions[0].get('source'), peer):
            failed('adoption-foreign', 'the adopted source '
                   + str(adoptions[0].get('source')) + ' does not '
                   'resolve the endpoint the standing claim declared (:'
                   + str(PAIR_PORTS[peer]) + ')')
    served = adoption.get('document')
    if served is None:
        failed('document-unreadable', 'the re-joined ex-owner serves no '
               'checkpoint document — the adoption it journaled is not '
               'reflected on its own served line')
    elif served.get('owns') is not False:
        failed('document-foreign', 'the re-joined ex-owner serves a '
               'checkpoint whose source_owns_field is '
               + json.dumps(served.get('owns')) + ' — a tracking peer\'s '
               'honest stamp is the serving run\'s own non-ownership')
    elif not _ahead_endpoint(served.get('line_owner'), peer):
        failed('document-misnamed', 'the re-joined ex-owner serves '
               'line_owner ' + json.dumps(served.get('line_owner'))
               + ' — expected the promoted successor on :'
               + str(PAIR_PORTS[peer]))


def _ahead_digest(record, violations):
    """The pass's normalized verdict set — identical digests across two
    consecutive passes is the determinism contract. Each field holds
    its clean value only while no violation, contract or instability,
    names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'staging': 'ahead-of-the-bound'
            if clean('stage-outage', 'stage-lead', 'stage-converge',
                     'stage-promote-posture') else 'unstaged',
        'lead-declaration': 'honest'
            if clean('stream-nonsense') else 'nonsense',
        'switch': 'in-place'
            if clean('promote-unanswered', 'promote-gated',
                     'promote-refused', 'demotion-held', 'demotion-walk',
                     'successor-inactive', 'loss-unreadable', 'loss-silent',
                     'loss-duplicated', 'loss-unattributed',
                     'walk-unreadable', 'demotion-unwalked',
                     'verdict-unreadable', 'verdict-open',
                     'verdict-foreign', 'verdict-undeclared',
                     'verdict-misnamed') else 'disturbed',
        'rejoin': 'tracked'
            if clean('rejoin-watch', 'rejoin-stranded', 'rejoin-late')
            else 'stranded',
        'adoption': 'claimed-monitor'
            if clean('adoption-unreadable', 'restart-rejoined',
                     'refusal-journaled', 'adoption-silent',
                     'adoption-duplicated', 'adoption-foreign')
            else 'silent',
        'document': 'honest'
            if clean('document-unreadable', 'document-foreign',
                     'document-misnamed') else 'foreign',
        'promotable': 'granted'
            if clean('restore-unanswered', 'restore-refused')
            else 'refused',
        'layout': 'restored'
            if clean('layout-unrestored') else 'moved'}


def _ahead_clean_record():
    """The record the self-check's planted negatives start from: the
    outage accrued a measured lead past the retired bound, the pair
    reconverged, the lead-carrying document declared an honest
    position, the routine promote preempted the claim, the ex-owner
    demoted in place with its attributed loss and its fenced walk, the
    field's verdict named the promoted claim and its declared monitor,
    the ex-owner re-joined through the claimed monitor and journaled
    exactly one adoption naming it with no restart and no refusal, the
    re-joined document stamps the successor honestly, and the
    documented switch back answered the converged path on the launch
    roles."""
    posture = {'role': 'active', 'sync': 'none', 'tick': 900}
    tracking = {'role': 'standby', 'sync': 'tracking', 'tick': 940}
    return {
        'pass': 1,
        'launch': {'owner': OWNER, 'peer': PEER},
        'tokens': {'active': 424243, 'standby': 424244},
        'roles': {'before': {'active': dict(posture),
                             'standby': dict(tracking)},
                  'after': {'active': dict(posture),
                            'standby': dict(tracking)},
                  'final': {'active': dict(posture),
                            'standby': dict(tracking)}},
        'stage': {'outage': 'restarted',
                  'before': dict(posture),
                  'frozen': 900,
                  'accrued': {'owner': 900, 'peer': 960, 'lead': 60},
                  'reconverged': {'active': dict(posture),
                                  'standby': dict(tracking)},
                  'lead': {'owner': 900, 'peer': 960, 'lead': 60},
                  'document': {'tick': 960, 'stream': 940,
                               'declared': True, 'stamped': True,
                               'owns': False, 'line_owner': None}},
        'switch': {'floors': {'active': 12, 'standby': 12},
                   'boundaries': {'active': 2, 'standby': 1},
                   'posture': dict(tracking),
                   'promote': {'status': 200, 'cause': None,
                               'body': {'role': 'promoting',
                                        'tick': 960}},
                   'walk': ['active', 'demoting', 'standby'],
                   'demoted': {'role': 'standby',
                               'sync': 'unsynchronized', 'tick': 964},
                   'promoted_after': {'role': 'active', 'sync': 'none',
                                      'tick': 961},
                   'losses': [{'point': 20, 'claimant': 424244}],
                   'roles': [('active', 'demoting', 'fenced'),
                             ('demoting', 'standby', 'fenced')],
                   'verdict': {'fenced': True, 'owner': 424244,
                               'monitor': '172.20.0.5:8081'}},
        'rejoin': {'rows': [{'role': 'standby',
                             'sync': 'unsynchronized', 'tick': 965},
                            {'role': 'standby', 'sync': 'tracking',
                             'tick': 968}],
                   'tracked': {'role': 'standby', 'sync': 'tracking',
                               'tick': 968},
                   'ticks': 4},
        'adoption': {'boundaries': 2,
                     'adoptions': [{'source': '172.20.0.5:8081'}],
                     'refusals': [],
                     'document': {'tick': 968, 'stream': 950,
                                  'declared': True, 'stamped': True,
                                  'owns': False,
                                  'line_owner': '172.20.0.5:8081'}},
        'restore': {'promote': {'status': 200, 'cause': None,
                                'body': {'role': 'promoting',
                                         'tick': 970}},
                    'layout': {'active': dict(posture),
                               'standby': dict(tracking)}}}


def _ahead_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — the issue's doctored case, a re-join asserted while the
    ex-owner stays unsynchronized past the bound, the adoption missing,
    duplicated or foreign, a journaled source refusal, a restart that
    performed the re-join, an unattributed loss, an unwalked demotion,
    a foreign or undeclared verdict, a dishonest served document, a
    refused switch back, unrestored launch roles — and every
    instability class the leg reports nondeterministic, and report
    every one that let slip."""
    def audit(record):
        found = {}
        _ahead_judge(record,
                     lambda key, diagnostic, detail:
                     found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(_ahead_clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = _ahead_clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    # The issue's doctored negative: the re-join asserted while the
    # ex-owner stays unsynchronized past the documented bound. The
    # doctoring touches the served surface itself, so the judge — which
    # reads the record's own evidence and never a stored verdict — has
    # to catch it.
    expect('rejoin-asserted-while-stranded', lambda record:
           record['rejoin'].update(
               rows=[{'role': 'standby', 'sync': 'unsynchronized',
                      'tick': 965},
                     {'role': 'standby', 'sync': 'unsynchronized',
                      'tick': 966}],
               tracked=None, ticks=None))
    expect('rejoin-late-past-the-bound', lambda record:
           record['rejoin'].update(ticks=REJOIN_TICKS + 1))
    expect('rejoin-watch-never-answered', lambda record:
           record['rejoin'].update(rows=[], tracked=None, ticks=None),
           DIAG_NONDET)
    expect('adoption-missing', lambda record:
           record['adoption'].update(adoptions=[]))
    expect('adoption-duplicated', lambda record:
           record['adoption']['adoptions'].append(
               {'source': '172.20.0.5:8081'}))
    expect('adoption-foreign', lambda record:
           record['adoption'].update(
               adoptions=[{'source': '172.20.0.5:8082'}]))
    expect('source-refusal-journaled', lambda record:
           record['adoption'].update(
               refusals=[{'source': '172.20.0.5:8081',
                          'detail': "the pulled document's stream "
                                    'position leads the line'}]))
    expect('restart-rejoined', lambda record:
           record['adoption'].update(boundaries=3))
    expect('served-document-foreign', lambda record:
           record['adoption']['document'].update(owns=True))
    expect('served-document-misnamed', lambda record:
           record['adoption']['document'].update(
               line_owner='172.20.0.5:8082'))
    expect('served-document-unreadable', lambda record:
           record['adoption'].update(document=None))
    expect('loss-silent', lambda record:
           record['switch'].update(losses=[]))
    expect('loss-duplicated', lambda record:
           record['switch']['losses'].append(
               {'point': 20, 'claimant': 424244}))
    expect('loss-unattributed', lambda record:
           record['switch'].update(losses=[{'point': 20}]))
    expect('loss-misattributed', lambda record:
           record['switch'].update(losses=[{'point': 20,
                                           'claimant': 424243}]))
    expect('demotion-unwalked', lambda record:
           record['switch'].update(
               roles=[('active', 'standby', 'fenced')]))
    expect('demotion-origin-unattributed', lambda record:
           record['switch'].update(roles=[('active', 'demoting', None),
                                         ('demoting', 'standby',
                                          'fenced')]))
    expect('demotion-held', lambda record:
           record['switch'].update(demoted=None,
                                   walk=['active', 'active']))
    expect('demotion-unexpected-walk', lambda record:
           record['switch'].update(walk=['active', 'starting']))
    expect('successor-inactive', lambda record:
           record['switch']['promoted_after'].update(role='standby'))
    expect('verdict-open', lambda record:
           record['switch']['verdict'].update(fenced=False))
    expect('verdict-foreign', lambda record:
           record['switch']['verdict'].update(owner=424243))
    expect('verdict-undeclared', lambda record:
           record['switch']['verdict'].update(monitor=None))
    expect('verdict-misnamed', lambda record:
           record['switch']['verdict'].update(
               monitor='172.20.0.5:8080'))
    expect('stream-position-ahead-of-its-own-tick', lambda record:
           record['stage']['document'].update(stream=9999))
    expect('restore-refused', lambda record:
           record['restore'].update(
               promote={'status': 409, 'cause': 'not_converged',
                        'body': {'not_converged': {}}}))
    expect('layout-unrestored', lambda record:
           record['restore'].update(layout=None))
    expect('launch-roles-lost-after-the-pass', lambda record:
           record['roles']['final']['active'].update(role='standby'))
    # The instability the contract does not answer for must report
    # nondeterministic rather than failed: a refused staging lever, an
    # outage that never separated, a pair that never reconverged, a
    # promote the gate answered, an unanswered promote, an unreadable
    # durable sink, and an unreadable field verdict.
    expect('staging-lever-refused', lambda record:
           record['stage'].update(outage='stop refused: docker refused'),
           DIAG_NONDET)
    expect('restart-lever-refused', lambda record:
           record['stage'].update(outage='restart refused: refused'),
           DIAG_NONDET)
    expect('owner-tick-never-served', lambda record:
           record['stage'].update(outage='unreadable: no tick',
                                  before=None, frozen=None), DIAG_NONDET)
    expect('lead-never-separated', lambda record:
           record['stage'].update(accrued=None), DIAG_NONDET)
    expect('lead-inside-the-retired-bound', lambda record:
           record['stage']['lead'].update(peer=920, lead=20), DIAG_NONDET)
    expect('lead-unreadable', lambda record:
           record['stage'].update(lead=None), DIAG_NONDET)
    expect('pair-never-reconverged', lambda record:
           record['stage'].update(reconverged=None), DIAG_NONDET)
    expect('successor-not-converged-at-promote', lambda record:
           record['switch']['posture'].update(sync='unsynchronized'),
           DIAG_NONDET)
    expect('promote-answered-by-the-gate', lambda record:
           record['switch']['promote'].update(
               status=409, cause='not_converged',
               body={'not_converged': {'sync': 'tracking'}}), DIAG_NONDET)
    expect('promote-unanswered', lambda record:
           record['switch'].update(promote=None), DIAG_NONDET)
    expect('durable-loss-unreadable', lambda record:
           record['switch'].update(losses=None), DIAG_NONDET)
    expect('durable-walk-unreadable', lambda record:
           record['switch'].update(roles=None), DIAG_NONDET)
    expect('durable-adoption-unreadable', lambda record:
           record['adoption'].update(adoptions=None), DIAG_NONDET)
    expect('process-lifetimes-unreadable', lambda record:
           record['switch']['boundaries'].update(active=None),
           DIAG_NONDET)
    expect('field-verdict-never-answered', lambda record:
           record['switch'].update(verdict=None), DIAG_NONDET)
    expect('restore-unanswered', lambda record:
           record['restore'].update(promote=None), DIAG_NONDET)
    return slipped


def scenario_own_tick_ahead_rejoin(ctx):
    """A demoted ex-owner whose own paced tick leads the line past the
    retired ahead bound still re-joins through the field-arbitrated
    claimed-monitor rendezvous: `POST /promote` on a tracking standby
    that accrued a permanent run-tick lead through a tracking-source
    outage preempts the claim, the ex-owner demotes in place under the
    fenced origin with its loss attributed to the promoted claim, and
    it converges to a `tracking` verdict inside the documented bound
    with exactly one `tracking_source_adopted` naming the standing
    claim's declared monitor, no source refusal naming the successor
    and no restart performing the convergence; a later `POST /promote`
    on the converged ex-owner answers the converged path and the pair's
    launch roles restore."""
    case = Case(
        'own-tick-ahead-rejoin',
        'A demoted ex-owner rejoins the line through the claim\'s '
        'declared monitor however far its own tick leads it',
        'with the deployed pair settled on its launch layout — the '
        'no-configured-source owner writing the field, the --standby '
        'tracker tracking it — the lane\'s controller lifecycle holding '
        'the owner\'s tracking source down while the tracker keeps its '
        'cadence, so the successor\'s own run tick is measured leading '
        'the frozen line\'s past the retired ' + str(AHEAD_BOUND)
        + '-tick ahead bound once the owner is brought back and the '
          'pair reconverges: the routine POST /promote on the '
          'lead-carrying tracker preempts the claim, the ex-owner '
          'demotes in place through the fenced-origin '
          'active → demoting → standby walk with exactly one '
          'field_claim_lost attributed to the promoted claim, the '
          'field\'s own post-promotion fencing verdict names that claim '
          'and its declared monitor, and the ex-owner re-joins inside '
          'the documented bound of ' + str(REJOIN_TICKS) + ' of its own '
          'scans — sync converging to tracking rather than parking '
          'unsynchronized — with exactly one tracking_source_adopted '
          'naming the declared monitor on :'
        + str(PAIR_PORTS['standby']) + ', no tracking_source_refused '
          'naming the successor, no restart boundary inside the '
          're-join, and the re-joined document stamping the '
          'successor\'s ownership honestly; the later POST /promote on '
          'the converged ex-owner answers promoting and the pair\'s '
          'launch roles restore — two consecutive passes producing '
          'identical digests')
    try:
        missing = [seam for seam in ('stop_controller', 'start_controller')
                   if ctx.get(seam) is None]
        if missing:
            return case.finish(
                'inconclusive', 'the run context carries no '
                + ', '.join(missing) + ' action — the tracking-source '
                'outage the ahead bound rides has no documented seam')
        owner, peer = OWNER, PEER
        if not all(ctx.get(name) for name in (owner, peer)):
            return case.finish(
                'inconclusive', 'the run config did not publish both '
                'peer monitor endpoints — the leg cannot drive the '
                'deployed pair')
        if not ctx.get('plant'):
            return case.finish(
                'inconclusive', 'the run config did not publish the '
                'simulated plant endpoint — the leg cannot read the '
                'field\'s own claim arbitration')
        placement = ctx.get('endpoint_placement') or {}
        if any(placement.get(name) != 'loopback'
               for name in (owner, peer, 'plant')):
            return case.finish(
                'inconclusive', 'the run config does not publish '
                'loopback endpoints for both peers and the plant — the '
                'rig\'s declared placements are '
                + str(placement)[:200] + ' so the leg cannot read the '
                'field\'s fencing verdicts host-side')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name) for name in (owner, peer)):
            return case.finish(
                'inconclusive', 'the run config does not bind-mount '
                'per-controller journal files — the durable half of the '
                're-join audit cannot run')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name) for name in (owner, peer)):
            return case.finish(
                'inconclusive', 'the run config records no pinned '
                '--owner-token for the pair — the superseded '
                'ex-owner\'s journaled claim loss cannot be attributed '
                'to the promoted successor')
        seated = _ahead_pair(ctx, owner, peer, SETTLE)
        if seated is None:
            reports = {name: _ahead_posture(ctx, name)
                       for name in (owner, peer)}
            if all(report is None for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is unreachable — '
                    'monitor endpoints ' + str(ctx.get(owner)) + ' and '
                    + str(ctx.get(peer)))
            reseat = _ahead_reseat(ctx, owner, peer)
            seated = reseat.get('layout')
            if seated is None:
                return case.finish(
                    'inconclusive', 'the pair is off its launch layout '
                    'and the documented re-seat did not restore it: '
                    + json.dumps(reseat, sort_keys=True)[:300])
            case.observe('pair re-seated onto its launch layout: '
                         + json.dumps(reseat['demoted'])[:200])
        case.observe('pair under audit: ' + owner + ' owns the field '
                     'with no configured source (' + ctx[owner] + '), '
                     + peer + ' tracks it (' + ctx[peer] + '); '
                     + ('keyed' if ctx.get('pair_token') else 'unkeyed')
                     + ' posture, miss budget '
                     + str(ctx.get('failover_misses')))
        predates = _ahead_surfaces(ctx, owner, peer)
        if predates:
            return case.finish('inconclusive', predates)
        verdict = _ahead_probe(ctx)
        if verdict is None:
            return case.finish(
                'inconclusive', 'the baseline fencing probe never '
                'answered — the field\'s own claim arbitration could '
                'not be read')
        if not verdict.get('fenced'):
            return case.finish(
                'inconclusive', 'the baseline fencing probe was not '
                'fenced — the field stands open, so the claim '
                'arbitration the re-join rides has nothing to arbitrate: '
                + json.dumps(verdict)[:200])
        if verdict.get('owner') is None:
            return case.finish(
                'inconclusive', 'the baseline fencing verdict names no '
                'standing owner — the pinned revision predates the '
                'verdict attribution the rendezvous reads')
        if verdict.get('monitor') is None:
            return case.finish(
                'inconclusive', 'the baseline fencing verdict declares no '
                'monitor — the pinned revision predates the '
                'claim-declared monitor the re-join resolves')
        if not _ahead_endpoint(verdict['monitor'], owner):
            return case.finish(
                'failed', 'the settled owner\'s claim declares monitor '
                + str(verdict['monitor']) + ' — it does not resolve the '
                'launch owner on :' + str(PAIR_PORTS[owner]))
        case.observe('settled baseline: ' + owner + ' holds the field '
                     'and the field names its monitor '
                     + str(verdict['monitor']))
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record = _ahead_pass(ctx, number, owner, peer)
            record['roles']['final'] = {
                name: _ahead_posture(ctx, name) for name in (owner, peer)}
            record['tokens'] = {owner: tokens.get(owner),
                                peer: tokens.get(peer)}
            predates = record.pop('predates', None)
            if predates:
                return case.finish(
                    'inconclusive', 'the monitored revision predates the '
                    'own-tick-ahead rejoin contract: ' + predates)
            _ahead_judge(record, note)
            digest = _ahead_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'own-tick-ahead-rejoin-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'own-tick-ahead rejoin pass ' + str(number)
                          + ' — the outage\'s measured run-tick lead, '
                          'the lead-carrying document, the routine '
                          'promote and the fenced supersede, the '
                          'demoted ex-owner\'s re-join watch, its '
                          'durable adoption and refusal records, the '
                          'documented switch back, the pair\'s '
                          'before/after/final framing, and the '
                          'normalized digest')
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
                'failed', DIAG_NONDET + ": the two passes' digests "
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two own-tick-ahead rejoin passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _ahead_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               "leg's own audits: "
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
