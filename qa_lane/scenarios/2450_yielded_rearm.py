"""The yielded_rearm acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The yielded-rearm case restores what it moves — the socket
# legs' one contract preemption re-seats through the recorded owner's
# own loss-marked reclaim, and the controller half's switch, yield,
# orphan re-promotion, and cold restart land back on the launch claim
# state and roles — so it needs no declared window.

# --------------------------------------------------------------------
# The yielded-claim re-grant contract (WW-LCM-001's continuity clause —
# the per-revision lane evidence for the yield lifecycle's re-arm half,
# answering QA finding
# `yielded-claim-regrant-leaves-live-incumbent-preemptable`, #1123):
# `release_writer{keep_claim}` — the demotion shape — leaves the
# standing claim yielded so a successor's conditional claim can
# preempt the deliberate hand-off 'despite other holders'. The yield
# mark is a hand-off signal, not a state: once the same owner re-grants
# itself into a live *controller* hold — an orphan failover's
# re-promotion, a manual promote's unconditional claim, or a
# re-attaching attachment's bound `ensure_writer` — the mark must end,
# re-arming the live-incumbent refusal `claim_writer_unless_held`
# exists to give a restarted peer's stale-resume claim. Before the fix
# the mark never cleared: every later conditional claimer preempted
# the live re-bound incumbent, and the observed consequence was a
# restarted, previously-demoted controller seizing the field from a
# live failover successor with no operator action.
#
# The leg exercises both halves of the reproduction. The socket legs
# run the claim verbs directly on the deployed plant under the
# standing owner's own pinned token — yield the claim, re-grant it
# through each same-owner shape, and assert a different owner's
# conditional claim meets the refusal naming the incumbent — ending
# with the contract's other half: an unbound orphan probe and a
# tool's bound hold leave the hand-off preemptable, so the foreign
# conditional grant lands, and the ex-owner's loss-marked reclaim
# re-seats the field as in every claim leg. The controller half then
# runs the rig's own failover path: the documented switch puts the
# failover-armed peer on the field, `POST /demote` yields its claim
# under its own token, its own tracking scans pull its demoted
# tracker's ownerless checkpoints — each orphaned apply a heartbeat
# miss — until the budget-th apply self-promotes it over its own
# yielded claim, and the demoted peer then cold-restarts as its
# configured active: the startup conditional claim must meet the
# refusal and exit rather than preempt the live successor mid-run.
# The named diagnostics are yielded-rearm-failed and
# yielded-rearm-nondeterministic; the socket legs' two passes must
# produce identical digests.

YIELDED_REARM_SETTLE = 30    # bound on the pair reporting settled
YIELDED_REARM_POLL = 0.5     # cadence polling the peers mid-episode
YIELDED_REARM_SWITCH = 45    # bound on the switch's reconvergence
YIELDED_REARM_DEMOTE = 20    # bound on a demotion settling standby
YIELDED_REARM_REPROMOTE = 20 # base bound the orphan failover scales
YIELDED_REARM_RECLAIM = 30   # bound on the preemption's re-seat
YIELDED_REARM_ROUNDS = 6     # polls the restartee window spans
YIELDED_REARM_GRACE = 2      # beat for the restartee's claim to land
YIELDED_REARM_RESTORE = 20   # grace the finally gives a pending settle
# The induction token the different-owner conditional probes claim
# under — a small fixed value that collides with none of the run
# config's pinned controller tokens nor the tool's "dcs-pltc".
YIELDED_REARM_FOREIGN = 0x7969_656c_64  # "yield"


def _verdict_owner(response):
    """The owner token a fencing verdict attributes the standing
    claim to — the `owner` field the plant's `fenced` and `io.fenced`
    answers both carry under the loss-attribution contract — or None
    on an unfenced answer or a build predating the field."""
    return ((response or {}).get('error') or {}).get('owner')


def _write_fenced(response):
    """Whether a write probe's answer is the point-level fencing
    verdict — `{"kind":"io","error":{"fenced":N}}` — the refusal the
    standing claim gives a non-holder's write."""
    error = (response or {}).get('error') or {}
    inner = error.get('error')
    return error.get('kind') == 'io' and isinstance(inner, dict) \
        and 'fenced' in inner


