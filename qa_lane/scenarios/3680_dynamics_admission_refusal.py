"""The dynamics_admission_refusal acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: The dynamics-admission case runs late — after the
# failover switch and the earlier field legs, inside the unpinned
# window before the dcs-ctl closer: its doctored-document probes run
# in labeled scratch containers that never touch the deployed pair,
# so the leg needs only a settled pair and a serving plant to bind
# the malformed classes against.
RUNS_AFTER = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The malformed-dynamics named-refusal contract (the deployed-rig,
# per-revision evidence for the #957 and #958 fixes — the field-path
# honesty the plant's own admission gates owe): a dynamics document
# whose element reads and drives the same point — the
# bool_flow/threshold self-point shape that passed the emitted schema
# and --check-dynamics, then panicked the plant server's first step and
# poisoned its state mutex behind an open listener — and a document
# whose element drives a controller-owned Out point — the shape whose
# every step stomped the operator's staged command — must be refused by
# name at check, load, or merge time: 'dynamics element <i> (driving
# point <p>) is invalid: <rule>' on the rejection stream, a nonzero
# exit, and no 'listening on' bind. Never a panic unwind, never a
# serving plant whose first step poisons the field's shared state.
#
# The leg stages each recorded class through the run context's
# admit_dynamics lever — the harness's per-run variant seam for a
# doctored document: the document lands inside the bounded run dir,
# then the run's own plant image mounts it read-only in two labeled
# scratch containers — the released `--check-dynamics` preflight
# exiting with the merge verdict, and a detached `--dynamics` serving
# load polled across the bind grace, where 'still running' is the
# accepted verdict. An honest control document — a valid element
# composed off the live census — must pass both gates first, proving
# 'accepted' is observable before a malformed class is convicted on
# it. Across the refused loads the deployed plant — untouched by the
# scratch containers — must keep answering its census and the
# shared-claim probes (no poisoned mutex), its element-driven samples
# holding shape with advancing tick, and every bound Out point's
# stored value the field owner's commanded value — the stomp the
# Out-direction refusal exists to prevent. Named diagnostics are
# dynamics-admission-failed for a contract miss — acceptance, an
# unnamed refusal, a panic, a silent or stomped field —
# dynamics-admission-nondeterministic for a role move or two passes
# disagreeing, and dynamics-admission-unchecked when the
# self-check's planted negatives slip the leg's own auditors. A rig
# that is unreachable or never settles, whose census binds no points
# of the shapes the classes need, whose run context carries no
# admit_dynamics lever, or whose plant tooling predates
# --check-dynamics reports inconclusive; the pair leaves on its
# launch roles.

ADMIT_SETTLE = 30     # bound on the pair settling before the leg runs
ADMIT_POLL = 0.5      # the sampling cadence for converging reads
ADMIT_CONVERGE = 10   # bound on a stored Out value tracking the
                      # owner's commanded value
MALFORMED_CLASSES = ('self_point', 'out_point')
DIAG_FAILED = 'dynamics-admission-failed'
DIAG_NONDET = 'dynamics-admission-nondeterministic'
DIAG_UNCHECKED = 'dynamics-admission-unchecked'


def _kind_of(entry):
    """A census entry's served value kind — 'float', 'bool', 'int' —
    from the tagged-union sample's single key."""
    value = ((entry or {}).get('sample') or {}).get('value')
    if isinstance(value, dict) and value:
        return next(iter(value))
    return None


def _value_of(sample):
    """A served sample's plain value out of the tagged-union value."""
    value = (sample or {}).get('value')
    if isinstance(value, dict):
        return next(iter(value.values()), None)
    return value


def _census(ctx):
    """{point: census entry} the serving plant's `list` answers — its
    own account of every bound point's direction, sample, and fault —
    or None when the census never lands (a silent or poisoned
    field)."""
    body = _try_plant_ctl(ctx, 'list')
    if not isinstance(body, dict) or body.get('result') != 'points':
        return None
    return {entry.get('point'): entry
            for entry in (body.get('points') or [])
            if isinstance(entry, dict)}


