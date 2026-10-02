"""The attributed_switch_isolation acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages on the same settled window the monitor-starvation
# flood uses — the deployed pair in its launch roles, tracking and
# answering — and leaves that window as it found it: the driven peer
# it launches is torn down and the paused source thawed before the
# duty rotation behind it moves the pair's roles.
RUNS_AFTER = frozenset({'scenario_monitor_starvation'})
RUNS_BEFORE = frozenset({'scenario_duty_rotation'})


# --------------------------------------------------------------------
# The attributed-switch control-lane isolation contract — the
# per-revision lane evidence for the contract #1264's fix establishes
# (WW-FND-004's disposable-consumer quarantine extended to the
# actuation half; decision 83's schedule). The monitor's `role_change`
# routing documents promote/demote as control-plane actuation on a
# dedicated control lane, so an operator's switch — issued during an
# incident, exactly when consoles wedge — never queues behind serving
# workers pinned by undrained responses. The attributed
# `{"actor": …}` form carries a body, and before the fix every request
# with `body_length > 0` was quarantined on the submission lane beside
# `POST /scan`: an operator's *declared-identity* switch queued behind
# severed scan batches (~40 s behind two `scans: 40` batches on a
# driven peer whose source had gone silent) and stalled outright
# behind raw-socket clients that declare a `Content-Length` and never
# deliver it (both submit workers pinned, no read timeout to bound
# them). The attributed body is tens of bytes — always inside
# tiny_http's eager-read bound — so it arrives already buffered and
# rides the control lane beside the bare request.
#
# The leg stages the two congestion shapes the fix names, in that
# order, on the lane's own driven standby: `start_driven` launches the
# run's third monitor `--standby <field owner> --driven`, so every
# checkpoint pull it ever performs happens inside a `POST /scan`
# request, and `pause_controller` on the owner freezes that source in
# place — the listener completes the handshake and nothing ever
# answers, so each scan of a batch waits the documented
# CHECKPOINT_PULL_TIMEOUT out and the batch occupies its submission
# worker for its whole span. The first shape is the holding set: two
# raw sockets (the lane's existing socket helper) send a request head
# declaring a body past the eager-read bound plus a partial body and
# then hold, pinning both submission workers for as long as the leg
# keeps them — the congestion that never drains. The second is the
# reproduction's own: two severed `POST /scan` batches sized to
# occupy both submit workers, sent whole and never read.
#
# Under each shape the leg proves the pin is real — a one-scan
# submission that must not answer inside the declared probe bound —
# then asserts the contract through the peer whose lane is congested:
# an attributed `POST /demote` and an attributed `POST /promote` each
# answer inside the declared bound with the verdict the peer's own
# reported posture earns (`not_active` from a non-owner,
# `not_converged` from a standby whose final-sync pull just failed
# against the silent source, the applied transition where the peer can
# perform the switch), never a lane-timeout silence; and the bare
# control lanes the fix keeps beside it — `GET /health`, `GET /role`,
# and a bodiless `POST /demote` — stay at baseline throughout. The
# congestion is then released: the holding set is closed and the
# driven peer's submission lane must serve a scan again, and the pair
# must reconverge with its launch roles restored. Two consecutive
# passes must produce identical outcome digests.
#
# The holding shape runs first and is the contract's presence gate.
# #1264's fix is a behavioural routing guard with no new served field
# and no new durable record, so the leg's pre-contract signature is
# behavioural too, and it is positive rather than a mere lateness: an
# attributed switch that *shared* the submission lane's wait — its
# answer landing only once the staged congestion drained, or never
# while it stood, while the bare control lanes beside it answered at
# baseline — is a monitored revision that routes bodied switches onto
# the submission lane, the routing the fix replaced, and reports
# inconclusive. Every released and staged build predates the contract
# until the fix lands, so that is the honest verdict for them; a
# revision whose holding shape answered in isolation and whose batch
# shape then queued or timed out is not that signature — the contract
# was demonstrated present on that very monitor — and the leg names it
# as the contract failure it is.
#
# Named diagnostics: attributed-switch-isolation-failed tags the
# contract clauses — an attributed switch that queued behind or timed
# out behind the staged congestion on a rig whose other shape proved
# the control-lane routing, an attributed answer carrying a verdict
# the peer's reported posture does not earn, a bare control lane that
# stopped answering at baseline, a submission lane that never drained
# after the release, or a pair that never reconverged with its launch
# roles restored — and attributed-switch-isolation-nondeterministic
# tags the instability the contract does not answer for: a refused
# staging call, a congestion shape that never stood or never proved
# its pin, a starved baseline sample, a driven peer that converged
# where the leg refuses to move the field, a deployed peer's role
# moving under the window, or two passes whose digests diverge. The
# unchecked-diagnostic self-check replays the judge over planted
# negatives — isolation asserted held while an attributed switch sits
# queued behind the staged batches or times out behind them, an
# attributed answer carrying the wrong verdict, a baseline lane
# starved, an unreleased lane, an unrestored pair, and the instability
# shapes — and reports attributed-switch-isolation-unchecked for any
# that slip through.

ISOLATION_ACTOR = 'qa-lane-attributed'   # the declared identity the
                                         # attributed actuations carry
SUBMIT_WORKERS = 2        # the monitor's submission-lane width — the
                          # saturating set each congestion shape
                          # occupies whole, one pinning client or one
                          # severed batch per worker
BATCH_SCANS = 8           # scans per severed batch: each scan's pull
                          # waits the documented one-second
                          # CHECKPOINT_PULL_TIMEOUT out against the
                          # paused source, so a batch holds its
                          # submission worker for ~8 s — far past the
                          # declared switch bound, far inside the
                          # armed failover budget the pause spends
SWITCH_BOUND = 4.0        # the declared bound an attributed switch
                          # owes under the congestion: the fix's own
                          # reproduction bound (its regression test
                          # answers inside three seconds) plus the one
                          # bounded network wait a promotion's
                          # final-sync fetch against the silent source
                          # carries
BASELINE_BOUND = 4.0      # the bare control lanes' baseline bound —
                          # lock-free reads and a bodiless actuation,
                          # never queued behind the congestion
PIN_PROBE_BOUND = 1.5     # a one-scan submission issued while the
                          # congestion stands must NOT answer inside
                          # this — the pin's own proof
PIN_SETTLE = 0.6          # the margin the pinning clients get to
                          # occupy their workers before the shape
                          # probes
HOLDER_HOLD = 6.0         # how long the holding set stands past the
                          # pin's landing — several bounds, so an
                          # attributed answer inside it answered
                          # *through* the congestion, not beside it
HOLDER_LENGTH = 2048      # the holding clients' declared
                          # Content-Length — past tiny_http's eager
                          # -read bound, so the body stays a live
                          # socket reader and its worker waits on the
                          # client with no read timeout to bound it
HOLDER_PREFIX = b'{"scans":1}'   # the partial body they deliver and
                                # never complete
TRAILING_SCANS = 3        # scans an attributed answer may trail the
                          # run tick by on the served surface and
                          # still count as isolated — the queue-order
                          # slack inside the batches' own span
RELEASE_BOUND = 30        # bound on the submission lane draining once
                          # the congestion is released: the batches'
                          # own bounded remainder plus the probe
                          # requests the pin proofs queued behind them
SETTLE = 30               # bound on the pair settling, the driven
                          # peer answering, and the restore wait
ISOLATION_POLL = 0.4      # the cadence watching roles and ticks
DIAG_FAILED = 'attributed-switch-isolation-failed'
DIAG_NONDET = 'attributed-switch-isolation-nondeterministic'
DIAG_UNCHECKED = 'attributed-switch-isolation-unchecked'

# The two actuations the incident-time guarantee is about, in the
# order the leg submits them under each congestion shape.
ISOLATION_PATHS = ('/demote', '/promote')

# The bare control-lane probes the fix keeps answering beside the
# congestion — two pair-liveness reads and the bodiless actuation.
BASELINE_PATHS = ('/health', '/role', '/demote')

# The judge's gate clause: an attributed switch that shared the
# submission lane's wait under the holding set. The scenario
# reinterprets it as the pre-contract routing signature (inconclusive
# — every staged build predates the contract until the fix lands);
# the judge's own digest and self-check still name it.
PRECONTRACT_KEY = 'gate-isolation'


def _stop_driven(ctx):
    """Remove the run's driven standby again — the launch
    configuration every leg behind this one expects. Returns the
    teardown failure as text, or None: a removal that never completed
    is left to the run's own container reconcile."""
    stop = ctx.get('stop_driven')
    if stop is None:
        return 'the run context carries no stop_driven action'
    try:
        stop()
    except Exception as exc:
        return str(exc)[:160]
    return None


