"""The dcs_ctl acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The dcs-ctl case closes the schedule: it observes the post-failover
# role layout and perturbs nothing earlier cases established.
RUNS_AFTER = frozenset({'scenario_failover'})
RUNS_LAST = True


def _ctl_write_legs(ctx, case, ctl, signals, schema, roles):
    """The dcs-ctl case's mutation legs over the closure's ctl: the
    named rejections (a write to a non-writable point, a command aimed
    at the standby peer), a receipted write, a descriptor-bounded
    set-parameter, force and unforce, promote/demote through the CLI,
    and the driven scan probe. `roles` maps each ctx endpoint key to
    the role string the case's role reads already established. Returns
    (outcome, detail, legs) — legs carries each leg's argv, exit,
    printed answer, and journaled settlement for the per-leg evidence
    files."""
    active = next((name for name, role in roles.items()
                   if role == 'active'), None)
    if active is None:
        return 'inconclusive', 'no peer reports role=active', {}
    standby = next((name for name, role in roles.items()
                    if role == 'standby'), None)
    base = ctx[active]
    target = reject = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            target = entry.get('point')
        elif reject is None and not entry.get('writable'):
            reject = entry
    if target is None:
        return 'inconclusive', 'the rig model declares no writable ' \
            'bool p101-oos point for the mutation legs', {}
    if reject is None:
        return 'inconclusive', 'the rig model declares no ' \
            'non-writable point for the named-rejection leg', {}
    rc0, baseline, _e0 = ctl(base, 'snapshot')
    if rc0 != 0 or not isinstance(baseline, dict):
        return 'inconclusive', 'dcs-ctl snapshot failed ahead of the ' \
            'mutation legs', {}
    held = _point_value(baseline, target)
    if not isinstance(held, bool):
        return 'inconclusive', 'the write target holds no bool ' \
            'served baseline: ' \
            + json.dumps(_point_sample(baseline, target))[:300], {}
    written = not held
    case.observe('mutation legs on ' + active + ' (' + base + '): '
                 'target point ' + str(target) + ' holds '
                 + str(held) + ' in the served snapshot')
    tuned = _float_tune_plan(schema, baseline)
    if tuned is None:
        return 'inconclusive', 'the served registry offers no ' \
            'descriptor-declared Float parameter the tune leg can ' \
            'exercise', {}
    tune_component, tune_name, tune_current, tune_value, _outside = tuned
    legs = {}
    seq = {'n': 0}
    pre_roles = dict(roles)

    def ctl_legged(base_url, *args):
        """One receipted submission under this leg's own actor — the
        per-leg `--actor` the journaled settlement must echo."""
        seq['n'] += 1
        actor = CTL_ACTOR + '-leg-' + str(seq['n'])
        rc, body, err = ctl(base_url, *args, '--actor', actor)
        return rc, body, err, actor

    def admitted(receipt, actor):
        """The printed receipt's half of a submission leg: a dict with
        a non-rejected outcome echoing the declared actor."""
        outcome = (receipt or {}).get('outcome') \
            if isinstance(receipt, dict) else None
        return isinstance(outcome, dict) and bool(outcome) \
            and 'rejected' not in outcome \
            and receipt.get('actor') == actor

    def journal_since(s):
        rc, entries, _err = ctl(base, 'journal', '--since', str(s))
        return entries if rc == 0 and isinstance(entries, list) else None

    def snap_of(name):
        rc, snap, _err = ctl(ctx[name], 'snapshot')
        return snap if rc == 0 and isinstance(snap, dict) else None

    def wait_snap(name, pred):
        def check():
            snap = snap_of(name)
            return snap if snap is not None and pred(snap) else None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def wait_role(name, role):
        def check():
            rc, report, _err = ctl(ctx[name], 'role')
            if rc == 0 and isinstance(report, dict) \
                    and report.get('role') == role:
                return report
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def journaled_role(name, to):
        """The `role_changed` journal entry naming `to` on `name`'s
        own journal — each peer audits its own transitions."""
        def check():
            rc, journal, _err = ctl(ctx[name], 'journal',
                                    '--since', '0')
            if rc != 0 or not isinstance(journal, list):
                return None
            for item in journal:
                changed = (((item or {}).get('event') or {})
                           .get('role_changed') or {})
                if changed.get('to') == to:
                    return item
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def wait_settled(command, actor):
        """The journaled CommandSettled receipt for this leg's command
        under this leg's actor — actor-matched like the invoke leg's
        read so an earlier leg's identical command or a checkpoint's
        carryover echo can never stand in for this submission."""
        def check():
            entries = journal_since(floor)
            if entries is None:
                return None
            for item in entries:
                receipt = (((item or {}).get('event') or {})
                           .get('command_settled') or {}) \
                          .get('receipt') or {}
                if receipt.get('command') == command \
                        and receipt.get('actor') == actor:
                    return receipt
            return None
        return wait_for(check, time.monotonic() + CTL_DEADLINE)

    def submitted(leg_name, argv, command):
        """The shared assertion head of a receipted leg: nonzero or a
        non-admitted receipt fails naming the leg; the journaled
        applied echo under the leg's actor is recorded beside the
        printed receipt. Returns the failure detail or None."""
        rc, receipt, err, actor = ctl_legged(base, *argv)
        legs[leg_name] = {'argv': list(argv) + ['--actor', actor],
                          'exit': rc, 'receipt': receipt,
                          'stderr': err, 'actor': actor}
        if rc != 0 or not admitted(receipt, actor):
            return 'the ' + leg_name + ' leg was not admitted under ' \
                'its declared actor: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200]
        settle = wait_settled(command, actor)
        legs[leg_name]['settled'] = settle
        if not isinstance(settle, dict) \
                or 'applied' not in (settle.get('outcome') or {}):
            return 'the served journal never settled the ' \
                + leg_name + ' leg applied under ' + actor
        return None

    state = {'wrote': False, 'tuned': False, 'forced': False}
    restored = {'done': False}

    def restore():
        """Best-effort return to the pre-scenario plant: roles first —
        the value, parameter, and force reverts need `base` owning the
        field again — each under the run's restore actor so the audit
        trail tells a revert from a leg."""
        if restored['done']:
            return
        restored['done'] = True
        try:
            if pre_roles:
                settled_roles = {}
                for name in pre_roles:
                    rc, report, _err = ctl(ctx[name], 'role')
                    settled_roles[name] = report.get('role') \
                        if rc == 0 and isinstance(report, dict) \
                        else None
                want = next((name for name, role in pre_roles.items()
                             if role == 'active'), None)
                holder = next((name for name, role
                               in settled_roles.items()
                               if role == 'active'), None)
                if holder is not None and holder != want:
                    ctl(ctx[holder], 'demote')
                    wait_role(holder, 'standby')
                if want is not None \
                        and settled_roles.get(want) != 'active':
                    ctl(ctx[want], 'promote')
                    wait_role(want, 'active')
            if state['forced']:
                ctl(base, 'unforce', str(target), '--actor',
                    CTL_ACTOR + '-restore')
            if state['tuned']:
                ctl(base, 'set-parameter', str(tune_component),
                    str(tune_name), repr(tune_current), '--actor',
                    CTL_ACTOR + '-restore')
            if state['wrote']:
                ctl(base, 'write', str(target),
                    'true' if held else 'false', '--actor',
                    CTL_ACTOR + '-restore')
        except Exception:
            pass

    try:
        rc, journal, err = ctl(base, 'journal', '--since', '0')
        if rc != 0 or not isinstance(journal, list):
            return 'inconclusive', 'dcs-ctl journal failed ahead of ' \
                'the mutation legs: exit ' + str(rc) + ' ' \
                + str(err)[:200], legs
        floor = max((item.get('seq') or 0 for item in journal
                     if isinstance(item, dict)), default=0)

        # The named rejections lead: both are refused at admission, so
        # they can run ahead of the mutating legs without perturbing
        # them. A write to a non-writable point names not_writable; the
        # same writable write aimed at the tracking peer names
        # not_active — and must leave the served point untouched.
        literal = {'bool': 'true', 'int': '1',
                   'float': '1.0'}.get(reject.get('value_type'), '1.0')
        rc, receipt, err, actor = ctl_legged(
            base, 'write', str(reject.get('point')), literal)
        legs['write-nonwritable'] = {
            'argv': ['write', str(reject.get('point')), literal,
                     '--actor', actor],
            'exit': rc, 'receipt': receipt, 'stderr': err,
            'actor': actor, 'point': reject.get('point')}
        if rc == 0 or _outcome_key(receipt) \
                != 'rejected:not_writable' \
                or 'not_writable' not in str(err):
            return 'failed', 'the write to a non-writable point was ' \
                'not refused by name: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200], legs
        case.observe('non-writable write refused by name: '
                     'rejected:not_writable')

        if standby is None:
            return 'inconclusive', 'the pair layout offers no ' \
                'standby peer for the directed-rejection leg', legs
        rc, receipt, err, actor = ctl_legged(
            ctx[standby], 'write', str(target),
            'true' if written else 'false')
        legs['standby-directed'] = {
            'argv': ['write', str(target),
                     'true' if written else 'false', '--actor', actor],
            'exit': rc, 'receipt': receipt, 'stderr': err,
            'actor': actor, 'peer': standby}
        if rc == 0 or _outcome_key(receipt) != 'rejected:not_active' \
                or 'not_active' not in str(err):
            return 'failed', 'the command aimed at the standby peer ' \
                'was not refused by name: exit ' + str(rc) + ' ' \
                + json.dumps(receipt)[:300] + ' ' + str(err)[:200], legs
        snap = snap_of(active)
        if snap is None or _point_value(snap, target) != held:
            return 'failed', 'the standby-directed write reached ' \
                'the served point: ' \
                + json.dumps(_point_sample(snap or {}, target))[:300], \
                legs
        case.observe('standby-directed write refused by name: '
                     'rejected:not_active; the served point still '
                     'holds ' + str(held))

        failure = submitted(
            'write', ['write', str(target),
                      'true' if written else 'false'],
            {'write_value': {'point': target, 'kind': 'bool',
                             'value': {'bool': written}}})
        if failure:
            return 'failed', failure, legs
        snap = wait_snap(active, lambda s: _point_value(s, target)
                         == written)
        if snap is None:
            return 'failed', 'the written value never surfaced in ' \
                'the served snapshot', legs
        state['wrote'] = True
        case.observe('write leg: admitted under '
                     + legs['write']['actor'] + ', journaled applied, '
                     'served snapshot now holds ' + str(written))

        failure = submitted(
            'set-parameter',
            ['set-parameter', str(tune_component), str(tune_name),
             repr(tune_value)],
            {'set_parameter': {'component': tune_component,
                               'name': tune_name,
                               'value': {'float': tune_value}}})
        if failure:
            return 'failed', failure, legs
        snap = wait_snap(active, lambda s: _parameter_value(
            s, tune_component, tune_name) == tune_value)
        if snap is None:
            return 'failed', 'the tuned value never surfaced in ' \
                'the served parameter report', legs
        state['tuned'] = True
        case.observe('set-parameter leg: ' + str(tune_component)
                     + '.' + str(tune_name) + ' -> '
                     + repr(tune_value) + ' applied, journaled, '
                     'and served')

        failure = submitted(
            'force', ['force', str(target),
                      'true' if written else 'false'],
            {'force_point': {'point': target, 'kind': 'bool',
                             'value': {'bool': written}}})
        if failure:
            return 'failed', failure, legs

        def forced(s):
            badge = _forced_entry(s, target)
            return badge is not None \
                and badge.get('value') == {'bool': written} \
                and _point_value(s, target) == written \
                and _point_quality(s, target) \
                == {'uncertain': 'substituted'}

        if wait_snap(active, forced) is None:
            return 'failed', 'the forced point never served the ' \
                'substituted badge and value', legs
        state['forced'] = True
        case.observe('force leg: point ' + str(target)
                     + ' badged under snapshot.forces at '
                     'Uncertain(Substituted) holding ' + str(written))

        failure = submitted('unforce', ['unforce', str(target)],
                            {'unforce_point': {'point': target}})
        if failure:
            return 'failed', failure, legs

        def released(s):
            return _forced_entry(s, target) is None \
                and _point_quality(s, target) == 'good'

        if wait_snap(active, released) is None:
            return 'failed', 'the forces badge never cleared ' \
                'after the release', legs
        state['forced'] = False
        case.observe('unforce leg: badge cleared, the held value '
                     're-stamped Good')

        # The switch legs: demote the field owner and promote the
        # converged tracking peer through the binary's switch verbs —
        # no --actor, per the shipped contract — each answering its
        # transitional RoleReport, settling through GET /role, and
        # journaling its own role_changed audit entries.
        rc, report, err = ctl(ctx[active], 'demote')
        legs['demote'] = {'argv': ['demote'], 'exit': rc,
                          'report': report, 'stderr': err}
        if rc != 0 or not isinstance(report, dict) \
                or report.get('role') not in ('demoting', 'standby'):
            return 'failed', 'the demote leg did not answer a ' \
                'transitional RoleReport: exit ' + str(rc) + ' ' \
                + json.dumps(report)[:300] + ' ' + str(err)[:200], legs
        legs['demote']['settled'] = wait_role(active, 'standby')
        if legs['demote']['settled'] is None:
            return 'failed', 'the demoted peer never settled ' \
                'standby through GET /role', legs
        case.observe('demote leg: transitional report '
                     + json.dumps(report.get('role'))
                     + ', peer settled standby')
        if standby is None:
            return 'inconclusive', 'the pair layout offers no ' \
                'peer to promote', legs
        rc, report, err = ctl(ctx[standby], 'promote')
        legs['promote'] = {'argv': ['promote'], 'exit': rc,
                           'report': report, 'stderr': err}
        if rc != 0 or not isinstance(report, dict) \
                or report.get('role') not in ('promoting', 'active'):
            return 'failed', 'the promote leg did not answer a ' \
                'transitional RoleReport: exit ' + str(rc) + ' ' \
                + json.dumps(report)[:300] + ' ' + str(err)[:200], legs
        legs['promote']['settled'] = wait_role(standby, 'active')
        if legs['promote']['settled'] is None:
            return 'failed', 'the promoted peer never settled ' \
                'active through GET /role', legs
        legs['demote']['role_changed'] = journaled_role(
            active, 'standby')
        legs['promote']['role_changed'] = journaled_role(
            standby, 'active')
        if legs['demote']['role_changed'] is None \
                or legs['promote']['role_changed'] is None:
            return 'failed', 'the switch legs\' role_changed ' \
                'transitions never journaled on their own peers', legs
        case.observe('switch legs: demote and promote settled '
                     'through the CLI; each peer journaled its '
                     'role_changed transition')

        # The driven-scan probe: only an externally driven instance
        # accepts POST /scan — a paced monitor answers the named
        # refusal, which is this rig's honest scan-leg evidence. The
        # run's dedicated driven peer is the natural target when the
        # context names one; otherwise the post-switch active stands
        # in and a paced refusal records the skip.
        scan_base = ctx.get('driven') or ctx[standby]
        rc, snap, err = ctl(scan_base, 'scan', '1')
        legs['scan'] = {'argv': ['scan', '1'], 'exit': rc,
                        'answer': snap, 'stderr': err,
                        'peer': scan_base}
        if rc != 0 and 'paced' in str(err):
            legs['scan']['skipped'] = 'paced monitor — the driven ' \
                'scan refused by name'
            case.observe('scan leg: paced monitor refused by name — '
                         + str(err)[:200])
        elif rc != 0:
            return 'inconclusive', 'the scan leg failed: exit ' \
                + str(rc) + ' ' + json.dumps(snap)[:300] + ' ' \
                + str(err)[:200], legs
        elif not isinstance(snap, dict):
            return 'inconclusive', 'the scan leg answered no ' \
                'snapshot: ' + json.dumps(snap)[:300], legs
        else:
            def served_at_scan():
                rc, body, _err = ctl(scan_base, 'snapshot')
                if rc == 0 and isinstance(body, dict) \
                        and (body.get('tick') or 0) \
                        >= (snap.get('tick') or 0):
                    return body
                return None

            served = wait_for(served_at_scan,
                              time.monotonic() + CTL_DEADLINE)
            if served is None:
                return 'failed', 'the served snapshot never reached ' \
                    'the scan leg\'s returned tick', legs
            case.observe('scan leg: driven scan returned tick '
                         + str(snap.get('tick'))
                         + ', the served snapshot reached it')
        return 'passed', 'mutation legs complete', legs
    except Exception as exc:
        return 'inconclusive', str(exc), legs
    finally:
        restore()


# --------------------------------------------------------------------
# The shipped operator CLI as an external consumer (WW-FND-004's
# replaceable-consumer contract, WW-OPS-001/002's operator-facing
# surface): the lane's one proof that the real dcs-ctl binary — not a
# test harness — reads and commands a deployed pair. The binary comes
# from the run's image build (the bounded builder's
# `cargo build -p dcs-monitor --bin dcs-ctl` beside the image
# binaries), handed to the scenario as ctx['dcs_ctl']; every asserted
# read and mutation travels through CLI invocations against the
# published monitor addresses — the whole served-resource surface,
# role/signals/schema beside resources, the keyed and per-component
# events reads, receipts, and history. The read and refusal legs
# leave the plant untouched; the mutation legs — the named
# rejections, the receipted write, descriptor-bounded tune,
# force/unforce, the CLI switch legs, and the driven scan probe —
# run in _ctl_write_legs with per-leg actors and a full restore
# before the case returns.

DCS_CTL_TIMEOUT = 20   # bound on one dcs-ctl invocation
CTL_DEADLINE = 30      # bound on the journaled-settlement wait
CTL_ACTOR = 'qa-lane-dcs-ctl'  # the --actor the invoke declares


def _ctl_addr(base):
    """A monitor base URL as dcs-ctl's `<addr>` argument — host:port."""
    return base.split('://', 1)[-1]


