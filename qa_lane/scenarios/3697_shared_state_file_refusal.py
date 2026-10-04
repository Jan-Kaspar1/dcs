"""The shared_state_file_refusal acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the shared-state-file leg extends the persistence-path
# distinctness family the 3695 leg pins inside one launch, so it runs
# after it — the pair is settled exactly as that leg leaves it, and
# that leg's scratch probes share no file, port, or field claim with
# the pair members this one relaunches, which is what keeps the
# same-launch refusal's named verdict intact — and before the
# unclaimed-rearm leg, whose own field-claim window opens on a pair
# this leg leaves with both members on their own persistence paths.
RUNS_AFTER = frozenset({'scenario_persistence_path_alias_refusal'})
RUNS_BEFORE = frozenset({'scenario_unclaimed_rearm'})


# --------------------------------------------------------------------
# The cross-peer persistence-path distinctness refusal (the deployed
# rig, per-revision lane evidence for #1341's fix — the single-writer
# guard the `--state-file` checkpoint owes): that guard compares the
# declared paths inside one launch only, so the deployed pair had never
# been read against it. Two peers configured with the same --state-file
# — a shared volume, a copied bind-mount stanza, one member's directory
# mounted over the other's — each replace the other's checkpoint on
# every capture: silent cross-peer state destruction, since the
# write-then-rename keeps every read whole and nothing reports the
# second writer, and a restart resuming that file adopts whichever run
# renamed last as its own tick domain, receipt log, and component state
# under the model fingerprint any same-model writer matches. #1341's
# fix refuses the configuration at startup: the launch finding its
# --state-file already claimed by a live peer exits nonzero naming the
# file, the `.lock` sidecar the single-writer lock rides, and the
# conflict it found held — never two peers overwriting one checkpoint
# file.
#
# The leg stages exactly that misconfiguration on the deployed pair
# through the run context's `share_state_with` deployment doctoring —
# the runner's relaunch lever rebuilding one member's launch with its
# declared persistence directory bind-mounted onto the *peer's*, the
# member's own directory still mounted at a second path serving only
# its --journal-file/--history-file append sinks. The two peers'
# identical --state-file declarations then resolve to one backing
# checkpoint, the live peer's writer lock included, and the only
# refusal available is the cross-peer single-writer claim: never an
# append sink's own lock, never a same-launch path comparison. The
# member relaunched is the pair's non-owner, so the staged refusal
# costs the field nothing, and the live owner must read undisturbed
# throughout — still role=active, still answering every poll, its tick
# still advancing across the staged window.
#
# The claims the leg asserts: the correctly-pathed pair settles and
# tracks, and each member's own checkpoint carries its own run
# (`generation`) before the staging — the distinct-path control the
# staged alias is measured against. The aliased member's process
# verdict is the exit evidence: down, with a nonzero exit where docker
# reports one, and its captured output naming the shared checkpoint
# file, its `.lock` sidecar, and the writer-lock conflict; a launch
# that stayed up, exited 0, or refused without naming the path is a
# contract miss. And the shared checkpoint is sampled across the staged
# window: its generation must stay the owner's while the owner's tick
# keeps advancing, and the aliased member's own file must stand
# untouched — a generation that flips, or an aliased member that
# captured anywhere, is two live writers on one file. Finally the
# correctly-pathed relaunch restores the member and the pair reconverges
# to one active plus one tracking standby, so the legs behind this one
# inherit the rig this leg found. Named diagnostics are
# shared-state-file-accepted for a contract miss — an aliased launch
# that served, exited 0, refused unnamed, or left the shared checkpoint
# overwritten — shared-state-file-nondeterministic for unread verdicts,
# an unsettled or disturbed owner, an unrestored pair, or two passes
# disagreeing, and shared-state-file-unchecked when the self-check's
# planted negatives slip the leg's own auditors. A rig that is
# unreachable or never settles, a run context carrying no relaunch
# lever, no container-state probe, or no per-member --state-file path,
# and a pass whose staging never landed report inconclusive.

SHARED_SETTLE = 45       # bound on the pair settling before/after staging
SHARED_POLL = 0.4        # cadence on the pair posture waits
SHARED_WATCH = 0.5       # cadence on the staged-window samples
SHARED_WINDOW = 12       # bound on the staged window the samples span
SHARED_VERDICT = 15      # extra bound on an aliased launch that is
                         # still serving when the window closes
SHARED_SAMPLES = 3       # samples a completed staged window must carry
SHARED_ACCEPTED = 'shared-state-file-accepted'
SHARED_NONDET = 'shared-state-file-nondeterministic'
SHARED_UNCHECKED = 'shared-state-file-unchecked'


def _shared_document(path):
    """The checkpoint document `path` holds — one run's identity and
    position: its tick-domain `generation` and its `tick`. None when the
    file is absent, unreadable, or carries no run's identity, which is
    one lost sample rather than a verdict."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    generation, tick = document.get('generation'), document.get('tick')
    if not isinstance(generation, int) or isinstance(generation, bool) \
            or not isinstance(tick, int) or isinstance(tick, bool):
        return None
    return {'generation': generation, 'tick': tick}