def _sync_name(report):
    """The named convergence a `/role` report carries — `tracking`,
    `unsynchronized`, `degraded`, … or `unknown` when it serves
    neither."""
    sync = (report or {}).get('sync')
    if isinstance(sync, dict) and sync:
        return str(next(iter(sync)))
    return str(sync) if sync is not None else 'unknown'


def _verdict(status, body):
    """One actuation answer's normalized form: the applied transition's
    role for a 200, the named refusal for a 409 — `not_active`,
    `not_converged`, `already_active`, `no_tracking_source` — and the
    raw status for anything else."""
    if status == 200 and isinstance(body, dict):
        return 'applied:' + str(body.get('role'))
    if status == 409 and isinstance(body, dict) and len(body) == 1:
        return str(next(iter(body)))
    return 'status:' + str(status)


def _acceptable(path, posture, verdict):
    """Whether `verdict` is the semantically correct served answer to
    `path` for a peer that reported `posture` — the named refusal that
    peer's own state earns, or the applied transition it can perform.
    A lane that answered the right question with the wrong verdict is
    not the contract holding."""
    role = posture.get('role')
    sync = posture.get('sync')
    if path == '/demote':
        if role in ('standby', 'demoting'):
            return verdict in ('not_active', 'applied:demoting',
                               'applied:standby')
        return verdict in ('applied:demoting', 'applied:standby')
    if role in ('active', 'promoting'):
        return verdict == 'already_active'
    if isinstance(sync, dict) and 'tracking' in sync:
        return verdict in ('applied:promoting', 'applied:active')
    return verdict == 'not_converged'


