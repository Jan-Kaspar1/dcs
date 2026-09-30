"""The persistence_path_alias_refusal acceptance leg — one module per
leg of the scenario schedule; see qa_lane/scenarios/__init__.py for
the ordering rule and the shared seam."""
from .common import *

# Ordering: the persistence-alias case runs late — after the failover
# switch, inside the unpinned window before the dcs-ctl closer: its
# doctored launches run in labeled networkless scratch containers that
# never touch the deployed pair, so the leg needs only a settled pair
# to prove non-interference against.
RUNS_AFTER = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The persistence-path distinctness startup contract (the deployed-rig,
# per-revision evidence for #1292's fix — WW-LCM-001's continuity
# clause): --state-file, --journal-file, and --history-file are three
# distinct-file declarations carrying three distinct formats — a
# write-then-rename checkpoint document, and two append-only record
# streams. The observed run aliased --state-file with --history-file:
# the launch accepted, served a healthy /history/durable whose appends
# landed on an inode the checkpoint's rename had orphaned, and
# crash-looped the next restart trying to replay checkpoint JSON as a
# history record. The fix refuses every aliased pair at startup by
# name — 'monitor persistence paths must be distinct files' carrying
# both conflicting flags and the shared path — before either sink
# opens.
#
# The leg stages each recorded pair through the run context's
# admit_persistence lever — the harness's per-run variant seam for a
# doctored launch: each probe is the run's own controller image in a
# labeled '--rm' networkless scratch container mounting a per-probe
# run-dir directory, launched on the rig's pacing shape bounded by
# --ticks so an admitted launch exits 0 with its final snapshot while
# a refused one exits at option-parse. The --state-file/--history-file
# and --state-file/--journal-file aliases must each refuse nonzero by
# name; the --journal-file/--history-file append-append alias is the
# standing contrast — every build fails it closed (the second append
# sink's writer lock refuses even where the named startup check never
# ran); and a correctly-distinct launch of the same shape must scan
# its ticks and write each sink's own file. The deployed pair never
# moves: no member launch is doctored — the probes' scratch containers
# share no file, port, or field claim with the running members, so the
# launch configuration has nothing to restore beyond the probes' own
# self-removing containers. Named diagnostics are
# persistence-alias-accepted for a contract miss — an aliased launch
# accepted or refused unnamed, the fail-closed alias accepted —
# persistence-alias-nondeterministic for unread verdicts, a refused
# distinct control, a moved or wedged pair, or two passes disagreeing,
# and persistence-alias-unchecked when the self-check's planted
# negatives slip the leg's own auditors. A rig that is unreachable or
# never settles, a run context carrying no admit_persistence lever, or
# a staged revision predating the contract — where the append-append
# alias still fails closed through the writer lock alone — reports
# inconclusive.

PERSIST_SETTLE = 30     # bound on the pair settling before the leg runs
PERSIST_POLL = 0.5      # the sampling cadence for converging reads
PERSIST_ACCEPTED = 'persistence-alias-accepted'
PERSIST_NONDET = 'persistence-alias-nondeterministic'
PERSIST_UNCHECKED = 'persistence-alias-unchecked'

# The probe set: (name, the aliased sink pair or None, the three
# sinks' names inside the probe's scratch mount). 'distinct' aliases
# nothing — the control launch proving 'accepted' is observable before
# an aliased refusal is convicted on a broken shape.
PERSIST_PROBES = (
    ('state-history', ('state_file', 'history_file'),
     {'state_file': 'shared.json', 'journal_file': 'journal.jsonl',
      'history_file': 'shared.json'}),
    ('state-journal', ('state_file', 'journal_file'),
     {'state_file': 'shared.json', 'journal_file': 'shared.json',
      'history_file': 'history.jsonl'}),
    ('journal-history', ('journal_file', 'history_file'),
     {'state_file': 'state.json', 'journal_file': 'shared.json',
      'history_file': 'shared.json'}),
    ('distinct', None,
     {'state_file': 'state.json', 'journal_file': 'journal.jsonl',
      'history_file': 'history.jsonl'}),
)
PERSIST_FLAG = {'state_file': '--state-file',
              'journal_file': '--journal-file',
              'history_file': '--history-file'}


def _persist_text(verdict):
    """A launch verdict's captured output — the refusal text lives on
    stderr, the final snapshot on stdout; the judge reads both."""
    if not isinstance(verdict, dict):
        return ''
    return str(verdict.get('stderr') or '') \
        + str(verdict.get('stdout') or '')


def _persist_shape(verdict):
    """'accepted' — exit 0 after the paced --ticks budget ran;
    'refused' — a nonzero startup exit; 'unread' — the launch never
    produced a captured verdict."""
    if not isinstance(verdict, dict):
        return 'unread'
    exit_code = verdict.get('exit')
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return 'unread'
    return 'accepted' if exit_code == 0 else 'refused'


def _persist_named(verdict, pair, paths):
    """The contract's startup refusal: a nonzero exit whose captured
    text names both conflicting flags, the file name they share, and
    the distinct-files clause."""
    if _persist_shape(verdict) != 'refused' or pair is None:
        return False
    text = _persist_text(verdict)
    shared = {paths[key] for key in pair}
    return 'distinct' in text and len(shared) == 1 \
        and next(iter(shared)) in text \
        and all(PERSIST_FLAG[key] in text for key in pair)


def _persist_scanned(verdict):
    """The distinct launch's 'scans normally' verdict: exit 0 with the
    printed final snapshot's tick proving the paced budget ran — and,
    when the scratch mount is host-visible, each declared sink holding
    its own format (the checkpoint a JSON document, each append stream
    a run_boundary-led record file)."""
    if _persist_shape(verdict) != 'accepted':
        return False
    out = str(verdict.get('stdout') or '').strip()
    try:
        snapshot = json.loads(out.splitlines()[-1]) if out else None
    except ValueError:
        snapshot = None
    if not isinstance(snapshot, dict):
        return False
    tick = snapshot.get('tick')
    if not isinstance(tick, int) or isinstance(tick, bool) \
            or tick < 1:
        return False
    directory = verdict.get('dir')
    if not isinstance(directory, str):
        return True
    declared = verdict.get('paths') or {}
    try:
        files = [Path(directory) / declared[key] for key in
                 ('state_file', 'journal_file', 'history_file')]
    except KeyError:
        return False
    if any(not path.is_file() or path.stat().st_size == 0
           for path in files):
        return False
    try:
        if not isinstance(json.loads(files[0].read_text()), dict):
            return False
        for path in files[1:]:
            _journal_entries(path)
    except (OSError, ValueError):
        return False
    return True


def _persist_probe_record(verdict, name, pair, paths):
    """One launch's normalized record — the digest and judge replay
    these fields; the raw capture is trimmed to the refusal text's
    head."""
    record = {'shape': _persist_shape(verdict),
              'exit': verdict.get('exit')
              if isinstance(verdict, dict) else None}
    text = _persist_text(verdict)
    if text:
        record['text'] = text[:240]
    if isinstance(verdict, dict) and verdict.get('argv'):
        record['argv'] = verdict['argv']
    if pair is not None:
        record['named'] = _persist_named(verdict, pair, paths)
    if name == 'distinct':
        record['scanned'] = _persist_scanned(verdict)
    return record


def _persist_contract(probes):
    """#1292's fix is present when an aliased launch refused at
    startup naming the conflicting flags — the named refusal no build
    predating the contract can produce."""
    return any((probes.get(name) or {}).get('named') is True
               for name, pair, _paths in PERSIST_PROBES
               if pair is not None)


def _persist_role(report):
    """The pass's normalized role evidence for one member — role, scan
    tick, and tracking posture — None when the endpoint dropped its
    read."""
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _persist_pair_held(record):
    """The deployed pair's undisturbed verdict: the owner still active
    and advancing its scan across the launches, the peer still a
    tracking standby — before and after alike."""
    launch = record.get('launch') or {}
    roles = record.get('roles') or {}
    owner, peer = launch.get('owner'), launch.get('peer')
    before = roles.get('before') or {}
    after = roles.get('after') or {}
    for view in (before, after):
        if (view.get(owner) or {}).get('role') != 'active':
            return False
        seen = view.get(peer) or {}
        if seen.get('role') != 'standby' \
                or seen.get('tracking') is not True:
            return False
    tick0 = (before.get(owner) or {}).get('tick')
    tick1 = (after.get(owner) or {}).get('tick')
    return isinstance(tick0, int) and not isinstance(tick0, bool) \
        and isinstance(tick1, int) and not isinstance(tick1, bool) \
        and tick1 > tick0


def _judge_persist(record, note):
    """Replay one pass's record: the standing fail-closed contrast on
    every build, then — once the contract's named refusal is present —
    each aliased launch's named nonzero refusal, the distinct
    launch's paced scans, and the deployed pair's held posture.
    note(key, diagnostic, detail) collects the violations."""
    probes = record.get('probes') or {}
    jhist = probes.get('journal-history') or {}
    if jhist.get('shape') == 'accepted':
        note('journal-history-accepted', PERSIST_ACCEPTED,
             'journal-history: the --journal-file/--history-file '
             'alias was accepted — the append-append alias must '
             'fail closed on every revision')
    elif jhist.get('shape') == 'unread':
        note('journal-history-unread', PERSIST_NONDET,
             'journal-history: the launch never produced a captured '
             'verdict — the leg cannot prove the standing refusal')
    if not _persist_contract(probes):
        return
    for name, pair, _paths in PERSIST_PROBES:
        if pair is None:
            continue
        probe = probes.get(name) or {}
        flags = ' and '.join(PERSIST_FLAG[key] for key in pair)
        if probe.get('shape') == 'unread':
            note(name + '-unread', PERSIST_NONDET,
                 name + ': the ' + flags + ' launch never produced '
                 'a captured verdict — the leg cannot prove the '
                 'refusal')
        elif probe.get('shape') == 'accepted':
            note(name + '-accepted', PERSIST_ACCEPTED,
                 name + ': ' + flags + ' aliased on one path was '
                 'accepted — the startup contract owes a named '
                 'nonzero refusal before either sink opens')
        elif probe.get('named') is not True:
            note(name + '-unnamed', PERSIST_ACCEPTED,
                 name + ': the ' + flags + ' alias exited '
                 + json.dumps(probe.get('exit')) + ' without naming '
                 'the conflicting flags and shared path — the '
                 'contract owes the named refusal')
    distinct = probes.get('distinct') or {}
    if distinct.get('shape') == 'unread':
        note('distinct-unread', PERSIST_NONDET,
             'distinct: the distinct-path control launch never '
             'produced a captured verdict')
    elif distinct.get('shape') != 'accepted' \
            or distinct.get('scanned') is not True:
        note('distinct-refused', PERSIST_NONDET,
             'distinct: the distinct-path launch of the same shape '
             'did not scan its ticks — the probe shape cannot '
             'stage a runnable controller')
    if not _persist_pair_held(record):
        note('pair-disturbed', PERSIST_NONDET,
             'the deployed pair moved or wedged across the doctored '
             'launches: ' + json.dumps(record.get('roles'),
                                       sort_keys=True))


def _persist_digest(record):
    """The pass's normalized verdict set — identical digests across
    two consecutive passes is the determinism contract."""
    probes = record.get('probes') or {}
    digest = {}
    for name, pair, _paths in PERSIST_PROBES:
        probe = probes.get(name) or {}
        if pair is None:
            digest[name] = 'scanned' \
                if probe.get('shape') == 'accepted' \
                and probe.get('scanned') is True else 'defect'
        else:
            digest[name] = 'refused-named' \
                if probe.get('shape') == 'refused' \
                and probe.get('named') is True else 'defect'
    digest['pair'] = 'held' if _persist_pair_held(record) else 'disturbed'
    return digest


def _persist_pass(ctx, number, launch):
    """One alias/refusal set: the role evidence framing, then each
    launch verdict through the admit_persistence lever, then the
    deployed pair's after posture. Returns (digest, violations,
    record): digest the normalized verdict set two passes compare;
    violations maps each clause key to (diagnostic, detail); record
    carries 'inconclusive' when the pass itself could not run."""
    record = {'pass': number, 'launch': dict(launch),
              'probes': {}, 'roles': {}}
    violations = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    admit = ctx['admit_persistence']
    owner, peer = launch['owner'], launch['peer']
    record['roles']['before'] = {
        name: _persist_role(_try_role(ctx, ctx[name]))
        for name in (owner, peer)}
    for name, pair, paths in PERSIST_PROBES:
        try:
            verdict = admit(name + '-' + str(number), paths)
        except Exception as exc:
            record['inconclusive'] = 'the persistence-alias launch ' \
                'lever never ran ' + name + ': ' + str(exc)[:200]
            return None, violations, record
        record['probes'][name] = _persist_probe_record(
            verdict, name, pair, paths)
    record['roles']['after'] = {
        name: _persist_role(_try_role(ctx, ctx[name]))
        for name in (owner, peer)}
    probes = record['probes']
    if not _persist_contract(probes):
        jhist = probes.get('journal-history') or {}
        if jhist.get('shape') == 'refused':
            record['inconclusive'] = 'the staged revision predates ' \
                'the persistence-path distinctness contract — the ' \
                'journal/history alias still fails closed through ' \
                'the append-sink writer lock rather than the named ' \
                'startup refusal'
            return None, violations, record
        if jhist.get('shape') != 'accepted':
            record['inconclusive'] = 'the journal/history probe ' \
                'never produced a captured verdict — the revision ' \
                'cannot be read against the contract'
            return None, violations, record
    _judge_persist(record, note)
    digest = _persist_digest(record)
    record['digest'] = digest
    record['violations'] = {key: diagnostic
                            for key, (diagnostic, _)
                            in violations.items()}
    return digest, violations, record


def _persist_self_check():
    """The unchecked-diagnostic guard: replay the leg's auditors over
    planted negatives — each aliased pair accepted, an unnamed or
    unread refusal, the append-append alias accepted, the distinct
    control refused or unscanned, the pair moved or wedged — and
    report every one let slip."""
    named_text = 'monitor persistence paths must be distinct files: ' \
        '--state-file and --history-file both name ' \
        '/var/lib/dcs-run/shared.json'

    def clean_record(number=1):
        named = {'shape': 'refused', 'named': True, 'exit': 2,
                 'text': named_text}
        return {'pass': number,
                'launch': {'owner': 'active', 'peer': 'standby'},
                'probes': {
                    'state-history': dict(named),
                    'state-journal': dict(named),
                    'journal-history': dict(named),
                    'distinct': {'shape': 'accepted', 'scanned': True,
                                 'exit': 0,
                                 'text': '{"tick": 5}'}},
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
                                    'tracking': True}}}}

    def audit(record):
        found = []
        _judge_persist(record,
                     lambda key, diagnostic, detail: found.append(key))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    negatives = [
        ('state-history-accepted',
         lambda r: r['probes']['state-history'].update(
             shape='accepted', named=False)),
        ('state-journal-accepted',
         lambda r: r['probes']['state-journal'].update(
             shape='accepted', named=False)),
        ('journal-history-accepted',
         lambda r: r['probes']['journal-history'].update(
             shape='accepted', named=False)),
        ('distinct-refused',
         lambda r: r['probes']['distinct'].update(
             shape='refused', scanned=False, exit=1)),
        ('distinct-unscanned',
         lambda r: r['probes']['distinct'].update(scanned=False)),
        ('state-history-unnamed',
         lambda r: r['probes']['state-history'].update(named=False)),
        ('state-journal-unnamed',
         lambda r: r['probes']['state-journal'].update(named=False)),
        ('journal-history-unnamed',
         lambda r: r['probes']['journal-history'].update(named=False)),
        ('state-history-unread',
         lambda r: r['probes']['state-history'].update(
             shape='unread', named=False)),
        ('distinct-unread',
         lambda r: r['probes']['distinct'].update(
             shape='unread', scanned=False)),
        ('pair-moved',
         lambda r: r['roles']['after']['standby'].update(
             role='active', tracking=False)),
        ('pair-tracking-lost',
         lambda r: r['roles']['after']['standby'].update(
             tracking=False)),
        ('pair-wedged',
         lambda r: r['roles']['after']['active'].update(tick=10)),
        ('pair-unread',
         lambda r: r['roles']['after'].update(standby=None)),
    ]
    for name, doctor in negatives:
        record = clean_record()
        doctor(record)
        if not audit(record):
            slipped.append(name)
    return slipped