def _unsupported(response):
    """Whether a claim-verb answer is the wire's `invalid_request` —
    a rig whose release predates the lifecycle verbs."""
    error = (response or {}).get('error') or {}
    return error.get('kind') == 'invalid_request'


def _yielded_socket_pass(ctx, owner, probe_point, held, note):
    """One socket pass over the standing claim's own token: yield the
    claim through `release_writer{keep_claim}` then re-grant it under
    the same owner through each live-controller shape —
    `claim_writer_unless_held`, `claim_writer`, and a bound
    `ensure_writer` — asserting each re-grant re-arms the
    live-incumbent refusal a different owner's conditional claim
    meets, and that the re-bound holder's own mutations land.
    Returns (digest, evidence): digest is the pass's normalized
    verdict record, identical across clean passes."""
    digest, evidence = {}, {}
    owner_io = _plant_connect(ctx)
    regrant_io = _plant_connect(ctx)
    probe_io = _plant_connect(ctx)
    try:
        # The shared-claim join the run config's pinned tokens exist
        # for: the attachment stands beside the running owner's own
        # hold — claimed_shared — so its keep-release marks the
        # standing claim yielded rather than dissolving it.
        join = _plant_request(owner_io,
                              {'op': 'ensure_writer', 'owner': owner})
        evidence['join'] = join
        if _unsupported(join):
            raise ConnectionError('ensure_writer answered '
                                  'invalid_request — the rig '
                                  'predates the claim lifecycle '
                                  'verbs: ' + json.dumps(join)[:300])
        if join.get('result') not in ('done', 'claimed_shared'):
            raise ConnectionError('the shared-claim join under the '
                                  'standing owner\'s token was '
                                  'refused: '
                                  + json.dumps(join)[:300])
        legs = (
            ('conditional',
             {'op': 'claim_writer_unless_held', 'owner': owner}),
            ('unconditional',
             {'op': 'claim_writer', 'owner': owner}),
            ('bound-ensure',
             {'op': 'ensure_writer', 'owner': owner}),
        )
        for name, regrant in legs:
            # Each yield needs a hold to release: the previous leg's
            # keep-release dropped this attachment's, so it re-joins
            # the shared claim before yielding it again.
            rejoin = _plant_request(owner_io, {'op': 'ensure_writer',
                                    'owner': owner})
            if rejoin.get('result') not in ('done', 'claimed_shared'):
                note('rejoin-refused-' + name,
                     'the holder\'s re-join before the ' + name
                     + ' yield was refused: '
                     + json.dumps(rejoin)[:300])
                digest[name] = 'refused'
                continue
            yielded = _plant_request(owner_io, {'op': 'release_writer',
                           'keep_claim': True})
            evidence['yield-' + name] = yielded
            if _unsupported(yielded):
                raise ConnectionError('release_writer{keep_claim} '
                                      'answered invalid_request — '
                                      'the rig predates the yield '
                                      'lifecycle: '
                                      + json.dumps(yielded)[:300])
            if yielded.get('result') != 'done':
                note('yield-refused-' + name,
                     'the keep-claim release was refused: '
                     + json.dumps(yielded)[:300])
                continue
            grant = _plant_request(regrant_io, regrant)
            evidence['regrant-' + name] = grant
            if _unsupported(grant):
                raise ConnectionError(regrant['op'] + ' answered '
                                      'invalid_request — the rig '
                                      'predates the claim lifecycle '
                                      'verbs: '
                                      + json.dumps(grant)[:300])
            if grant.get('result') not in ('done', 'claimed_shared'):
                note('regrant-refused-' + name,
                     'the same-owner ' + name + ' re-grant over its '
                     'own yielded claim was refused: '
                     + json.dumps(grant)[:300])
                digest[name] = 'refused'
                continue
            write = _plant_request(
                regrant_io, {'op': 'write', 'point': probe_point,
                             'value': held})
            step = _plant_request(regrant_io,
                                   {'op': 'step', 'dt': 0.1})
            evidence['bound-' + name] = {'write': write, 'step': step}
            if write.get('result') != 'done':
                note('bound-write-fenced-' + name,
                     'the re-bound holder\'s write answered '
                     + json.dumps(write)[:300])
            if step.get('result') != 'stepped':
                note('bound-step-fenced-' + name,
                     'the re-bound holder\'s step answered '
                     + json.dumps(step)[:300])
            probe = _plant_request(probe_io, {'op': 'claim_writer_unless_held',
                           'owner': YIELDED_REARM_FOREIGN})
            evidence['probe-' + name] = probe
            if _unsupported(probe):
                raise ConnectionError('claim_writer_unless_held '
                                      'answered invalid_request — '
                                      'the rig predates the '
                                      'conditional grant: '
                                      + json.dumps(probe)[:300])
            if _fenced(probe):
                if _verdict_owner(probe) is None:
                    raise ConnectionError('the fencing verdict names '
                                          'no standing owner — the '
                                          'rig predates the '
                                          'loss-attribution '
                                          'contract: '
                                          + json.dumps(probe)[:300])
                if _verdict_owner(probe) != owner:
                    note('probe-misnamed-' + name,
                         'the fencing verdict attributes the '
                         're-granted claim to '
                         + str(_verdict_owner(probe))
                         + ', not the re-bound owner: '
                         + json.dumps(probe)[:300])
                    digest[name] = 'misnamed'
                else:
                    digest[name] = 'named'
            else:
                # The defect itself: the re-granted claim still read
                # yielded, so the foreign conditional grant landed.
                note('probe-granted-' + name,
                     'the same-owner ' + name + ' re-grant left the '
                     'claim preemptable — a different owner\'s '
                     'conditional claim answered '
                     + json.dumps(probe)[:300])
                digest[name] = 'granted'
                # The induction grant now stands: release it so the
                # pass's later legs and the owner's claim are
                # untouched by it.
                _plant_request(probe_io, {'op': 'release_writer'})
    finally:
        for stream in (owner_io, regrant_io, probe_io):
            try:
                stream.close()
            except Exception:
                pass
    return digest, evidence