def _shared_role(report):
    """One peer's normalized role evidence — role, scan tick, and
    tracking posture. None when the endpoint dropped its read, which the
    disturbance audits read as silence rather than as absence."""
    if not isinstance(report, dict):
        return None
    return {'role': report.get('role'), 'tick': report.get('tick'),
            'tracking': 'tracking' in (report.get('sync') or {})}


def _shared_shape(state):
    """The aliased launch's process verdict: 'running' — the launch
    served, the both-peers-one-file miss the leg exists to name;
    'down' — no live process, with a nonzero exit where docker reports
    one; 'exited' — no live process after a clean exit 0, which is
    neither a refusal nor a serving process; 'absent' — the container
    is not there to read at all; 'unread' — no captured verdict."""
    if not isinstance(state, dict):
        return 'unread'
    if state.get('absent'):
        return 'absent'
    running = state.get('running')
    if running is True:
        return 'running'
    if running is not False:
        return 'unread'
    exit_code = state.get('exit')
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) \
            and exit_code == 0:
        return 'exited'
    return 'down'


def _shared_named(state, shared_file):
    """The contract's startup refusal: the captured output names the
    shared checkpoint, the `.lock` sidecar the single-writer lock rides
    (a sibling the write-then-rename never replaces, so the lock stays
    attached to the path across every save), and the writer-lock
    conflict the launch found already held."""
    if not isinstance(state, dict) or not isinstance(shared_file, str) \
            or not shared_file:
        return False
    text = str(state.get('logs') or '')
    return shared_file in text \
        and shared_file + '.lock' in text \
        and 'writer lock' in text


def _shared_control(record):
    """The correctly-pathed pair the leg stages against: the two
    members' declared --state-file paths distinct, and each member's own
    checkpoint carrying its own run. 'settled' once both hold, 'shared'
    where the declarations name one path, 'unread' where a path or a
    member's own checkpoint could not be read."""
    declared = record.get('declared') or {}
    owner, member = declared.get('owner'), declared.get('member')
    if not isinstance(owner, str) or not isinstance(member, str) \
            or not owner or not member:
        return 'unread'
    if Path(owner).resolve() == Path(member).resolve():
        return 'shared'
    baseline = record.get('baseline') or {}
    if baseline.get('owner') is None or baseline.get('member') is None:
        return 'unread'
    return 'settled'


def _shared_checkpoint(record):
    """The shared checkpoint's verdict across the staged window:
    'single-writer' — every sample names the owner's generation while
    the owner's tick advances and the aliased member's own file stands
    untouched; 'overwritten' — a foreign run's generation reached the
    shared file, the silent state destruction the refusal exists to
    prevent; 'alias-write' — the aliased member captured at all, a
    second live writer; 'stalled' — the owner stopped persisting, so
    the shared file is contested; 'unread' — no usable sample."""
    if record.get('alias_write'):
        return 'alias-write'
    baseline = (record.get('baseline') or {}).get('owner')
    samples = record.get('shared')
    if not isinstance(baseline, dict) or not isinstance(samples, list) \
            or not samples:
        return 'unread'
    usable = [sample for sample in samples if isinstance(sample, dict)]
    if not usable:
        return 'unread'
    if any(sample['generation'] != baseline['generation']
           for sample in usable):
        return 'overwritten'
    if usable[-1]['tick'] <= baseline['tick']:
        return 'stalled'
    return 'single-writer'


