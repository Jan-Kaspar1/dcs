"""The claim_skew_bound acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the scenario seats 'revised'/'driven'/
# 'foreign' and the born legs' scratch field — it needs the sim-bus
# claim-refusal leg's seats and its device server released, and must be
# done before the revision legs take the born seats over.
RUNS_AFTER = frozenset({'scenario_sim_bus_startup_claim_refusal'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The skewed-claim preemption bound — the per-revision lane evidence
# for the single-field-writer contract over the tick-domain
# comparability rule CONTEXT.md records (WW-FND-002's failover
# continuity): two ticks order and subtract meaningfully only when
# minted in the same domain, so the field's claim arbitration must not
# resolve on an untranslated comparison. The rule bounds the tracking
# path — #1336 retired the window that stood between a pulled
# document's declared stream position and a detached prober's own paced
# position, since the gap between them measures pace asymmetry and
# never line membership — while claim arbitration has no equivalent
# bound: a standby computing its claim against a live incumbent reads
# the claimant's basis and the incumbent's stamps directly and takes
# the field whenever the comparison lands past the promotion bound.
# The consequence is the defect this leg stages: the incumbent's next
# staged exchange meets the fence, it demotes itself in place under the
# field-arbitration origin, and the claimant — whose basis was never
# translated into the incumbent's domain — writes the field from a
# position the comparability rule forbids resolving on.
#
# The bound's contract: a claim whose basis exceeds the recorded skew
# bound is refused or bounded by name, so a live incumbent keeps
# write-ownership; a claim whose basis sits inside the bound is not
# affected, so the documented switchover — a converged tracking standby
# promoted over the peer it tracks, which the failover and
# misordered-promote legs depend on — still takes the field.
#
# The leg stages both arms on the born legs' scratch field, one field
# and one proven holder per arm, so the skew is the only variable
# either arm turns:
#
# - the holder (`revised`): a born-active declaring no pair over the
#   unclaimed sim-serve field. Its conditional startup grant lands and
#   it scans as the field's writer — the register protocol's
#   `probe_writer` hook serves the claim posture, so `/role` answers
#   `field_claim: held`, and the seat is the live incumbent the
#   in-bound arm's claim has to meet.
# - the in-bound arm (`driven`): the same born-active shape declaring
#   `--standby <holder>` at the rig's documented cadence, converged
#   `tracking` on the holder. Its basis is read against the holder's
#   immediately before it claims and must sit inside the bound — a
#   freshly launched tracker's own run tick trails the holder's by the
#   holder's accrued scans, nowhere near the bound. Its `POST /promote`
#   must land the documented switchover: the seat settles `active` with
#   the claim held, and the holder demotes in place through the
#   `fenced`-origin `active → demoting → standby` walk beside exactly
#   one `field_claim_lost` naming the promoted peer's owner token.
# - the skewed arm (`foreign`): a third born-active declaring
#   `--standby <the promoted peer>` but launched through the born
#   launcher's per-container `scan_ms` lever at 25 ms. A run's tick
#   accrues one per scan, so this seat's basis accrues four ticks for
#   every one the promoted holder's does — the rig's own clock skew,
#   staged on the rig rather than injected, and read back off both
#   seats' served `/role` ticks before the attempt goes out. The
#   staging is declared, not assumed: the leg waits for the measured
#   separation to pass the recorded bound and leaves the attempt
#   unjudged if it never did, since a claim computed inside the bound
#   proves nothing about one computed past it.
#
# The attempt itself is the contract's subject. `POST /promote` on the
# skewed claimant carries the documented unconditional claim — the
# winner-take-all grant a switchover relies on — and it must not take
# the field from the live incumbent. Either outcome the contract allows
# counts: refused, or granted and then bounded. What it may not be is
# silent: the attempt must be refused or bounded *by name* — a named
# arbitration cause on the answer or a named refusal the claimant's own
# journal records — so the refusal is auditable rather than an
# unexplained gate that stays shut. A refusal the promote's own
# convergence gate answers before a claim is computed at all is not that
# audit trail: the leg reads the claimant's convergence immediately
# before the post, so a `not_converged` answer names a race rather than
# the bound and is reported as the instability it is.
#
# Across the attempt the promoted holder must be untouched: still the
# field's writer, still `active`, its scan advancing, its journal
# carrying no `field_claim_lost` and no fenced-origin demotion. The
# deployed pair never enters the staging — the scratch field is a
# different container on a different claim token — and the leg frames
# the pair's roles and advancing scan before, after, and once the leg's
# own seats are gone. Each pass ends with the rig swept: the three
# seats and the scratch field removed, the sweep audited back over the
# rig (a seat or a field that outlived it is a claim the legs behind
# this one would inherit), and two consecutive passes produce identical
# outcome digests.
#
# Named diagnostics: claim-skew-bound-failed tags the contract clauses
# — a skewed claim that took the field from the live incumbent, a
# refusal that named nothing, an in-bound claim the bound refused or
# left unclaimed, a promoted holder the skew disturbed (the claim
# flipped, a fenced demotion journaled, the scan stopped), a first
# holder whose documented demotion was not the fenced-origin walk or
# was not attributed to the promoted claim — while
# claim-skew-bound-nondeterministic tags the instability the contract
# does not answer for: refused staging calls, a starved watch or
# monitor, an unread verdict, a claimant that never converged, a skew
# that never separated past the recorded bound, a promote the
# convergence gate answered before a claim was computed, a moved or
# wedged deployed pair, a rig the sweep did not restore, or two passes
# whose digests diverge.
#
# No staged revision predates this contract in a shape the leg declines
# on: a skewed claim that resolves the field is the defect the leg
# names, not an unread surface, so every inconclusive verdict here is
# rig-side — an unstaged lever, an unreachable endpoint, an unsettled
# pair, or a staging that never produced the basis it declared. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives — the issue's doctored case, the attempt read as refused
# and bounded while the skewed claim preempts the field, the in-bound
# claim refused, the holder disturbed — and reports
# claim-skew-unchecked for any that slip through.

INCUMBENT_SEAT = 'revised'   # the first controller — takes the claim
CONTROL_SEAT = 'driven'      # the in-bound claimant — claims normally
SKEWED_SEAT = 'foreign'      # the skewed claimant — bounded, not seated
SKEWED_SCAN_MS = 25          # its per-container cadence lever
SKEW_BOUND = 64              # the leg's recorded staging bound, in run
                            # ticks: the separation a claimant's basis
                            # must pass before its claim is judged, and
                            # the margin an in-bound tracker's honest
                            # trailing basis sits well inside
SKEW_SETTLE = 45             # bound on each launch settling, on the
                            # skew's separation crossing the bound, and
                            # on a promotion settling to field ownership
SKEW_POLL = 0.4              # cadence polling a seat's verdict mid-arm
SKEW_WATCH = 4               # served reads each arm's window spans
CLAUSE = 'claim-skew-bound-failed'
NONDET = 'claim-skew-bound-nondeterministic'
UNCHECKED = 'claim-skew-unchecked'

# The promote gate's own named refusals — the answers `POST /promote`
# gives *before* a claim is computed at all. A skewed attempt answered
# one of these never reached the field's arbitration, so it is not the
# contract's named bound and the leg reports the race it is.
GATE_CAUSES = ('not_converged', 'already_active', 'no_tracking_source')


def _skew_posture(ctx, base):
    """One normalized `/role` read — role, the field's claim posture,
    tracking convergence, and the served scan tick — or None when the
    endpoint dropped it. Every seat and deployed-pair read the leg
    makes goes through this one shape."""
    report = _try_role(ctx, base)
    if not isinstance(report, dict):
        return None
    tick = report.get('tick')
    return {'role': report.get('role'),
            'field_claim': report.get('field_claim'),
            'tracking': 'tracking' in (report.get('sync') or {}),
            'tick': tick if isinstance(tick, int)
            and not isinstance(tick, bool) else None}


def _skew_view(ctx, seat):
    """One monitor read of a born seat, or None. A seat's own
    `field_claim: held` is never its own claim — every attachment
    observes the standing claim through `probe_writer` — so the role is
    what says who owns the field."""
    base = ctx.get(seat)
    return _skew_posture(ctx, base) if base else None


def _skew_state(ctx, seat):
    """The seat container's process verdict through the runner's
    read-only state lever — the presence read the sweep's audit takes,
    and the one surface that answers for a seat whose monitor has
    stopped serving."""
    lever = ctx.get('born_controller_state')
    if lever is None:
        return None
    try:
        return lever(seat)
    except Exception:
        return None


def _skew_journal(ctx, seat):
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


def _skew_events(ctx, seat, kind):
    """The `kind` event bodies the seat's durable journal carries."""
    out = []
    for item in _skew_journal(ctx, seat):
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _skew_fenced(ctx, seat):
    """Whether the seat's journal records the field taking its claim
    away — the `field_claim_lost` a preempted owner's first fenced
    write lands, or the fenced `active → standby` demotion that
    follows it. Either is the claim flipping."""
    if _skew_events(ctx, seat, 'field_claim_lost'):
        return True
    return any(event.get('from') == 'active'
               and event.get('to') == 'standby'
               and event.get('origin') == 'fenced'
               for event in _skew_events(ctx, seat, 'role_changed'))


