"""The writable-field-point settlement leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

from qa_lane import rig_model

# Ordering: the leg stages the lane's own scratch field and one born
# seat on a derived document, the same staging the born-active legs
# take, so it runs after the last of them and before the incompatible
# and model-revision legs close the schedule.
RUNS_AFTER = frozenset({'scenario_claim_skew_bound'})
RUNS_BEFORE = frozenset({'scenario_demote_release_stays_released',
                         'scenario_incompatible_revision',
                         'scenario_model_revision'})


# --------------------------------------------------------------------
# The command-writable **field** point leg — the settle-contract half
# the QA capability finding records as unexercisable on this plant:
# every command-writable point the pinned fixture declares is internal
# and channel-less, so a receipted `write_value` on one lands in the
# image and never reaches a driver write. Without a channel-bound
# writable field input, the applied-mint path and the `DriverRejected`
# verdict path — the two settlements every future leg asserting how a
# receipted command's driver write behaves under field contention has
# to read — are unreachable on the rig.
#
# The fixture is a byte-pinned emitted artifact, so the point lands as a
# **lane-owned per-run variant**: `qa_lane/rig_model.py` derives one
# from the document this run mounted (a channel of its own on a device
# the pair already scans, one writable `In` point bound to it, one
# signal for it), and the leg stages the *same* derived document on
# both ends — the scratch field serves it and the born seat mounts it —
# so the remote driver's correspondence probe reads one declaration.
# Nothing under `crates/dcs-demo/fixtures/` changes.
#
# Three legs, each queued through the served receipted command path:
#
#   * **Applied, field-side.** A receipted `write_value` on the derived
#     point settles `applied` at a scan boundary, and the field's own
#     served read — the shipped `dcs-plant-ctl`'s `read` against the
#     field the controller writes through — carries the written value.
#     A settlement into the image alone leaves the field reading what it
#     held before, which is the defect this leg exists to exclude.
#   * **Rejected, named.** With the field side denying the write — the
#     shipped tool's `fault <point> disconnected` injection, the
#     contention class the lane's field-fault legs already drive — the
#     receipt settles `rejected:driver_rejected`, naming the driver's
#     own refusal. `applied` here would be a phantom settlement for a
#     write that never landed, and a receipt that never left
#     `accepted` would be a silent one; both fail here.
#   * **Re-entry.** Clearing the injected fault returns the point to a
#     writable field input: the next receipted write settles `applied`
#     again and the field's read carries it, so the rejection was the
#     contention and not a point that can never be written.
#
# Each verdict is read from the served receipt chain at the submission
# index taken *before* the post, so the receipt the leg reads is
# provably its own command's and never the next one; a command enters
# provisionally `accepted` and settles at the next scan boundary, which
# is the boundary this leg is about.
#
# Named diagnostics: writable-field-point-failed tags the contract
# clauses — a settlement that never reached the driver, a receipt that
# settled applied over a denied write, a rejection that named some other
# verdict, and a cleared field that still refused the write — while
# writable-field-point-nondeterministic tags the instability the
# contract does not answer for: a refused derivation, staging, launch,
# or control-tool call, a seat that never settled, a starved or unread
# monitor, a field that stopped answering, a leftover container, and
# two passes whose digests diverge. The unchecked-diagnostic self-check
# replays the judge over planted negatives and reports
# writable-field-point-unchecked for any that slip through.

WFP_SEAT = 'driven'                # the born seat the leg launches
#: The device whose driver the derived point rides: the rig fixture's
#: `sim-di` — a discrete input device whose channels the pair already
#: scans, so the derived channel needs no device of its own.
WFP_DEVICE = 2
#: The channel the derivation is asked for. It names nothing the
#: fixture declares, so the derivation adds it under its own derived
#: name; the returned `channel` is the name the field serves.
WFP_REQUESTED_CHANNEL = 'write-back'
#: The value the receipted write asks for, and the value the field's
#: read must then carry.
WFP_WRITTEN = True
#: The value the **contended** leg asks for: the opposite of the one
#: the field already holds, so a denied write that landed anyway is
#: visible in the field's own read rather than hidden behind an
#: identical value.
WFP_CONTENDED = not WFP_WRITTEN
#: The field-side denial the contended leg stages.
WFP_FAULT = 'disconnected'
#: The receipted outcome key a driver refusal settles under — the
#: `CommandError::DriverRejected` variant's serde name beside the
#: `rejected` outcome.
WFP_REJECTED = 'rejected:driver_rejected'

WFP_SETTLE_BOUND = 90.0
WFP_SETTLE_POLL = 0.5
WFP_WRITE_BOUND = 30.0
WFP_WRITE_POLL = 0.2

WFP_CLAUSE = 'writable-field-point-failed'
WFP_NONDET = 'writable-field-point-nondeterministic'
WFP_UNCHECKED = 'writable-field-point-unchecked'


def _wfp_ctl(ctx, *args):
    """One shipped-tool invocation against the leg's scratch field: a
    fault injection, its clear, or the field's own served read — with
    the tool's verdict, so a refused call is classified rather than
    raised out of the pass."""
    try:
        result = ctx['born_field_ctl'](*args)
    except Exception as exc:
        return {'args': list(args), 'ok': False,
                'detail': 'the shipped field tool never ran: '
                          + str(exc)[:200]}
    code = getattr(result, 'returncode', None)
    answer = str(getattr(result, 'stdout', '') or '').strip()
    done = '"done"' in answer
    # A mutating op answers the protocol's own `done` verdict; a read
    # answers the served sample, so its exit is the whole verdict and
    # the parse below is what reads it.
    return {'args': list(args),
            'ok': code == 0 and (done or args[:1] == ('read',)),
            'done': done, 'exit': code, 'answer': answer[-400:],
            'detail': str(getattr(result, 'stderr', '') or '').strip()[-200:]}


def _wfp_field_value(ctx, point):
    """The field's own served read of `point` through the shipped tool
    — the field side a driver write is observable on, read from the
    same endpoint the controller writes it rather than from the
    controller's own image. None when the tool answered nothing this
    leg can read, which the judge treats as an unread surface."""
    read = _wfp_ctl(ctx, 'read', str(point))
    if not read.get('ok'):
        return None
    try:
        served = json.loads(read.get('answer') or 'null')
    except ValueError:
        return None
    if isinstance(served, list):
        served = served[0] if served else None
    if not isinstance(served, dict):
        return None
    sample = served.get('sample') if 'sample' in served else served
    if not isinstance(sample, dict):
        return None
    value = sample.get('value')
    if isinstance(value, dict):
        return next(iter(value.values()), None)
    return value


def _wfp_derive(ctx):
    """The lane-derived writable-field document, written beside the
    run's evidence so both the field and the controller mount it.

    Returns the written path, the derived point id, and the
    derivation's description — or the reason it cannot be made, as a
    string."""
    mounted = ctx.get('mounted_model')
    if not mounted:
        return ('the run stages no mounted model document — this leg '
                'derives its own variant from it')
    try:
        document = json.loads(Path(mounted).read_text())
    except (OSError, ValueError, TypeError) as exc:
        return 'the mounted model is unreadable: ' + str(exc)[:200]
    try:
        derivation = rig_model.writable_field_point(
            document, WFP_DEVICE, WFP_REQUESTED_CHANNEL)
    except rig_model.RigModelError as exc:
        return str(exc)
    path = Path(ctx['evidence_dir']) / 'writable-field-point-model.json'
    try:
        path.write_text(json.dumps(derivation['document'], indent=1,
                                   sort_keys=True) + '\n')
    except OSError as exc:
        return ('the derived writable-field document is unwritable: '
                + str(exc)[:200])
    return {'path': str(path), 'point': derivation['point'],
            'description': rig_model.describe(derivation)}


def _wfp_write(ctx, base, point, value, timeout=WFP_WRITE_BOUND):
    """One receipted `write_value` through the served command path,
    read back as its **settled** outcome key.

    The submission index is read out of the served chain *before* the
    post, so the receipt read afterwards is provably this command's;
    the shared outcome key (`applied`, `rejected:<reason>`) is awaited
    past the provisional `accepted`, because a command enters
    provisionally and settles at the next scan boundary."""
    index = _next_receipt_index(ctx, base)
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': value}}},
        'actor': 'qa-lane'}
    status, receipt = http_json('POST', base + '/command', command)
    settled = wait_for(
        lambda: _settled_outcome(ctx, base, index),
        time.monotonic() + timeout, interval=WFP_WRITE_POLL)
    entry = None
    try:
        chain, base_index = _receipt_window(ctx, base)
        position = index - base_index
        if 0 <= position < len(chain):
            entry = chain[position]
    except Exception:
        entry = None
    return {'status': status, 'receipt': receipt, 'index': index,
            'outcome': settled, 'settled': entry}


def _wfp_teardown(ctx):
    """Best-effort teardown: the leg's seat and its scratch field."""
    lever = ctx.get('stop_born_controller')
    if lever is not None:
        try:
            lever(WFP_SEAT)
        except Exception:
            pass
    try:
        if ctx.get('stop_born_field') is None:
            return None
        ctx['stop_born_field']()
    except Exception as exc:
        return str(exc)[:200]
    return None