def _shared_owner(record):
    """The field owner's verdict across the staged window:
    'undisturbed' where it held the field before and after with its tick
    advancing and no poll dropped, 'disturbed' where it moved roles,
    stalled, or fell silent, 'unread' where its endpoint dropped its
    reads."""
    owner = record.get('owner') or {}
    before, after = owner.get('before'), owner.get('after')
    if not isinstance(before, dict) or not isinstance(after, dict):
        return 'unread'
    if before.get('role') != 'active' or after.get('role') != 'active':
        return 'disturbed'
    first, last = before.get('tick'), after.get('tick')
    if not isinstance(first, int) or isinstance(first, bool) \
            or not isinstance(last, int) or isinstance(last, bool):
        return 'unread'
    if owner.get('dropped'):
        return 'disturbed'
    return 'undisturbed' if last > first else 'disturbed'


def _shared_pair(record):
    """The restored pair's verdict: 'restored' where the member's
    correctly-pathed relaunch landed and the pair reconverged to one
    active plus a tracking standby, 'unrestored' otherwise."""
    restore = record.get('restore') or {}
    if restore.get('relaunched') is not True:
        return 'unrestored'
    settled, owner = restore.get('settled'), restore.get('owner')
    if not isinstance(settled, dict) or settled.get('role') != 'standby' \
            or settled.get('tracking') is not True:
        return 'unrestored'
    if not isinstance(owner, dict) or owner.get('role') != 'active':
        return 'unrestored'
    return 'restored'


def _judge_shared(record, note):
    """Replay one pass's record: the distinct-path control, the aliased
    launch's named refusal, the one-writer audit on the shared
    checkpoint, the undisturbed owner, and the restored pair.
    note(key, diagnostic, detail) collects the violations — every clause
    the facts do not satisfy names one."""
    control = _shared_control(record)
    if control == 'shared':
        note('distinct', SHARED_ACCEPTED,
             'the deployed pair\'s two members declare one --state-file '
             'path before any staging: '
             + json.dumps(record.get('declared'), sort_keys=True)
             + ' — the leg cannot measure the staged alias against '
               'distinct persistence paths')
    elif control == 'unread':
        note('distinct-unread', SHARED_NONDET,
             'the pair\'s own checkpoints could not be read before the '
             'staging: ' + json.dumps(record.get('baseline'),
                                      sort_keys=True))
    refusal = record.get('refusal') or {}
    shape = refusal.get('shape')
    if shape == 'running':
        note('accepted', SHARED_ACCEPTED,
             'the aliased launch kept serving — both peers persisted '
             'into one --state-file, each replacing the other\'s '
             'checkpoint on every capture')
    elif shape == 'exited':
        note('exited', SHARED_ACCEPTED,
             'the aliased launch exited 0 without serving — a clean exit '
             'is neither the named refusal nor the served pair')
    elif shape == 'unread':
        note('refusal-unread', SHARED_NONDET,
             'the aliased launch never produced a readable process '
             'verdict — the leg cannot prove the refusal')
    elif shape == 'absent':
        note('refusal-absent', SHARED_NONDET,
             'the aliased launch\'s container is absent — there is no '
             'exit verdict to read')
    elif refusal.get('named') is not True:
        note('unnamed', SHARED_ACCEPTED,
             'the aliased launch went down on exit '
             + json.dumps(refusal.get('exit'))
             + ' without naming the shared checkpoint, its .lock '
               'sidecar, and the writer-lock conflict — the contract '
               'owes the named refusal')
    if (record.get('monitor') or 'silent') == 'answered':
        note('monitor', SHARED_ACCEPTED,
             'the aliased member\'s monitor kept answering through the '
             'staged window — the launch survived the shared '
             '--state-file instead of refusing it')
    checkpoint = _shared_checkpoint(record)
    if checkpoint in ('overwritten', 'alias-write'):
        note('checkpoint', SHARED_ACCEPTED,
             'the shared checkpoint carried both peers\' writes '
             '(verdict ' + checkpoint + '): '
             + json.dumps(record.get('shared'), sort_keys=True)[:300]
             + ' against the owner\'s baseline '
             + json.dumps((record.get('baseline') or {}).get('owner')))
    elif checkpoint in ('stalled', 'unread'):
        note('checkpoint-' + checkpoint, SHARED_NONDET,
             'the shared checkpoint yielded no one-writer evidence — a '
             + checkpoint + ' verdict across the staged window')
    owner = _shared_owner(record)
    if owner in ('disturbed', 'unread'):
        note('owner-' + owner, SHARED_NONDET,
             'the field owner was not undisturbed across the staged '
             'alias: ' + json.dumps(record.get('owner'),
                                    sort_keys=True))
    if _shared_pair(record) != 'restored':
        note('restore', SHARED_NONDET,
             'the correctly-pathed relaunch did not restore the pair — '
             + json.dumps(record.get('restore'), sort_keys=True))