def _skew_claimants(ctx, seat):
    """The owner tokens the seat's journaled `field_claim_observed`
    records attribute the standing claim to — recorded as the
    attribution audit and not required of it, since a bound refusing a
    claim before it reaches the field names nothing on the wire."""
    return sorted({event.get('claimant')
                   for event in _skew_events(ctx, seat,
                                             'field_claim_observed')
                   if event.get('claimant') is not None})


def _skew_causes(ctx, seat):
    """The refusal causes the seat's own durable journal names — a
    `promotion_refused` record's error, read as the cause name its
    serialized form carries. The armed-failover path's durable refusal
    trail; a requested promotion answers its caller instead."""
    out = set()
    for event in _skew_events(ctx, seat, 'promotion_refused'):
        cause = _skew_refusal_name(event.get('error'))
        if cause is not None:
            out.add(cause)
    return out


def _skew_refusal_name(body):
    """The cause a switchover refusal names — a bare variant string
    (`"already_active"`) or the single key of the object form
    (`{"not_converged": {...}}`, `{"field_claim_failed": {...}}`) — or
    None when the body names no cause at all."""
    if isinstance(body, str):
        return body or None
    if isinstance(body, dict) and len(body) == 1:
        return next(iter(body)) or None
    return None


def _skew_launch(ctx, seat, remote, standby=None, scan_ms=None):
    """Launch one controller on the leg's own field: the scratch
    sim-serve address as `--remote`, the launch shape's tracking wiring
    in `standby`, and the per-container skew lever in `scan_ms`. A
    refused staging call is the record's own answer — the judge's
    instability class, never a verdict."""
    try:
        launched = ctx['start_born_controller'](
            seat, remote, standby=standby, scan_ms=scan_ms)
    except Exception as exc:
        return {'seat': seat, 'stage_error': str(exc)[:300]}
    return {'seat': seat, 'launch': launched,
            'scan_ms': (launched or {}).get('scan_ms')}


def _skew_tracking(ctx, seat):
    """The served view of a converged tracking standby, or None."""
    return wait_for(
        lambda: (lambda view: view if view is not None
                 and view.get('role') == 'standby'
                 and view.get('tracking') is True else None)(
                     _skew_view(ctx, seat)),
        time.monotonic() + SKEW_SETTLE, interval=SKEW_POLL)