def _act(ctx, base, path, actor=None, timeout=None):
    """One control-plane actuation on a monitor base — attributed when
    `actor` is declared, bare otherwise. Returns the timed record the
    judge reads: the served posture the correct verdict follows from,
    the run tick at submission, the status and normalized verdict, the
    elapsed seconds, whether the answer came inside the bound, whether
    it was the semantically correct one, and the scans the answer
    trailed the run tick by — the queue-order evidence that separates
    an answer that rode the control lane from one that waited the
    submission lane's congestion out."""
    timeout = SWITCH_BOUND if timeout is None else timeout
    posture = _try_role(ctx, base)
    before = (posture or {}).get('tick')
    body = {'actor': actor} if actor is not None else None
    started = time.monotonic()
    status, answer, error = None, None, None
    try:
        status, answer = http_json('POST', base + path, body,
                                   timeout=timeout)
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            answer = json.loads(exc.read() or b'null')
        except ValueError:
            answer = None
        finally:
            exc.close()
    except Exception as exc:
        error = str(exc)[:160]
    elapsed = time.monotonic() - started
    after = (_try_role(ctx, base) or {}).get('tick')
    verdict = _verdict(status, answer)
    ticks = [tick for tick in (before, after)
             if isinstance(tick, int) and not isinstance(tick, bool)]
    return {'path': path, 'actor': actor, 'status': status,
            'verdict': verdict, 'elapsed': round(elapsed, 3),
            'answered': error is None and status is not None,
            'bounded': elapsed <= timeout, 'error': error,
            'correct': _acceptable(path, posture or {}, verdict),
            'posture': {'role': (posture or {}).get('role'),
                        'sync': _sync_name(posture)},
            'ticks_behind': after - before
            if len(ticks) == 2 else None}


def _sample(call, bound):
    """Run one baseline read under its own timeout:
    `{'answered', 'bounded', 'elapsed', 'error'}` — a dropped read is
    one lost sample, never a raised leg."""
    started = time.monotonic()
    try:
        call()
    except Exception as exc:
        return {'answered': False, 'bounded': False,
                'elapsed': round(time.monotonic() - started, 3),
                'error': str(exc)[:160]}
    elapsed = time.monotonic() - started
    return {'answered': True, 'bounded': elapsed <= bound,
            'elapsed': round(elapsed, 3), 'error': None}


def _held_client(base):
    """One holding client against a monitor base: a request head
    declaring a body past the eager-read bound, a partial body, and
    then silence — the submission worker waits on this client for as
    long as it cares to, with no read timeout to bound it. Returns the
    open socket."""
    stream = _connect(base)
    stream.sendall(b'POST /scan HTTP/1.1\r\nHost: qa-lane\r\n'
                   b'Content-Type: application/json\r\nContent-Length: '
                   + str(HOLDER_LENGTH).encode() + b'\r\n\r\n'
                   + HOLDER_PREFIX)
    return stream


def _severed_batch(base, scans):
    """One severed `POST /scan` batch against a monitor base: the head
    and the whole body go out, the answer is never read and the
    connection is held — the reproduction's pinning client, whose batch
    occupies its submission worker for the batch's whole span."""
    payload = json.dumps({'scans': scans}).encode()
    stream = _connect(base)
    stream.sendall(b'POST /scan HTTP/1.1\r\nHost: qa-lane\r\n'
                   b'Content-Type: application/json\r\nContent-Length: '
                   + str(len(payload)).encode() + b'\r\n\r\n' + payload)
    return stream


def _pinned(base, bound=None):
    """Whether the submission lane is pinned: a one-scan submission
    issued now must not answer inside the probe bound. Returns
    (pinned, error) — an answered probe is the lane still serving."""
    bound = PIN_PROBE_BOUND if bound is None else bound
    try:
        _request_status('POST', base + '/scan', {'scans': 1},
                        timeout=bound)
    except Exception as exc:
        return True, str(exc)[:160]
    return False, None


def _drained(base, bound=None):
    """One scan answered inside the release bound — the submission
    lane serving again."""
    bound = RELEASE_BOUND if bound is None else bound
    return _sample(lambda: _request_status('POST', base + '/scan',
                                           {'scans': 1}, timeout=bound),
                   bound)