def _ping_tick(ctx):
    """The plant's served tick via `ping`, or None — the liveness mark
    the refused loads must keep advancing."""
    body = _try_plant_ctl(ctx, 'ping')
    return (body or {}).get('tick') if isinstance(body, dict) else None


def _self_point_doc(point):
    """The recorded self-point class — bool_flow and threshold
    elements each reading and driving the same point: the shape that
    passed the emitted schema and --check-dynamics, then panicked the
    plant server's first step and poisoned its state mutex behind an
    open listener. Each element now owes a named refusal — a
    bool_flow's gate input must be Bool, a threshold's driven contact
    must be a Bool point."""
    return [
        {'bool_flow': {'input': point, 'output': point,
                       'on_rate': -1.0, 'off_rate': 0.0,
                       'initial': 0.0}},
        {'threshold': {'input': point, 'output': point,
                       'on': 1.0, 'off': 0.0, 'initial': False}}]


def _out_point_doc(float_in, outs):
    """The recorded field-ownership class — threshold elements driving
    controller-owned bool Out points, the pump-cmd shape whose every
    step stomped the operator's staged command."""
    return [{'threshold': {'input': float_in, 'output': output,
                           'on': 1.0, 'off': 0.0, 'initial': False}}
            for output in outs[:2]]


def _derive_documents(census):
    """The malformed-class documents bound against the live census:
    each recorded class's elements target points the deployed model
    actually binds, so the refusal names real points. Returns (docs,
    missing) — missing lists the shapes the census cannot supply."""
    def points(direction, kind=None):
        return sorted(p for p, e in census.items()
                      if e.get('direction') == direction
                      and (kind is None or _kind_of(e) == kind))
    float_in = next(iter(points('in', 'float')), None)
    bool_in = next(iter(points('in', 'bool')), None)
    bool_outs = points('out', 'bool')
    float_outs = points('out', 'float')
    outs = points('out')
    docs = {'out_targets': outs, 'probe_point': float_in}
    missing = []
    if float_in is not None and bool_in is not None:
        docs['honest'] = [{'threshold': {'input': float_in,
                                         'output': bool_in,
                                         'on': 1.0, 'off': 0.0,
                                         'initial': False}}]
    else:
        missing.append('an in float plus an in bool point for the '
                       'honest control element')
    if float_in is not None:
        docs['self_point'] = _self_point_doc(float_in)
    else:
        missing.append('an in float point for the self-point class')
    if float_in is not None and bool_outs:
        docs['out_point'] = _out_point_doc(float_in, bool_outs)
    elif float_in is not None and float_outs:
        docs['out_point'] = [{'first_order_lag': {
            'input': float_in, 'output': float_outs[0],
            'time_constant': 0.5, 'initial': 0.0}}]
    else:
        missing.append('a bound Out point for the out-point class')
    if not outs:
        missing.append('a bound Out point for the '
                       'command-intactness audit')
    return docs, missing


def _missing_refusals(text, doc):
    """The element names 'dynamics element <i> (driving point <p>)'
    absent from `text` — the named refusal every malformed element
    owes the rejection stream."""
    missing = []
    for index, element in enumerate(doc):
        body = next(iter(element.values()))
        needle = ('dynamics element ' + str(index)
                  + ' (driving point ' + str(body.get('output')) + ')')
        if needle not in text:
            missing.append(needle)
    return missing


def _honest_verdict(verdict):
    """Classify the honest control's gate answers: 'admitted' — the
    preflight accepted ('check ok', exit 0) and the spawned load kept
    serving past the bind grace with a bound listener; 'predates' —
    the tooling lacks the --check-dynamics surface itself (a usage
    exit on the control); 'refused' or 'unbound' — a valid document
    met a nonzero verdict, or kept running without ever binding,
    itself an admission defect."""
    check = verdict.get('check') or {}
    serve = verdict.get('serve') or {}
    text = str(check.get('stdout') or '') + str(check.get('stderr')
                                              or '')
    if check.get('exit') == 2:
        return 'predates'
    if check.get('exit') != 0 or 'check ok' not in text:
        return 'refused'
    if serve.get('running') is not True:
        return 'refused'
    if 'listening on' not in str(serve.get('logs') or ''):
        return 'unbound'
    return 'admitted'