def _skew_holder(ctx, seat):
    """The served view of a seat that reports itself the field's
    writer, or None."""
    return wait_for(
        lambda: (lambda view: view if view is not None
                 and view.get('role') == 'active'
                 and view.get('field_claim') == 'held' else None)(
                     _skew_view(ctx, seat)),
        time.monotonic() + SKEW_SETTLE, interval=SKEW_POLL)


def _skew_watch(ctx, seat, count=SKEW_WATCH):
    """Served reads across one arm's window — the settled evidence
    tuple a role that moved or a claim that flipped shows up in."""
    views = []
    for _ in range(count):
        view = _skew_view(ctx, seat)
        if view is not None:
            views.append(view)
        time.sleep(SKEW_POLL)
    return views


def _skew_incumbent(ctx, remote):
    """The live claim holder: a born-active declaring no pair over the
    unclaimed field. Its conditional startup grant must land and it
    must scan as the field's writer — the served claim posture both
    arms' claims are computed against."""
    incumbent = _skew_launch(ctx, INCUMBENT_SEAT, remote)
    if incumbent.get('stage_error') is not None:
        return incumbent
    incumbent['granted'] = _skew_holder(ctx, INCUMBENT_SEAT)
    incumbent['before'] = _skew_view(ctx, INCUMBENT_SEAT)
    return incumbent


def _skew_basis(ctx, claimant, holder):
    """The two seats' claim bases at one moment: the claimant's and the
    holder's own served run ticks and their separation — the
    cross-domain comparison a claim is computed on, read off the rig
    rather than assumed. None while either seat's tick is unreadable."""
    claimant_tick = (_skew_view(ctx, claimant) or {}).get('tick')
    holder_tick = (_skew_view(ctx, holder) or {}).get('tick')
    if not isinstance(claimant_tick, int) \
            or not isinstance(holder_tick, int):
        return None
    return {'claimant': claimant_tick, 'holder': holder_tick,
            'separation': claimant_tick - holder_tick}


def _skew_separated(ctx, claimant, holder, bound):
    """The first basis separation past the declared bound, or None —
    the skew's staging evidence. Waiting for it is what keeps the leg's
    claim about a basis past the bound: an attempt computed inside it
    proves nothing."""
    return wait_for(
        lambda: (lambda basis: basis if basis is not None
                 and basis['separation'] > bound else None)(
                     _skew_basis(ctx, claimant, holder)),
        time.monotonic() + SKEW_SETTLE, interval=SKEW_POLL)


def _skew_promote(ctx, seat):
    """One `POST /promote` and its answer: `{'status', 'body',
    'cause'}` — a refusal's named cause is in the body, so the refusal
    is read here rather than through the 2xx-only helper. None when the
    request itself went unanswered, which is the judge's instability
    class."""
    base = ctx.get(seat)
    if not base:
        return None
    try:
        status, body = http_json('POST', base + '/promote',
                                 {'actor': 'qa-claim-skew'})
    except urllib.error.HTTPError as exc:
        try:
            status, body = exc.code, json.loads(exc.read() or b'null')
        except ValueError:
            status, body = exc.code, None
        finally:
            exc.close()
    except Exception:
        return None
    return {'status': status, 'body': body,
            'cause': _skew_refusal_name(body)}


def _skew_control(ctx, remote):
    """The in-bound arm: a `--standby` claimant at the rig's documented
    cadence, converged on the live holder, whose basis must read inside
    the recorded bound before it claims. Its promotion is the
    documented switchover the bound must not wedge."""
    control = _skew_launch(ctx, CONTROL_SEAT, remote,
                           standby=INCUMBENT_SEAT)
    if control.get('stage_error') is not None:
        return control
    control['converged'] = _skew_tracking(ctx, CONTROL_SEAT)
    control['basis'] = _skew_basis(ctx, CONTROL_SEAT, INCUMBENT_SEAT)
    control['promote'] = _skew_promote(ctx, CONTROL_SEAT)
    control['claimed'] = _skew_holder(ctx, CONTROL_SEAT)
    control['views'] = _skew_watch(ctx, CONTROL_SEAT)
    control['causes'] = sorted(_skew_causes(ctx, CONTROL_SEAT))
    return control


def _skew_claimant(ctx, remote, holder):
    """The skewed arm: a third `--standby` claimant launched through the
    per-container `scan_ms` lever, converged on the promoted holder,
    with the skew staged by the measured separation passing the
    recorded bound before it computes its claim. The attempt's own
    disposition is the contract's subject, and the promoted holder's
    surface across it is the incumbent evidence."""
    skewed = _skew_launch(ctx, SKEWED_SEAT, remote,
                          standby=holder, scan_ms=SKEWED_SCAN_MS)
    if skewed.get('stage_error') is not None:
        return skewed
    skewed['holder'] = holder
    skewed['converged'] = _skew_tracking(ctx, SKEWED_SEAT)
    skewed['basis'] = _skew_separated(ctx, SKEWED_SEAT, holder,
                                      SKEW_BOUND)
    skewed['holder_before'] = _skew_view(ctx, holder)
    skewed['promote'] = _skew_promote(ctx, SKEWED_SEAT)
    skewed['views'] = _skew_watch(ctx, SKEWED_SEAT)
    skewed['holder_after'] = _skew_view(ctx, holder)
    skewed['holder_fenced'] = _skew_fenced(ctx, holder)
    causes = set(_skew_causes(ctx, SKEWED_SEAT))
    answer = skewed.get('promote') or {}
    if answer.get('cause'):
        causes.add(answer['cause'])
    skewed['causes'] = sorted(causes)
    skewed['claimants'] = _skew_claimants(ctx, SKEWED_SEAT)
    skewed['disposition'] = _skew_disposition(skewed)
    return skewed