def _isolated(act, trailing):
    """The isolation verdict one attributed answer earns: it answered,
    inside the declared bound, with the verdict the peer's reported
    posture earns, and — where the shape's queue order can show it —
    without trailing the run tick far enough to say it waited the
    staged congestion out."""
    if not act.get('answered') or not act.get('bounded'):
        return False
    if not act.get('correct'):
        return False
    behind = act.get('ticks_behind')
    if isinstance(trailing, int) and isinstance(behind, int) \
            and behind > trailing:
        return False
    return True


def _baseline(ctx, base, path):
    """One bare control-lane sample under the congestion: `GET
    /health` and `GET /role` read the published mirror, the bodiless
    `POST /demote` rides the control lane the attributed form now
    shares. Every sample carries the same shape the attributed records
    do, so the judge's baseline clauses read one vocabulary."""
    if path == '/demote':
        act = _act(ctx, base, '/demote', timeout=BASELINE_BOUND)
        return {'answered': act['answered'], 'bounded': act['bounded'],
                'elapsed': act['elapsed'], 'error': act['error'],
                'verdict': act['verdict']}
    return _sample(lambda: http_json('GET', base + path,
                                     timeout=BASELINE_BOUND),
                   BASELINE_BOUND)


def _shape_capture(ctx, base, entry):
    """Sample one congestion shape's contract through the congested
    peer: the bare control lanes at baseline, then the two attributed
    actuations, each timed and correlated against the run tick it
    trailed. Fills `entry` in place."""
    entry['baseline'] = {path: _baseline(ctx, base, path)
                         for path in BASELINE_PATHS}
    entry['switches'] = {path: _act(ctx, base, path,
                                    actor=ISOLATION_ACTOR)
                         for path in ISOLATION_PATHS}