def scenario_persistence_path_alias_refusal(ctx):
    case = Case('persistence-path-alias-refusal',
                'Persistence-path aliases refused by name at startup',
                'a labeled launch aliasing --state-file with '
                '--history-file or --journal-file exits nonzero '
                'naming the conflicting flags and shared path; the '
                '--journal-file/--history-file append-append alias '
                'stays fail-closed; the same launch shape with three '
                'distinct paths scans normally; and the deployed '
                'pair\'s roles and scan never move')
    try:
        if not callable(ctx.get('admit_persistence')):
            return case.finish('inconclusive',
                               'the run context carries no '
                               'admit_persistence lever — the harness '
                               'admits no doctored-launch variant '
                               'seam for this leg\'s probes')
        deadline = time.monotonic() + PERSIST_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=PERSIST_POLL)
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
                    interval=PERSIST_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settled '
                               'posture the leg proves undisturbed '
                               'was never reached')
        launch = {'owner': owner, 'peer': peer}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + ' ('
                     + ctx[peer] + ')')
        digests = []
        for number in (1, 2):
            digest, violations, record = _persist_pass(
                ctx, number, launch)
            ref = save_evidence(
                ctx['evidence_dir'],
                'persistence-alias-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref, 'persistence-alias pass '
                          + str(number) + ' — each doctored '
                          'launch\'s verdict, the before/after pair '
                          'roles, and the normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations or digest is None:
                diagnostic = PERSIST_ACCEPTED \
                    if digest is None or any(
                        name == PERSIST_ACCEPTED
                        for name, _ in violations.values()) \
                    else PERSIST_NONDET
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', PERSIST_NONDET + ': the two passes\' '
                'digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two launch passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _persist_self_check()
        if slipped:
            return case.finish('failed', PERSIST_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed', 'digest '
                           + json.dumps(digests[0], sort_keys=True))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