def _yielded_socket_tool(ctx, owner, probe_point, held, note):
    """The yield's other half — run once: with the claim yielded
    again, an unbound same-owner `ensure_writer` probe joins no
    holder and a tool's bound `ensure_writer` (controller=false)
    holds without incumbency, so the hand-off stays preemptable —
    the different owner's conditional claim lands by contract, the
    field's recorded owner demotes at its fenced write, and the
    induction release hands the field back for the reclaim to
    re-seat. Returns the leg's evidence dict."""
    evidence = {}
    owner_io = _plant_connect(ctx)
    tool_io = _plant_connect(ctx)
    probe_io = _plant_connect(ctx)
    foreign_granted = False
    try:
        join = _plant_request(owner_io,
                              {'op': 'ensure_writer', 'owner': owner})
        if _unsupported(join):
            raise ConnectionError('ensure_writer answered '
                                  'invalid_request — the rig '
                                  'predates the claim lifecycle '
                                  'verbs: ' + json.dumps(join)[:300])
        if join.get('result') not in ('done', 'claimed_shared'):
            raise ConnectionError('the tool leg\'s shared-claim join '
                                  'was refused: '
                                  + json.dumps(join)[:300])
        yielded = _plant_request(
            owner_io, {'op': 'release_writer', 'keep_claim': True})
        evidence['yield'] = yielded
        if yielded.get('result') != 'done':
            raise ConnectionError('the tool leg\'s keep-claim '
                                  'release was refused: '
                                  + json.dumps(yielded)[:300])
        unbound = _plant_request(
            probe_io, {'op': 'ensure_writer', 'owner': owner,
                       'rebind': False})
        evidence['unbound'] = unbound
        tool = _plant_request(
            tool_io, {'op': 'ensure_writer', 'owner': owner,
                      'controller': False})
        evidence['tool'] = tool
        for label, verdict in (('unbound orphan probe', unbound),
                               ('tool bound ensure', tool)):
            if _unsupported(verdict):
                raise ConnectionError('the ' + label + ' answered '
                                      'invalid_request — the rig '
                                      'predates the ensure_writer '
                                      'attachment fields: '
                                      + json.dumps(verdict)[:300])
            if verdict.get('result') not in ('done', 'claimed_shared'):
                note('tool-' + label.replace(' ', '-'),
                     'the ' + label + ' was refused: '
                     + json.dumps(verdict)[:300])
        grant = _plant_request(
            probe_io, {'op': 'claim_writer_unless_held',
                       'owner': YIELDED_REARM_FOREIGN})
        evidence['preemption'] = grant
        if grant.get('result') in ('done', 'claimed_shared'):
            foreign_granted = True
        elif _fenced(grant):
            note('tool-preemption-fenced',
                 'the still-yielded claim fenced the foreign '
                 'conditional grant — an unbound probe or a tool '
                 'hold wrongly re-armed the incumbent: '
                 + json.dumps(grant)[:300])
        else:
            raise ConnectionError('the foreign conditional claim '
                                  'answered neither grant nor '
                                  'fencing: '
                                  + json.dumps(grant)[:300])
    finally:
        if foreign_granted:
            try:
                _plant_request(probe_io, {'op': 'release_writer'})
            except Exception:
                pass
        for stream in (owner_io, tool_io, probe_io):
            try:
                stream.close()
            except Exception:
                pass
    return evidence


