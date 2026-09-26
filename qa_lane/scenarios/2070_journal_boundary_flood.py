"""The journal_boundary_flood acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the flood case runs in the launch-layout window ahead of
# the tune case's a->b switch — it restarts the tracked peer (ctrl-b,
# the rig's only checkpoint-tracking and restart-safe member) and its
# restore gate assumes ctrl-a owns the field.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The bounded served journal's run-boundary survival contract
# (WW-OPS-002's durable-record clause and WW-LCM-002's
# audit-retention duty — the lane evidence for #623's pinned-marker
# fix, mirrored from the reference plant's journal-boundary leg).
# The served ring evicts ordinary entries oldest-first past its
# declared bound and the never-reused seqs make eviction read as a
# numbering gap — with one exception: a run_boundary entry is the
# semantic marker a since-cursor consumer's run attribution stands
# on, so eviction migrates it to a pinned stream the served journal
# keeps answering ahead of the retained tail.
#
# With the deployed pair settled, each pass restarts the tracked peer
# so its durable journal gains another run-boundary marker and its
# served journal the boundary entry, then floods the field owner's
# receipted path — receipted write_value submissions plus the
# quality transitions the simulated plant's fault seam generates
# deterministically — until the tracked peer's adopted journal sits
# past the declared bound. The audit then proves through the served
# /journal surface alone that every lifetime's run_boundary entry
# survived pinned ahead of the bounded tail, that the evicted
# ordinary entries surface as the recorded seq gap rather than
# silent loss, that a since-cursor past the last boundary answers
# exactly the retained tail, and that every served entry attributes
# to the run its section of the durable record declares — while the
# journal file's ordered record (sequential markers, contiguous
# seqs) is unaffected and the pair restores its launch roles.
#
# Contract violations report journal-boundary-failed; pass-to-pass
# divergence reports journal-boundary-nondeterministic. A rig that
# is unreachable, that lacks the restart/journal seams, or that
# predates the served marker contract reports inconclusive.

JOURNAL_BOUND = 1024      # the runtime's declared served-ring bound
                          # (dcs-monitor's journal capacity)
FLOOD_MARGIN = 96         # journaled volume driven past the bound —
                          # the evicted window the seq gap must show
FLOOD_BATCH = 60          # receipted submissions per batch — inside
                          # the 64-command admission bound
FLOOD_SETTLE = 60         # bound on the tracked journal reaching the
                          # flood's journaled volume
FLOOD_POLL = 0.25         # cadence between flood batches — a scan's
                          # room to drain the admission queue
BOUNDARY_SETTLE = 45      # bound on the restart's boundary landing,
                          # served and durable
BOUNDARY_POLL = 0.5       # poll cadence across the restart window
FAULT_CYCLES = 2          # inject/clear quality drives folded into
                          # the flood when the plant seam exists
FAULT_HOLD = 0.4          # scans the injected fault stands for — the
                          # quality transition must record a scan
FLOOD_ACTOR = 'qa-journal-boundary'


class _PredatesContract(Exception):
    """The rig lacks a contract surface the leg measures through — a
    raised pass aborts to the scenario's inconclusive outcome."""


def _journal_file(path):
    """The ordered records of a `--journal-file`: {'boundary': {...}}
    markers and {'entry': {...}} records, oldest first. A torn final
    line — a crash mid-append — is skipped; any earlier unparseable or
    unrecognized line raises."""
    items = []
    lines = Path(path).read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1) + ' does not parse')
        if isinstance(record, dict) and 'run_boundary' in record:
            items.append({'boundary': record['run_boundary']})
        elif isinstance(record, dict) and 'entry' in record:
            items.append({'entry': record['entry'] or {}})
        else:
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1)
                             + ' is not a journal record')
    return items


def _file_markers(records):
    """The durable record's run-boundary markers in file order."""
    return [item['boundary'] for item in records if 'boundary' in item]


def _file_seq_runs(records):
    """{seq: run} for every entry the durable record files — the run
    attribution the file's ordered record itself declares."""
    runs = {}
    current = None
    for item in records:
        if 'boundary' in item:
            current = item['boundary'].get('run')
        else:
            seq = item['entry'].get('seq')
            if seq is not None:
                runs[seq] = current
    return runs