def _shared_digest(record):
    """The pass's normalized verdict set — identical digests across two
    consecutive passes are the determinism contract."""
    refusal = record.get('refusal') or {}
    shape = refusal.get('shape')
    if shape in ('running', 'exited', 'absent', 'unread'):
        word = shape
    else:
        word = 'down-named' if refusal.get('named') is True \
            else 'down-unnamed'
    return {'distinct': _shared_control(record),
            'refusal': word,
            'monitor': 'answered'
            if (record.get('monitor') or 'silent') == 'answered'
            else 'silent',
            'checkpoint': _shared_checkpoint(record),
            'owner': _shared_owner(record),
            'pair': _shared_pair(record)}


def _shared_pass(ctx, number, owner, member):
    """One staged-alias pass: read the pair's own checkpoints, relaunch
    the non-owner member with its --state-file aliased onto the owner's,
    watch the window the aliased launch's process verdict, the owner's
    undisturbed serving, and the shared checkpoint's single-writer audit
    span, then restore the member's own persistence path and prove the
    pair reconverges. Returns (digest, violations, record): the digest
    two passes compare, violations maps each clause key to (diagnostic,
    detail), and record carries 'inconclusive' when the pass itself could
    not run."""
    record = {'pass': number, 'subject': {'owner': owner,
                                          'member': member},
              'declared': {}, 'baseline': {}, 'alias': {},
              'owner': {'before': None, 'after': None, 'dropped': 0},
              'refusal': {}, 'monitor': 'silent',
              'shared': [], 'alias_write': False,
              'restore': {'relaunched': False, 'settled': None,
                          'owner': None}}
    violations = {}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    owner_file = (ctx.get('state_files') or {}).get(owner)
    member_file = (ctx.get('state_files') or {}).get(member)
    if not owner_file or not member_file:
        record['inconclusive'] = ('the run context carries no per-member '
                                  '--state-file path — the leg cannot '
                                  'audit which checkpoint each peer '
                                  'persists into')
        return None, violations, record
    record['declared'] = {'owner': owner_file, 'member': member_file}
    record['baseline'] = {'owner': _shared_document(owner_file),
                          'member': _shared_document(member_file)}
    shared_file = Path(member_file).name
    record['alias'] = {'member': member, 'shared_with': owner,
                       'state_file': shared_file}
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, member) is None:
        note('settle', SHARED_NONDET,
             'the pair is not settled on its launch roles before the '
             'staging — ' + owner + ' holds no active role with '
             + member + ' tracking behind it')
        return _shared_digest(record), violations, record
    record['owner']['before'] = _shared_role(_try_role(ctx, ctx[owner]))

    # The staged alias: the runner's relaunch lever rebuilds the
    # non-owner's launch with its declared persistence directory mounted
    # onto the live owner's, so the pair's identical --state-file
    # declarations resolve to one checkpoint — the owner's live writer
    # lock included. The member's own directory keeps serving its append
    # sinks from a second mount, so the only refusal available is the
    # cross-peer single-writer claim. A lever that raises from here on
    # is recorded rather than raised: the record's staged-alias flag is
    # what tells the scenario body to recreate the member out of the
    # alias before it returns.
    try:
        ctx['relaunch_controller'](member, share_state_with=owner)
    except Exception as exc:
        record['inconclusive'] = ('the runner\'s relaunch lever never '
                                  'staged the shared --state-file on '
                                  + member + ': ' + str(exc)[:200])
        return None, violations, record
    record['alias']['staged'] = True
    try:
        _shared_window(ctx, record, owner, member, owner_file,
                       member_file, shared_file, note)
    except Exception as exc:
        record['inconclusive'] = ('the staged shared --state-file window '
                                  'never completed: ' + str(exc)[:200])

    _judge_shared(record, note)
    digest = _shared_digest(record)
    record['digest'] = digest
    record['violations'] = {key: diagnostic for key, (diagnostic, _)
                            in violations.items()}
    return digest, violations, record