def _wfp_rig_state(ctx, field_error):
    """The rig's claim state after the sweep."""
    state = ctx.get('born_controller_state')
    present = None
    if state is not None:
        try:
            present = (state(WFP_SEAT) or {}).get('absent')
        except Exception:
            present = None
    return {'seat': present, 'field_error': field_error}


def _judge_wfp(record, note):
    """Replay one pass's record — runnable against planted negatives in
    the self-check."""
    def failed(key, detail):
        note(key, WFP_CLAUSE, detail)

    def nondet(key, detail):
        note(key, WFP_NONDET, detail)

    if record.get('stage_error') is not None:
        nondet('stage', 'the derivation, the staging, the launch, or a '
               'control-tool call never completed: '
               + str(record['stage_error']))
        return
    settled = record.get('settled') or {}
    applied = record.get('applied') or {}
    rejected = record.get('rejected') or {}
    reentered = record.get('reentered') or {}

    # The seat must have taken the field and be scanning: without a
    # scan boundary no receipt settles at all, and a run that never
    # scanned cannot violate the settlement contract — the three legs
    # are unread here, not failed, so nothing below is judged.
    if (settled.get('view') or {}).get('role') != 'active':
        nondet('seat-unsettled', "the born seat never settled as the "
               "field's writer: " + json.dumps(settled)[:200])
        return

    # Leg 1 — the applied settlement reached the driver.
    if applied.get('outcome') != 'applied':
        failed('no-apply', 'a receipted write on a writable field point '
               'must settle applied at a scan boundary — the receipt '
               'reads ' + json.dumps(applied)[:300])
    if applied.get('field') != WFP_WRITTEN:
        failed('field-not-written', 'the driver write must be observable '
               'field-side — the field serves '
               + json.dumps(applied.get('field')) + ', expected '
               + json.dumps(WFP_WRITTEN) + ': '
               + json.dumps(applied)[:300])
    if applied.get('served') is not None \
            and applied.get('served') != WFP_WRITTEN:
        failed('served-mismatch', 'the same scan must read the write back '
               'on the point the model serves: the monitor read '
               + json.dumps(applied.get('served')) + ': '
               + json.dumps(applied)[:300])

    # Leg 2 — the contended write names the rejection verdict.
    if rejected.get('outcome') != WFP_REJECTED:
        failed('no-rejection', 'a denied driver write must settle the '
               'named rejection verdict, never applied and never a '
               'still-provisional receipt — the receipt reads '
               + json.dumps(rejected)[:300])
    if rejected.get('field') != WFP_WRITTEN:
        failed('phantom-write', 'the denied write must leave the field '
               'holding what it held before, not the value the receipt '
               'declined to apply — the field serves '
               + json.dumps(rejected.get('field')) + ': '
               + json.dumps(rejected)[:300])

    # Leg 3 — clearing the contention returns the point to writable.
    if reentered.get('outcome') != 'applied':
        failed('no-reentry', 'a cleared field must accept the write '
               'again — the receipt reads ' + json.dumps(reentered)[:300])
    elif reentered.get('field') != WFP_WRITTEN:
        failed('reentry-not-written', 'the re-entered write must reach '
               'the driver field-side as well — the field serves '
               + json.dumps(reentered.get('field')) + ': '
               + json.dumps(reentered)[:300])

    if (record.get('fault') or {}).get('ok') is not True:
        nondet('fault-unanswered', "the field's fault injection never "
               'answered: ' + json.dumps(record.get('fault'))[:200])
    if (record.get('clear') or {}).get('ok') is not True:
        nondet('clear-unreadable', "the field's fault clear never "
               'answered: ' + json.dumps(record.get('clear'))[:200])
    rig = record.get('rig') or {}
    if rig.get('seat') is not True or rig.get('field_error') is not None:
        nondet('rig-not-restored', "the leg left the rig's claim state "
               'standing — a seat or the scratch field outlived the '
               'sweep the legs behind this one inherit: '
               + json.dumps(rig, sort_keys=True)[:300])