def _judge_gate(name, doc, gate, verdict, note):
    """Audit one admission gate's verdict for a malformed class's
    document: refused by name — every element's 'dynamics element <i>
    (driving point <p>)' line present in the captured output — with a
    nonzero exit, no 'listening on' bind, and no panic evidence. Any
    acceptance, unnamed refusal, or unwind is a named failure."""
    label = name + '-' + gate
    text = ' '.join(str(verdict.get(part) or '')
                    for part in ('stdout', 'stderr', 'logs'))
    if verdict.get('running'):
        note(label + '-accepted', DIAG_FAILED,
             'the spawned plant kept serving ' + name + '\'s '
             'malformed document past the bind grace — the '
             'serving-load path admitted it')
        return
    exit_code = verdict.get('exit')
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        note(label + '-unread', DIAG_FAILED,
             name + '\'s ' + gate + ' gate produced no exit verdict '
             '— the admission evidence never landed')
        return
    if exit_code == 0:
        note(label + '-accepted', DIAG_FAILED,
             'the ' + gate + ' gate accepted ' + name
             + '\'s malformed document (exit 0) — refused by name '
             'is the contract')
        return
    panic = [mark for mark in ('panic', 'backtrace', 'abort')
             if mark in text.lower()]
    if panic:
        note(label + '-panic', DIAG_FAILED,
             name + '\'s ' + gate + ' refusal carries panic evidence '
             '(' + ', '.join(panic) + ') — the merge must refuse by '
             'name, never unwind')
        return
    if 'listening on' in text:
        note(label + '-served', DIAG_FAILED,
             name + '\'s ' + gate + ' output carries a listener bind '
             '— the malformed document reached serving')
        return
    missing = _missing_refusals(text, doc)
    if missing:
        note(label + '-unnamed', DIAG_FAILED,
             name + '\'s ' + gate + ' refusal never named '
             + ', '.join(missing))


def _field_moved(before, after):
    """The first (point, detail) where the serving field moved across
    the refused loads — a vanished census point, a rewound sample
    tick, or a moved value kind — or None. The refused loads owe the
    field an untouched serving plant: element state still
    integrating, no reset, no poisoned census."""
    for point, entry in sorted((before or {}).items()):
        post = (after or {}).get(point)
        if not isinstance(post, dict):
            return point, 'vanished from the census'
        prior = entry.get('sample') or {}
        sample = post.get('sample') or {}
        if isinstance(prior.get('tick'), int) \
                and isinstance(sample.get('tick'), int) \
                and sample['tick'] < prior['tick']:
            return point, 'sample tick rewound ' \
                + json.dumps(prior['tick']) + ' -> ' \
                + json.dumps(sample['tick'])
        prior_kind, kind = _kind_of(entry), _kind_of(post)
        if prior_kind is not None and kind != prior_kind:
            return point, 'value kind moved ' + str(prior_kind) \
                + ' -> ' + str(kind)
    return None


def _commanded_held(ctx, owner, outs, deadline):
    """{point: {'commanded', 'stored', 'held'}} — for each bound Out
    point, the field's stored value converging to the field owner's
    snapshot-served commanded value inside the bound. A stomping
    element — the shape the Out-direction refusal exists to prevent —
    reads as a persistent stored/commanded divergence; a snapshot
    serving no commanded value leaves 'held' None."""
    verdicts = {}
    pending = list(outs)
    while pending and time.monotonic() < deadline:
        census = _census(ctx) or {}
        snapshot = _try_snapshot(ctx, ctx[owner])
        pending = []
        for point in outs:
            commanded = _point_value(snapshot or {}, point)
            entry = census.get(point) or {}
            stored = _value_of(entry.get('sample') or {})
            held = None if commanded is None else stored == commanded
            verdicts[point] = {'commanded': commanded,
                               'stored': stored, 'held': held}
            if held is not True:
                pending.append(point)
        if pending:
            time.sleep(ADMIT_POLL)
    return verdicts