def _judge_pass(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the isolation
    contract's clauses and DIAG_NONDET the instability the contract
    does not answer for. An aborted shape ends the audit where the
    pass ended — the keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    shapes = record.get('shapes') or {}
    if not shapes:
        nondet('staged', 'the pass staged no congestion shape at all '
               '— the contract has no lane to read')
        return
    for shape in ('holders', 'batches'):
        entry = shapes.get(shape)
        if not entry:
            nondet('staged-' + shape, 'the ' + shape
                   + ' congestion shape never stood — the isolation '
                     'it owes was never exercised')
            continue
        if not entry.get('opened'):
            nondet('opened-' + shape, "the " + shape
                   + " shape's pinning clients never opened: "
                   + str(entry.get('open_error'))[:160])
            continue
        if not entry.get('pinned'):
            nondet('pin-' + shape, 'a one-scan submission answered '
                   'inside the ' + str(PIN_PROBE_BOUND) + 's probe '
                   'bound while the ' + shape + ' congestion stood — '
                   'the submission lane was never pinned, so nothing '
                   'was ever queued behind it')
        prefix = 'gate' if shape == 'holders' else 'batch'
        for path in ISOLATION_PATHS:
            act = (entry.get('switches') or {}).get(path)
            key = prefix + '-' + path
            if not act:
                nondet(key, 'the attributed POST ' + path
                       + ' was never submitted under the ' + shape
                       + ' congestion')
                continue
            if path == '/promote' \
                    and act.get('posture', {}).get('sync') \
                    == 'tracking':
                nondet(key, 'the driven peer reported tracking before '
                       'the attributed POST /promote — a promotion '
                       'there would apply and move the field, and the '
                       'leg refuses to')
                continue
            if _isolated(act, entry.get('trailing')):
                continue
            behind = act.get('ticks_behind')
            waited = True
            if not act.get('answered'):
                detail = ('the attributed POST ' + path + ' never '
                          'answered inside the declared '
                          + str(SWITCH_BOUND) + 's bound while the '
                          + shape + ' congestion stood'
                          + (' — its answer landed only once the '
                             'congestion drained'
                             if act.get('released_late') else '')
                          + ': the submission lane held its worker')
            elif not act.get('correct'):
                waited = False
                detail = ('the attributed POST ' + path + ' answered '
                          + str(act.get('verdict'))
                          + ' on a peer reporting role '
                          + str(act.get('posture', {}).get('role'))
                          + '/' + str(act.get('posture', {}).get('sync'))
                          + ' — not the verdict that posture earns')
            elif not act.get('bounded'):
                detail = ('the attributed POST ' + path + ' answered '
                          + format(act.get('elapsed') or 0.0, '.2f')
                          + 's past the declared ' + str(SWITCH_BOUND)
                          + 's bound under the ' + shape
                          + ' congestion')
            else:
                detail = ('the attributed POST ' + path + ' answered '
                          'only after the ' + shape + ' congestion '
                          'drained — its answer trailed the run tick by '
                          + str(behind) + ' scans, the submission '
                          "lane's own wait")
            failed(key, detail)
            if shape == 'holders' and waited:
                # The wait the fix removed, positively observed: the
                # switch answered — late, trailing the congestion's own
                # scans, or not at all until it drained — where the
                # routing a pre-contract monitor still gives a bodied
                # switch queues it on the submission lane. An answer
                # that came back with the wrong verdict is the contract
                # failing instead, and names no pre-contract signature.
                note(PRECONTRACT_KEY, DIAG_FAILED, detail)
        for path in BASELINE_PATHS:
            sample = (entry.get('baseline') or {}).get(path)
            key = 'baseline-' + shape + '-' + path
            if not sample:
                nondet(key, 'the bare control lane ' + path
                       + ' was never sampled under the ' + shape
                       + ' congestion')
                continue
            if not sample.get('answered'):
                failed(key, 'the bare control lane ' + path
                       + ' never answered under the ' + shape
                       + ' congestion — the control lanes the fix '
                         'keeps beside the submission quarantine '
                         'stalled with it')
            elif not sample.get('bounded'):
                failed(key, 'the bare control lane ' + path
                       + ' answered ' + format(
                           sample.get('elapsed') or 0.0, '.2f')
                       + 's past its ' + str(BASELINE_BOUND)
                       + 's baseline bound under the ' + shape
                       + ' congestion')
        if shape == 'holders' and not entry.get('held_long_enough'):
            nondet('held-' + shape, 'the holding set stood for '
                   + str(entry.get('held_for')) + 's, short of the '
                   'declared ' + str(HOLDER_HOLD)
                   + 's — an attributed answer beside it proves '
                     'nothing about the congestion it stood through')
    recovery = record.get('recovery')
    if not recovery:
        nondet('recovery', 'the pass never released its congestion or '
               'audited the restore — the lane-recovery and '
               'reconvergence clauses were never exercised')
        return
    if not recovery.get('drained'):
        failed('released', 'the driven peer\'s submission lane never '
               'served a scan again after the congestion drained'
               + (': ' + str(recovery.get('drain_error'))[:200]
                  if recovery.get('drain_error') else ''))
    if recovery.get('moved'):
        nondet('roles-moved', 'a deployed peer changed role under the '
               'window (' + json.dumps(recovery.get('roles'))
               + ') — the armed failover budget fired while the '
                 'source was paused')
    if not recovery.get('reconverged'):
        failed('reconverged', 'the pair did not reconverge with its '
               'launch roles restored after the source was thawed: '
               + json.dumps(recovery.get('pair'))[:300])


def _digest_isolation(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is its clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    return {
        'gate': 'isolated' if clean('gate-/demote', 'gate-/promote',
                                     PRECONTRACT_KEY) else 'queued',
        'batches': 'isolated' if clean('batch-/demote',
                                       'batch-/promote') else 'queued',
        'baseline': 'held' if clean(*[
            'baseline-' + shape + '-' + path
            for shape in ('holders', 'batches')
            for path in BASELINE_PATHS]) else 'broke',
        'released': 'drained' if clean('released') else 'held',
        'reconverged': 'reconverged' if clean('reconverged') \
            else 'unreconverged',
        'roles': 'moved' if not clean('roles-moved') else 'restored',
    }


def _clean_record():
    """The record both self-check negatives start from: both shapes
    staged and pinned, both attributed actuations isolated with the
    verdict their reported posture earns, every bare control lane at
    baseline, the lane drained after the release, and the pair
    reconverged."""
    def switch(path, verdict):
        return {'path': path, 'actor': ISOLATION_ACTOR, 'status': 409,
                'verdict': verdict, 'elapsed': 0.4, 'answered': True,
                'bounded': True, 'error': None, 'correct': True,
                'posture': {'role': 'standby', 'sync': 'degraded'},
                'ticks_behind': 1}

    def shape(trailing):
        return {'opened': True, 'open_error': None, 'pinned': True,
                'pin_error': 'timed out', 'held_for': HOLDER_HOLD,
                'held_long_enough': True, 'trailing': trailing,
                'switches': {'/demote': switch('/demote', 'not_active'),
                             '/promote': switch('/promote',
                                                'not_converged')},
                'baseline': {path: {'answered': True, 'bounded': True,
                                    'elapsed': 0.1, 'error': None,
                                    'verdict': 'not_active'
                                    if path == '/demote' else None}
                             for path in BASELINE_PATHS}}

    return {'pass': 1, 'owner': 'active', 'peer': 'standby',
            'source': 'paused',
            'shapes': {'holders': shape(TRAILING_SCANS),
                       'batches': shape(BATCH_SCANS - TRAILING_SCANS)},
            'recovery': {'drained': True, 'drain_error': None,
                         'moved': False,
                         'roles': {'active': 'active',
                                   'standby': 'standby'},
                         'reconverged': True,
                         'pair': {'active': 'active',
                                  'standby': 'standby'}}}


def _self_check_isolation():
    """The leg's unchecked-diagnostic self-test: replay the isolation
    judge over each planted negative the issue names — isolation
    asserted held while an attributed switch sits queued behind the
    staged batches or never answers behind them, the same shared-lane
    wait under the holding set, an attributed answer carrying a
    verdict the peer's posture does not earn, a bare control lane
    starved at baseline, a lane that never drained after the release,
    a pair that never reconverged, and the instability shapes — and
    require the judge to note each. A silent judge returns the
    negative names it let through."""
    slipped = []

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = _clean_record()
        mutate(record)
        found = {}
        _judge_pass(record,
                    lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    def shape_of(record, shape):
        return record['shapes'][shape]

    # The doctored negative the issue names first: isolation asserted
    # held while the attributed demote sits queued behind the staged
    # batches — its answer trails the batch's own scans.
    expect('attributed-queued-behind-batches', lambda record:
           shape_of(record, 'batches')['switches']['/demote'].update(
               {'elapsed': BATCH_SCANS + 2.0, 'bounded': False,
                'ticks_behind': BATCH_SCANS}))
    # ... and while it never answered at all under them.
    expect('attributed-silent-behind-batches', lambda record:
           shape_of(record, 'batches')['switches']['/promote'].update(
               {'answered': False, 'status': None,
                'verdict': 'status:None', 'bounded': False,
                'correct': False, 'error': 'timed out',
                'elapsed': SWITCH_BOUND + 0.1, 'ticks_behind': None}))
    # The same shared-lane wait under the holding set: the pre-contract
    # routing the gate names, which the scenario reports inconclusive
    # on a real rig.
    expect('attributed-shared-lane-wait', lambda record:
           shape_of(record, 'holders')['switches']['/demote'].update(
               {'answered': False, 'status': None,
                'verdict': 'status:None', 'bounded': False,
                'correct': False, 'error': 'timed out',
                'elapsed': SWITCH_BOUND + 0.1, 'ticks_behind': None,
                'released_late': True}))
    # An attributed answer carrying the wrong verdict: the lane
    # answered, the switch it answered was not the one asked.
    expect('attributed-wrong-verdict', lambda record:
           shape_of(record, 'batches')['switches']['/promote'].update(
               {'verdict': 'applied:active', 'status': 200,
                'correct': False}))
    # A bare control lane that stopped answering at baseline ...
    expect('baseline-lane-starved', lambda record:
           shape_of(record, 'batches')['baseline']['/role'].update(
               {'answered': False, 'bounded': False,
                'error': 'timed out'}))
    # ... and one that answered only past its baseline bound.
    expect('baseline-lane-late', lambda record:
           shape_of(record, 'holders')['baseline']['/health'].update(
               {'elapsed': BASELINE_BOUND + 3.0, 'bounded': False}))
    # The submission lane never freed after the release.
    expect('lane-never-drained', lambda record:
           record['recovery'].update({'drained': False,
                                      'drain_error': 'timed out'}))
    # The pair never reconverged with its launch roles restored.
    expect('pair-never-reconverged', lambda record:
           record['recovery'].update(
               {'reconverged': False,
                'pair': {'active': 'active', 'standby': 'degraded'}}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a shape that never stood, a pin
    # that never proved, a starved baseline sample, a driven peer that
    # converged where the leg refuses to move the field, and a missing
    # recovery audit.
    expect('shape-never-stood', lambda record:
           record['shapes'].update({'holders': None}), DIAG_NONDET)
    expect('pin-never-proved', lambda record:
           shape_of(record, 'batches').update({'pinned': False}),
           DIAG_NONDET)
    expect('baseline-sample-dropped', lambda record:
           shape_of(record, 'batches')['baseline'].update(
               {'/demote': None}), DIAG_NONDET)
    expect('driven-peer-converged', lambda record:
           shape_of(record, 'batches')['switches']['/promote'][
               'posture'].update({'sync': 'tracking'}), DIAG_NONDET)
    expect('recovery-never-audited', lambda record:
           record.update({'recovery': None}), DIAG_NONDET)
    return slipped


def _isolation_pass(ctx, number, owner, peer):
    """One isolation pass: with the deployed pair settled in its launch
    roles and the driven standby answering, freeze the owner's source
    so every pull the driven peer makes waits its documented bound,
    congest its submission lane with the holding set and prove the
    contract's presence there, release that set, stage the
    reproduction's two severed scan batches and assert the contract
    under them, wait for the lane to drain, thaw the source, and audit
    the restore. Returns the record the judge replays; an aborted
    stage simply leaves its later keys absent for the judge to name."""
    base = ctx['driven']
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'shapes': {}, 'recovery': {}}
    clients, paused = [], False
    try:
        try:
            ctx['pause_controller'](owner)
            paused = True
            record['source'] = 'paused'
        except Exception as exc:
            record['source'] = 'pause refused: ' + str(exc)[:160]
            return record

        # ---- the holding set: the contract's presence gate ----
        entry = {'opened': False, 'open_error': None, 'pinned': False,
                 'trailing': TRAILING_SCANS, 'held_for': 0,
                 'held_long_enough': False}
        record['shapes']['holders'] = entry
        held = []
        try:
            for _ in range(SUBMIT_WORKERS):
                held.append(_held_client(base))
            entry['opened'] = True
        except OSError as exc:
            entry['open_error'] = str(exc)
        if entry['opened']:
            time.sleep(PIN_SETTLE)
            entry['pinned'], entry['pin_error'] = _pinned(base)
            landed = time.monotonic()
            _shape_capture(ctx, base, entry)
            # The set stands well past the declared bound: an
            # attributed answer inside it answered *through* the
            # congestion, not beside it.
            remaining = HOLDER_HOLD - (time.monotonic() - landed)
            if remaining > 0:
                time.sleep(remaining)
            entry['held_for'] = round(time.monotonic() - landed, 2)
            entry['held_long_enough'] = entry['held_for'] >= HOLDER_HOLD
        for stream in held:
            stream.close()
        # A switch that never answered while the set stood may still
        # answer once it drained: that answer is the pre-contract
        # routing's own evidence — it waited the lane's wait out.
        for path in ISOLATION_PATHS:
            act = (entry.get('switches') or {}).get(path)
            if act is None or act.get('answered'):
                continue
            retry = _act(ctx, base, path, actor=ISOLATION_ACTOR)
            act['released_late'] = retry.get('answered')
            act['retry_verdict'] = retry.get('verdict')

        # ---- the reproduction's severed batches ----
        entry = {'opened': False, 'open_error': None, 'pinned': False,
                 'trailing': BATCH_SCANS - TRAILING_SCANS}
        record['shapes']['batches'] = entry
        try:
            for _ in range(SUBMIT_WORKERS):
                clients.append(_severed_batch(base, BATCH_SCANS))
            entry['opened'] = True
        except OSError as exc:
            entry['open_error'] = str(exc)
        if entry['opened']:
            time.sleep(PIN_SETTLE)
            entry['pinned'], entry['pin_error'] = _pinned(base)
            _shape_capture(ctx, base, entry)
        # The batches are severed: their answers are never read and
        # their connections are held until the lane has drained, the
        # shape the reproduction staged.
        def served_again():
            sample = _drained(base)
            return sample if sample.get('answered') else None

        drained = wait_for(served_again, time.monotonic() + RELEASE_BOUND,
                           interval=ISOLATION_POLL)
        for stream in clients:
            stream.close()
        clients = []
        record['recovery']['drained'] = bool(drained)
        record['recovery']['drain_error'] = None if drained \
            else 'no scan answered inside the ' + str(RELEASE_BOUND) \
            + 's release bound'

        # ---- thaw and restore ----
        if paused:
            try:
                ctx['unpause_controller'](owner)
                paused = False
            except Exception as exc:
                record['recovery']['drain_error'] = (
                    str(record['recovery']['drain_error'] or '')
                    + '; the source thaw failed: ' + str(exc)[:160])
        roles = {name: (_try_role(ctx, ctx[name]) or {}).get('role')
                for name in (owner, peer)}
        record['recovery']['roles'] = roles
        record['recovery']['moved'] = roles.get(owner) != 'active' \
            or roles.get(peer) != 'standby'

        def restored():
            current = {name: (_try_role(ctx, ctx[name]) or {}).get(
                'role') for name in (owner, peer)}
            if current.get(owner) != 'active' \
                    or current.get(peer) != 'standby':
                return None
            if 'tracking' not in ((_try_role(ctx, ctx[peer]) or {})
                                  .get('sync') or {}):
                return None
            return current

        record['recovery']['pair'] = wait_for(
            restored, time.monotonic() + SETTLE, interval=ISOLATION_POLL)
        record['recovery']['reconverged'] = record['recovery']['pair'] \
            is not None
    finally:
        for stream in clients:
            stream.close()
        if paused:
            try:
                ctx['unpause_controller'](owner)
            except Exception:
                pass
    return record


def scenario_attributed_switch_isolation(ctx):
    """An actor-attributed promote or demote answers inside the
    control lane's declared bound while the submission lane is
    congested by severed scan batches or by clients holding undelivered
    bodies, and releasing the congestion leaves the pair reconverged
    with its launch roles."""
    case = Case(
        'attributed-switch-isolation',
        'Attributed role switches keep the control lane under '
        'submission congestion',
        'with the deployed pair settled and tracking, the run\'s '
        'driven standby staged on a silently paused source, and its '
        'submission lane congested in turn by two raw-socket clients '
        'declaring a body they never deliver and by two severed '
        'POST /scan batches sized to occupy both submit workers — each '
        'congestion standing while a one-scan submission stays '
        'unanswered inside the declared probe bound: an attributed '
        'POST /demote and an attributed POST /promote carrying '
        '{"actor": "qa-lane-attributed"} each answer inside the '
        'declared ' + str(SWITCH_BOUND) + 's bound with the verdict '
        'the driven peer\'s reported posture earns — not_active from '
        'the non-owner, not_converged from the standby whose '
        'final-sync pull just failed against the silent source — '
        'never a lane-timeout silence, while GET /health, GET /role, '
        'and the bodiless POST /demote stay at baseline throughout; '
        'releasing the congestion lets the submission lane serve a '
        'scan again and leaves the pair reconverged with its launch '
        'roles restored, two consecutive passes producing identical '
        'digests')
    try:
        seams = ('start_driven', 'stop_driven', 'pause_controller',
                 'unpause_controller')
        missing = [seam for seam in seams if ctx.get(seam) is None]
        if missing:
            return case.finish(
                'inconclusive', 'the run context carries no '
                + ', '.join(missing) + ' action — the driven-peer '
                'launch and the frozen-source induction have no '
                'documented seam')
        driven = ctx.get('driven')
        if driven is None:
            return case.finish(
                'inconclusive', 'the run context carries no driven '
                'monitor endpoint — the congestion the contract is '
                'about has no lane to stand on')
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + SETTLE,
                         interval=ISOLATION_POLL)
        if owner is None:
            return case.finish('failed',
                               'no launched peer reports role=active')
        if owner not in ('active', 'standby'):
            return case.finish(
                'inconclusive', 'the field writer is ' + str(owner)
                + ' — outside the launched pair the pause action '
                  'names')
        peer = 'standby' if owner == 'active' else 'active'
        if _tracking_peer(ctx, owner) != peer:
            return case.finish(
                'inconclusive', 'the pair is not converged — no peer '
                'reports tracking behind ' + str(owner) + ', so the '
                'driven standby has no proven source to share')
        case.observe('pair under audit: active=' + str(owner)
                     + ' standby=' + str(peer) + ' miss budget '
                     + str(ctx.get('failover_misses')))

        # The driven standby: the run's third monitor, --standby on the
        # owner and --driven, so every checkpoint pull it performs
        # happens inside a POST /scan request — the per-request pull
        # chain a batch occupies a submission worker for.
        try:
            launched = ctx['start_driven'](owner)
        except Exception as exc:
            return case.finish(
                'inconclusive', 'the driven-peer launch never '
                'completed: ' + str(exc)[:300])
        case.observe('driven standby up: '
                     + str((launched or {}).get('container')))
        if wait_for(lambda: _try_role(ctx, driven),
                    time.monotonic() + SETTLE,
                    interval=ISOLATION_POLL) is None:
            _stop_driven(ctx)
            return case.finish(
                'inconclusive', 'the driven monitor never answered '
                '/role after its launch')
        digests = []
        try:
            for number in (1, 2):
                record = _isolation_pass(ctx, number, owner, peer)
                violations = {}
                _judge_pass(
                    record,
                    lambda key, diagnostic, detail:
                    violations.setdefault(key, (diagnostic, detail)))
                record['violations'] = {key: name for key, (name, _)
                                        in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'attributed-switch-isolation-pass-' + str(number)
                    + '.json', record)
                case.evidence('file', ref, 'isolation pass '
                              + str(number) + ' — both congestion '
                              'shapes\' pins, baseline samples and '
                              'attributed actuations, the release, '
                              'the restore, and the named violations')
                # The pre-contract signature: an attributed switch that
                # shared the submission lane's own wait under the
                # holding set. Every released and staged build
                # predates the contract until the fix lands, and that
                # observation is a revision predating it — never the
                # contract failing.
                predate = next((detail for key, (_name, detail)
                                in violations.items()
                                if key == PRECONTRACT_KEY), None)
                if predate is not None:
                    return case.finish(
                        'inconclusive', 'the monitored revision '
                        'predates the attributed-switch control-lane '
                        'contract: ' + predate)
                if violations:
                    name = DIAG_FAILED if any(
                        diagnostic == DIAG_FAILED
                        for diagnostic, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', name + ': ' + '; '.join(
                            detail for _, detail
                            in list(violations.values())[:4]))
                digests.append(_digest_isolation(violations))
        finally:
            # The launch configuration for the legs behind this one:
            # the source thawed and the driven peer removed whatever
            # the passes did.
            try:
                ctx['unpause_controller'](owner)
            except Exception:
                pass
            case.observe('driven standby torn down: '
                         + str(_stop_driven(ctx) or 'removed'))
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ": the two passes' digests "
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two isolation passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the isolation judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _self_check_isolation()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               "leg's own audits: "
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