def _settled_owner(ctx, deadline):
    """The endpoint key of the settled field owner restricted to the
    pair containers — the restartee and the armed peer this leg
    cycles."""
    return wait_for(
        lambda: next(
            (name for name in ('active', 'standby')
             if (_try_role(ctx, ctx.get(name) or '')
                 or {}).get('role') == 'active'),
            None) or None,
        deadline, interval=YIELDED_REARM_POLL)


def _switch_to(ctx, armed, note):
    """The documented switch onto the armed peer — demote the
    settled owner, promote the armed standby, and wait for the
    armed report to settle — returns the demoted owner's endpoint
    key, or None where the switch never landed."""
    owner = _settled_owner(ctx, time.monotonic()
                           + YIELDED_REARM_SETTLE)
    if owner is None:
        note('no-owner', 'no pair peer reports role=active — the '
             'switch has no field owner to move')
        return None
    if owner == armed:
        return armed
    status, body = _settle_call(ctx[owner] + '/demote')
    if status != 200:
        note('switch-demote-refused',
             'POST /demote on the settled owner answered '
             + str(status) + ' ' + json.dumps(body)[:200])
        return None
    deadline = time.monotonic() + YIELDED_REARM_SWITCH
    promoted = None
    while time.monotonic() < deadline and promoted is None:
        status, body = _settle_call(ctx[armed] + '/promote')
        if status == 200:
            promoted = body
        else:
            time.sleep(YIELDED_REARM_POLL)
    if promoted is None:
        note('switch-promote-refused',
             'POST /promote on the armed peer never landed — it '
             'never converged tracking for the switch')
        return None
    settled = wait_for(
        lambda: (_try_role(ctx, ctx[armed]) or {}).get('role')
                == 'active' or None,
        deadline, interval=YIELDED_REARM_POLL)
    if not settled:
        note('switch-never-settled',
             'the armed peer never reported active after its '
             'promotion')
        return None
    return owner