def _claim_probes(ctx, claim, point):
    """The unpoisoned-mutex follow-up — a shared-claim attachment
    under the field owner's pinned token answers ensure_writer, a
    dt:0 step, and a read: the live claim/mutation/read surface a
    poisoned state mutex would fence or hang."""
    probes = {}
    try:
        stream = _plant_connect(ctx)
        try:
            for label, request in (
                    ('ensure', {'op': 'ensure_writer',
                                'owner': claim}),
                    ('step', {'op': 'step', 'dt': 0}),
                    ('read', {'op': 'read', 'point': point})):
                answer = _plant_request(stream, request)
                probes[label] = answer.get('result')
                if probes[label] is None:
                    probes[label] = str(answer.get('error'))[:150]
        finally:
            stream.close()
    except Exception as exc:
        probes['error'] = str(exc)[:200]
    return probes


def _judge_intact(record, note):
    """Audit a pass's intactness half — everything after the refused
    loads must prove the serving field untouched: the census still
    answering (no poisoned state mutex), no served point moved, the
    field tick advanced, every bound Out point's stored value the
    owner's commanded value, and the shared-claim probes answered.
    Each miss names its diagnostic."""
    after = record.get('after')
    if not isinstance(after, dict):
        note('plant-silent', DIAG_FAILED,
             'the serving plant never answered its census after the '
             'refused loads — the poisoned-mutex shape the named '
             'refusal exists to prevent')
        return
    moved = _field_moved(record.get('before'), after)
    if moved:
        note('field-' + str(moved[0]), DIAG_FAILED,
             'field point ' + str(moved[0]) + ' ' + moved[1]
             + ' across the refused loads')
    tick0, tick1 = record.get('ping0'), record.get('ping1')
    if isinstance(tick0, int) and isinstance(tick1, int) \
            and tick1 <= tick0:
        note('field-frozen', DIAG_FAILED,
             'the serving field never stepped across the refused '
             'loads — tick ' + str(tick0) + ' -> ' + str(tick1))
    for point, held in sorted((record.get('commands') or {}).items()):
        if held.get('held') is False:
            note('stomped-' + str(point), DIAG_FAILED,
                 'field Out point ' + str(point) + ' stored '
                 + json.dumps(held.get('stored')) + ' while the '
                 'operator\'s commanded value is '
                 + json.dumps(held.get('commanded')) + ' — the '
                 'element-stomp shape the Out-direction refusal '
                 'exists to prevent')
    probes = record.get('probes') or {}
    if probes.get('error'):
        note('probes-dead', DIAG_FAILED,
             'the plant protocol stopped answering the shared-claim '
             'probes: ' + str(probes['error']))
    else:
        if probes.get('step') != 'stepped':
            note('step-refused', DIAG_FAILED,
                 'the shared claim\'s dt:0 step answered '
                 + json.dumps(probes.get('step')) + ' — the serving '
                 'field stopped stepping for a live attachment')
        if probes.get('read') != 'sample':
            note('read-refused', DIAG_FAILED,
                 'the shared claim\'s read answered '
                 + json.dumps(probes.get('read')) + ' — the serving '
                 'field stopped answering a live attachment')
    for name, role in sorted((record.get('roles') or {}).items()):
        wanted = (record.get('launch_roles') or {}).get(name)
        if role != wanted:
            note('role-' + name, DIAG_NONDET,
                 name + ' left its launch role ' + json.dumps(wanted)
                 + ' during the refused loads — now '
                 + json.dumps(role))