def _wfp_digest(record, violations):
    """The pass's normalized verdict record — identical digests across
    two consecutive passes is the determinism contract."""
    def clean(*keys):
        return not any(key in violations for key in keys)

    def verdict(keys, values, other):
        for value in values:
            if clean(*keys):
                return value
        return other

    return {
        'apply': verdict(('no-apply', 'field-not-written',
                          'served-mismatch'), ['applied'], 'image-only'),
        'contention': verdict(('no-rejection', 'phantom-write'),
                              ['rejected'], 'silent'),
        'reentry': verdict(('no-reentry', 'reentry-not-written'),
                           ['applied'], 'refused'),
        'field': 'serving' if clean('fault-unanswered',
                                    'clear-unreadable') else 'unreadable',
        'rig': 'restored' if clean('rig-not-restored') else 'dirty'}


def _wfp_pass(ctx, number):
    """One pass over the three legs: derive the writable-field
    document, stage the scratch field and one born seat on it, let the
    seat settle, then write applied with the field's read beside it,
    deny the write field-side and read the named rejection, and clear
    the contention and write again."""
    record = {'pass': number, 'applied': {}, 'rejected': {},
              'reentered': {}}
    try:
        derived = _wfp_derive(ctx)
    except Exception as exc:
        record['stage_error'] = ('the derivation never ran: '
                                 + str(exc)[:250])
        return record
    if isinstance(derived, str):
        record['inconclusive'] = derived
        return record
    record['field_document'] = derived['description']
    point = derived['point']
    record['point'] = point
    try:
        field = ctx['start_born_field']('serving',
                                        document=derived['path'])
        launch = ctx['start_born_controller'](WFP_SEAT, field['remote'],
                                              document=derived['path'])
    except Exception as exc:
        record['stage_error'] = ('the staging or the launch never ran: '
                                 + str(exc)[:250])
        return record
    record['field'] = {'mode': field.get('mode'), 'model': field.get('model')}
    record['launch'] = {'monitor': launch.get('monitor'),
                        'mounted': launch.get('model')}
    base = launch.get('monitor')
    settled = wait_for(
        lambda: (lambda seen: seen if seen is not None
                 and seen.get('role') == 'active' else None)(
                     _try_role(ctx, base)),
        time.monotonic() + WFP_SETTLE_BOUND, interval=WFP_SETTLE_POLL)
    record['settled'] = {'view': settled}
    if settled is None:
        return record

    # Leg 1 — the applied settlement, observed field-side.
    applied = _wfp_write(ctx, base, point, WFP_WRITTEN)
    record['applied'] = {
        'outcome': applied.get('outcome'),
        'status': applied.get('status'),
        'field': _wfp_field_value(ctx, point),
        'served': _point_value(_try_snapshot(ctx, base) or {}, point),
        'receipt': applied.get('settled')}

    # Leg 2 — the contended write: the field side refuses it.
    record['fault'] = _wfp_ctl(ctx, 'fault', str(point), WFP_FAULT)
    if record['fault'].get('ok') is not True:
        return record
    contended = _wfp_write(ctx, base, point, WFP_CONTENDED)
    record['rejected'] = {
        'outcome': contended.get('outcome'),
        'status': contended.get('status'),
        'field': _wfp_field_value(ctx, point),
        'receipt': contended.get('settled')}

    # Leg 3 — the cleared field takes the write again.
    record['clear'] = _wfp_ctl(ctx, 'clear-fault', str(point))
    if record['clear'].get('ok') is not True:
        return record
    reentered = _wfp_write(ctx, base, point, WFP_WRITTEN)
    record['reentered'] = {
        'outcome': reentered.get('outcome'),
        'status': reentered.get('status'),
        'field': _wfp_field_value(ctx, point),
        'receipt': reentered.get('settled')}
    return record