def _run_ctl(binary, addr, args):
    """One dcs-ctl invocation, captured — the subprocess seam the pool
    tests fake."""
    return subprocess.run([binary, addr, *args], capture_output=True,
                          text=True, timeout=DCS_CTL_TIMEOUT)


def _value_literal(value):
    """A wire `{"bool": true}`-shaped Value as dcs-ctl's `<value>` text."""
    if 'bool' in value:
        return 'true' if value['bool'] else 'false'
    if 'int' in value:
        return str(value['int'])
    return repr(value['float'])


def _ctl_command_args(command):
    """The dcs-ctl argv submitting the picked receipted-path command:
    `invoke`, `set_parameter`, and `write_value` map to the same-named
    subcommands; any other variant has no CLI spelling and returns
    None."""
    if 'invoke' in command:
        body = command['invoke']
        return ['invoke', str(body['component']), str(body['command'])] \
            + [str(name) + '=' + _value_literal(value)
               for name, value in
               (body.get('arguments') or {}).items()]
    if 'set_parameter' in command:
        body = command['set_parameter']
        return ['set-parameter', str(body['component']),
                str(body['name']), _value_literal(body['value'])]
    if 'write_value' in command:
        body = command['write_value']
        return ['write', str(body['point']), _value_literal(body['value'])]
    return None