def _self_check():
    """The unchecked-diagnostic guard: replay the leg's auditors over
    planted negatives — an accepted document, a serving listener, a
    panic unwind, an unnamed refusal, a verdict that never landed, a
    refused honest control, a silent or moved or stomped field, dead
    probes, a moved role — and report every one let slip."""
    slipped = []
    doc = _self_point_doc(20)
    refusals = '\n'.join(
        'error: dynamics element ' + str(index) + ' (driving point '
        + str(next(iter(element.values()))['output'])
        + ') is invalid: <rule>' for index, element in enumerate(doc))

    def clean_census():
        return {
            10: {'direction': 'in', 'sample': {
                'value': {'float': 0.8}, 'tick': 3}},
            20: {'direction': 'in', 'sample': {
                'value': {'float': 1.5}, 'tick': 3}},
            40: {'direction': 'in', 'sample': {
                'value': {'bool': False}, 'tick': 3}},
            100: {'direction': 'out', 'sample': {
                'value': {'bool': True}, 'tick': 3}}}

    def clean_record():
        return {'before': clean_census(), 'after': clean_census(),
                'ping0': 40, 'ping1': 44,
                'commands': {100: {'commanded': True,
                                   'stored': True, 'held': True}},
                'probes': {'ensure': 'claimed_shared',
                           'step': 'stepped', 'read': 'sample'},
                'roles': {'ctrl-a': 'active', 'ctrl-b': 'standby'},
                'launch_roles': {'ctrl-a': 'active',
                                 'ctrl-b': 'standby'}}

    def gate_audit(verdict, gate='check'):
        found = []
        _judge_gate('self_point', doc, gate, verdict,
                    lambda key, diagnostic, detail: found.append(key))
        return found

    def intact_audit(record):
        found = []
        _judge_intact(
            record,
            lambda key, diagnostic, detail: found.append(key))
        return found

    def expect_flagged(name, found):
        if not found:
            slipped.append(name)

    def expect_clean(name, found):
        if found:
            slipped.append(name + '-overstrict')

    clean_gate = {'exit': 1, 'stdout': '', 'stderr': refusals}
    clean_serve = {'exit': 1, 'running': False, 'logs': refusals}
    expect_clean('clean-check', gate_audit(clean_gate))
    expect_clean('clean-serve', gate_audit(clean_serve, 'serve'))
    expect_flagged('check-accepted', gate_audit(
        {'exit': 0, 'stdout': 'check ok', 'stderr': ''}))
    expect_flagged('serve-accepted', gate_audit(
        {'exit': None, 'running': True,
         'logs': 'listening on 127.0.0.1:60000'}, 'serve'))
    expect_flagged('check-panic', gate_audit(
        {'exit': 101, 'stdout': '',
         'stderr': 'thread \'main\' panicked at driver.rs:42: '
                   'assertion failed'}))
    expect_flagged('check-unnamed', gate_audit(
        {'exit': 1, 'stdout': '',
         'stderr': 'error: the dynamics document was refused'}))
    expect_flagged('check-served', gate_audit(
        {'exit': 1, 'stdout': '',
         'stderr': refusals + '\nlistening on 127.0.0.1:60000'}))
    expect_flagged('check-unread', gate_audit(
        {'exit': None, 'stdout': '', 'stderr': ''}))
    if _honest_verdict({'check': clean_gate,
                        'serve': clean_serve}) == 'admitted':
        slipped.append('honest-refused')
    if _honest_verdict({'check': {'exit': 2, 'stdout': '',
                                  'stderr': 'usage'},
                        'serve': clean_serve}) != 'predates':
        slipped.append('honest-predates')
    if _honest_verdict({
            'check': {'exit': 0, 'stdout': 'check ok',
                      'stderr': ''},
            'serve': {'exit': None, 'running': True,
                      'logs': 'listening on 127.0.0.1:60000'}}
            ) != 'admitted':
        slipped.append('honest-admitted')
    expect_clean('intact-clean', intact_audit(clean_record()))
    record = clean_record()
    record['after'] = None
    expect_flagged('plant-silent', intact_audit(record))
    record = clean_record()
    record['after'][20]['sample']['tick'] = 1
    expect_flagged('field-rewound', intact_audit(record))
    record = clean_record()
    record['after'][40]['sample']['value'] = {'float': 0.0}
    expect_flagged('field-kind', intact_audit(record))
    record = clean_record()
    record['ping1'] = 40
    expect_flagged('field-frozen', intact_audit(record))
    record = clean_record()
    record['commands'][100] = {'commanded': True, 'stored': False,
                               'held': False}
    expect_flagged('command-stomped', intact_audit(record))
    record = clean_record()
    record['probes'] = {'error': 'connection reset'}
    expect_flagged('probes-dead', intact_audit(record))
    record = clean_record()
    record['probes']['step'] = 'fenced'
    expect_flagged('step-refused', intact_audit(record))
    record = clean_record()
    record['roles']['ctrl-b'] = 'active'
    expect_flagged('role-moved', intact_audit(record))
    return slipped