def _shared_window(ctx, record, owner, member, owner_file, member_file,
                   shared_file, note):
    """The staged window itself: the watch that spans the aliased
    launch's verdict, the owner's undisturbed serving, and the shared
    checkpoint's single-writer audit, then the restore onto the member's
    own persistence path. Split out of the pass so the pass records a
    raising lever instead of unwinding past the record the scenario body
    cleans up from."""
    # The staged window: the aliased launch's process verdict, the
    # member's monitor, the owner's undisturbed serving, and the shared
    # checkpoint's generations — sampled until the refused launch is down
    # with its monitor silent and the window carries its samples. A
    # launch still serving when the window closes gets the exit-verdict
    # bound on top: the refusal lands at startup, so a process that
    # keeps running that long is the accepted alias, not a slow one.
    window = time.monotonic() + SHARED_WINDOW
    verdict = window + SHARED_VERDICT
    state, answered, dropped = None, False, 0
    while True:
        state = ctx['controller_state'](member)
        if _try_role(ctx, ctx[member]) is not None:
            answered = True
        record['shared'].append(_shared_document(owner_file))
        if _shared_document(member_file) != record['baseline']['member']:
            record['alias_write'] = True
        owner_role = _try_role(ctx, ctx[owner])
        if owner_role is None:
            dropped += 1
        record['owner']['after'] = _shared_role(owner_role)
        shape = _shared_shape(state)
        if shape in ('down', 'exited', 'absent') and not answered \
                and len(record['shared']) >= SHARED_SAMPLES:
            break
        if time.monotonic() >= (window if shape == 'running'
                                else verdict):
            break
        time.sleep(SHARED_WATCH)
    record['owner']['dropped'] = dropped
    record['monitor'] = 'answered' if answered else 'silent'
    record['refusal'] = {
        'container': (state or {}).get('container'),
        'shape': _shared_shape(state),
        'exit': (state or {}).get('exit'),
        'named': _shared_named(state, shared_file)}
    text = str((state or {}).get('logs') or '')
    if text:
        record['refusal']['text'] = text[:400]

    # The restore: the member's own persistence path back, its process
    # resuming the run its own checkpoint carries, and the pair back on
    # one active plus a tracking standby for the legs behind this one.
    try:
        ctx['relaunch_controller'](member)
    except Exception as exc:
        note('restore', SHARED_NONDET,
             'the restore relaunch on ' + member + ' never completed: '
             + str(exc)[:200])
    else:
        record['restore']['relaunched'] = True
        record['alias']['staged'] = False
    record['restore']['settled'] = wait_for(
        lambda: _shared_role(_tracking_standby(ctx, member)),
        time.monotonic() + SHARED_SETTLE, interval=SHARED_POLL)
    record['restore']['owner'] = _shared_role(_try_role(ctx, ctx[owner]))