def _skew_disposition(skewed):
    """The attempt's disposition, derived from the record's own
    evidence — the served views, the live holder's surface, the
    promote's answer, and the named causes beside it. The judge and the
    digest both read it through here rather than off the record's
    stored verdict, so a doctored disposition field proves nothing:

    - 'preempted' — the claimant reported field ownership across the
      window, or the live holder lost the field: the claim resolved on
      the untranslated comparison the contract forbids;
    - 'gated' — the attempt was answered by the promote gate's own
      named refusal, so no claim was ever computed and the verdict
      belongs to the staging's race, not the arbitration;
    - 'refused' — the holder kept the field and the attempt was
      refused or bounded by name;
    - 'unnamed' — the holder kept the field and an answered attempt
      named no reason;
    - 'unanswered' — the attempt itself produced no verdict at all,
      which is a lost request rather than a silent one.
    """
    if not _skew_holder_held(skewed):
        return 'preempted'
    names = skewed.get('causes') or []
    if names:
        return 'gated' if all(name in GATE_CAUSES for name in names) \
            else 'refused'
    if (skewed.get('promote') or {}).get('status') is not None:
        return 'unnamed'
    return 'unanswered'


def _skew_inside(basis):
    """Whether a read basis sits inside the recorded bound — the
    in-bound arm's own staging gate."""
    return isinstance(basis, dict) \
        and isinstance(basis.get('separation'), int) \
        and abs(basis['separation']) <= SKEW_BOUND


def _skew_past(basis):
    """Whether a read basis sits past the recorded bound — the skewed
    arm's own staging gate."""
    return isinstance(basis, dict) \
        and isinstance(basis.get('separation'), int) \
        and basis['separation'] > SKEW_BOUND


def _skew_demoted(walk):
    """The documented in-place demotion of a preempted field owner:
    the `fenced`-origin `active → demoting → standby` walk and nothing
    else on the journal."""
    return [(event.get('from'), event.get('to'), event.get('origin'))
            for event in walk] == [
                ('active', 'demoting', 'fenced'),
                ('demoting', 'standby', 'fenced')]


def _skew_attributed(claims, claimant_token):
    """Whether the journaled claim losses are exactly one, attributed to
    the promoted claim's owner token."""
    return [event.get('claimant') for event in claims] \
        == [claimant_token]


def _skew_holder_held(skewed):
    """The live incumbent was never disturbed by the skewed attempt:
    still the field's writer, still active, its scan advancing across
    the attempt's window, and its journal carrying no fenced demotion.
    Only meaningful where the skewed arm actually ran — an attempt never
    computed leaves the holder nothing to lose."""
    if skewed.get('stage_error') is not None \
            or not skewed.get('views'):
        return True
    before = skewed.get('holder_before') or {}
    after = skewed.get('holder_after') or {}
    if before.get('role') != 'active' \
            or before.get('field_claim') != 'held':
        return True
    if after.get('role') != 'active' or after.get('field_claim') != 'held':
        return False
    if skewed.get('holder_fenced') is True:
        return False
    tick0, tick1 = before.get('tick'), after.get('tick')
    return isinstance(tick0, int) and isinstance(tick1, int) \
        and tick1 > tick0


def _skew_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the leg's staging, the peer still a
    tracking standby — in every framing the record carries, the
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
        if not isinstance(seen_tick, int) or (tick is not None
                                              and seen_tick <= tick):
            return False
        tick = seen_tick
    return tick is not None


