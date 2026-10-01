"""The invoke_undeclared_arg acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the invoke-undeclared-arg case shares the post-failover
# window the other command cases use — it probes whichever endpoint
# reports settled active — and its only mutation is a receipted
# kind-declared invoke, the same admission/apply path the command
# legs already exercise, so it needs no declared window.

# --------------------------------------------------------------------
# The invoke argument-schema admission rule (#981, WW-FND-004's
# bounded validated ingress): a Command::Invoke's `arguments` are
# keyed by the declared CommandSpec.request names — a submission
# carrying a name the schema does not declare refuses at admission
# with the named `unknown_argument` CommandError rather than settle
# `applied` for a payload outside the declared request: nothing
# queued, nothing dispatched, and — the admission-refusal convention —
# the terminal rejection receipted on the wire answer, mirrored in the
# served receipt log, and journaled `command_settled` at the run's
# current tick before any scan. The schema bounds names and kinds,
# not presence: the same command submitted with a declared argument
# omitted still resolves and applies, the kind owning the
# absent-argument default. Each leg runs the probe pair twice —
# identical normalized digests are the determinism pin. Functional
# misses name invoke-undeclared-arg-failed; a served or journaled
# record disagreeing with the wire answer, a settle recorded more
# than once, or the two passes diverging names
# invoke-undeclared-arg-nondeterministic. A rig that is unreachable,
# exposes no kind-declared command, or predates the contract — the
# undeclared name admitted rather than refused — reports
# inconclusive.

INVOKE_ARG_DEADLINE = 30   # bound on each settle/journal-coverage wait
INVOKE_ARG_ACTOR = 'qa-lane-invoke-args'

# The ValueKind spellings a submission may carry — the schema's
# declared-argument vocabulary.
ARGUMENT_DEFAULTS = {'bool': {'bool': True}, 'int': {'int': 1},
                     'float': {'float': 1.0}}


def _invoke_specs(schema, resources):
    """[(component, spec, row)] — the served registry's kind-declared
    commands (CommandSpec.adapted 'declared' — the invoke surface the
    request schema bounds), each beside the /resources verdict row
    serving its availability."""
    rows = {}
    for record in (resources or {}).get('components') or []:
        for row in record.get('commands') or []:
            rows[(record.get('name'), row.get('name'))] = row
    found = []
    for entry in (schema or {}).get('interfaces') or []:
        interface = (entry or {}).get('interface') or {}
        for spec in interface.get('commands') or []:
            if spec.get('adapted') == 'declared':
                found.append((entry.get('name'), spec,
                              rows.get((entry.get('name'),
                                        spec.get('name')))))
    return found


def _invoke_probe(declared):
    """The leg's probe command out of the kind-declared specs:
    preferring one whose request schema declares an argument the
    omitted half can withhold AND whose served verdict stands
    invocable, then (component, command) order — deterministic across
    passes and reruns."""
    def rank(item):
        component, spec, row = item
        has_args = bool(spec.get('request'))
        available = isinstance(row, dict) and row.get('available') is True
        group = (0 if has_args and available else 1 if has_args
                 else 2 if available else 3)
        return group, str(component), str(spec.get('name'))
    return sorted(declared, key=rank)[0]


def _settle_view(receipt):
    """A terminal receipt's digest-stable projection — the outcome
    kind and a rejection's named-reason body (names, never ticks or
    seqs) so two passes on a moving tick domain compare equal."""
    outcome = (receipt or {}).get('outcome')
    if not isinstance(outcome, dict) or not outcome:
        return 'missing'
    name = next(iter(outcome))
    if name == 'rejected':
        body = outcome.get('rejected')
        reason = body.get('reason') if isinstance(body, dict) else None
        return {'rejected': reason}
    return name


def _invoke_settles(ctx, base, floor, command):
    """[(seq, tick, receipt)] — the journal's `command_settled`
    entries above `floor` carrying this pass's submission, matched on
    command and actor so an earlier leg's identical command can never
    stand in for it."""
    _, payload = http_json('GET', base + '/journal?since='
                           + str(floor))
    found = []
    for entry in _journal_list(payload):
        receipt = _journal_settled(entry)
        if isinstance(receipt, dict) \
                and receipt.get('command') == command \
                and receipt.get('actor') == INVOKE_ARG_ACTOR:
            found.append((entry.get('seq'), entry.get('tick'),
                          receipt))
    return found


def _invoke_arg_pass(ctx, base, probe, number):
    """One pass over the probe command: the invoke carrying an
    argument name outside the declared request schema beside its
    admission-refusal evidence, then the same command with a declared
    argument omitted. Returns (status, detail, digest, evidence) —
    'ok' carries the normalized digest the two passes compare;
    'predates' marks a build that admitted the undeclared name (the
    schema bound absent from it); 'failed'/'inconclusive' carry the
    leg's verdict."""
    component, spec, row = probe
    name = spec.get('name')
    request = [argument for argument in spec.get('request') or []
               if isinstance(argument, dict)]
    evidence = {'pass': number, 'component': component,
                'command': name, 'request': request,
                'served_availability': (row or {}).get('available'),
                'served_refusal': (row or {}).get('refusal')}
    declared = {}
    for argument in request:
        value = ARGUMENT_DEFAULTS.get(str(argument.get('kind')))
        if not isinstance(argument.get('name'), str) or value is None:
            return 'inconclusive', 'the declared request schema ' \
                'carries an argument outside the lane\'s value ' \
                'vocabulary: ' + json.dumps(argument)[:200], None, \
                evidence
        declared[argument['name']] = value
    undeclared = 'qa_undeclared'
    while undeclared in declared:
        undeclared += '_x'
    refused_arguments = dict(declared)
    refused_arguments[undeclared] = {'bool': True}
    refused_command = {'invoke': {'component': component,
                                  'command': name,
                                  'arguments': refused_arguments}}
    omitted = str(request[0].get('name')) if request else None
    applied_command = {'invoke': {'component': component,
                                  'command': name,
                                  'arguments': {
                                      key: value for key, value
                                      in declared.items()
                                      if key != omitted}}}
    evidence['undeclared_argument'] = undeclared
    evidence['omitted_argument'] = omitted

    floor = _journal_cursor(ctx, base)
    depth0 = ((_snapshot(ctx, base).get('command_queue') or {})
              .get('depth'))

    def submit(command, tag):
        index = _next_receipt_index(ctx, base)
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': command, 'actor': INVOKE_ARG_ACTOR})
        evidence[tag] = {'index': index, 'command': command,
                         'status': status, 'receipt': receipt}
        return index, receipt

    # The undeclared-argument leg: the wire answer is the admission
    # verdict — a refused submission never enters the pending queue,
    # so its receipt is terminal before it is returned.
    try:
        index, receipt = submit(refused_command, 'undeclared')
    except urllib.error.HTTPError as exc:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'undeclared-argument invoke answered HTTP ' \
            + str(exc.code) + ' — no receipted answer', None, evidence
    outcome = (receipt or {}).get('outcome') or {}
    if 'accepted' in outcome or _outcome_key(receipt) \
            == 'rejected:queue_full':
        # Admitted past the schema check — either queued outright or
        # met the bounded queue's refusal, which only a validation
        # that passed the undeclared name can reach. Both mean the
        # argument-schema rule is absent from this build.
        settled = wait_for(
            lambda: _submitted_receipt(ctx, base, index,
                                       refused_command),
            time.monotonic() + INVOKE_ARG_DEADLINE)
        evidence['undeclared']['settled'] = settled
        return 'predates', 'the undeclared-argument invoke ' \
            + undeclared + ' was admitted rather than refused — ' \
            'settled ' + _outcome_key(settled) + '; the build ' \
            'predates the invoke-admission contract', None, evidence
    if _outcome_key(receipt) != 'rejected:unknown_argument':
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'undeclared-argument invoke answered ' \
            + _outcome_key(receipt) + ' — the schema-bound refusal ' \
            'is the named unknown_argument', None, evidence
    reason = (outcome.get('rejected') or {}).get('reason') or {}
    named = reason.get('unknown_argument') or {}
    if named.get('component') != component \
            or named.get('command') != name \
            or named.get('argument') != undeclared:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'unknown_argument refusal names ' \
            + json.dumps(named, sort_keys=True) + ' — the ' \
            'submission\'s offending name is ' + undeclared, \
            None, evidence

    # The refusal's served evidence: the bounded receipt log carries
    # the same terminal verdict, the queue never took it, and the
    # journal records exactly one `command_settled` echoing it.
    served = wait_for(
        lambda: _submitted_receipt(ctx, base, index,
                                   refused_command),
        time.monotonic() + INVOKE_ARG_DEADLINE)
    evidence['undeclared']['served'] = served
    if served is None:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'admission refusal never reached the served receipt ' \
            'log', None, evidence
    if _settle_view(served) != _settle_view(receipt):
        return 'failed', 'invoke-undeclared-arg-nondeterministic: ' \
            'the served receipt disagrees with the wire answer: ' \
            + json.dumps(served.get('outcome'), sort_keys=True) \
            + ' vs ' + json.dumps(outcome, sort_keys=True), \
            None, evidence
    depth1 = ((_try_snapshot(ctx, base) or {}).get('command_queue')
              or {}).get('depth')
    evidence['undeclared']['queue_depth'] = [depth0, depth1]
    if isinstance(depth0, int) and isinstance(depth1, int) \
            and not isinstance(depth0, bool) \
            and not isinstance(depth1, bool) and depth1 > depth0:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'refused submission entered the pending queue — ' \
            'command_queue.depth ' + str(depth0) + ' -> ' \
            + str(depth1), None, evidence
    settles = wait_for(
        lambda: _invoke_settles(ctx, base, floor, refused_command)
                or None,
        time.monotonic() + INVOKE_ARG_DEADLINE)
    evidence['undeclared']['journaled'] = [
        {'seq': seq, 'tick': tick, 'receipt': body}
        for seq, tick, body in (settles or [])]
    if not settles:
        return 'failed', 'invoke-undeclared-arg-failed: the served ' \
            'journal never recorded the admission refusal\'s ' \
            'command_settled', None, evidence
    if len(settles) != 1:
        return 'failed', 'invoke-undeclared-arg-nondeterministic: ' \
            'the refused submission journaled ' \
            + str(len(settles)) + ' command_settled records', \
            None, evidence
    if _settle_view(settles[0][2]) != _settle_view(receipt):
        return 'failed', 'invoke-undeclared-arg-nondeterministic: ' \
            'the journaled settle disagrees with the wire ' \
            'answer: ' + json.dumps(
                (settles[0][2] or {}).get('outcome'),
                sort_keys=True) + ' vs ' \
            + json.dumps(outcome, sort_keys=True), None, evidence

    # The omitted-argument leg: the same command with its first
    # declared argument withheld — presence is never the schema's
    # bound, so the kind's absent-argument default resolves and the
    # invoke applies at its scan boundary.
    try:
        index2, receipt2 = submit(applied_command, 'omitted')
    except urllib.error.HTTPError as exc:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'omitted-argument invoke answered HTTP ' \
            + str(exc.code) + ' — no receipted answer', None, evidence
    outcome2 = (receipt2 or {}).get('outcome') or {}
    if 'rejected' in outcome2:
        key2 = _outcome_key(receipt2)
        if key2 == 'rejected:queue_full':
            return 'inconclusive', 'the omitted-argument invoke ' \
                'met the bounded queue_full admission — the apply ' \
                'half could not run', None, evidence
        if key2 == 'rejected:not_active':
            return 'inconclusive', 'the serving peer left the ' \
                'active role mid-leg', None, evidence
        return 'failed', 'invoke-undeclared-arg-failed: a ' \
            'declared-subset invoke was refused by name at ' \
            'admission: ' + key2 + ' — the schema bounds names, ' \
            'not presence', None, evidence
    if 'accepted' not in outcome2:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'omitted-argument invoke returned no structured ' \
            'outcome: ' + json.dumps(receipt2)[:300], None, evidence
    settled2 = wait_for(
        lambda: _submitted_receipt(ctx, base, index2,
                                   applied_command),
        time.monotonic() + INVOKE_ARG_DEADLINE)
    evidence['omitted']['settled'] = settled2
    if settled2 is None:
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'omitted-argument invoke never settled a terminal ' \
            'outcome', None, evidence
    key2 = _outcome_key(settled2)
    if key2 == 'rejected:command_refused':
        if (row or {}).get('available') is True:
            return 'failed', 'invoke-undeclared-arg-failed: the ' \
                'served-available command refused the ' \
                'omitted-argument invoke at dispatch: ' \
                + json.dumps(settled2.get('outcome'),
                             sort_keys=True)[:300], None, evidence
        return 'inconclusive', 'the picked command stands ' \
            'kind-declared-refused — the apply half is ' \
            'uncovered: ' + json.dumps(settled2.get('outcome'),
                                       sort_keys=True)[:300], \
            None, evidence
    if key2 != 'applied':
        return 'failed', 'invoke-undeclared-arg-failed: the ' \
            'omitted-argument invoke settled ' + key2 \
            + ' rather than applied', None, evidence
    settles2 = wait_for(
        lambda: _invoke_settles(ctx, base, floor, applied_command)
                or None,
        time.monotonic() + INVOKE_ARG_DEADLINE)
    evidence['omitted']['journaled'] = [
        {'seq': seq, 'tick': tick, 'receipt': body}
        for seq, tick, body in (settles2 or [])]
    if not settles2:
        return 'failed', 'invoke-undeclared-arg-failed: the served ' \
            'journal never recorded the applied invoke\'s ' \
            'command_settled', None, evidence
    if len(settles2) != 1:
        return 'failed', 'invoke-undeclared-arg-nondeterministic: ' \
            'the applied submission journaled ' \
            + str(len(settles2)) + ' command_settled records', \
            None, evidence
    if _settle_view(settles2[0][2]) != 'applied':
        return 'failed', 'invoke-undeclared-arg-nondeterministic: ' \
            'the journaled settle disagrees with the applied ' \
            'outcome: ' + json.dumps(
                (settles2[0][2] or {}).get('outcome'),
                sort_keys=True), None, evidence

    digest = {'component': component, 'command': name,
              'undeclared': undeclared,
              'refusal': _settle_view(receipt),
              'refusal_served': _settle_view(served),
              'refusal_journaled': _settle_view(settles[0][2]),
              'applied': _settle_view(settled2),
              'applied_journaled': _settle_view(settles2[0][2])}
    return 'ok', None, digest, evidence