def _leave_clean(ctx, launch):
    """The launch-role restore the acceptance criteria demand: the
    pair leaves the leg on the roles it held when it settled. Best
    effort — a dead monitor is the leg's own evidence, never a
    raised teardown."""
    owner = next((name for name, role in launch.items()
                  if role == 'active'), None)
    peer = next((name for name, role in launch.items()
                 if role == 'standby'), None)
    if owner is None or peer is None:
        return
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            wait_for(lambda: (_try_role(ctx, ctx[peer]) or {})
                     .get('role') == 'standby' or None,
                     time.monotonic() + ADMIT_SETTLE,
                     interval=ADMIT_POLL)
        if (_try_role(ctx, ctx[owner]) or {}).get('role') != 'active':
            _settle_call(ctx[owner] + '/promote')
            wait_for(lambda: (_try_role(ctx, ctx[owner]) or {})
                     .get('role') == 'active' or None,
                     time.monotonic() + ADMIT_SETTLE,
                     interval=ADMIT_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + ADMIT_SETTLE, interval=ADMIT_POLL)
    except Exception:
        pass


def _admission_pass(ctx, number, owner, peer, launch, docs, claim):
    """One probe pass: stage the honest control plus each recorded
    malformed class through the admit_dynamics lever — the preflight
    and spawned-load verdicts — then audit the serving field's
    intactness across them. Returns (digest, violations, record):
    digest the normalized verdict set two passes compare; violations
    maps each clause key to (diagnostic, detail); record carries
    'inconclusive' when the pass itself could not run."""
    record = {'pass': number, 'owner': owner,
              'launch_roles': dict(launch),
              'documents': {name: docs[name]
                            for name in ('honest',)
                            + MALFORMED_CLASSES},
              'out_targets': docs['out_targets'],
              'gates': {}, 'roles': {}}
    violations = {}
    problems = []

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))
        problems.append(key)

    admit = ctx['admit_dynamics']
    record['before'] = _census(ctx)
    if record['before'] is None:
        record['inconclusive'] = 'the serving plant answered no ' \
            'census for the admission baseline'
        return None, violations, record
    record['ping0'] = _ping_tick(ctx)
    for name in ('honest',) + MALFORMED_CLASSES:
        try:
            verdict = admit(name, docs[name])
        except Exception as exc:
            record['inconclusive'] = 'the doctored-document lever ' \
                'never ran ' + name + ': ' + str(exc)[:200]
            return None, violations, record
        record['gates'][name] = verdict
    honest = _honest_verdict(record['gates']['honest'])
    if honest == 'predates':
        record['inconclusive'] = 'the staged plant tooling predates ' \
            'the --check-dynamics surface — a usage exit on the ' \
            'honest control'
        return None, violations, record
    if honest != 'admitted':
        note('honest-' + honest, DIAG_FAILED,
             'the honest control document met a ' + repr(honest)
             + ' verdict — the admission gates must admit a valid '
             'document before they can convict a malformed one')
    for name in MALFORMED_CLASSES:
        gates = record['gates'][name]
        for gate in ('check', 'serve'):
            if not isinstance(gates.get(gate), dict):
                record['inconclusive'] = 'the lever returned no ' \
                    + gate + ' verdict for ' + name
                return None, violations, record
            _judge_gate(name, docs[name], gate, gates[gate], note)
    record['ping1'] = _ping_tick(ctx)
    record['after'] = _census(ctx)
    record['commands'] = _commanded_held(
        ctx, owner, docs['out_targets'],
        time.monotonic() + ADMIT_CONVERGE)
    record['probes'] = _claim_probes(ctx, claim, docs['probe_point'])
    for name in (owner, peer):
        report = _try_role(ctx, ctx[name])
        record['roles'][name] = (report or {}).get('role')
    # A fenced or unowned claim is a rig anomaly, not a contract miss
    # — inconclusive before the auditors read its fenced probes as
    # the field's verdict (and never masking a violation already
    # seen).
    if not violations and not record['probes'].get('error'):
        ensure = record['probes'].get('ensure')
        if ensure not in ('done', 'claimed_shared'):
            record['inconclusive'] = 'the writer claim refused the ' \
                'shared attachment — the field is not owned the ' \
                'way the probes need: ' + str(ensure)
            return None, violations, record
    _judge_intact(record, note)
    if not violations:
        served = [p for p, v in record['commands'].items()
                  if v.get('held') is not None]
        if not served:
            record['inconclusive'] = 'the field owner serves no ' \
                'commanded values for the field\'s bound Out points'
            return None, violations, record
    keys = set(problems)
    digest = {'honest': 'unprobed', 'self_point': 'unprobed',
              'out_point': 'unprobed', 'field': 'unprobed',
              'commands': 'unprobed', 'probes': 'unprobed',
              'roles': 'moved'}
    if not any(key.startswith('honest') for key in keys):
        digest['honest'] = 'admitted'
    for name in MALFORMED_CLASSES:
        if not any(key.startswith(name + '-') for key in keys):
            digest[name] = 'named-refusal'
    if not any(key.startswith(('plant-', 'field-')) for key in keys):
        digest['field'] = 'intact'
    if not any(key.startswith('stomped') for key in keys):
        digest['commands'] = 'held'
    if not any(key.startswith(('probes-', 'step-', 'read-'))
               for key in keys):
        digest['probes'] = 'answered'
    if not any(key.startswith('role-') for key in keys):
        digest['roles'] = 'held'
    record['digest'] = dict(digest)
    record['violations'] = {key: diagnostic
                            for key, (diagnostic, _)
                            in violations.items()}
    return digest, violations, record