def _shared_self_check():
    """The unchecked-diagnostic guard: replay the leg's auditors over
    planted negatives — the declared paths aliased, an unread control,
    the aliased launch served, exited 0, unread, absent, refused
    unnamed, its monitor still answering, the shared checkpoint
    overwritten or the aliased member writing one, the checkpoint
    unreadable or stalled, a moved, frozen, silent, or unread owner, and
    an unrestored pair — and report every one let slip."""
    refusal_text = ('error: cannot lock state file /var/lib/dcs-run/'
                    'state.json.lock: a live process already holds its '
                    'writer lock — two writers on one --state-file '
                    'overwrite each other\'s run\'s tick domain, '
                    'receipts, and component state, and a restart '
                    'resumes whichever wrote last; give each process '
                    'its own state file')

    def clean_record():
        owner_doc = {'generation': 41, 'tick': 40}
        member_doc = {'generation': 42, 'tick': 40}
        return {
            'pass': 1, 'subject': {'owner': 'active',
                                   'member': 'standby'},
            'declared': {'owner': '/run/controllers/a/state.json',
                         'member': '/run/controllers/b/state.json'},
            'baseline': {'owner': owner_doc, 'member': member_doc},
            'alias': {'member': 'standby', 'shared_with': 'active',
                      'state_file': 'state.json'},
            'refusal': {'container': 'dcs-hw-qa-1-b', 'shape': 'down',
                        'exit': 1, 'named': True,
                        'text': refusal_text},
            'monitor': 'silent',
            'shared': [{'generation': 41, 'tick': tick}
                       for tick in (41, 42, 43)],
            'alias_write': False,
            'owner': {'before': {'role': 'active', 'tick': 40,
                                 'tracking': False},
                      'after': {'role': 'active', 'tick': 43,
                                'tracking': False},
                      'dropped': 0},
            'restore': {
                'relaunched': True,
                'settled': {'role': 'standby', 'tick': 43,
                            'tracking': True},
                'owner': {'role': 'active', 'tick': 43,
                          'tracking': False}}}

    def audit(record):
        found = []
        _judge_shared(record,
                      lambda key, diagnostic, detail: found.append(key))
        return found

    slipped = []
    if audit(clean_record()):
        slipped.append('clean-overstrict')

    negatives = [
        ('declared-paths-aliased',
         lambda r: r['declared'].update(
             member=r['declared']['owner'])),
        ('control-unread',
         lambda r: r['baseline'].update(owner=None)),
        ('alias-served',
         lambda r: r['refusal'].update(shape='running', exit=None,
                                       named=False)),
        ('alias-exited',
         lambda r: r['refusal'].update(shape='exited', exit=0,
                                       named=False)),
        ('alias-unread',
         lambda r: r['refusal'].update(shape='unread', named=False)),
        ('alias-absent',
         lambda r: r['refusal'].update(shape='absent', exit=None,
                                       named=False)),
        ('refusal-unnamed',
         lambda r: r['refusal'].update(named=False)),
        ('monitor-answered',
         lambda r: r.update(monitor='answered')),
        ('checkpoint-overwritten',
         lambda r: r['shared'].__setitem__(1, {'generation': 42,
                                               'tick': 41})),
        ('checkpoint-alias-write',
         lambda r: r.update(alias_write=True)),
        ('checkpoint-stalled',
         lambda r: r['shared'].__setitem__(2, {'generation': 41,
                                               'tick': 40})),
        ('checkpoint-unread',
         lambda r: r.update(shared=[None, None, None])),
        ('owner-moved',
         lambda r: r['owner']['after'].update(role='standby')),
        ('owner-frozen',
         lambda r: r['owner']['after'].update(tick=40)),
        ('owner-silent',
         lambda r: r['owner'].update(dropped=2)),
        ('owner-unread',
         lambda r: r['owner'].update(after=None)),
        ('restore-not-relaunched',
         lambda r: r['restore'].update(relaunched=False)),
        ('restore-unsettled',
         lambda r: r['restore'].update(settled=None)),
        ('restore-untracking',
         lambda r: r['restore']['settled'].update(tracking=False)),
        ('restore-owner-lost',
         lambda r: r['restore'].update(
             owner={'role': 'standby', 'tick': 43,
                    'tracking': True})),
    ]
    for name, doctor in negatives:
        record = clean_record()
        doctor(record)
        if not audit(record):
            slipped.append(name)
    return slipped