def _served(ctx, base, since=0):
    """The served journal tail past `since` — the pinned run-boundary
    stream ahead of the retained ring."""
    _, body = http_json('GET', base + '/journal?since=' + str(since))
    return _journal_list(body)


def _try_served(ctx, base, since=0):
    """_served or None while the monitor does not answer — a lost
    poll is not the leg's verdict."""
    try:
        return _served(ctx, base, since)
    except Exception:
        return None


def _entry_seq(entry):
    """A served entry's seq — a positive int, or None on a payload
    predating the served-journal shape."""
    seq = (entry or {}).get('seq')
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return None
    return seq


def _boundary_run(entry):
    """The lifetime ordinal a served run_boundary entry declares —
    a positive int, or None."""
    run = ((entry or {}).get('event') or {}) \
        .get('run_boundary', {}).get('run')
    if not isinstance(run, int) or isinstance(run, bool) or run < 1:
        return None
    return run


def _boundary_entries(served):
    """A served journal's run_boundary entries in served order."""
    return [entry for entry in served
            if _boundary_run(entry) is not None]


def _last_boundary_seq(served):
    """The last served run-boundary entry's seq — the cursor the
    since-read resumes from."""
    boundaries = _boundary_entries(served)
    return boundaries[-1]['seq'] if boundaries else 0


def _file_audit(name, records, note):
    """The durable record's ordering audit: sequential run-boundary
    markers and contiguous entry seqs across the whole file — the
    'ordered record unaffected' half the flood must leave. Returns
    the audit detail, or None once a violation is noted."""
    markers = _file_markers(records)
    runs = [marker.get('run') for marker in markers]
    if runs != list(range(1, len(runs) + 1)):
        note(name + '-file', 'journal-boundary-failed',
             name + '\'s durable journal records run markers '
             + json.dumps(runs) + ' — the file lost a lifetime '
             'boundary under the flood')
        return None
    seqs = [item['entry'].get('seq') for item in records
            if 'entry' in item]
    if seqs != list(range(1, len(seqs) + 1)):
        note(name + '-file', 'journal-boundary-failed',
             name + '\'s durable journal seqs are not 1..n '
             'contiguous across the run boundaries: '
             + json.dumps(seqs[:8]) + '…' + json.dumps(seqs[-4:]))
        return None
    return {'file_runs': runs, 'file_entries': len(seqs)}


def _served_audit(name, served, expected_runs, seq_runs, note,
                  require_gap):
    """The served-surface audit one peer's bounded journal must pass
    after the flood: every post-launch lifetime's run_boundary entry
    pinned ahead of the retained tail, strict seq order, the tail
    inside the declared bound with eviction reading as the recorded
    numbering gap, and every served entry attributing to the run its
    file section declares. Returns the audit detail, or None once a
    violation is noted."""
    boundaries = _boundary_entries(served)
    runs = [_boundary_run(entry) for entry in boundaries]
    if runs != expected_runs:
        note(name + '-boundaries', 'journal-boundary-failed',
             name + '\'s served journal carries run boundaries '
             + json.dumps(runs) + ' after the flood, expected '
             + json.dumps(expected_runs) + ' — lifetime attribution '
             'was lost under ordinary event volume')
        return None
    if served[:len(boundaries)] != boundaries:
        note(name + '-pinned', 'journal-boundary-failed',
             name + '\'s served journal does not pin its '
             'run-boundary entries ahead of the retained tail: '
             + json.dumps(served[:len(boundaries) + 2])[:300])
        return None
    seqs = [_entry_seq(entry) for entry in served]
    if any(seq is None for seq in seqs):
        note(name + '-seqs', 'journal-boundary-failed',
             name + '\'s served journal carries entries without a '
             'seq: ' + json.dumps(served[:4])[:300])
        return None
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        note(name + '-seqs', 'journal-boundary-failed',
             name + '\'s served journal is not in strict seq order '
             'after the flood')
        return None
    tail = served[len(boundaries):]
    if len(tail) > JOURNAL_BOUND:
        note(name + '-bound', 'journal-boundary-failed',
             name + '\'s served journal retains ' + str(len(tail))
             + ' tail entries, over the declared '
             + str(JOURNAL_BOUND) + '-entry bound')
        return None
    gap = None
    if require_gap:
        if not tail:
            note(name + '-gap', 'journal-boundary-failed',
                 name + '\'s served journal emptied under the flood '
                 '— the bound consumed the whole tail')
            return None
        anchor = boundaries[-1]['seq'] if boundaries else 0
        gap = tail[0]['seq'] - anchor - 1
        if gap <= 0:
            note(name + '-gap', 'journal-boundary-failed',
                 name + '\'s served journal shows no seq gap past '
                 'seq ' + str(anchor) + ' while its journaled volume '
                 'passed the bound — the flood\'s ordinary eviction '
                 'was silent loss')
            return None
    # The attribution walk: the pinned boundaries are the markers a
    # since-cursor consumer stands on — run-1 entries carry the
    # launch lifetime implicitly — and every served seq must land in
    # the file section its attributed run declares.
    attributed = 1
    for entry in served:
        declared = _boundary_run(entry)
        if declared is not None:
            attributed = declared
        seq = entry['seq']
        filed = seq_runs.get(seq)
        if filed is None:
            note(name + '-attribution', 'journal-boundary-failed',
                 name + ' serves seq ' + str(seq) + ' the durable '
                 'record never wrote — the served journal diverged '
                 'from the file')
            return None
        if filed != attributed:
            note(name + '-attribution', 'journal-boundary-failed',
                 name + ' attributes seq ' + str(seq) + ' to run '
                 + str(attributed) + ' but the durable record files '
                 'it under run ' + str(filed) + ' — a served entry '
                 'lost its run attribution')
            return None
    return {'served_runs': runs, 'tail': len(tail), 'gap': gap}