def scenario_dynamics_admission_refusal(ctx):
    case = Case('dynamics-admission-refusal',
                'Malformed dynamics refused by name, plant unharmed',
                'a self-point document and a document driving a '
                'controller-owned Out point are each refused by name '
                'at the preflight and spawned-load gates — never '
                'accepted, never a panic — while the serving field\'s '
                'element state and the operator\'s commanded Out '
                'values stay intact')
    launch = {}
    try:
        admit = ctx.get('admit_dynamics')
        if not callable(admit):
            return case.finish('inconclusive',
                               'the run context carries no '
                               'admit_dynamics lever — the harness '
                               'admits no doctored-document variant '
                               'seam for this leg\'s probes')
        if ctx.get('plant') is None \
                or ctx.get('plant_ctl') is None:
            return case.finish('inconclusive',
                               'the rig serves no simulated plant '
                               'endpoint for the leg\'s admission '
                               'probes')
        deadline = time.monotonic() + ADMIT_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ADMIT_POLL)
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
            return case.finish('failed', 'no peer reports '
                               'role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ADMIT_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'layout the leg restores to was '
                               'never reached')
        launch = {owner: 'active', peer: 'standby'}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        census = _census(ctx)
        if census is None:
            return case.finish('inconclusive',
                               'the serving plant answered no '
                               'census to bind the malformed '
                               'classes against')
        docs, missing = _derive_documents(census)
        if missing:
            return case.finish('inconclusive',
                               'the deployed census binds no '
                               + '; '.join(missing))
        claim = (ctx.get('plant_owner') or {}).get(owner)
        if claim is None:
            return case.finish('inconclusive',
                               'no pinned owner token for ' + owner
                               + ' — the shared-claim probes '
                               'cannot attach')
        digests = []
        for number in (1, 2):
            digest, violations, record = _admission_pass(
                ctx, number, owner, peer, launch, docs, claim)
            ref = save_evidence(
                ctx['evidence_dir'],
                'dynamics-admission-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref, 'dynamics-admission pass '
                          + str(number) + ' — the staged documents, '
                          'each admission gate\'s verdict, the '
                          'before/after field census, the commanded '
                          'Out audit, and the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations or digest is None:
                diagnostic = DIAG_FAILED \
                    if digest is None or any(
                        name == DIAG_FAILED
                        for name, _ in violations.values()) \
                    else DIAG_NONDET
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two probe passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed', 'digest '
                           + json.dumps(digests[0], sort_keys=True))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        _leave_clean(ctx, launch)