def _judge_skew(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check. `note(key, diagnostic, detail)` records each clause
    the record violates: CLAUSE tags the contract clauses and NONDET
    the instability the contract does not answer for."""
    def failed(key, detail):
        note(key, CLAUSE, detail)

    def nondet(key, detail):
        note(key, NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the born field never staged: '
               + str(record['stage_error']))
        return
    incumbent = record.get('incumbent') or {}
    control = record.get('control') or {}
    skewed = record.get('skewed') or {}
    tokens = record.get('tokens') or {}

    if incumbent.get('stage_error') is not None:
        nondet('incumbent-stage', 'the holder launch never staged: '
               + str(incumbent['stage_error']))
    elif not incumbent.get('granted'):
        nondet('incumbent-claim', 'the first launch never reported '
               'itself the field\'s writer inside the bound — the live '
               'holder neither claim has to meet ever existed: '
               + json.dumps(incumbent.get('before'))[:200])

    # ---- the in-bound arm: the documented switchover the bound may
    # not wedge.
    if control.get('stage_error') is not None:
        nondet('control-stage', 'the in-bound claimant never staged: '
               + str(control['stage_error']))
    elif not control.get('converged'):
        nondet('control-converge', 'the in-bound claimant never '
               'converged tracking on the live holder: '
               + json.dumps({key: control.get(key) for key in
                             ('scan_ms', 'converged', 'basis')})[:250])
    elif not _skew_inside(control.get('basis')):
        nondet('control-basis', 'the in-bound claimant\'s basis did not '
               'read inside the recorded bound of ' + str(SKEW_BOUND)
               + ' — its claim proves nothing about a basis inside one: '
               + json.dumps(control.get('basis')))
    elif control.get('claimed'):
        if incumbent.get('read') is False:
            nondet('incumbent-journal', 'the holder\'s journal file '
                   'could not be read — the demotion\'s durable '
                   'attribution cannot be audited')
        elif not _skew_demoted(incumbent.get('walk') or []):
            failed('demotion-unwalked', 'the live holder did not demote '
                   'in place through the fenced-origin '
                   'active → demoting → standby walk the switchover '
                   'documents: '
                   + json.dumps(incumbent.get('walk'))[:300])
        elif not _skew_attributed(incumbent.get('claims') or [],
                                  tokens.get(CONTROL_SEAT)):
            failed('demotion-unattributed', 'the holder\'s journaled '
                   'claim loss is not exactly the promoted peer\'s owner '
                   'token ' + str(tokens.get(CONTROL_SEAT)) + ': '
                   + json.dumps(incumbent.get('claims'))[:300])
    elif not control.get('views'):
        nondet('control-watch', 'the in-bound claimant\'s monitor never '
               'answered the settled reads across its promotion')
    else:
        failed('control-refused', 'a claimant whose basis reads inside '
               'the recorded bound was refused its claim — the bound '
               'must leave the documented switchover alone: '
               + json.dumps({'promote': control.get('promote'),
                             'views': control.get('views')})[:300])
    if control.get('claimed') and control.get('causes'):
        failed('control-refused-journal', 'the in-bound claimant '
               'journaled a refusal of its own promotion — a claim '
               'inside the bound is granted: '
               + json.dumps(control.get('causes'))[:200])

    # ---- the skewed arm: the contract's own subject.
    if skewed.get('stage_error') is not None:
        nondet('skewed-stage', 'the skewed claimant never staged: '
               + str(skewed['stage_error']))
    elif not skewed.get('converged'):
        nondet('skewed-converge', 'the skewed claimant never converged '
               'tracking on the promoted holder: '
               + json.dumps({key: skewed.get(key) for key in
                             ('scan_ms', 'converged', 'basis')})[:250])
    elif not _skew_past(skewed.get('basis')):
        nondet('skewed-basis', 'the skewed claimant\'s basis never '
               'separated past the recorded bound of ' + str(SKEW_BOUND)
               + ' — an attempt computed inside the bound says nothing '
               'about one computed past it: '
               + json.dumps(skewed.get('basis')))
    elif not skewed.get('views'):
        nondet('skewed-watch', 'the skewed claimant\'s monitor never '
               'answered the attempt\'s settled reads')
    else:
        disposition = _skew_disposition(skewed)
        if disposition == 'preempted':
            failed('skewed-preempted', 'a claim whose basis sits past '
                   'the recorded bound took the field from a live '
                   'incumbent — the arbitration resolved on a '
                   'comparison the comparability rule forbids: '
                   + json.dumps({'basis': skewed.get('basis'),
                                 'promote': skewed.get('promote'),
                                 'views': skewed.get('views'),
                                 'holder': skewed.get('holder_after'),
                                 'holder_fenced': skewed.get(
                                     'holder_fenced')})[:500])
        elif disposition == 'unnamed':
            failed('skewed-unnamed', 'the skewed attempt left the '
                   'incumbent\'s field alone but named no reason — the '
                   'refusal or the bound must be auditable: '
                   + json.dumps({'promote': skewed.get('promote'),
                                 'causes': skewed.get('causes')})[:300])
        elif disposition == 'gated':
            nondet('skewed-gated', 'the skewed attempt was answered by '
                   'the promote gate\'s own '
                   + ', '.join(skewed.get('causes') or [])
                   + ' refusal before any claim was computed — the '
                   'arbitration never saw the attempt: '
                   + json.dumps(skewed.get('promote'))[:300])
        elif disposition == 'unanswered':
            nondet('skewed-unanswered', 'the skewed attempt produced no '
                   'readable verdict at all: '
                   + json.dumps(skewed.get('promote'))[:300])

    if not _skew_holder_held(skewed):
        failed('holder-disturbed', 'the live incumbent was disturbed by '
               'the skewed claim — a bound claim leaves the holder '
               'writing: '
               + json.dumps({'before': skewed.get('holder_before'),
                             'after': skewed.get('holder_after'),
                             'fenced': skewed.get('holder_fenced')})[:300])

    if not _skew_pair_held(record):
        nondet('pair-disturbed', 'the deployed pair moved or wedged '
               'across the staging: '
               + json.dumps(record.get('roles'), sort_keys=True)[:300])

    rig = record.get('rig') or {}
    seats = rig.get('seats') or {}
    standing = sorted(seat for seat, absent in seats.items()
                      if absent is not True)
    if standing:
        nondet('rig-not-restored', 'the leg left a seat\'s claim state '
               'standing — a seat that outlived the sweep the legs '
               'behind this one inherit: '
               + json.dumps({'standing': standing,
                             'field_error': rig.get('field_error')},
                            sort_keys=True)[:300])
    serving = rig.get('field_serving')
    if serving is True:
        nondet('rig-not-restored', 'the scratch field outlived the sweep '
               '— its own tool still answers, and the legs behind this '
               'one would stage onto a field carrying the claim this '
               'leg left: ' + json.dumps(rig, sort_keys=True)[:300])
    elif serving is None:
        nondet('field-unread', 'the scratch field\'s own shipped tool '
               'could not be read back after the sweep, so the field\'s '
               'own removal cannot be audited: '
               + json.dumps(rig, sort_keys=True)[:300])


def _skew_digest(record, violations):
    """The pass's normalized verdict set — identical digests across two
    consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    skewed = record.get('skewed') or {}
    return {
        'holder': 'holds-and-scans'
            if clean('holder-disturbed', 'incumbent-stage',
                     'incumbent-claim')
            else 'defect',
        'in-bound': 'claims-normally'
            if clean('control-stage', 'control-converge',
                     'control-basis', 'control-watch',
                     'control-refused', 'control-refused-journal',
                     'incumbent-journal', 'demotion-unwalked',
                     'demotion-unattributed')
            else 'defect',
        'skewed': {'refused': 'refused-and-named',
                   'unnamed': 'unnamed',
                   'preempted': 'preempted',
                   'gated': 'unanswered',
                   'unanswered': 'unanswered'}.get(
                       _skew_disposition(skewed), 'unanswered'),
        'staging': 'past-the-bound'
            if clean('skewed-stage', 'skewed-converge',
                     'skewed-basis', 'skewed-watch', 'skewed-gated',
                     'skewed-unanswered')
            else 'unstaged',
        'pair': 'held' if clean('pair-disturbed') else 'disturbed',
        'rig': 'restored' if clean('rig-not-restored', 'field-unread')
        else 'dirty'}


def _skew_pass(ctx, number, launch):
    """One pass over the contract: frame the deployed pair's roles,
    stage the scratch field, seat the live holder, run the in-bound arm
    (a claim inside the recorded bound must take the field), then the
    skewed arm against the promoted holder (a claim past the bound must
    not), and frame the pair again. The `final` framing and the rig's
    restoration read belong to the caller: they only mean anything once
    the sweep has run."""
    record = {'pass': number, 'launch': dict(launch), 'roles': {},
              'field': {}, 'incumbent': {}, 'control': {}, 'skewed': {},
              'rig': {}}
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _skew_posture(ctx, ctx[name]) for name in (owner, peer)}
    try:
        field = ctx['start_born_field']('serving')
    except Exception as exc:
        record['stage_error'] = str(exc)[:300]
    else:
        remote = field.get('remote')
        record['field'] = {'remote': remote, 'mode': field.get('mode')}
        record['incumbent'] = _skew_incumbent(ctx, remote)
        record['control'] = _skew_control(ctx, remote)
        # The first holder's documented demotion, read after the
        # in-bound arm's promotion: exactly one attributed claim loss
        # and the fenced-origin walk that carries it. An unreadable
        # file is the rig's staging surface, not a missing record, so
        # the walk's clause waits on its own readability read.
        incumbent = record['incumbent']
        if incumbent.get('granted'):
            path = (ctx.get('journal_files') or {}).get(INCUMBENT_SEAT)
            incumbent['read'] = bool(path) and Path(path).is_file()
            incumbent['walk'] = _skew_events(ctx, INCUMBENT_SEAT,
                                             'role_changed')
            incumbent['claims'] = _skew_events(ctx, INCUMBENT_SEAT,
                                               'field_claim_lost')
        # The in-bound arm's promotion, when it landed, seats this
        # arm's live holder: the skewed claimant declares *it* as its
        # tracking source, so the attempt it computes is a claim
        # against a proven field writer rather than against a standing
        # claim nobody holds.
        holder = (CONTROL_SEAT if record['control'].get('claimed')
                  else INCUMBENT_SEAT)
        record['holder'] = holder
        record['skewed'] = _skew_claimant(ctx, remote, holder)
    record['roles']['after'] = {
        name: _skew_posture(ctx, ctx[name]) for name in (owner, peer)}
    return record