def _flood_pass(ctx, number, owner, peer, point, fault_point):
    """One restart-and-flood pass: the tracked peer's restart grows a
    new durable run boundary, the receipted flood pushes the adopted
    journal past the declared bound, and the served/durable audit
    proves the markers survived. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record —
    identical across clean passes; violations is {key: (diagnostic,
    detail)} in first-seen order. A lost restart action, a monitor
    that never returns, or a rig-side contract predecessor raises —
    the inconclusive outcomes the scenario reports."""
    violations = {}
    evidence = {'pass': number, 'owner': owner, 'peer': peer,
                'point': point}
    digest = {'boundary': 'unseen', 'pinned': 'unseen',
              'tail': 'unseen', 'gap': 'unseen', 'cursor': 'unseen',
              'attribution': 'unseen', 'file': 'unseen',
              'owner': 'unseen', 'roles': 'unrestored'}

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'journal-boundary-failed', detail)

    def nondet(key, detail):
        note(key, 'journal-boundary-nondeterministic', detail)

    def finish(result):
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        return result, violations, evidence

    owner_base, peer_base = ctx[owner], ctx[peer]
    journals = ctx.get('journal_files') or {}

    # The settle gate: the launch layout the pass's restore owes —
    # the field owner holding and the tracked peer behind it.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return finish(None)

    # The durable baseline: the restart's boundary must land as the
    # file's next sequential marker.
    try:
        records0 = _journal_file(journals[peer])
    except FileNotFoundError:
        raise _PredatesContract('the tracked peer\'s journal file is '
                                'absent — the rig predates the '
                                'durable-record contract')
    except ValueError as exc:
        failed('file', 'the durable record lost its ordering ahead '
               'of the restart: ' + str(exc)[:200])
        return finish(None)
    expected = len(_file_markers(records0)) + 1
    evidence['restart'] = {'run': expected}

    # ---- the restart: a second process lifetime on the tracked
    # peer, its boundary landing durable and served ----
    try:
        ctx['restart_controller'](peer)
    except Exception as exc:
        raise ConnectionError('the restart action never completed: '
                              + str(exc)[:200])
    if wait_for(lambda: _tracking_standby(ctx, peer),
                time.monotonic() + BOUNDARY_SETTLE,
                interval=BOUNDARY_POLL) is None:
        raise ConnectionError('the restarted peer\'s monitor never '
                              'returned to tracking')
    if _pair_active(ctx) != owner:
        failed('roles', 'the restart moved the field owner off '
               + owner)
        return finish(None)

    last = {}

    def boundary_entry():
        served = _try_served(ctx, peer_base)
        if served is None:
            return None
        last['served'] = served
        for entry in _boundary_entries(served):
            if _boundary_run(entry) == expected:
                return entry
        return None

    boundary = wait_for(boundary_entry,
                        time.monotonic() + BOUNDARY_SETTLE,
                        interval=BOUNDARY_POLL)
    if boundary is None:
        try:
            markers = _file_markers(_journal_file(journals[peer]))
        except FileNotFoundError:
            raise _PredatesContract('the tracked peer\'s journal '
                                    'file is absent after its '
                                    'restart — the rig predates the '
                                    'durable-record contract')
        if len(markers) < expected:
            raise _PredatesContract(
                'the restart left no durable run boundary — the rig '
                'predates the file-marker contract')
        raise _PredatesContract(
            'the restart grew the durable boundary to run '
            + str(expected) + ' but the served journal never '
            'carried its run_boundary entry — the rig predates the '
            'served-marker contract')
    digest['boundary'] = 'served'
    boundary_seq = boundary['seq']
    evidence['restart']['boundary_seq'] = boundary_seq

    # ---- the flood: receipted write_value submissions on the field
    # owner — each terminal outcome journaled and adopted into the
    # tracked peer's journal — interleaved with the quality
    # transitions the plant's fault seam generates deterministically.
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool',
        'value': {'bool': True}}}, 'actor': FLOOD_ACTOR}
    # The submission cap while the tracked peer's adopted journal
    # catches up — twice the required volume is rig trouble, not a
    # slower pull.
    flood_max = 2 * (JOURNAL_BOUND + FLOOD_MARGIN)
    outcomes = {}
    submitted = 0
    injected = []

    def post_boundary_count():
        # The journaled volume past the boundary, measured on the
        # durable record — the served view saturates at the bound by
        # construction (and a dishonest one can fabricate), so the
        # append-only file's never-reused seq axis carries the count:
        # its last seq less the boundary's.
        try:
            records = _journal_file(journals[peer])
        except (OSError, ValueError):
            return None
        seqs = [item['entry'].get('seq') for item in records
                if 'entry' in item]
        seqs = [seq for seq in seqs if isinstance(seq, int)
                and not isinstance(seq, bool)]
        if not seqs:
            return 0
        return max(seqs) - boundary_seq

    try:
        count = 0
        cycles = 0
        while count is not None \
                and count < JOURNAL_BOUND + FLOOD_MARGIN \
                and submitted < flood_max:
            for _ in range(min(FLOOD_BATCH, flood_max - submitted)):
                command['command']['write_value']['value'] = {
                    'bool': submitted % 2 == 0}
                try:
                    status, receipt = http_json(
                        'POST', owner_base + '/command', command)
                except Exception as exc:
                    raise ConnectionError('the flood\'s submission '
                                          'erred mid-drive: '
                                          + str(exc)[:200])
                if status != 200 or not isinstance(receipt, dict):
                    failed('flood', 'the receipted path refused '
                           'ordinary event volume: ' + str(status)
                           + ' ' + json.dumps(receipt)[:200])
                    return finish(None)
                outcomes[_outcome_key(receipt)] = \
                    outcomes.get(_outcome_key(receipt), 0) + 1
                submitted += 1
            if fault_point is not None and cycles < FAULT_CYCLES:
                # The quality half of the driven transitions: a
                # quality fault the field's own seam injects and
                # clears records its transitions on both observers.
                try:
                    verdict = _plant_ctl(ctx, 'fault',
                                         str(fault_point),
                                         'bad:device_fault')
                    if verdict.get('result') == 'done':
                        injected.append(fault_point)
                        time.sleep(FAULT_HOLD)
                        _plant_ctl(ctx, 'clear-fault',
                                   str(fault_point))
                        injected.remove(fault_point)
                        cycles += 1
                except Exception:
                    pass
            time.sleep(FLOOD_POLL)
            count = post_boundary_count()
    finally:
        for point_id in injected:
            try:
                _plant_ctl(ctx, 'clear-fault', str(point_id))
            except Exception:
                pass
    evidence['flood'] = {'submitted': submitted,
                         'outcomes': outcomes,
                         'post_boundary': count,
                         'quality_drives': cycles}
    if count is None:
        raise ConnectionError('the tracked peer\'s durable record '
                              'went unreadable mid-flood')
    if count == 0:
        raise _PredatesContract('the tracked peer\'s journal files '
                                'no transitions past its boundary — '
                                'the rig predates the adoption '
                                'contract the flood drives')
    if count < JOURNAL_BOUND + 1:
        nondet('flood-volume', 'the flood journaled only '
               + str(count) + ' post-boundary transitions on the '
               'tracked peer — the declared bound never came into '
               'play this pass')
        return finish(digest)

    # ---- the audit: the tracked peer's served journal ----
    served = _try_served(ctx, peer_base)
    if served is None:
        raise ConnectionError('the tracked peer\'s served journal '
                              'never answered the audit read')
    expected_runs = list(range(2, expected + 1))
    try:
        records = _journal_file(journals[peer])
    except FileNotFoundError:
        raise _PredatesContract('the tracked peer\'s journal file '
                                'vanished under the flood')
    except ValueError as exc:
        failed('file', 'the durable record lost its ordering under '
               'the flood: ' + str(exc)[:200])
        return finish(digest)
    seq_runs = _file_seq_runs(records)
    audit = _served_audit('the tracked peer', served, expected_runs,
                          seq_runs, note, require_gap=True)
    if audit is None:
        return finish(digest)
    evidence['peer_audit'] = audit
    digest['pinned'] = 'ahead'
    digest['tail'] = 'bounded'
    digest['gap'] = 'honest'
    digest['attribution'] = 'verified'

    # The since-cursor honesty: a consumer resuming past the last
    # boundary must be answered exactly the retained tail — the
    # recorded gap, never fabricated continuity.
    tail = served[len(_boundary_entries(served)):]
    cursor = _try_served(ctx, peer_base, _last_boundary_seq(served))
    if cursor is None or cursor != tail:
        failed('cursor', 'the ?since=<last boundary> read did not '
               'answer exactly the retained tail — the seq-cursor '
               'semantics broke under the flood')
        return finish(digest)
    digest['cursor'] = 'exact'

    # The durable record: the flood's served-ring eviction leaves
    # the file's ordered record untouched — sequential markers, the
    # new boundary included, and contiguous entry seqs.
    filed = _file_audit('the tracked peer', records, note)
    if filed is None:
        return finish(digest)
    if len(filed['file_runs']) != expected:
        failed('file', 'the restart grew the durable record to '
               + json.dumps(filed['file_runs']) + ' runs — expected '
               'the new boundary as marker ' + str(expected))
        return finish(digest)
    evidence['peer_file'] = filed
    digest['file'] = 'ordered'

    # ---- the field owner's own journal: the same bounded served
    # rules on the peer the flood journaled first — its lifetimes'
    # boundaries pinned, its file undisturbed.
    served_owner = _try_served(ctx, owner_base)
    if served_owner is None:
        raise ConnectionError('the field owner\'s served journal '
                              'never answered the audit read')
    try:
        records_owner = _journal_file(journals[owner])
    except FileNotFoundError:
        raise _PredatesContract('the field owner\'s journal file is '
                                'absent — the rig predates the '
                                'durable-record contract')
    except ValueError as exc:
        failed('file', 'the field owner\'s durable record lost its '
               'ordering under the flood: ' + str(exc)[:200])
        return finish(digest)
    owner_runs = [marker.get('run')
                  for marker in _file_markers(records_owner)]
    audit = _served_audit('the field owner', served_owner,
                          list(range(2, len(owner_runs) + 1)),
                          _file_seq_runs(records_owner), note,
                          require_gap=True)
    if audit is None:
        return finish(digest)
    filed = _file_audit('the field owner', records_owner, note)
    if filed is None:
        return finish(digest)
    evidence['owner_audit'] = dict(audit, **filed)
    digest['owner'] = 'audited'

    # ---- the restore: the flood and the restart left the pair on
    # its launch roles — the field owner holding, the tracked peer
    # behind it.
    restored = wait_for(
        lambda: _pair_active(ctx) == owner
                and _tracking_standby(ctx, peer) or None,
        time.monotonic() + BOUNDARY_SETTLE, interval=BOUNDARY_POLL)
    if not restored:
        failed('roles', 'the flood moved the pair off its launch '
               'roles — no active with ' + peer + ' tracking behind '
               'it')
        return finish(digest)
    digest['roles'] = 'restored'
    return finish(digest)