def _yielded_rearm_episode(ctx, armed, restartee, tokens, budget,
                         note, evidence):
    """The controller half: the armed field owner's demote yields its
    claim under its own token; its own tracking scans pull the
    demoted peer's ownerless checkpoints — each orphaned apply a
    heartbeat miss — until the budget-th apply self-promotes it over
    its own yielded claim; then the demoted peer cold-restarts as
    its configured active and the restarted startup claim must meet
    the re-armed incumbent refusal rather than preempt the live
    successor."""
    armed_base, restartee_base = ctx[armed], ctx[restartee]
    armed_token = tokens.get(armed)
    restartee_token = tokens.get(restartee)

    # The demote: POST /demote on the armed field owner yields the
    # standing claim under its own token — the deliberate hand-off
    # a successor may preempt — while third-party mutations stay
    # fenced naming it.
    status, body = _settle_call(armed_base + '/demote')
    evidence['demote'] = {'status': status, 'report': body}
    if status != 200 or (body or {}).get('role') != 'demoting':
        note('demote-refused',
             'POST /demote on the armed field owner answered '
             + str(status) + ' ' + json.dumps(body)[:300])
        return
    settled = wait_for(
        lambda: (_try_role(ctx, armed_base) or {}).get('role')
                == 'standby' or None,
        time.monotonic() + YIELDED_REARM_DEMOTE,
        interval=YIELDED_REARM_POLL)
    if not settled:
        note('never-settled-standby',
             'the demoted armed owner never settled standby')
        return
    yielded_probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['yielded_probe'] = yielded_probe
    if not _fenced(yielded_probe) \
            or _verdict_owner(yielded_probe) != armed_token:
        note('yield-missing',
             'the demote did not leave the claim yielded under '
             'the armed owner\'s token — the probe answered '
             + json.dumps(yielded_probe)[:300])
        return

    # The orphan failover: the armed peer's own scans pull its
    # demoted tracker's ownerless checkpoints — each orphaned apply
    # a heartbeat miss — until the budget-th apply self-promotes it
    # over its own yielded claim: the same-owner conditional
    # re-grant of the reproduction.
    bound = time.monotonic() + YIELDED_REARM_REPROMOTE + budget * 0.2
    watch = []
    repromoted = None
    while time.monotonic() < bound and repromoted is None:
        report = _try_role(ctx, armed_base)
        partner = _try_role(ctx, restartee_base)
        watch.append({'armed': (report or {}).get('role'),
                      'restartee': (partner or {}).get('role'),
                      'sync': (report or {}).get('sync')})
        if (report or {}).get('role') == 'active':
            repromoted = report
        else:
            time.sleep(YIELDED_REARM_POLL)
    evidence['regrant'] = {'watch': watch[-8:], 'report': repromoted}
    if repromoted is None:
        note('never-repromoted',
             'the armed orphan never re-promoted over its own '
             'yielded claim inside the failover budget — the rig '
             'predates the orphan-failover claim path: '
             + json.dumps(watch[-4:])[:300])
        return
    if any(row['restartee'] not in ('standby', 'demoting', None)
           for row in watch):
        note('restartee-moved',
             'the demoted peer left standby through the orphan '
             'window: ' + json.dumps(watch[-4:])[:300])
    reseated = _try_plant(ctx, {'op': 'step', 'dt': 0})
    evidence['reseated'] = reseated
    if not _fenced(reseated) \
            or _verdict_owner(reseated) != armed_token:
        note('reseat-missing',
             'the orphan re-promotion did not re-seat the armed '
             'peer\'s claim — the probe answered '
             + json.dumps(reseated)[:300])
        return

    # The restartee: the demoted peer cold-restarts as its
    # configured active — its deployment shape — and its startup
    # conditional claim meets the re-granted claim. The claim must
    # read as the live incumbent's unyielded hold again: the
    # restartee's activation refuses and exits rather than
    # preempting the failover successor mid-run — the
    # stale-resume seizure the conditional grant exists to refuse.
    # The journal floor is re-read here so the audit diffs only the
    # restartee window — the demote -> standby -> promote -> active
    # walk the re-promotion itself journaled is legitimately above
    # any earlier floor.
    try:
        _, journal = http_json('GET', armed_base + '/journal')
        entries = _journal_list(journal)
        floor = (entries[-1].get('seq') or 0) if entries else 0
    except Exception:
        floor = 0
    ctx['cold_restart_controller'](restartee)
    window = []
    clean = True
    time.sleep(YIELDED_REARM_GRACE)
    for index in range(YIELDED_REARM_ROUNDS):
        report = _try_role(ctx, armed_base)
        restartee_role = _try_role(ctx, restartee_base)
        probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
        row = {'armed': (report or {}).get('role'),
               'restartee': (restartee_role or {}).get('role'),
               'probe': _probe_error(probe),
               'probe_owner': _verdict_owner(probe)}
        window.append(row)
        if report is None:
            clean = False
            note('successor-silent',
                 'the live successor\'s monitor stopped answering '
                 'through the restartee\'s claim')
        elif report.get('role') != 'active':
            clean = False
            note('successor-left-active',
                 'the live failover successor left active through '
                 'the restartee\'s claim — the yielded mark '
                 'outlived the re-grant: '
                 + json.dumps(window)[:300])
        if row['restartee'] in ('active', 'promoting'):
            clean = False
            note('restartee-preempted',
                 'the cold-restarted peer\'s startup claim '
                 'preempted the live successor and reached '
                 + str(row['restartee']) + ' — the stale-resume '
                 'seizure the conditional grant exists to refuse')
        if probe is not None:
            if not _fenced(probe):
                clean = False
                note('claim-open',
                     'a third-party mutation landed through the '
                     'restartee\'s claim: '
                     + json.dumps(probe)[:300])
            elif _verdict_owner(probe) != armed_token:
                clean = False
                note('claim-moved',
                     'the standing claim moved off the armed '
                     'peer\'s token to '
                     + str(_verdict_owner(probe))
                     + ' — the restartee\'s conditional grant '
                     'preempted it')
        if index + 1 < YIELDED_REARM_ROUNDS:
            time.sleep(YIELDED_REARM_POLL)
    evidence['restart_window'] = window
    if restartee_token is None:
        note('restartee-token-missing',
             'the run pins no owner token for the restartee '
             'endpoint — the moved claim cannot be attributed to '
             'the restartee rather than any foreign claimer')

    # The standing claim still refuses a different owner's
    # conditional grant — naming the live re-granted incumbent. The
    # probe rides a held attachment so a grant (the defect build)
    # can hand its hold straight back rather than stranding a
    # foreign claim fencing the field.
    contender_io = _plant_connect(ctx)
    try:
        verdict = _plant_request(
            contender_io, {'op': 'claim_writer_unless_held',
                           'owner': YIELDED_REARM_FOREIGN})
        evidence['contender'] = verdict
        if _fenced(verdict):
            if _verdict_owner(verdict) != armed_token:
                note('contender-misnamed',
                     'the fencing verdict attributes the claim to '
                     + str(_verdict_owner(verdict))
                     + ', not the re-granted successor')
        elif verdict.get('result') in ('done', 'claimed_shared'):
            note('contender-granted',
                 'a different owner\'s conditional claim preempted '
                 'the re-granted live incumbent — the yielded mark '
                 'still stands: ' + json.dumps(verdict)[:300])
            try:
                _plant_request(contender_io,
                               {'op': 'release_writer'})
            except Exception:
                pass
        else:
            note('contender-odd',
                 'the post-restart conditional probe answered '
                 + json.dumps(verdict)[:300])
    finally:
        try:
            contender_io.close()
        except Exception:
            pass

    # The successor's journal above the floor: no fencing loss, no
    # demotion — the restartee's claim never touched it.
    try:
        _, body = http_json('GET', armed_base + '/journal?since='
                            + str(floor))
        journal = _journal_list(body)
    except Exception as exc:
        journal = None
        note('successor-journal',
             'the successor\'s journal never served the '
             'post-episode audit: ' + str(exc)[:200])
    evidence['successor_journal'] = journal
    if journal is not None:
        losses = [entry for entry in journal
                  if 'field_claim_lost'
                  in (entry.get('event') or {})]
        if losses:
            note('successor-loss',
                 'the successor journaled a fencing loss through '
                 'the restartee\'s claim — its live claim was '
                 'preempted: ' + json.dumps(losses[:2])[:300])
        walk = [(change.get('from'), change.get('to'))
                for entry in journal
                for change in [(entry.get('event') or {})
                               .get('role_changed') or {}]
                if change]
        if any(to in ('demoting', 'standby') for _frm, to in walk):
            note('successor-demoted',
                 'the successor\'s journal carries a demotion '
                 'through the episode: ' + json.dumps(walk))
    evidence['window'] = 'clean' if clean else 'breached'