def _skew_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives — the issue's doctored case, the attempt read as refused
    and bounded by name while the skewed claim preempts the field; an
    unnamed refusal; an in-bound claim the bound refused or left
    unclaimed; a holder the skew disturbed; an unattributed demotion —
    and every instability class the leg reports nondeterministic, and
    report every one let slip."""
    def clean_record():
        return {
            'pass': 1,
            'launch': {'owner': 'active', 'peer': 'standby'},
            'field': {'remote': 'dcs-hw-qa-1-born-plant:9003',
                      'mode': 'serving'},
            'holder': CONTROL_SEAT,
            'incumbent': {'seat': INCUMBENT_SEAT, 'scan_ms': 100,
                          'granted': {'role': 'active',
                                      'field_claim': 'held',
                                      'tracking': False, 'tick': 5},
                          'before': {'role': 'active',
                                     'field_claim': 'held',
                                     'tracking': False, 'tick': 10},
                          'walk': [
                              {'from': 'active', 'to': 'demoting',
                               'origin': 'fenced'},
                              {'from': 'demoting', 'to': 'standby',
                               'origin': 'fenced'}],
                          'claims': [{'point': 20,
                                      'claimant': 424245}],
                          'read': True},
            'control': {'seat': CONTROL_SEAT, 'scan_ms': 100,
                        'converged': {'role': 'standby',
                                      'tracking': True, 'tick': 4},
                        'basis': {'claimant': 4, 'holder': 12,
                                  'separation': -8},
                        'promote': {'status': 200,
                                    'body': {'role': 'promoting',
                                             'tick': 6},
                                    'cause': None},
                        'claimed': {'role': 'active',
                                    'field_claim': 'held',
                                    'tracking': False, 'tick': 40},
                        'views': [{'role': 'active',
                                   'field_claim': 'held',
                                   'tracking': False, 'tick': 40}],
                        'causes': []},
            'skewed': {'seat': SKEWED_SEAT,
                       'scan_ms': SKEWED_SCAN_MS,
                       'holder': CONTROL_SEAT,
                       'converged': {'role': 'standby',
                                     'tracking': True, 'tick': 300},
                       'basis': {'claimant': 400, 'holder': 20,
                                 'separation': 380},
                       'holder_before': {'role': 'active',
                                         'field_claim': 'held',
                                         'tracking': False,
                                         'tick': 380},
                       'promote': {'status': 409,
                                   'body': {
                                       'field_claim_failed': {
                                           'detail': "the claimant's "
                                                     'basis is past the '
                                                     'recorded skew '
                                                     'bound'}},
                                   'cause': 'field_claim_failed'},
                       'views': [{'role': 'standby',
                                  'field_claim': 'held',
                                  'tracking': True, 'tick': 400}],
                       'holder_after': {'role': 'active',
                                        'field_claim': 'held',
                                        'tracking': False,
                                        'tick': 470},
                       'holder_fenced': False,
                       'causes': ['field_claim_failed'],
                       'claimants': [424245],
                       'disposition': 'refused'},
            'tokens': {INCUMBENT_SEAT: 424243, CONTROL_SEAT: 424245,
                       SKEWED_SEAT: 424244},
            'rig': {'seats': {INCUMBENT_SEAT: True, CONTROL_SEAT: True,
                              SKEWED_SEAT: True},
                    'field_error': None, 'field_serving': False},
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
        _judge_skew(record,
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

    # The issue's doctored negative: the attempt read as refused and
    # bounded by name while the skewed claim preempts the field. Each
    # negative doctores the *evidence* — the served views, the holder's
    # surface, the promote's answer, the named causes — because the
    # judge derives its verdict from those and never reads the record's
    # own stored disposition.
    expect('refusal-asserted-but-preempted', lambda r:
           r['skewed'].update(
               views=[{'role': 'promoting', 'field_claim': 'held',
                       'tracking': False, 'tick': 401}],
               holder_after={'role': 'standby', 'field_claim': None,
                             'tracking': False, 'tick': 401}))
    expect('refusal-asserted-but-holder-lost', lambda r:
           r['skewed'].update(
               holder_after={'role': 'active', 'field_claim': 'held',
                             'tracking': False, 'tick': 470},
               holder_fenced=True))
    expect('refusal-asserted-but-claimant-reported-active', lambda r:
           r['skewed'].update(views=[
               {'role': 'active', 'field_claim': 'held',
                'tracking': False, 'tick': 402}],
               holder_after={'role': 'standby', 'field_claim': None,
                             'tracking': False, 'tick': 402}))
    expect('refusal-unnamed', lambda r:
           r['skewed'].update(promote={'status': 409, 'body': {},
                                       'cause': None},
                              causes=[]))
    expect('in-bound-claim-refused', lambda r:
           r['control'].update(claimed=None,
                               views=[{'role': 'standby',
                                       'field_claim': 'held',
                                       'tracking': True, 'tick': 40}],
                               promote={'status': 409,
                                        'body': {'field_claim_failed':
                                                 {'detail': 'past the '
                                                  'bound'}},
                                        'cause': 'field_claim_failed'}))
    expect('in-bound-claim-never-landed', lambda r:
           r['control'].update(claimed=None,
                               views=[{'role': 'standby',
                                       'field_claim': 'held',
                                       'tracking': True, 'tick': 40}],
                               promote={'status': 200, 'body': None,
                                        'cause': None}))
    expect('in-bound-claim-journaled-a-refusal', lambda r:
           r['control'].update(causes=['field_claim_failed']))
    expect('holder-demotion-unwalked', lambda r:
           r['incumbent']['walk'].pop())
    expect('holder-demotion-wrong-origin', lambda r:
           r['incumbent']['walk'][0].update(origin='request'))
    expect('holder-demotion-unattributed', lambda r:
           r['incumbent']['claims'].append({'point': 20,
                                            'claimant': 424244}))
    expect('holder-left-active', lambda r:
           r['skewed']['holder_after'].update(role='standby'))
    expect('holder-lost-its-claim', lambda r:
           r['skewed'].update(holder_after={'role': 'standby',
                                            'field_claim': None,
                                            'tracking': False,
                                            'tick': 470}))
    expect('holder-demoted-fenced', lambda r:
           r['skewed'].update(holder_fenced=True))
    expect('holder-scan-wedged', lambda r:
           r['skewed']['holder_after'].update(tick=380))
    # The instability shapes must report nondeterministic: refused
    # staging calls, a holder that never claimed, a claimant that never
    # converged, a skew that never separated past the bound, a starved
    # watch, a gate answered before a claim was computed, an unanswered
    # attempt, a moved pair, a rig left standing.
    expect('field-stage-refused', lambda r:
           r.update(stage_error='docker run failed'), NONDET)
    expect('holder-launch-refused', lambda r:
           r['incumbent'].update(stage_error='docker run failed'), NONDET)
    expect('holder-never-claimed', lambda r:
           r['incumbent'].update(granted=None), NONDET)
    expect('in-bound-launch-refused', lambda r:
           r['control'].update(stage_error='docker run failed'), NONDET)
    expect('in-bound-never-converged', lambda r:
           r['control'].update(converged=None), NONDET)
    expect('in-bound-basis-outside-the-bound', lambda r:
           r['control'].update(basis={'claimant': 400, 'holder': 20,
                                      'separation': 380}), NONDET)
    expect('in-bound-watch-starved', lambda r:
           r['control'].update(claimed=None, views=[]), NONDET)
    expect('holder-journal-unreadable', lambda r:
           r['incumbent'].update(read=False), NONDET)
    expect('skewed-launch-refused', lambda r:
           r['skewed'].update(stage_error='docker run failed'), NONDET)
    expect('skewed-never-converged', lambda r:
           r['skewed'].update(converged=None), NONDET)
    expect('skewed-basis-inside-the-bound', lambda r:
           r['skewed'].update(basis={'claimant': 20, 'holder': 12,
                                     'separation': -8}), NONDET)
    expect('skewed-basis-unreadable', lambda r:
           r['skewed'].update(basis=None), NONDET)
    expect('skewed-watch-starved', lambda r:
           r['skewed'].update(views=[]), NONDET)
    expect('skewed-answered-by-the-promote-gate', lambda r:
           r['skewed'].update(promote={'status': 409,
                                       'body': {'not_converged': {}},
                                       'cause': 'not_converged'},
                              causes=['not_converged']), NONDET)
    expect('skewed-unanswered', lambda r:
           r['skewed'].update(promote=None,
                              causes=[],
                              views=[{'role': 'standby',
                                     'field_claim': 'held',
                                     'tracking': True, 'tick': 400}]),
           NONDET)
    expect('pair-owner-moved', lambda r:
           r['roles']['after']['active'].update(role='standby'), NONDET)
    expect('pair-peer-lost-tracking', lambda r:
           r['roles']['after']['standby'].update(tracking=False),
           NONDET)
    expect('pair-scan-wedged', lambda r:
           r['roles']['after']['active'].update(tick=10), NONDET)
    expect('pair-final-owner-moved', lambda r:
           r['roles']['final']['active'].update(role='standby'), NONDET)
    expect('rig-left-standing', lambda r:
           r['rig']['seats'].update({CONTROL_SEAT: False}), NONDET)
    expect('rig-presence-unreadable', lambda r:
           r['rig']['seats'].update({SKEWED_SEAT: None}), NONDET)
    expect('field-left-serving', lambda r:
           r['rig'].update(field_serving=True), NONDET)
    expect('field-presence-unreadable', lambda r:
           r['rig'].update(field_serving=None), NONDET)
    return slipped


def _skew_teardown(ctx):
    """Best-effort teardown: the leg's three seats and its scratch
    field — a clean pass leaves nothing standing and the rig's claim
    state free, and an aborted pass gets the same sweep so the legs
    behind this one see free seats and a fresh field. Returns the
    field's own removal error, or None when it came down."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        for seat in (SKEWED_SEAT, CONTROL_SEAT, INCUMBENT_SEAT):
            try:
                lever(seat)
            except Exception:
                pass
    try:
        if ctx.get('stop_born_field') is None:
            return None
        ctx['stop_born_field']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _skew_rig_state(ctx, field_error):
    """The rig's claim state after the sweep: every born seat's own
    presence read back through the read-only state lever, beside the
    scratch field's own removal error and whether its own shipped tool
    still answers — a field or a seat that outlived the sweep is a
    claim the legs behind this one would inherit. `absent` None is a
    read the lever could not answer, never a seat proven gone."""
    serving = None
    probe = ctx.get('born_field_ctl')
    if probe is not None:
        try:
            serving = probe('list').returncode == 0
        except Exception:
            serving = None
    return {'seats': {seat: (_skew_state(ctx, seat) or {}).get('absent')
                      for seat in (INCUMBENT_SEAT, CONTROL_SEAT,
                                   SKEWED_SEAT)},
            'field_error': field_error,
            'field_serving': serving}


