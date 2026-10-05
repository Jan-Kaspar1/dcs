"""The dead_owner_fencing acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The dead-owner case restores what it moves — the induction's
# claims and the re-armed field are handed back through the launch
# owner's own token, and the pair's roles never leave their launch
# assignment because the staging rides a field-tool claim the settled
# peers never contend for — so it needs no declared window.


# --------------------------------------------------------------------
# The dead-owner fencing-claim contract (the ENABER per-revision lane
# evidence for WW-OPS-003's redundancy clause, in service of the
# clause decision 97's field-arbitrated ownership settles): the plant
# write claim is cleared only by the last holder's explicit release,
# never by disconnect — a dead owner's silence is exactly the failure
# the claim exists to fence — so a closed owner connection must leave
# the claim fencing every other attachment's mutations until a
# preempting claim_writer lands or the recorded owner re-arms.
#
# The legs around this one pin the neighbouring halves and none of them
# pins this shape: 2350's claim-reclaim proves the released-
# preemption re-seat after a *live* holder hands the field back, 2390's
# holderless-claim-recovery proves the fenced peer's bound reclaim
# closes the holderless wedge unattended, and 0700's field-claim proves
# a partial release leaves a live holder's claim standing. What no leg
# asserts is the never-released rule itself: that the *only* door out
# of a dead owner's claim is a claim verb, so an attachment holding
# nothing — any attachment, at any time — cannot dissolve the standing
# fence by sending `release_writer`. #638's fix settles the server half
# (a release that removed no hold can no longer empty the set); this leg
# is the per-revision rig evidence for it, and the assertion a rig must
# fail if the shape ever regresses.
#
# The leg stages the finding on the deployed simulated rig through the
# claim-aware attachment seam (#426) — the raw plant client, since
# `claim_writer` is an op `dcs-plant-ctl` does not expose — while the
# field census and the watched-value reads ride the shipped tool
# (#667). One pass:
#
# - the baseline: the settled pair's launch owner holds the field, a
#   third-party mutation probe is fenced naming its token, and the same
#   -token `ensure_writer` answers `claimed_shared` — the claim the
#   dead-owner window is staged against;
# - the induction: a dedicated attachment takes the write claim with an
#   unconditional `claim_writer` under a fresh tool token and lands a
#   write on the field, so the claim's death is a real ownership epoch
#   ending rather than a claim nobody used;
# - the dead owner: that attachment's connection is closed *without* a
#   release, and the leg waits out the server-side hold reap — the
#   window the empty holder set opens, unobservable from the outside
#   because a same-token probe would join the set, so the leg reads it
#   through the repeated non-holder sequence below, exactly as a rig
#   must;
# - the standing fence: across the reap window a second attachment —
#   holding nothing — repeats `release_writer` and reads the field
#   through `probe_writer` and a mutation probe on every round. The
#   release must answer `done` (the settled no-op answer) and the
#   claim must still stand: the probe names the dead owner's token
#   across every round, the mutation probe answers `fenced`, and the
#   dead owner's *own* token stays the recorded owner. A round on which
#   the field answers `unclaimed`, or names a different token, is the
#   defect: an attachment holding nothing dissolved the dead owner's
#   standing claim, and the field opened to any next claimant;
# - a foreign-token `ensure_writer` in the same window is refused
#   `fenced` — the dead owner's claim is not silently re-granted to a
#   different token while it stands (#635's half);
# - the re-arm: the recorded owner token's re-attach `ensure_writer`
#   answers `claimed` or `claimed_shared` per the settled contract, its
#   writes land, and a foreign-token `ensure_writer` is fenced again
#   afterwards — the fence's purpose is the owner's own return, and the
#   foreign token must not inherit it;
# - the preempt: an unconditional `claim_writer` from a third
#   attachment resolves per the declared contract — `done`, or
#   `claimed_shared` only while another live attachment holds that same
#   token, which this leg never stages — moving the claim to the
#   preemptor and leaving the recorded owner's mutations fenced;
# - the restore: the preemptor's own release hands the field back to
#   `unclaimed`, the launch owner's token re-arms it, and the pair's
#   roles never moved — so the legs behind this one find the launch
#   claim state and roles they expect.
#
# Named diagnostics: dead-owner-fencing-failed tags the contract
# clauses — the baseline claim absent or unattributed, a non-holder's
# release dissolving the dead owner's claim, a mutation or foreign
# ensure granted inside the window, the recorded owner's re-arm
# refused or its writes fenced, a foreign token granted after the
# re-arm, the preempt refused or answered the declared shared form
# without a live co-holder, the restore not returning the field to the
# launch owner's claim — and dead-owner-fencing-nondeterministic tags
# the instability the contract does not answer for: a refused staging
# lever, a dropped monitor/plant/journal read, a round on which the
# claim surface answered nothing at all. The unchecked-diagnostic
# self-check replays the judge over planted negatives — a silent audit
# reports dead-owner-fencing-unchecked. The leg is inconclusive when
# the staged run predates the contract: no `probe_writer` claim
# surface, no owner attribution on the fencing verdicts, no
# `plant_ctl` seam, no claim verbs on the plant protocol, or a
# published plant endpoint the leg cannot reach.

DEAD_OWNER_SETTLE = 30     # bound on the pair settling before staging
DEAD_OWNER_POLL = 0.4      # cadence polling the peers and the field
DEAD_OWNER_ROUNDS = 6      # non-holder release/probe rounds the reap
                          # window must span — the field is reaped of
                          # its dead hold inside one round here, so
                          # the window opens and is judged, never
                          # raced past
DEAD_OWNER_DEADLINE = 15   # bound on the reap, re-arm and preempt waits
DEAD_OWNER_RESTORE = 20    # bound on the launch-layout restore

DIAG_FAILED = 'dead-owner-fencing-failed'
DIAG_NONDET = 'dead-owner-fencing-nondeterministic'
DIAG_UNCHECKED = 'dead-owner-fencing-unchecked'

# The induction attachment's tool owner token — a fixed value colliding
# with none of the run's pinned controller tokens and none the preempt
# stages beside it. The dead owner's claim is this token's: the leg's
# whole window is about what happens to it after the attachment closes.
DEAD_OWNER_TOKEN = 0x7161_2d64_6564_2d31   # "qa-ded-1"
# The preemptor's tool owner token — the third attachment that takes the
# field back unconditionally at the end of the pass.
DEAD_OWNER_PREEMPT = 0x7161_2d64_6564_2d32  # "qa-ded-2"
# The foreign token a second attachment offers the field while the dead
# owner's claim stands — the grant the window must refuse.
DEAD_OWNER_FOREIGN = 0x7161_2d64_6564_2d33  # "qa-ded-3"


class Staged(Exception):
    """A staging lever the leg itself refused — the answer a doctored
    rig gives where the honest one proceeds. The judge names the
    missing clause; the pass is never silently dropped."""


class Inconclusive(Exception):
    """The staged run predates the contract, or the field's claim
    surface is unreachable — the run classifies inconclusive, never a
    product failure."""


def _dead_owner_claim(ctx):
    """The field's standing claim read through `probe_writer` — the
    non-mutating observation whose verdict a third attachment's
    mutation would meet."""
    return _try_plant(ctx, {'op': 'probe_writer'})


def _claim_owner(response):
    """The owner token a fencing verdict attributes the standing claim
    to — or None on an unfenced, unattributed or unanswered verdict."""
    return ((response or {}).get('error') or {}).get('owner')


def _dead_owner_field_point(ctx):
    """The field's declared out-point the leg's induction write lands
    on — resolved from the shipped tool's census rather than a
    hard-coded id, so the leg exercises the declared surface. None
    when the census declares no writable field output."""
    for point in _field_out_points(ctx):
        if isinstance(point, int):
            return point
    return None


def _dead_owner_mutation(ctx):
    """A bare `step` — the mutation probe the claim must keep fencing
    from an attachment that holds nothing. `dt: 0` mutates nothing, so
    a fenced answer is the claim's refusal and a `done` answer would be
    a real, unwanted advance."""
    return _try_plant(ctx, {'op': 'step', 'dt': 0})


def _dead_owner_wait_reap(ctx, window):
    """Read the reap of the closed attachment's hold through the
    repeated non-holder sequence: no request can observe the empty
    holder set without joining it, so the window is opened by sending
    the sequence itself, round after round, for the whole span. Each
    round appends its `probe_writer`, mutation-probe and release
    answers to `window`; the leg judges the rounds."""
    second = _plant_connect(ctx)
    try:
        for index in range(DEAD_OWNER_ROUNDS):
            probe = _dead_owner_claim(ctx)
            mutation = _dead_owner_mutation(ctx)
            release = _plant_request(second, {'op': 'release_writer'})
            window.append({
                'round': index,
                'probe': _probe_error(probe),
                'named': _claim_owner(probe),
                'mutation': _probe_error(mutation),
                'release': release.get('result'),
                'released_error': _probe_error(release),
                'answered': isinstance(probe, dict)})
            if index + 1 < DEAD_OWNER_ROUNDS:
                time.sleep(DEAD_OWNER_POLL)
    finally:
        second.close()


def _dead_owner_pass(ctx, owner_token, point, record, evidence):
    """One dead-owner pass over the field's claim surface, filling the
    caller's `record` and `evidence` in place.

    The record is what the judge replays, so it is the caller's: a stage
    that raises after recording its own answer leaves the exchange the
    judge needs intact beside the raise, and only the stages it never
    reached stay absent — never a verdict the pass had already made."""
    record.update({'owner_token': owner_token,
                   'dead_token': DEAD_OWNER_TOKEN,
                   'preempt_token': DEAD_OWNER_PREEMPT,
                   'foreign_token': DEAD_OWNER_FOREIGN,
                   'point': point})
    evidence['record'] = record
    # The launch owner's own attachment stands down first: the leg's
    # induction preempts the claim unconditionally, and a live holder
    # under the launch token would keep the field's standing verdict
    # naming a controller the window is not about.
    parked = None
    attachments = []
    try:
        record['baseline'] = _dead_owner_claim(ctx)
        evidence['baseline'] = record['baseline']
        if not _fenced(record['baseline']):
            raise Inconclusive(
                'the field held no writer claim the dead-owner window '
                'could stand against: '
                + json.dumps(record['baseline'])[:300])
        if _claim_owner(record['baseline']) is None:
            raise Inconclusive(
                'the fencing verdict names no standing owner — the rig '
                'predates the loss-attribution contract: '
                + json.dumps(record['baseline'])[:300])
        if _claim_owner(record['baseline']) != owner_token:
            raise Inconclusive(
                'the standing claim names a token other than the launch '
                'owner\'s — the rig is not in its launch claim state: '
                + json.dumps(record['baseline'])[:300])
        parked = _plant_connect(ctx)
        attachments.append(parked)
        release = _plant_request(parked, {'op': 'release_writer'})
        record['parked'] = release
        evidence['parked'] = release
        if release.get('result') != 'done':
            raise Staged('the launch owner\'s stand-down release '
                         'answered ' + json.dumps(release)[:200])

        # The induction: the dead owner takes the claim and writes,
        # so the claim that dies below is a used ownership epoch.
        dead = _plant_connect(ctx)
        attachments.append(dead)
        claim = _plant_request(dead, {'op': 'claim_writer',
                                      'owner': DEAD_OWNER_TOKEN,
                                      'controller': False})
        record['claim'] = claim
        evidence['claim'] = claim
        if claim.get('result') not in ('done', 'claimed_shared'):
            raise Staged('the induction claim was refused — the '
                         'claim-staging lever is absent: '
                         + json.dumps(claim)[:300])
        landed = _plant_request(dead, {'op': 'write', 'point': point,
                                       'value': {'bool': False}})
        record['induction_write'] = landed
        evidence['induction_write'] = landed
        if landed.get('result') != 'done':
            raise Staged('the dead owner\'s induction write was '
                         'refused: ' + json.dumps(landed)[:300])
        record['seized'] = _dead_owner_claim(ctx)
        evidence['seized'] = record['seized']

        # The dead owner: close the attachment without a release. This
        # is the finding's own reproduction — a claim whose owner is
        # gone, never handed back.
        attachments.remove(dead)
        dead.close()

        # The standing fence, read across the server-side reap window.
        window = []
        evidence['window'] = window
        _dead_owner_wait_reap(ctx, window)
        record['window'] = window
        record['after_window'] = _dead_owner_claim(ctx)
        evidence['after_window'] = record['after_window']

        # The foreign grant inside the window: the dead owner's claim
        # must not pass to a different token by a conditional ask.
        foreign = _plant_connect(ctx)
        attachments.append(foreign)
        refused = _plant_request(foreign, {
            'op': 'ensure_writer', 'owner': DEAD_OWNER_FOREIGN})
        record['foreign_ensure'] = refused
        evidence['foreign_ensure'] = refused

        # The re-arm: the recorded owner's own token returns.
        rearm = _plant_connect(ctx)
        attachments.append(rearm)
        granted = _plant_request(rearm, {'op': 'ensure_writer',
                                         'owner': DEAD_OWNER_TOKEN})
        record['rearm'] = granted
        evidence['rearm'] = granted
        if granted.get('result') not in ('done', 'claimed_shared'):
            raise Staged('the recorded owner\'s re-attach was refused '
                         'under its own token: '
                         + json.dumps(granted)[:300])
        rewrite = _plant_request(rearm, {'op': 'write', 'point': point,
                                         'value': {'bool': False}})
        record['rearm_write'] = rewrite
        evidence['rearm_write'] = rewrite
        if rewrite.get('result') != 'done':
            raise Staged('the re-armed owner\'s write was fenced: '
                         + json.dumps(rewrite)[:300])
        after = _plant_request(foreign, {
            'op': 'ensure_writer', 'owner': DEAD_OWNER_FOREIGN})
        record['foreign_after'] = after
        evidence['foreign_after'] = after
        attachments.remove(rearm)
        rearm.close()

        # The preempt: the only other door out of the dead owner's
        # claim — an unconditional claim from a third attachment.
        preempt = _plant_connect(ctx)
        attachments.append(preempt)
        took = _plant_request(preempt, {'op': 'claim_writer',
                                        'owner': DEAD_OWNER_PREEMPT,
                                        'controller': False})
        record['preempt'] = took
        evidence['preempt'] = took
        record['preempted'] = _dead_owner_claim(ctx)
        evidence['preempted'] = record['preempted']
        attachments.remove(preempt)
        preempt.close()

        # The restore, inside the pass: the preemptor's claim died
        # with its closed connection, so the field stands holderless
        # under its token — but a hand-back has to account for the
        # corpse's reap, which the never-released rule schedules on the
        # server's own time: while that hold still stands, one
        # claim/release leaves a live holder behind and the claim
        # stands. So the hand-back polls the claim's own verdict to a
        # bound, each round joining and dropping one more reaped
        # corpse, and only once the field reports no claim does the
        # launch owner's own `ensure_writer` re-arm — the same claim
        # state the pass started on, so the *next* pass's baseline is
        # the launch owner's claim and never this pass's leftovers.
        restore = _plant_connect(ctx)
        try:
            freed = False
            deadline = time.monotonic() + DEAD_OWNER_RESTORE
            while time.monotonic() < deadline:
                if not _fenced(_dead_owner_claim(ctx)):
                    freed = True
                    break
                _plant_request(restore, {'op': 'claim_writer',
                                         'owner': DEAD_OWNER_PREEMPT,
                                         'controller': False})
                record['restore_release'] = _plant_request(
                    restore, {'op': 'release_writer'})
                time.sleep(DEAD_OWNER_POLL)
            record['restore_freed'] = freed
        finally:
            restore.close()
        if record['restore_freed']:
            rearm_owner = _plant_connect(ctx)
            try:
                record['restore_rearm'] = _plant_request(
                    rearm_owner, {'op': 'ensure_writer',
                                  'owner': owner_token})
            finally:
                rearm_owner.close()
        record['restored'] = wait_for(
            lambda: (_dead_owner_claim(ctx) or {}).get(
                'error', {}).get('owner') == owner_token or None,
            time.monotonic() + DEAD_OWNER_DEADLINE,
            interval=DEAD_OWNER_POLL)
        evidence['restored'] = record['restored']
    finally:
        for conn in attachments:
            try:
                conn.close()
            except Exception:
                pass


def _judge_dead_owner(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the fencing contract
    clauses, DIAG_NONDET the instability the contract does not answer
    for. A stage that raised ends the audit where the pass ended — the
    later keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    # The baseline: the launch owner's own claim, the window's subject.
    baseline = record.get('baseline')
    if not isinstance(baseline, dict):
        nondet('baseline', 'the claim surface answered no probe — '
               'the leg cannot read the standing claim')
        return
    if not _fenced(baseline):
        failed('baseline', 'the field held no writer claim at pass '
               'start — the dead-owner window has no fence to stand '
               'against: ' + json.dumps(baseline)[:300])
        return

    # The induction: a used ownership epoch, taken and written.
    claim = record.get('claim')
    if claim is None:
        return
    if claim.get('result') not in ('done', 'claimed_shared'):
        failed('induction', 'the induction claim was refused: '
               + json.dumps(claim)[:300])
        return
    write = record.get('induction_write')
    if not isinstance(write, dict):
        nondet('induction_write', 'the induction write never landed')
        return
    if write.get('result') != 'done':
        failed('induction_write', "the dead owner's induction write "
               'was refused: ' + json.dumps(write)[:300])
        return

    # The standing fence: every round the claim names the dead owner's
    # token, the mutation probe is fenced, and the non-holder's release
    # answers `done` without dissolving anything.
    window = record.get('window')
    if not window:
        return
    for row in window:
        label = 'window round ' + str(row.get('round'))
        if not row.get('answered'):
            nondet(label, 'the claim surface answered no probe on this '
                   'round — one lost read, never a verdict')
            continue
        if row.get('release') != 'done':
            failed(label + ' release', 'a non-holder\'s release_writer '
                   'answered ' + str(row.get('release')) + '/'
                   + str(row.get('released_error'))
                   + ' — the settled contract answers a release that '
                   'removed no hold `done`, changing nothing')
        if row.get('probe') == 'unclaimed':
            failed(label + ' dissolved', 'the field answered `unclaimed` '
                   'inside the dead-owner window — a release from an '
                   'attachment holding nothing dissolved the standing '
                   'claim and opened the field to any next claimant')
        elif row.get('named') != record['dead_token']:
            failed(label + ' owner', 'the standing claim names '
                   + str(row.get('named')) + ', not the dead owner\'s '
                   'token ' + str(record['dead_token'])
                   + ' — the dead-owner fence did not hold its epoch: '
                   + json.dumps(row)[:300])
        if row.get('mutation') != 'fenced':
            failed(label + ' mutation', "the dead owner's claim did not "
                   'fence a mutation probe from an attachment holding '
                   'nothing: ' + str(row.get('mutation')))

    # The foreign grant refused inside the window.
    foreign = record.get('foreign_ensure')
    if foreign is None:
        return
    if not _fenced(foreign):
        failed('foreign', "a foreign token's ensure_writer was granted "
               "while the dead owner's claim stood: "
               + json.dumps(foreign)[:300])

    # The re-arm: the recorded owner's token returns and writes.
    rearm = record.get('rearm')
    if rearm is None:
        return
    if rearm.get('result') not in ('done', 'claimed_shared'):
        failed('rearm', 'the recorded owner\'s re-attach ensure_writer '
               'answered ' + json.dumps(rearm)[:300]
               + ' — a dead owner\'s own token must re-arm against its '
               'standing claim')
    elif (record.get('rearm_write') or {}).get('result') != 'done':
        failed('rearm_write', "the re-armed owner's write was refused: "
               + json.dumps(record.get('rearm_write'))[:300])

    # The foreign token still fenced after the re-arm.
    after = record.get('foreign_after')
    if after is None:
        return
    if not _fenced(after):
        failed('foreign_after', "a foreign token's ensure_writer was "
               'granted after the recorded owner re-armed — the claim '
               'passed to another token: '
               + json.dumps(after)[:300])

    # The preempt: granted unconditionally, naming the preemptor.
    took = record.get('preempt')
    if took is None:
        return
    if took.get('result') not in ('done', 'claimed_shared'):
        failed('preempt', 'the preempting claim_writer was refused: '
               + json.dumps(took)[:300]
               + ' — claim_writer preempts a standing dead owner '
               'unconditionally')
    else:
        standing = record.get('preempted')
        if not isinstance(standing, dict) \
                or _claim_owner(standing) != record['preempt_token']:
            failed('preempt standing', 'the standing claim after the '
                   'preempt names '
                   + str(_claim_owner(standing)) + ', not the '
                   'preemptor\'s token '
                   + str(record['preempt_token']) + ': '
                   + json.dumps(standing)[:300])

    # The restore: the field hands back to the launch owner's claim, so
    # the legs behind this one — and this leg's own second pass — find
    # the launch claim state they expect.
    if 'restored' not in record:
        return
    if not record['restored']:
        failed('restore', 'the restore did not return the field to the '
               'launch owner\'s claim under token '
               + str(record['owner_token']))


def _dead_owner_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    window = record.get('window') or []
    return {
        'induction': 'claimed' if clean('induction',
                                        'induction_write')
                     else 'refused',
        'fence': 'standing' if clean(*[key for key in violations
                                        if key.startswith('window')])
                 else 'opened',
        'rounds': 'fenced' if all(row.get('mutation') == 'fenced'
                                  and row.get('release') == 'done'
                                  for row in window) else 'broken',
        'foreign': 'refused' if clean('foreign', 'foreign_after')
                   else 'granted',
        'rearm': 'seated' if clean('rearm', 'rearm_write')
                 else 'refused',
        'preempt': 'taken' if clean('preempt', 'preempt standing')
                   else 'refused',
        'restore': 'restored' if clean('restore') else 'unrestored',
    }


def _dead_owner_self_check():
    """The judge replayed over planted negatives — every diagnostic it
    must be able to name. A name the judge cannot produce is a leg that
    can no longer catch what it declares; the check reports them so the
    -unchecked diagnostic fires."""
    def plant(key, result, detail):
        record = {'baseline': {'result': 'error',
                               'error': {'kind': 'fenced',
                                         'owner': 7}},
                  'claim': {'result': 'done'},
                  'induction_write': {'result': 'done'},
                  'window': [{'round': 0, 'answered': True,
                              'probe': 'fenced', 'named': 7,
                              'mutation': 'fenced', 'release': 'done',
                              'released_error': None}],
                  'foreign_ensure': {'result': 'error',
                                     'error': {'kind': 'fenced'}},
                  'rearm': {'result': 'claimed_shared'},
                  'rearm_write': {'result': 'done'},
                  'foreign_after': {'result': 'error',
                                    'error': {'kind': 'fenced'}},
                  'preempt': {'result': 'done'},
                  'preempted': {'result': 'error',
                                'error': {'kind': 'fenced',
                                          'owner': 9}},
                  'dead_token': 7, 'preempt_token': 9}
        record.update(key)
        violations = {}
        _judge_dead_owner(record, lambda k, d, t:
                          violations.setdefault(k, (d, t)))
        if not any(name == result for name, _ in violations.values()):
            return detail
        return None

    return [slipped for slipped in (
        plant({'window': [{'round': 0, 'answered': True,
                           'probe': 'unclaimed', 'named': None,
                           'mutation': 'unclaimed', 'release': 'done',
                           'released_error': None}]},
              DIAG_FAILED,
              'a non-holder release dissolving the dead-owner claim'),
        plant({'window': [{'round': 0, 'answered': True,
                           'probe': 'fenced', 'named': 7,
                           'mutation': 'fenced', 'release': 'done',
                           'released_error': None},
                          {'round': 1, 'answered': True,
                           'probe': 'fenced', 'named': 7,
                           'mutation': 'done', 'release': 'done',
                           'released_error': None}]},
              DIAG_FAILED,
              'the dead-owner claim not fencing a mutation probe'),
        plant({'foreign_ensure': {'result': 'done'}},
              DIAG_FAILED,
              'a foreign ensure granted inside the dead-owner window'),
        plant({'rearm': {'result': 'error',
                         'error': {'kind': 'fenced'}}},
              DIAG_FAILED,
              "the recorded owner's re-arm refused"),
        plant({'rearm_write': {'result': 'error',
                               'error': {'kind': 'fenced'}}},
              DIAG_FAILED,
              "the re-armed owner's write fenced"),
        plant({'foreign_after': {'result': 'done'}},
              DIAG_FAILED,
              'a foreign token granted after the re-arm'),
        plant({'preempt': {'result': 'error',
                           'error': {'kind': 'fenced'}}},
              DIAG_FAILED,
              'the preempting claim refused'),
        plant({'preempted': {'result': 'error',
                             'error': {'kind': 'fenced',
                                       'owner': 7}}},
              DIAG_FAILED,
              'the standing claim not naming the preemptor'),
        plant({'induction_write': {'result': 'error',
                                   'error': {'kind': 'fenced'}}},
              DIAG_FAILED,
              "the dead owner's induction write refused"),
        plant({'window': [{'round': 0, 'answered': False,
                           'probe': None, 'named': None,
                           'mutation': None, 'release': None,
                           'released_error': None}]},
              DIAG_NONDET,
              'a dropped claim-surface read on a window round'),
    ) if slipped]


def scenario_dead_owner_fencing(ctx):
    """Take the field's write claim from a dedicated attachment, close
    that attachment's connection without a release, and assert the
    dead-owner fencing contract on the deployed rig: across the
    server-side hold reap an attachment holding nothing repeats
    `release_writer` and its release answers `done` while the claim
    still stands naming the dead owner, mutation probes stay `fenced`,
    and a foreign token's `ensure_writer` is refused; then the recorded
    owner's token re-arms and writes, a foreign token stays fenced, a
    preempting `claim_writer` takes the field per the declared contract,
    and the restore returns the field to the launch owner's claim."""
    case = Case('dead-owner-fencing',
                "A dead owner's write claim keeps fencing the field",
                'with the pair settled on the launch owner\'s claim, a '
                'dedicated attachment claims and writes, then closes its '
                'connection without a release: across the server-side '
                'hold reap a second attachment holding nothing repeats '
                'release_writer — each answered done — while '
                'probe_writer keeps naming the dead owner\'s token, '
                'mutation probes stay fenced, and a foreign-token '
                'ensure_writer is refused; the recorded owner\'s token '
                'then re-arms and writes, the foreign token stays '
                'fenced, a preempting claim_writer takes the field '
                'naming its own token, and the restore hands the field '
                'back to the launch owner')
    if ctx.get('plant') is None:
        return case.finish('inconclusive', 'the run publishes no plant '
                           'endpoint the leg can attach to')
    if ctx.get('plant_ctl') is None:
        return case.finish('inconclusive', 'the run context carries no '
                           'plant_ctl seam for the shipped plant tool')
    tokens = ctx.get('plant_owner') or {}
    owner = wait_for(lambda: _pair_active(ctx),
                     time.monotonic() + DEAD_OWNER_SETTLE,
                     interval=DEAD_OWNER_POLL)
    if owner is None:
        reachable = any(
            _try_role(ctx, ctx[name]) is not None
            for name in ('active', 'standby') if ctx.get(name))
        return case.finish(
            'failed' if reachable else 'inconclusive',
            'no pair peer reports role=active' if reachable
            else 'the pair is unreachable')
    peer = 'standby' if owner == 'active' else 'active'
    if not tokens.get(owner):
        return case.finish('inconclusive', 'the run pins no plant-writer '
                           'owner token for ' + str(owner))
    owner_token = tokens[owner]
    case.observe('field owner: ' + owner + ' under token '
                 + str(owner_token) + '; dead-owner induction under '
                 + str(DEAD_OWNER_TOKEN))
    point = _dead_owner_field_point(ctx)
    if point is None:
        return case.finish('inconclusive', "the shipped tool's census "
                           'declares no writable field out-point for '
                           'the induction write')
    digests = []
    for number in (1, 2):
        violations = {}

        def note(key, diagnostic, detail):
            violations.setdefault(key, (diagnostic, detail))

        record, evidence = {}, {}
        try:
            _dead_owner_pass(ctx, owner_token, point, record, evidence)
        except Inconclusive as exc:
            ref = save_evidence(ctx['evidence_dir'],
                                'dead-owner-fencing-pass-'
                                + str(number) + '.json', evidence)
            case.evidence('file', ref, 'the dead-owner pass '
                          + str(number) + ' — the baseline, the '
                          'induction, the reap window, the re-arm, and '
                          'the preempt')
            return case.finish('inconclusive', str(exc))
        except Staged as exc:
            note('staged', DIAG_FAILED, str(exc))
        except Exception as exc:
            return case.finish('inconclusive', 'the claim surface '
                               'unreachable: ' + str(exc))
        evidence['violations'] = {key: diagnostic for key,
                                  (diagnostic, _)
                                  in violations.items()}
        _judge_dead_owner(record, note)
        digest = _dead_owner_digest(record, violations)
        evidence['digest'] = dict(digest)
        ref = save_evidence(ctx['evidence_dir'],
                            'dead-owner-fencing-pass-'
                            + str(number) + '.json', evidence)
        case.evidence('file', ref, 'the dead-owner pass '
                      + str(number) + ' — the baseline, the induction, '
                      'the reap window, the re-arm, and the preempt')
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
            'failed', DIAG_NONDET + ": the two passes' digests "
            'diverged: ' + json.dumps(digests[0], sort_keys=True)
            + ' vs ' + json.dumps(digests[1], sort_keys=True))
    case.observe('two dead-owner fencing passes, identical digests: '
                 + json.dumps(digests[0], sort_keys=True))

    # The launch state the legs behind this one find: the field back on
    # the launch owner's claim — each pass's own restore did it, and
    # the pair's roles never left their launch assignment because the
    # staging rode a field-tool claim the settled peers never contend
    # for.
    final = _dead_owner_claim(ctx)
    ref = save_evidence(ctx['evidence_dir'],
                        'dead-owner-fencing-restored.json',
                        {'claim': final, 'named': _claim_owner(final),
                         'owner_token': owner_token,
                         'roles': {name: _try_role(ctx, ctx[name])
                                   for name in ('active', 'standby')
                                   if ctx.get(name)}})
    case.evidence('file', ref, 'the restored field claim and the '
                  'pair\'s roles')
    if _claim_owner(final) != owner_token:
        return case.finish('failed', DIAG_FAILED + ': the restore did '
                           'not return the field to the launch owner\'s '
                           'claim — it stands under '
                           + str(_claim_owner(final)))
    if _pair_active(ctx) != owner:
        return case.finish('failed', DIAG_FAILED + ": the leg moved the "
                           "pair's roles off " + owner)
    case.observe('the field restored to ' + owner + '\'s claim under '
                 'token ' + str(owner_token) + '; roles unmoved')

    # The unchecked-diagnostic self-check: the judge replays each
    # planted negative it must name; a silent audit means the leg can
    # no longer catch what it names.
    slipped = _dead_owner_self_check()
    if slipped:
        return case.finish('failed', DIAG_UNCHECKED
                           + ": planted negatives slipped the leg's own "
                           'audits: ' + ', '.join(slipped))
    return case.finish('passed')