def scenario_shared_state_file_refusal(ctx):
    """Relaunch one deployed pair member with its --state-file aliased
    onto its live peer's — the pair's two identical declarations
    resolving to one checkpoint — and prove the launch refuses the
    shared-volume misconfiguration by name instead of both peers
    overwriting one file, while the correctly-pathed pair settles and
    tracks, the field owner reads undisturbed, and the restore puts the
    member back on its own persistence path."""
    case = Case('shared-state-file-refusal',
                'A pair sharing one --state-file is refused by name',
                'the deployed pair settles and tracks with each '
                'member\'s own checkpoint carrying its own run; the '
                'runner\'s relaunch lever then rebuilds the non-owner\'s '
                'launch with its declared persistence directory mounted '
                'onto the live owner\'s, so the pair\'s identical '
                '--state-file declarations resolve to one checkpoint; '
                'the aliased launch goes down nonzero (or stays down) '
                'naming the shared path, its .lock writer-lock sidecar, '
                'and the conflict; the shared checkpoint stays the '
                'owner\'s own run while the owner keeps serving and '
                'scanning; and the correctly-pathed relaunch restores '
                'the member and the pair\'s launch roles')
    member = None
    restore_pending = False
    alias_staged = False
    try:
        for lever in ('relaunch_controller', 'controller_state'):
            if not callable(ctx.get(lever)):
                return case.finish(
                    'inconclusive', 'the run context carries no '
                    + lever + ' lever — the harness admits no '
                    'deployment-doctoring seam for this leg\'s staged '
                    'alias')
        if not (ctx.get('state_files') or {}):
            return case.finish(
                'inconclusive', 'the run context carries no per-member '
                '--state-file path — the leg cannot audit which '
                'checkpoint each peer persists into')
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish(
                'inconclusive', 'the run context carries only one '
                'endpoint — the pair whose two peers could share one '
                '--state-file is absent')
        deadline = time.monotonic() + SHARED_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=SHARED_POLL)
        if owner is None:
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if not reports or all(report is None
                                  for report in reports.values()):
                return case.finish(
                    'inconclusive', 'the deployed pair is unreachable '
                    '— monitor endpoints ' + str(ctx.get('active'))
                    + ' and ' + str(ctx.get('standby')))
            return case.finish('failed', SHARED_NONDET + ': no peer '
                               'reports role=active')
        member = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, member), deadline,
                    interval=SHARED_POLL) is None:
            return case.finish(
                'inconclusive', 'the pair has no tracking standby — the '
                'settled posture this leg stages the shared --state-file '
                'against was never reached')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); aliased member: ' + member + ' ('
                     + ctx[member] + ') sharing the owner\'s '
                     + str(Path((ctx.get('state_files') or {})
                                .get(member, 'state.json')).name))
        digests = []
        for number in (1, 2):
            restore_pending = True
            alias_staged = False
            digest, violations, record = _shared_pass(
                ctx, number, owner, member)
            alias_staged = bool((record.get('alias') or {}).get('staged'))
            ref = save_evidence(
                ctx['evidence_dir'],
                'shared-state-file-pass-' + str(number) + '.json',
                record)
            case.evidence('file', ref, 'shared-state-file pass '
                          + str(number) + ' — the staged alias, the '
                          'aliased launch\'s process verdict, the shared '
                          'checkpoint\'s sampled generations, the '
                          'owner\'s undisturbed reads, and the '
                          'normalized digest')
            if record.get('inconclusive'):
                return case.finish('inconclusive',
                                   record['inconclusive'])
            if violations or digest is None:
                diagnostic = SHARED_ACCEPTED \
                    if digest is None or any(
                        name == SHARED_ACCEPTED
                        for name, _ in violations.values()) \
                    else SHARED_NONDET
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            restore_pending = False
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', SHARED_NONDET + ': the two passes\' digests '
                'diverged: ' + json.dumps(digests[0], sort_keys=True)
                + ' vs ' + json.dumps(digests[1], sort_keys=True))
        case.observe('two staged-alias passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        slipped = _shared_self_check()
        if slipped:
            return case.finish('failed', SHARED_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed', 'digest '
                           + json.dumps(digests[0], sort_keys=True))
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The launch posture for the legs behind this one: a clean pass
        # restores the member by construction, while an aborted pass can
        # have left it down or still carrying the aliased mount — which
        # only a recreate can undo, whether the process is serving under
        # it or not — so relaunch it onto its own persistence directory
        # until it serves again.
        if restore_pending and member is not None \
                and callable(ctx.get('relaunch_controller')):
            deadline = time.monotonic() + SHARED_SETTLE
            relaunched_at = 0.0
            try:
                while time.monotonic() < deadline:
                    alive = _try_role(ctx, ctx[member]) is not None
                    if alias_staged or not alive:
                        if not relaunched_at or time.monotonic() \
                                - relaunched_at > 10:
                            try:
                                ctx['relaunch_controller'](member)
                            except Exception:
                                pass
                            relaunched_at = time.monotonic()
                            alias_staged = False
                    elif _pair_active(ctx) is not None:
                        break
                    time.sleep(SHARED_POLL)
            except Exception:
                pass