def scenario_claim_skew_bound(ctx):
    """Exercise the skewed-claim preemption bound on the deployed rig:
    seat a live incumbent on the lane's scratch field, prove a claim
    whose basis reads inside the recorded bound still takes the field
    through the documented switchover, then stage the skew through the
    born launcher's per-container cadence lever and assert a claim
    computed past that bound takes nothing — the live incumbent keeps
    write-ownership with no fenced demotion journaled, and the attempt
    is refused or bounded by name. The rig's claim state and launch
    roles are restored afterward, and two consecutive passes produce
    identical outcome digests."""
    case = Case(
        'claim-skew-bound',
        'A claim computed on a skewed basis never preempts a live '
        'incumbent',
        'over a rig-mounted field a live controller owns, a claim '
        'whose basis sits inside the recorded skew bound still takes '
        'the field through the documented switchover — the holder '
        'demoting in place under the fenced origin with its loss '
        'attributed to the promoted claim — while a claim whose basis '
        'the rig\'s own per-container cadence lever drives past that '
        'bound takes nothing: the live incumbent keeps '
        'write-ownership, its role, and a moving scan with no fenced '
        'demotion journaled, and the attempt is refused or bounded by '
        'name; the rig\'s claim state and launch roles are restored — '
        'audited back over the swept rig, a seat or field that outlived '
        'the sweep fails the leg — and two passes produce identical '
        'digests')
    try:
        missing = [key for key in ('start_born_field', 'stop_born_field',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive', 'the run context carries '
                               'no born-field staging levers: '
                               + ', '.join(missing))
        if not all(ctx.get(seat)
                   for seat in (INCUMBENT_SEAT, CONTROL_SEAT,
                                SKEWED_SEAT)):
            return case.finish('inconclusive', 'the run context carries '
                               'no published monitor for the leg\'s '
                               'born seats')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(seat) for seat in
                   (INCUMBENT_SEAT, CONTROL_SEAT, SKEWED_SEAT)):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-seat journal files — the durable '
                               'half of the audit cannot run')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(seat) for seat in
                   (INCUMBENT_SEAT, CONTROL_SEAT, SKEWED_SEAT)):
            return case.finish('inconclusive', 'the run context records '
                               'no pinned --owner-token for the leg\'s '
                               'seats — the demotion\'s attribution '
                               'cannot be audited against a promoted '
                               'claim')
        deadline = time.monotonic() + SKEW_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SKEW_POLL)
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
                    interval=SKEW_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled posture '
                               'the leg proves undisturbed was never '
                               'reached')
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
                record = _skew_pass(ctx, number, launch)
            finally:
                # Each pass ends with the rig swept — the three seats
                # and the scratch field removed, so the next pass and
                # the legs behind this one start on a free claim state,
                # free seats, and a fresh field.
                field_error = _skew_teardown(ctx)
            # The restoration audit, read back over the swept rig: the
            # seats' own presence, the scratch field's own liveness, and
            # the pair's launch roles once the leg's claim is gone.
            record['rig'] = _skew_rig_state(ctx, field_error)
            record['roles']['final'] = {
                name: _skew_posture(ctx, ctx[name])
                for name in (owner, peer)}
            record['tokens'] = {seat: tokens.get(seat)
                                for seat in (INCUMBENT_SEAT, CONTROL_SEAT,
                                             SKEWED_SEAT)}
            _judge_skew(record, note)
            digest = _skew_digest(record, violations)
            record['digest'] = dict(digest)
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'claim-skew-bound-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'claim-skew-bound pass ' + str(number)
                          + ' — the staged scratch field, the holder\'s '
                          'claim posture, the in-bound arm\'s measured '
                          'basis and promotion, the skewed claimant\'s '
                          'measured basis separation and attempt, the '
                          'promoted holder\'s surface across it, the '
                          'deployed pair\'s before/after/final framing, '
                          'the swept rig\'s restoration read, and the '
                          'normalized digest')
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
        case.observe('two claim-skew passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _skew_self_check()
        if slipped:
            return case.finish('failed', UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))