def _wfp_self_check():
    """The unchecked-diagnostic guard: replay the judge over planted
    negatives and report each that slipped."""
    def clean_record():
        return {
            'pass': 1,
            'point': 1128,
            'field_document': {'channel': 'qa-writable-write-back',
                               'requested_channel': WFP_REQUESTED_CHANNEL,
                               'added_channel': True},
            'field': {'mode': 'serving',
                      'model': '/run/evidence/model.json'},
            'launch': {'monitor': 'http://127.0.0.1:18087',
                       'mounted': '/run/evidence/model.json'},
            'settled': {'view': {'role': 'active', 'tick': 40}},
            'applied': {'outcome': 'applied', 'status': 202, 'field': True,
                        'served': True,
                        'receipt': {'index': 5, 'outcome': {'applied': {}}}},
            'fault': {'ok': True},
            'rejected': {'outcome': WFP_REJECTED, 'status': 202, 'field': True,
                         'receipt': {'index': 6}},
            'clear': {'ok': True},
            'reentered': {'outcome': 'applied', 'status': 202,
                          'field': True,
                          'receipt': {'index': 7,
                                      'outcome': {'applied': {}}}},
            'rig': {'seat': True, 'field_error': None}}

    def audit(record):
        found = {}
        _judge_wfp(record, lambda key, diagnostic, detail:
               found.setdefault(key, diagnostic))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    def expect(name, mutate, diagnostic=WFP_CLAUSE):
        record = clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    expect('write-never-settled',
           lambda r: r['applied'].update(outcome=None))
    expect('write-settled-in-the-image-only',
           lambda r: r['applied'].update(field=False))
    expect('write-not-read-back',
           lambda r: r['applied'].update(served=False))
    expect('denied-write-settled-applied',
           lambda r: r['rejected'].update(outcome='applied'))
    expect('denied-write-still-provisional',
           lambda r: r['rejected'].update(outcome=None))
    expect('rejection-named-another-verdict',
           lambda r: r['rejected'].update(outcome='rejected:not_writable'))
    expect('denied-write-landed-anyway',
           lambda r: r['rejected'].update(field=False))
    expect('cleared-field-still-refused',
           lambda r: r['reentered'].update(outcome=WFP_REJECTED))
    expect('re-entered-write-did-not-land',
           lambda r: r['reentered'].update(field=False))
    expect('seat-never-settled',
           lambda r: r['settled'].update(view=None), WFP_NONDET)
    expect('fault-injection-unanswered',
           lambda r: r['fault'].update(ok=False), WFP_NONDET)
    expect('fault-clear-unanswered',
           lambda r: r['clear'].update(ok=False), WFP_NONDET)
    expect('rig-left-standing',
           lambda r: r['rig'].update(seat=False), WFP_NONDET)
    expect('rig-presence-unreadable',
           lambda r: r['rig'].update(seat=None), WFP_NONDET)
    expect('stage-failed',
           lambda r: r.update(stage_error='docker run failed'), WFP_NONDET)
    return slipped