def scenario_yielded_rearm(ctx):
    """Exercise the yielded claim's re-grant re-arm at both seams:
    the socket claim verbs on the deployed plant under the standing
    owner's pinned token, then the armed peer's demote -> orphan
    re-promotion -> restart-as-active reproduction on the deployed
    pair; restore the claim state and launch roles."""
    case = Case('yielded-rearm',
                'A yielded claim re-armed by a same-owner '
                'controller re-grant refuses the restartee\'s '
                'conditional claim',
                'with the deployed pair settled, the claim verbs '
                'on the plant socket stage claim -> '
                'release_writer{keep_claim} -> same-owner re-grant '
                'for every re-grant shape — conditional, '
                'unconditional, bound ensure — each restoring the '
                'live-incumbent refusal a different owner\'s '
                'claim_writer_unless_held meets, while an unbound '
                'probe and a tool hold leave the yielded claim '
                'preemptable; the controller half then demotes the '
                'failover-armed field owner, lets its orphan '
                'failover re-promote it over its own yielded '
                'claim, cold-restarts the demoted peer as its '
                'configured active, and asserts the live '
                'successor\'s claim refuses the startup claim — '
                'no fencing loss, no demotion, the claim still '
                'naming the re-granted owner; the pair and claim '
                'state are restored')
    try:
        if not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context '
                               'carries no plant endpoint — the '
                               'claim verbs cannot run')
        if ctx.get('cold_restart_controller') is None \
                or ctx.get('start_controller') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller lifecycle '
                               'actions — the restartee half '
                               'cannot run')
        budget = ctx.get('failover_misses')
        if not budget:
            return case.finish('inconclusive', 'the rig arms no '
                               'failover budget — the orphan '
                               're-promotion the reproduction '
                               'stages never runs')
        tokens = ctx.get('plant_owner') or {}
        if tokens.get('active') is None \
                or tokens.get('standby') is None:
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the '
                               'pair endpoints')
        field_in = sorted(_field_inputs(ctx)) \
            if ctx.get('plant_ctl') is not None else []
        probe_point = field_in[0] if field_in else None
        held = None
        if probe_point is not None:
            sample = _probe_sample(ctx, probe_point)
            held = (sample or {}).get('value')
        if probe_point is None or held is None:
            return case.finish('inconclusive', 'the simulated '
                               'plant serves no writable in-point '
                               'for the bound-holder write probe')
        violations = {}

        def note(key, detail):
            violations.setdefault(key,
                                  ('yielded-rearm-failed', detail))

        evidence = {}
        try:
            owner_ep = _settled_owner(
                ctx, time.monotonic() + YIELDED_REARM_SETTLE)
            if owner_ep is None:
                reachable = any(
                    _try_role(ctx, ctx[name]) is not None
                    for name in ('active', 'standby'))
                return case.finish(
                    'failed' if reachable else 'inconclusive',
                    'yielded-rearm-failed: no pair peer reports '
                    'role=active' if reachable
                    else 'the pair is unreachable')
            owner = tokens[owner_ep]
            case.observe('field owner: ' + owner_ep
                         + ' under pinned token ' + hex(owner))

            # The socket legs: two identical passes of the
            # re-grant shapes on the standing claim's own token,
            # then the once-run tool/unbound half whose contract
            # preemption the recorded owner's own reclaim re-seats.
            digests = []
            for number in (1, 2):
                digest, pass_evidence = _yielded_socket_pass(
                    ctx, owner, probe_point, held, note)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'yielded-rearm-socket-' + str(number) + '.json',
                    pass_evidence)
                case.evidence('file', ref, 'yielded-rearm socket '
                              'pass ' + str(number) + ' — the '
                              'yield and each same-owner re-grant '
                              'shape against the standing claim')
                digests.append(digest)
            evidence['socket'] = digests
            if digests[0] != digests[1]:
                violations.setdefault(
                    'digests',
                    ('yielded-rearm-nondeterministic',
                     'the two socket passes\' digests diverged: '
                     + json.dumps(digests[0], sort_keys=True)
                     + ' vs '
                     + json.dumps(digests[1], sort_keys=True)))
                raise _YieldedAbort
            tool = _yielded_socket_tool(
                ctx, owner, probe_point, held, note)
            ref = save_evidence(ctx['evidence_dir'],
                                'yielded-rearm-tool.json', tool)
            case.evidence('file', ref, 'the yield\'s other half — '
                          'the unbound probe and tool hold keep '
                          'the claim preemptable for the foreign '
                          'conditional grant')

            # The tool leg's contract preemption demoted the
            # recorded owner — the release hands the field back
            # and its loss-marked reclaim re-seats it, the same
            # recovery the claim legs rely on.
            reseated = wait_for(
                lambda: (_try_role(ctx, ctx[owner_ep])
                         or {}).get('role') == 'active' or None,
                time.monotonic() + YIELDED_REARM_RECLAIM,
                interval=YIELDED_REARM_POLL)
            evidence['reclaimed'] = bool(reseated)
            if not reseated:
                note('never-reseated',
                     'the contract preemption\'s release never '
                     're-seated the recorded owner — the '
                     'loss-marked reclaim wedge the claim legs '
                     'cover')
            if violations:
                raise _YieldedAbort

            # The controller half: the armed peer (ctrl-b, the
            # failover-armed standby container) must own the field
            # — the documented switch moves it there when the
            # restartee (ctrl-a) still does.
            armed_owner = _switch_to(ctx, 'standby', note)
            evidence['armed-owner'] = armed_owner
            if armed_owner is not None:
                _yielded_rearm_episode(
                    ctx, 'standby', 'active', tokens, budget,
                    note, evidence)
            ref = save_evidence(ctx['evidence_dir'],
                                'yielded-rearm-episode.json',
                                evidence)
            case.evidence('file', ref, 'the controller half — the '
                          'demote, the orphan re-promotion, the '
                          'restartee window, and the journal '
                          'audit')
        except _YieldedAbort:
            pass
        finally:
            # Best effort: the launch claim state and role layout
            # for the legs behind this one. The armed peer's live
            # claim yields to its own demote — the restartee's
            # startup conditional claim then takes it on the
            # lifecycle action's start — and the demoted peer
            # reconverges tracking on the restored owner. A clean
            # pass on the defect build already sits at the launch
            # layout: the preempting restartee owns the field and
            # its peer tracks it.
            try:
                report = _try_role(ctx, ctx['standby'])
                if (report or {}).get('role') in (
                        'active', 'promoting', 'demoting'):
                    _settle_call(ctx['standby'] + '/demote')
                if (_try_role(ctx, ctx['active'])
                        or {}).get('role') != 'active':
                    try:
                        ctx['start_controller']('active')
                    except Exception:
                        pass
                deadline = time.monotonic() + YIELDED_REARM_RESTORE
                wait_for(
                    lambda: (_try_role(ctx, ctx['active'])
                             or {}).get('role') == 'active'
                    or None, deadline,
                    interval=YIELDED_REARM_POLL)
                wait_for(
                    lambda: _tracking_standby(ctx, 'standby'),
                    time.monotonic() + YIELDED_REARM_RESTORE,
                    interval=YIELDED_REARM_POLL)
            except Exception:
                pass
        if violations:
            diagnostic = 'yielded-rearm-nondeterministic' \
                if 'digests' in violations \
                else 'yielded-rearm-failed'
            return case.finish(
                'failed', diagnostic + ': ' + '; '.join(
                    detail for _, detail in
                    list(violations.values())[:4]))
        case.observe('the yielded claim\'s every re-grant shape '
                     'restored the incumbent refusal, and the '
                     'armed peer\'s orphan re-promotion stood '
                     'unpreemptable by the restartee')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))


class _YieldedAbort(Exception):
    """The leg's early-exit sentinel: a recorded violation already
    names the run's verdict; the finally still restores."""