def _emitted_match(events, command):
    """The emitted-events entry covering `command`'s settlement — or
    any kind-emitted event — out of one component's attributed list:
    the produced-event proof both `events` read shapes owe."""
    for entry in events or []:
        event = (entry or {}).get('event') or {}
        settled = (event.get('command_settled') or {}) \
            .get('receipt') or {}
        if settled.get('command') == command \
                or event.get('event_emitted'):
            return entry
    return None


def scenario_dcs_ctl(ctx):
    """The shipped dcs-ctl binary against the deployed pair — the
    replaceable-consumer contract exercised through the operator CLI
    rather than raw HTTP."""
    case = Case('dcs-ctl',
                'dcs-ctl consumes the served contract externally',
                'the lane-built dcs-ctl binary reports exactly one '
                'active and one standby across the pair, its schema '
                'read covers every component kind the rig model '
                'declares, the resources read serves one live record '
                'per declared component beside the picked component\'s '
                'interface-parallel entry, the command the '
                'served-interface selection logic picks settles a '
                'receipt journaled with the --actor the leg passed '
                'and listed by the receipts read, the emitted-events '
                'read — keyed across the model and per component — '
                'attributes a produced event to its component, the '
                'history read returns a declared measurement point\'s '
                'retained samples, an undeclared or unavailable '
                'invocation is refused by name — never silently '
                'accepted — and the mutation legs refuse a write to '
                'a non-writable point and a command aimed at the '
                'standby peer by name, drive a receipted write of '
                'the writable p101-oos point from the served '
                'snapshot, a range-bounded set-parameter tune, a '
                'force and unforce asserting the '
                'Uncertain(Substituted) badge and its release, the '
                'demote/promote switch legs settling through the CLI '
                'with journaled role_changed events, and a scan '
                'probe the driven mode permits — every receipted '
                'leg under its own CTL_ACTOR-prefixed actor with the '
                'changed values, parameters, forces, and role layout '
                'restored before the case returns')
    transcript = []

    def done(outcome, detail=None):
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-transcript.json', transcript)
        if not any(entry['ref'] == ref
                   for entry in case.record['evidence']):
            case.evidence('file', ref,
                          'the dcs-ctl invocation transcript')
        return case.finish(outcome, detail)

    def ctl(base, *args):
        """Run the binary; append the invocation to the transcript;
        return (exit, parsed-stdout-or-None, stderr)."""
        addr = _ctl_addr(base)
        entry = {'argv': [addr] + [str(arg) for arg in args]}
        transcript.append(entry)
        try:
            result = _run_ctl(binary, addr, entry['argv'][1:])
        except Exception as exc:
            entry['error'] = str(exc)[:300]
            return None, None, str(exc)[:300]
        entry['exit'] = result.returncode
        try:
            body = json.loads(result.stdout)
        except (TypeError, ValueError):
            body = None
            entry['stdout'] = str(result.stdout)[:300]
        stderr = str(result.stderr or '').strip()
        if result.returncode or stderr:
            entry['stderr'] = stderr[:300]
        return result.returncode, body, stderr

    try:
        binary = ctx.get('dcs_ctl')
        if binary is None:
            return done('inconclusive', 'the run context carries no '
                        'dcs-ctl binary path')
        if not Path(binary).is_file() \
                or not os.access(binary, os.X_OK):
            return done('inconclusive', 'no executable dcs-ctl at '
                        + str(binary) + ' — the documented seam '
                        '(cargo build -p dcs-monitor --bin dcs-ctl '
                        'inside the lane\'s bounded image build) '
                        'produced nothing')
        case.observe('dcs-ctl binary: ' + str(binary) + ' — built by '
                     'the run\'s image build (cargo build --release '
                     '--locked -p dcs-monitor --bin dcs-ctl)')

        # The pair must be serving before the tool's answers mean
        # anything — the same liveness gate the other post-failover
        # cases apply, so a down monitor stays a rig failure rather
        # than masquerading as a CLI defect.
        if wait_for(lambda: _settled_active(ctx),
                    time.monotonic() + 30) is None:
            return done('failed', 'no peer reports role=active')

        roles = {}
        for name in ('active', 'standby'):
            rc, body, err = ctl(ctx[name], 'role')
            if rc != 0 or not isinstance(body, dict):
                return done('failed', 'dcs-ctl role failed on ' + name
                            + ' against a serving monitor: exit '
                            + str(rc) + ' ' + str(err)[:200])
            roles[name] = body.get('role')
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-roles.json',
                            roles)
        case.evidence('file', ref, 'dcs-ctl role on both endpoints')
        if sorted(str(role) for role in roles.values()) \
                != ['active', 'standby']:
            return done('failed', 'the post-failover pair is not one '
                        'active plus one standby: '
                        + json.dumps(roles, sort_keys=True))
        active = next(name for name in roles if roles[name] == 'active')
        base = ctx[active]
        case.observe('post-failover layout per dcs-ctl: '
                     + json.dumps(roles, sort_keys=True))

        rc, signals, err = ctl(base, 'signals')
        if rc != 0 or not isinstance(signals, dict):
            return done('failed', 'dcs-ctl signals failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        rc, schema, err = ctl(base, 'schema')
        if rc != 0 or not isinstance(schema, dict):
            return done('failed', 'dcs-ctl schema failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-schema.json',
                            {'signals': signals, 'schema': schema})
        case.evidence('file', ref, 'the CLI-printed signal index and '
                      'interface registry')
        declared = signals.get('components') or []
        if not declared:
            return done('inconclusive', 'the signal index serves no '
                        'component records to check coverage against')
        served = {}
        for entry in schema.get('interfaces') or []:
            if isinstance(entry, dict):
                served[entry.get('name')] = entry.get('interface') or {}
        missing = [record for record in declared
                   if (served.get(record.get('name')) or {}).get('kind')
                   != record.get('kind')]
        if missing:
            return done(
                'failed', 'the schema read misses declared kinds '
                + ', '.join(sorted({str(r.get('kind'))
                                    for r in missing}))
                + ' (instances: '
                + ', '.join(str(r.get('name')) for r in missing[:8])
                + ')')
        kinds = sorted({str(record.get('kind')) for record in declared})
        case.observe('schema read covers ' + str(len(declared))
                     + ' declared instances across '
                     + str(len(kinds)) + ' kinds ('
                     + ', '.join(kinds) + ')')
        declared_kinds = {str(record.get('name')): record.get('kind')
                          for record in declared}

        # The whole-model resources read: the served ResourceView —
        # one live record per declared component, each kind-matched to
        # the index's declaration.
        rc, resources, err = ctl(base, 'resources')
        if rc != 0 or not isinstance(resources, dict):
            return done('failed', 'dcs-ctl resources failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        live = {}
        for record in resources.get('components') or []:
            if isinstance(record, dict) and record.get('name'):
                live[str(record['name'])] = record
        mismatch = [name for name in sorted(declared_kinds)
                    if (live.get(name) or {}).get('kind')
                    != declared_kinds[name]]
        if sorted(live) != sorted(declared_kinds) or mismatch:
            return done(
                'failed', 'the resources read does not answer one '
                'kind-matched record per declared component: served '
                + ', '.join(sorted(live)[:8]) + ' against '
                + str(len(declared)) + ' declared'
                + (('; mismatched kinds: ' + ', '.join(mismatch[:8]))
                   if mismatch else ''))
        case.observe('resources read serves one live record per '
                     'declared component (' + str(len(live)) + ')')

        picked = _pick_declared_command(schema.get('interfaces') or [],
                                        signals)
        if picked is None:
            return done('inconclusive', 'no served command translates '
                        'to the receipted path')
        component, spec, command = picked
        argv = _ctl_command_args(command)
        if argv is None:
            return done('inconclusive', 'the picked command has no '
                        'dcs-ctl spelling: ' + json.dumps(command))
        case.observe('picked command: ' + str(component) + ' '
                     + str(spec.get('name')) + ' -> dcs-ctl '
                     + ' '.join(argv) + ' --actor ' + CTL_ACTOR)

        # The named-component resources read: the picked instance's
        # ComponentResources entry — name- and kind-matched, its live
        # collections parallel to the interface's declared ones.
        rc, res_entry, err = ctl(base, 'resources', component)
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-resources.json',
                            {'view': resources, 'component': component,
                             'entry': res_entry})
        case.evidence('file', ref, 'the ResourceView and '
                      + str(component) + '\'s entry')
        if rc != 0 or not isinstance(res_entry, dict):
            return done('failed', 'dcs-ctl resources ' + str(component)
                        + ' failed: exit ' + str(rc) + ' '
                        + str(err)[:200])
        interface = served.get(component) or {}
        collections = ('measurements', 'configuration', 'state',
                       'commands', 'events')
        absent = [name for name in collections
                  if not isinstance(res_entry.get(name), list)]
        short = [name for name in collections[:-1]
                 if isinstance(res_entry.get(name), list)
                 and len(res_entry[name])
                 != len(interface.get(name) or [])]
        if res_entry.get('name') != component \
                or res_entry.get('kind') != interface.get('kind') \
                or absent or short:
            return done('failed', 'the resources entry for '
                        + str(component) + ' does not mirror the '
                        'served interface: name='
                        + str(res_entry.get('name')) + ' kind='
                        + str(res_entry.get('kind')) + ' absent='
                        + json.dumps(absent) + ' non-parallel='
                        + json.dumps(short))
        case.observe('resources entry for ' + str(component)
                     + ' mirrors the interface\'s collections')

        # The journal cursor ahead of the submission: earlier legs
        # already settled identical commands into the ring — the
        # served-interface case picks from the same selection logic
        # and submits under its own actor — so the settlement read
        # below starts above the high-water seq and never matches a
        # prior leg's entry.
        rc, prior, err = ctl(base, 'journal', '--since', '0')
        if rc != 0 or not isinstance(prior, list):
            return done('failed', 'dcs-ctl journal failed ahead of the '
                        'submission: exit ' + str(rc) + ' '
                        + str(err)[:200])
        floor = max((entry.get('seq') or 0
                     for entry in prior if isinstance(entry, dict)),
                    default=0)

        rc, receipt, err = ctl(base, *argv, '--actor', CTL_ACTOR)
        ref = save_evidence(
            ctx['evidence_dir'], 'dcs-ctl-invoke.json',
            {'argv': argv + ['--actor', CTL_ACTOR], 'exit': rc,
             'receipt': receipt, 'stderr': err})
        case.evidence('file', ref, 'the command\'s printed receipt')
        outcome = receipt.get('outcome') if isinstance(receipt, dict) \
            else None
        if not isinstance(receipt, dict) \
                or receipt.get('command') != command \
                or not isinstance(outcome, dict) or not outcome:
            return done('failed', 'the command returned no settled '
                        'receipt: exit ' + str(rc) + ' '
                        + json.dumps(receipt)[:300] + ' '
                        + str(err)[:200])
        if receipt.get('actor') != CTL_ACTOR:
            return done('failed', 'the printed receipt dropped the '
                        'declared --actor: actor='
                        + json.dumps(receipt.get('actor')))
        case.observe('receipt outcome: '
                     + json.dumps(outcome, sort_keys=True))

        # The attributed CommandSettled in the served journal, read
        # through `dcs-ctl journal` — GET /journal through the shipped
        # consumer — above the pre-submission cursor. The cursor alone
        # cannot name this leg's settlement: a checkpoint-adopted
        # receipt re-journals on the observing peer — the pair's one
        # command audit trail — so an earlier leg's identical command
        # under its own actor can land above the floor, as the lenovo
        # run's actor="qa-lane" carryover did. The receipt identity is
        # the match: only this leg declares CTL_ACTOR, so a settled
        # entry carrying it for this command is this submission's
        # echo — while a same-command entry under a foreign actor is
        # recorded for the failure detail, not matched.
        observed = {}

        def journaled():
            rc, journal, _err = ctl(base, 'journal', '--since',
                                    str(floor))
            if rc != 0 or not isinstance(journal, list):
                return None
            observed['journal_len'] = len(journal)
            for entry in journal:
                settled = (((entry or {}).get('event') or {})
                           .get('command_settled') or {}) \
                           .get('receipt') or {}
                if settled.get('command') != command:
                    continue
                settled_outcome = settled.get('outcome') or {}
                if 'applied' not in settled_outcome \
                        and 'rejected' not in settled_outcome:
                    continue
                if settled.get('actor') == CTL_ACTOR:
                    observed['entry'] = entry
                    return True
                observed.setdefault('foreign', entry)
            return None

        covered = wait_for(journaled,
                           time.monotonic() + CTL_DEADLINE)
        ref = save_evidence(
            ctx['evidence_dir'], 'dcs-ctl-journal.json',
            {'entry': observed.get('entry'),
             'foreign': observed.get('foreign'),
             'journal_len': observed.get('journal_len')})
        case.evidence('file', ref, 'the CLI-read journal covering the '
                      'command\'s settlement')
        if not covered:
            foreign = (((observed.get('foreign') or {})
                        .get('event') or {})
                       .get('command_settled') or {}) \
                       .get('receipt') or {}
            if foreign:
                return done('failed', 'the journaled receipt is '
                            'unattributed: actor='
                            + json.dumps(foreign.get('actor')))
            return done('failed', 'the served journal never recorded '
                        'the command\'s CommandSettled')
        settled = (observed['entry'].get('event') or {}) \
            .get('command_settled', {}).get('receipt') or {}
        if settled.get('actor') != CTL_ACTOR:
            return done('failed', 'the journaled receipt is '
                        'unattributed: actor='
                        + json.dumps(settled.get('actor')))
        case.observe('journal carries the settled receipt attributed '
                     'to ' + CTL_ACTOR)

        # The emitted-events reads: the produced event — the command's
        # settled receipt — attributed to its component, both in the
        # keyed whole-model view and the per-component list.
        rc_all, keyed, err_all = ctl(base, 'events')
        rc, events, err = ctl(base, 'events', component)
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-events.json',
                            {'component': component,
                             'keyed': {'exit': rc_all, 'events': keyed},
                             'exit': rc, 'events': events})
        case.evidence('file', ref, 'the emitted-events reads — keyed '
                      'across the model and for ' + str(component))
        if rc_all != 0 or not isinstance(keyed, dict):
            return done('failed', 'dcs-ctl events failed: exit '
                        + str(rc_all) + ' ' + str(err_all)[:200])
        if rc != 0 or not isinstance(events, list):
            return done('failed', 'dcs-ctl events failed for '
                        + str(component) + ': exit ' + str(rc) + ' '
                        + str(err)[:200])
        unkeyed = [name for name in sorted(declared_kinds)
                   if not isinstance(keyed.get(name), list)]
        if unkeyed:
            return done('failed', 'the keyed events read serves no '
                        'attributed list for '
                        + ', '.join(unkeyed[:8]))
        match = _emitted_match(events, command)
        if match is None:
            return done('failed', 'the emitted-events read attributes '
                        'no produced event to ' + str(component))
        if _emitted_match(keyed.get(component), command) is None:
            return done('failed', 'the keyed events read attributes '
                        'no produced event to ' + str(component))
        case.observe('events read attributes '
                     + next(iter(match.get('event') or {}), '?')
                     + ' to ' + str(component))

        # The settled-command read: the receipt log carries this leg's
        # attributed invoke — the command audit's listing half beside
        # the journal's durable record.
        rc, receipts, err = ctl(base, 'receipts')
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-receipts.json',
                            {'exit': rc, 'receipts': receipts})
        case.evidence('file', ref, 'the settled-command receipt list')
        if rc != 0 or not isinstance(receipts, list):
            return done('failed', 'dcs-ctl receipts failed: exit '
                        + str(rc) + ' ' + str(err)[:200])
        own = next((entry for entry in receipts
                    if isinstance(entry, dict)
                    and entry.get('command') == command
                    and entry.get('actor') == CTL_ACTOR), None)
        if own is None:
            return done('failed', 'the receipt log never recorded the '
                        'leg\'s attributed invoke')
        outcome = own.get('outcome') or {}
        if 'applied' not in outcome and 'rejected' not in outcome:
            return done('failed', 'the attributed invoke\'s receipt '
                        'never settled: ' + json.dumps(outcome)[:200])
        case.observe('receipts carries the attributed invoke settled '
                     + next(iter(outcome)))

        # The retained-samples read on a declared measurement point —
        # the picked component's bound measurement first, else any
        # served instance's.
        history_point = None
        ordered = sorted(
            schema.get('interfaces') or [],
            key=lambda entry: entry.get('name') != component)
        for entry in ordered:
            for measurement in ((entry.get('interface') or {})
                                .get('measurements') or []):
                if isinstance(measurement, dict) \
                        and measurement.get('point') is not None:
                    history_point = measurement['point']
                    break
            if history_point is not None:
                break
        if history_point is None:
            return done('inconclusive', 'no served measurement binds a '
                        'point for the history read')
        rc, history, err = ctl(base, 'history', '--point',
                               str(history_point))
        ref = save_evidence(ctx['evidence_dir'], 'dcs-ctl-history.json',
                            {'point': history_point, 'exit': rc,
                             'history': history})
        case.evidence('file', ref, 'retained samples for declared '
                      'point ' + str(history_point))
        if rc != 0 or not isinstance(history, list):
            return done('failed', 'dcs-ctl history failed for point '
                        + str(history_point) + ': exit ' + str(rc)
                        + ' ' + str(err)[:200])
        record = next((item for item in history
                       if isinstance(item, dict)
                       and item.get('point') == history_point), None)
        if record is None:
            return done('failed', 'the history read serves no record '
                        'for declared point ' + str(history_point))
        samples = record.get('samples')
        if not isinstance(samples, list) or not samples:
            return done('failed', 'declared point ' + str(history_point)
                        + ' retains no served samples')
        bad = [item for item in samples
               if not isinstance(item, dict)
               or not isinstance(item.get('seq'), int)
               or not isinstance(item.get('sample'), dict)]
        if bad:
            return done('failed', 'the history read serves malformed '
                        'samples: ' + json.dumps(bad[:2])[:300])
        case.observe('history retains ' + str(len(samples))
                     + ' samples for declared point '
                     + str(history_point))

        # The refusal legs: an invoke the served contract does not
        # declare, and — when the resource view advertises one — a
        # command whose availability rule currently refuses. Both must
        # answer the named refusal, never a silent accept.
        refusals = {}
        declared_names = {str(item.get('name'))
                          for item in (served.get(component) or {})
                          .get('commands') or []}
        probe = 'dcs-ctl-undeclared'
        while probe in declared_names:
            probe += '-x'
        rc, refused, err = ctl(base, 'invoke', component, probe,
                               '--actor', CTL_ACTOR)
        refusals['undeclared'] = {
            'argv': ['invoke', component, probe, '--actor', CTL_ACTOR],
            'exit': rc, 'receipt': refused, 'stderr': err}

        unavailable = None
        rc, fresh, _err = ctl(base, 'resources')
        if rc != 0 or not isinstance(fresh, dict):
            fresh = {}
        for record in fresh.get('components') or []:
            interface = served.get(record.get('name')) or {}
            states = {state.get('name'): state
                      for state in record.get('commands') or []}
            for cspec in interface.get('commands') or []:
                state = states.get(cspec.get('name'))
                if not state or state.get('available') is not False:
                    continue
                submission = _command_for_spec(record.get('name'),
                                               cspec)
                un_argv = (_ctl_command_args(submission)
                           if submission else None)
                if un_argv:
                    unavailable = (record.get('name'), cspec.get('name'),
                                   un_argv, state.get('refusal'))
                    break
            if unavailable:
                break
        if unavailable:
            un_component, un_name, un_argv, advertised = unavailable
            rc, refused, err = ctl(base, *un_argv,
                                   '--actor', CTL_ACTOR)
            refusals['unavailable'] = {
                'argv': un_argv + ['--actor', CTL_ACTOR], 'exit': rc,
                'receipt': refused, 'stderr': err,
                'component': un_component, 'command': un_name,
                'advertised_refusal': advertised}
        else:
            case.observe('no unavailable command advertised; the '
                         'undeclared probe covers the refusal leg')
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-refusals.json', refusals)
        case.evidence('file', ref, 'the named refusals')

        def rejection(leg):
            """The named rejection a refusal leg answered, or None."""
            receipt = leg['receipt']
            reason = ((receipt or {}).get('outcome') or {}) \
                .get('rejected') if isinstance(receipt, dict) else None
            reason = (reason or {}).get('reason') \
                if isinstance(reason, dict) else None
            return next(iter(reason), None) \
                if isinstance(reason, dict) and reason else None

        undeclared = refusals['undeclared']
        if undeclared['exit'] == 0 \
                or rejection(undeclared) != 'unknown_command':
            return done('failed', 'the undeclared invoke was not '
                        'refused by name: exit '
                        + str(undeclared['exit']) + ' '
                        + json.dumps(undeclared['receipt'])[:300])
        case.observe('undeclared invoke refused by name: '
                     + rejection(undeclared))
        if 'unavailable' in refusals:
            if refusals['unavailable']['exit'] == 0 \
                    or rejection(refusals['unavailable']) is None:
                return done('failed', 'the contract-named unavailable '
                            'command was silently accepted: '
                            + json.dumps(refusals['unavailable'])[:300])
            case.observe('unavailable command refused by name: '
                         + rejection(refusals['unavailable']))

        outcome, detail, legs = _ctl_write_legs(ctx, case, ctl,
                                                signals, schema,
                                                roles)
        for leg_name, payload in legs.items():
            ref = save_evidence(ctx['evidence_dir'],
                                'dcs-ctl-leg-' + leg_name + '.json',
                                payload)
            case.evidence('file', ref, 'the ' + leg_name
                          + ' mutation leg through the binary')
        ref = save_evidence(ctx['evidence_dir'],
                            'dcs-ctl-write-legs.json',
                            {'outcome': outcome, 'detail': detail})
        case.evidence('file', ref, 'the mutation legs outcome')
        if outcome != 'passed':
            return done(outcome, detail)
        case.observe('mutation legs: ' + str(detail))
        return done('passed')
    except Exception as exc:
        return done('inconclusive', str(exc))