def scenario_writable_field_point_settle(ctx):
    """Prove a receipted command reaches the driver on a lane-derived
    writable field point: stage the derived document on both ends of a
    scratch field, write it applied with the field's own read carrying
    the value, deny the write field-side and read the named
    `driver_rejected` verdict, then clear the denial and write again.
    The rig is swept afterward and two consecutive passes produce
    identical outcome digests."""
    case = Case(
        'writable-field-point-settle',
        'A receipted write on a writable field point reaches the driver, '
        'and a denied write names the rejection verdict',
        "over a lane-derived model variant carrying one channel-bound "
        "writable field In point staged on both ends of the leg's own "
        "field, a receipted write settles applied at a scan boundary "
        "with the field's own served read carrying the written value "
        "and the same scan reading it back — never a settlement into "
        "the image alone; a denied driver write, the field side "
        "injecting a disconnection, settles rejected naming "
        "driver_rejected with the driver's own error and leaves the "
        "field as it was — never applied, never a phantom write, never "
        "a still-provisional receipt; clearing the fault returns the "
        "point to a writable field input whose next write applies and "
        "lands field-side again; the rig swept afterward, a seat or "
        "field that outlived the sweep fails the leg, and two passes "
        "produce identical digests")
    try:
        missing = [key for key in ('start_born_field', 'stop_born_field',
                                   'start_born_controller',
                                   'stop_born_controller',
                                   'born_controller_state',
                                   'born_field_ctl', 'evidence_dir')
                   if ctx.get(key) is None]
        if missing:
            return case.finish('inconclusive',
                               'the run stages no capability this leg '
                               'needs: ' + ', '.join(missing))
        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record = _wfp_pass(ctx, number)
            field_error = _wfp_teardown(ctx)
            record['rig'] = _wfp_rig_state(ctx, field_error)
            if not record.get('inconclusive'):
                _judge_wfp(record, note)
                digest = _wfp_digest(record, violations)
                record['digest'] = dict(digest)
            else:
                digest = None
            record['violations'] = {
                key: diagnostic
                for key, (diagnostic, _) in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'writable-field-point-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref,
                          'writable-field-point pass ' + str(number)
                          + ' — the lane-derived document and what it '
                          'added, the staged field and born seat, the '
                          "seat's settle, the three receipted writes "
                          'with each settled receipt, the field-side '
                          'reads beside them, the fault injection and '
                          'its clear, the swept rig\'s restoration '
                          'read, and the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive', record['inconclusive'])
            if violations:
                name = WFP_CLAUSE if any(
                    diagnostic == WFP_CLAUSE
                    for diagnostic, _ in violations.values()) else WFP_NONDET
                return case.finish(
                    'failed', name + ': ' + '; '.join(
                        detail for _, detail
                        in list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', WFP_NONDET + ": the two passes' digests diverged: "
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two writable-field-point passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        slipped = _wfp_self_check()
        if slipped:
            return case.finish('failed', WFP_UNCHECKED + ': planted negatives '
                               'slipped the leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc)[:400])