def scenario_invoke_undeclared_arg(ctx):
    """The invoke argument-schema admission rule on the deployed
    pair: an undeclared argument name refuses at admission — nothing
    queued, nothing dispatched, the refusal served and journaled —
    and the same command with a declared argument omitted resolves
    to the kind's default and applies."""
    case = Case('invoke-undeclared-arg',
                'Undeclared invoke arguments refuse at admission',
                'against the settled pair\'s serving active, a '
                'receipted invoke carrying an argument name absent '
                'from the command\'s declared request schema '
                'refuses at admission answering the named '
                'unknown_argument reason — never queued, never '
                'dispatched — the refusal observable through the '
                'served receipt and journal surfaces, and the same '
                'command submitted with a declared argument omitted '
                'resolves to the kind\'s default and settles '
                'applied; two consecutive passes produce identical '
                'digests')
    try:
        for name in ('active', 'standby', 'revised'):
            if ctx.get(name) is None:
                continue
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        base = ctx[active]
        case.observe('invoke-admission probes against ' + active
                     + ' (' + base + ')')

        bodies = {}
        for path in ('/schema', '/resources'):
            try:
                _, bodies[path] = http_json('GET', base + path)
            except urllib.error.HTTPError as exc:
                return case.finish('failed', 'GET ' + path
                                   + ' answered ' + str(exc.code))
        declared = _invoke_specs(bodies['/schema'],
                                 bodies['/resources'])
        ref = save_evidence(
            ctx['evidence_dir'],
            'invoke-undeclared-arg-schema.json',
            {'declared': [{'component': component, 'spec': spec,
                           'row': row}
                          for component, spec, row in declared]})
        case.evidence('file', ref, 'the served registry\'s '
                      'kind-declared commands beside their verdicts')
        if not declared:
            return case.finish('inconclusive', 'the served registry '
                               'exposes no kind-declared command — '
                               'the deployed model has no invoke '
                               'surface for the schema-bound leg')
        probe = _invoke_probe(declared)
        component, spec, row = probe
        case.observe('probe command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' — request '
                     + json.dumps(spec.get('request') or [])
                     + ', served availability '
                     + json.dumps((row or {}).get('available')))
        if not spec.get('request'):
            case.observe('the picked command declares no argument '
                         '— the omitted-argument half degenerates '
                         'to the bare invoke')

        digests = []
        for number in (1, 2):
            status, detail, digest, evidence = _invoke_arg_pass(
                ctx, base, probe, number)
            ref = save_evidence(
                ctx['evidence_dir'],
                'invoke-undeclared-arg-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref, 'pass ' + str(number)
                          + ' — the refused and applied '
                          'submissions with their served evidence')
            if status == 'predates':
                if not digests:
                    return case.finish('inconclusive', detail)
                return case.finish(
                    'failed', 'invoke-undeclared-arg-'
                    'nondeterministic: the identical submission '
                    'refused at admission on pass 1 was admitted '
                    'on pass 2: ' + str(detail))
            if status != 'ok':
                return case.finish(status, detail)
            digests.append(digest)
            case.observe('pass ' + str(number) + ': '
                         + json.dumps(digest, sort_keys=True))
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'invoke-undeclared-arg-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two probe passes, identical digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