def scenario_journal_boundary_flood(ctx):
    """Restart the tracked peer and flood the field owner's receipted
    path past the served journal's bound: the run_boundary markers
    must stay pinned while ordinary eviction reads as the seq gap."""
    case = Case(
        'journal-boundary-flood',
        'Run boundaries survive the bounded journal\'s flood '
        'eviction',
        'with the deployed pair settled, the tracked peer\'s '
        'restart_controller action grows a second durable run '
        'boundary and a receipted write_value flood plus injected '
        'quality transitions drive its adopted journal past the '
        'declared bound; the served /journal keeps every lifetime\'s '
        'run_boundary entry pinned ahead of the bounded tail, the '
        'evicted ordinary entries read as the recorded seq gap, a '
        'since-cursor past the last boundary answers exactly the '
        'retained tail, every served entry attributes to the run its '
        'file section declares, and the durable record stays ordered '
        '— the pair restoring its launch roles; two consecutive '
        'passes produce identical digests')
    try:
        if ctx.get('restart_controller') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller-restart action '
                               '— the boundary induction has no '
                               'documented seam')
        journals = ctx.get('journal_files') or {}
        if journals.get('active') is None \
                or journals.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for '
                               'the pair — the durable record cannot '
                               'be audited')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + BOUNDARY_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=BOUNDARY_POLL)
        if owner is None:
            return case.finish('failed', 'journal-boundary-failed: '
                               'no peer reports role=active')
        if owner != 'active':
            return case.finish('inconclusive', 'the field owner is '
                               + owner + ' — the leg\'s restart and '
                               'restore assume the launch layout '
                               'where ctrl-a owns the field')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=BOUNDARY_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the leg has no '
                               'peer to restart')

        # The served contract surface: each peer's /journal must
        # answer the retained/pinned record shape — an unreachable
        # route or a payload predating the seq'd-entry shape is a
        # rig the leg cannot measure.
        for name in (owner, peer):
            try:
                _, body = http_json('GET', ctx[name]
                                    + '/journal?since=0')
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   '/journal never answered: '
                                   + str(exc)[:200])
            entries = _journal_list(body)
            if not isinstance(body, list) or any(
                    not isinstance(entry, dict)
                    or _entry_seq(entry) is None
                    for entry in entries):
                return case.finish('inconclusive', name + '\'s '
                                   'served journal predates the '
                                   'seq\'d-entry contract: '
                                   + json.dumps(body)[:200])

        # The flood's receipted target: the scenarios' writable bool
        # point — the write_value seam the issue names.
        _, signals = http_json('GET', ctx[owner] + '/signals')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive', 'the model declares '
                               'no writable bool command point — '
                               'the flood has no receipted target')
        point = target['point']

        # The flood's quality half: the lowest field 'in' point off
        # the command target, faultable through the shipped plant
        # tool — skipped recorded when the run carries no seam.
        fault_point = None
        if ctx.get('plant_ctl') is not None:
            try:
                inputs = _field_inputs(ctx)
            except Exception:
                inputs = {}
            alternates = sorted(key for key in inputs
                                if key != point)
            if alternates:
                fault_point = alternates[0]
        ref = save_evidence(ctx['evidence_dir'],
                            'journal-boundary-flood-baseline.json',
                            {'owner': owner, 'peer': peer,
                             'point': point,
                             'fault_point': fault_point,
                             'bound': JOURNAL_BOUND})
        case.evidence('file', ref, 'the settled pair and the '
                      'flood\'s declared bound')
        case.observe('flooding point ' + str(point) + ' on ' + owner
                     + '; ' + peer + ' tracks')
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _flood_pass(
                    ctx, number, owner, peer, point, fault_point)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'journal-boundary-flood-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'boundary-flood pass '
                              + str(number) + ' — the restart, the '
                              'receipted flood, the served/durable '
                              'audit, and the normalized digest')
                if violations or digest is None:
                    diagnostic = 'journal-boundary-failed' \
                        if digest is None or any(
                            name == 'journal-boundary-failed'
                            for name, _ in violations.values()) \
                        else 'journal-boundary-nondeterministic'
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted
            # pass gets the same bounded settle wait.
            restored = wait_for(
                lambda: (_pair_active(ctx) == 'active' or None)
                        and _tracking_standby(ctx, 'standby'),
                time.monotonic() + BOUNDARY_SETTLE,
                interval=BOUNDARY_POLL)
            if not restored:
                case.observe('cleanup: the pair never settled back '
                             'to the launch roles')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'journal-boundary-nondeterministic: the '
                'two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two restart-and-flood passes, identical '
                     'digests')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
