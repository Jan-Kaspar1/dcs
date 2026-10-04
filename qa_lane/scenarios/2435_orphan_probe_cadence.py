"""The orphan_probe_cadence leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg stages a standing foreign claim on the deployed
# field and hands it back through the claim-seam cluster's own release
# path, so it sits behind the ownerless-backoff leg that leaves the
# field ownerless and ahead of the yielded-claim-rearm legs that
# re-arm a released claim.
RUNS_AFTER = frozenset({'scenario_ownerless_remote_backoff'})
RUNS_BEFORE = frozenset({'scenario_yielded_claim_rearm'})


# --------------------------------------------------------------------
# The bounded orphan tracking-source probe cadence — #1256's landed fix
# pinned as per-run lane evidence for WW-LCM-001's peer-health clause:
# every orphaned track_cycle resolves its tracking source by probing
# candidate endpoints synchronously on the scan thread, so a standing
# foreign claim whose declared monitor accepts the dial but never
# answers — or whose connect/read burns the full bound — cost the paced
# scan ~1s per cycle (CHECKPOINT_PULL_TIMEOUT) until the claim released,
# collapsing the container's own --scan-ms 100 cadence to ~2/s and
# stretching an armed standby's failover miss budget ~5x, because that
# budget is counted per slowed cycle. The settled contract follows the
# sibling `adopt_claimed_source` path's documented rule: a dead declared
# endpoint costs one bounded pull per probe window, not one per scan.
#
# The leg stages the shape through the lane's claim-aware seam: a
# scenario attachment issues `claim_writer` declaring a dead monitor
# address — the blackholed endpoint the contract names — and holds the
# claim live, so the fenced owner demotes in place and the orphaned
# peers resolve tracking sources while the claim stands. The cost is
# then read from the served surface, never from the endpoint's own
# timing — a fast-refused variant of the same shape cannot make the
# assertion vacuous, and a pre-contract build that paid the bound per
# cycle is caught by the refusal-count bound whatever the endpoint
# costs:
#
# - the claim: the holder's reply answers `done`/`claimed_shared`, and
#   the fencing verdict keeps naming the foreign owner and its dead
#   declared monitor for the window;
# - the standing window: each peer's served role, run tick and
#   `publication.published` — the one counter a completed paced scan
#   mints — must keep advancing at a rate inside the declared bound of
#   the cadence measured before the claim, and the served
#   `io_health.scan_overruns` must grow only by the bounded probe's own
#   cost — a handful across the passes the retry window paid, never the
#   per-cycle accumulation the finding measured;
# - the refused-probe bound: each peer's `tracking_source_refused`
#   records naming the dead endpoint must stay inside the contract's own
#   bound — one bounded pull per probe window — so they number far
#   below the orphan cycles the window spans. A per-cycle probe fills
#   the file; a cached candidate set does not;
# - the release: dropping the holder resolves the field, the ex-owner
#   reclaims it, the pair reconverges to one field-owning active plus
#   one tracking standby, and the pair's launch roles are restored.
#
# Named diagnostics: orphan-probe-cadence-failed tags the contract
# clauses — a peer's tick or publication rate collapsing below the
# measured cadence's bound, `scan_overruns` accumulating per orphan
# cycle, a refused-probe record count above the contract's bound, a
# peer reporting a verdict other than the ownerless one inside the
# window, the release leaving the pair off its launch roles — and
# orphan-probe-cadence-nondeterministic tags the instability the
# contract does not answer for: a claim that never landed, a fencing
# verdict naming no owner or no declared monitor, a pre-contract
# served surface, a demotion that never happened, a starved watch, a
# peer that never pinned orphaned, an unreleased claim, and two passes
# whose digests diverge. The unchecked-diagnostic self-check replays
# the judge over planted negatives — the issue's named collapsed
# cadence, the overrun flood, the refusal flood, the stalled
# publication, the verdict flicker, the unrestored roles — and reports
# orphan-probe-cadence-unchecked for any that slip through.

PROBE_FORM = 25          # bound on the island forming and on each
                         # settlement wait
PROBE_POLL = 0.3         # the standing window's watch cadence
PROBE_HOLD = 12.0        # seconds the standing window spans — enough
                         # paced scans to span at least one probe retry
                         # boundary, and far under the armed failover
                         # budget the collapse used to stretch
CADENCE_MEASURE = 4.0    # seconds the baseline cadence is sampled over
CADENCE_FLOOR = 0.4      # the fraction of the measured cadence a peer
                         # must hold inside the standing window: the
                         # collapse the finding measured fell to ~2/s
                         # against the container's own 10/s, five times
                         # under the floor the contract restores
STEP_FLOOR = 0.8         # the fraction of its publications a peer must
                         # have minted inside the window — the same
                         # counter the paced scan loop publishes once
                         # per completed scan
OVERRUN_SLACK = 12       # the bounded probe's own cost, in
                         # scan_overruns, absorbed across the window;
                         # the per-cycle accumulation the contract
                         # closed runs an order of magnitude past it
REFUSED_PER_WINDOW = 3   # the contract's own bound — one bounded pull
                         # per probe window — so the window's refused
                         # records must number no higher, while the
                         # orphan cycles they span run an order of
                         # magnitude more
# The standing foreign claim's owner token — never a rig pin and never
# the shipped tool's: "qa-opb".
PROBE_OWNER = 0x7161_5F6F_7262_7072
# The dead declared monitor's port on the holder's own bridge address:
# a routable IP nothing listens on, the blackholed endpoint shape the
# contract names. Port 9 is the discard port, reserved and unserved.
PROBE_MONITOR_PORT = 9

DIAG_FAILED = 'orphan-probe-cadence-failed'
DIAG_NONDET = 'orphan-probe-cadence-nondeterministic'
DIAG_UNCHECKED = 'orphan-probe-cadence-unchecked'


def _int(value):
    """Whether a served counter is an integer — a bool is not, and
    neither is an absent stamp."""
    return isinstance(value, int) and not isinstance(value, bool)


def _probe_row(ctx, name):
    """One normalized standing-window row off a peer's served
    surface: the reported role, the sync verdict's kind, the run tick,
    the publication counter and the `io_health` overrun count. None
    when the peer does not answer — a dropped observation, never a
    row."""
    report = _try_role(ctx, ctx[name])
    snapshot = _try_snapshot(ctx, ctx.get(name))
    if not isinstance(report, dict) or not isinstance(snapshot, dict):
        return None
    sync = report.get('sync')
    kind = None
    if isinstance(sync, dict) and sync:
        kind = next(iter(sync))
    elif isinstance(sync, str):
        kind = sync
    return {
        'role': report.get('role'),
        'sync': kind,
        'tick': report.get('tick'),
        'published': (snapshot.get('publication') or {}).get('published'),
        'overruns': (snapshot.get('io_health') or {}).get('scan_overruns'),
    }


def _counters(row):
    """One row's `(tick, published, overruns)` triple, or None where the
    row carries no integer stamps at all — the counters the cadence and
    cost clauses read."""
    if not isinstance(row, dict):
        return None
    return (row['tick'] if _int(row['tick']) else None,
            row['published'] if _int(row['published']) else None,
            row['overruns'] if _int(row['overruns']) else None)


def _advance(rows, index):
    """A counter's total advance across a collected row list, or None
    when the window carries no two stamped readings of it."""
    values = [counters[index] for counters in map(_counters, rows)
              if counters and counters[index] is not None]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def _measure_cadence(ctx, names, seconds):
    """Each peer's observed run ticks per second across a settled
    `seconds` window — the pair's own scan cadence, the baseline the
    standing window's collapse is judged against."""
    def sample():
        return time.monotonic(), {
            name: (_probe_row(ctx, name) or {}).get('tick')
            for name in names}
    first = sample()
    deadline = first[0] + seconds
    while time.monotonic() < deadline:
        time.sleep(seconds / 4.0)
    last = sample()
    span = last[0] - first[0]
    rates = {}
    for name in names:
        before, after = first[1][name], last[1][name]
        if _int(before) and _int(after) and span > 0:
            rates[name] = (after - before) / span
    return rates


def _refused_records(ctx, name, floor):
    """The `tracking_source_refused` records a peer's durable
    `--journal-file` carries since `floor` records whose recorded
    source is the claim's dead declared monitor — the refused-probe
    audit the contract's bound is counted on."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        refused = event.get('tracking_source_refused')
        if isinstance(refused, dict) and str(
                refused.get('source', '')).endswith(
                    ':' + str(PROBE_MONITOR_PORT)):
            out.append({'seq': (item.get('entry') or {}).get('seq'),
                        'source': refused.get('source')})
    return out


def _blackhole(address):
    """A blackholed endpoint on the holder's own bridge subnet: the
    last host octet moved off the live container's, nothing answers
    there, and the address is a declared monitor a launched controller
    could never have raised. A dial to it burns the connection bound
    instead of being refused outright, which is the shape #1256
    measured — the per-scan cost a refused-free probe would hide."""
    if not isinstance(address, str) or ':' not in address:
        return None
    host = address.rsplit(':', 1)[0]
    octets = host.split('.')
    if len(octets) != 4 or not all(
            octet.isdigit() for octet in octets):
        return None
    octets[-1] = '254'
    return '.'.join(octets) + ':' + str(PROBE_MONITOR_PORT)


def _verdict_fields(response):
    """A fencing verdict's named owner, declared monitor, and whether
    the answer declares a monitor at all — the claim's own evidence."""
    if not isinstance(response, dict):
        return {'owner': None, 'monitor': None, 'declared': False}
    error = response.get('error') or {}
    return {'owner': error.get('owner'),
            'monitor': error.get('monitor'),
            'declared': 'monitor' in error}


def _claim_probe(ctx):
    """The field's read-only claim observation through the lane's raw
    plant attachment — the claim state a third-party write would meet.
    None when the read itself failed."""
    try:
        return _plant_probe(ctx, {'op': 'probe_writer'})
    except Exception:
        return None


def _restore_launch(ctx, owner, peer):
    """Best-effort launch-layout restore on the deployed pair: the
    documented switchover order run again — the named owner promoted
    back over the field with its sibling tracking behind it. Every step
    is retried inside the bound and swallowed on refusal."""
    try:
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + PROBE_FORM
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(PROBE_POLL)
    except Exception:
        pass


def _judge_probe_cadence(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses —
    a peer's paced cadence collapsing below the measured cadence's
    bound, its publication counter stalling, `scan_overruns`
    accumulating per orphan cycle, the refused-probe records numbering
    above the contract's bound, a peer reporting a verdict other than
    the ownerless one, the release leaving the pair off its launch roles
    — and DIAG_NONDET tags the instability the contract does not answer
    for: a claim that never landed, a fencing verdict naming no owner or
    no declared monitor, a pre-contract served surface, a demotion that
    never happened, a starved watch, a peer that never pinned orphaned,
    an unreleased claim. An aborted stage ends the audit where the pass
    ended — the later keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    if record.get('claim_error') is not None:
        nondet('claim', "the standing foreign claim never landed: "
               + str(record['claim_error']))
        return
    surface = record.get('surface')
    if not isinstance(surface, dict) or not surface.get('ok'):
        nondet('surface', 'the staged run served no cadence-cost '
               'surface: ' + json.dumps(surface)[:200])
        return
    cadence = record.get('cadence')
    if not isinstance(cadence, dict) or not cadence:
        nondet('cadence', 'the rig never demonstrated a scan cadence on '
               'the pair the claim is staged against: '
               + json.dumps(cadence)[:200])
        return
    claim = record.get('claim') or {}
    if (claim.get('result') or '') not in ('done', 'claimed_shared'):
        nondet('claim', "the plant answered the held claim "
               + json.dumps(claim)[:200] + ' — the standing window '
               'never opened')
        return
    verdict = record.get('verdict')
    if not isinstance(verdict, dict) or not verdict.get('declared'):
        nondet('claim', 'the fencing verdict names no declared monitor — '
               'the claim the contract judges has no endpoint: '
               + json.dumps(verdict)[:200])
        return
    if not record.get('demoted'):
        nondet('demote', 'the fenced owner never demoted in place — the '
               'standing claim never orphaned a peer: '
               + json.dumps(record.get('window') or [])[:240])
        return
    orphaned = record.get('orphaned')
    if not isinstance(orphaned, dict) or not any(orphaned.values()):
        nondet('orphan', 'no peer pinned the ownerless verdict inside '
               'the standing window — the probe the contract bounds was '
               'never on the scan path: '
               + json.dumps(orphaned)[:200])
        return
    rows = [row for row in (record.get('window') or []) if row]
    if not rows:
        nondet('watch', 'the standing window collected no served rows — '
               'the starved monitor gave the audit nothing to read')
    for name, entry in sorted((record.get('cadence_rows') or {}).items()):
        if entry.get('tick_rate') is None:
            nondet('watch', "the " + name + " peer's served rows carried "
                   'no integer run ticks — the cadence claim has no '
                   'window to read')
        elif entry['tick_rate'] < entry.get('floor', 0):
            failed('cadence', "the " + name + " peer's paced scan advanced "
                   + str(round(entry['tick_rate'], 2)) + ' ticks/s against '
                   'the measured cadence\'s floor of '
                   + str(round(entry.get('floor', 0), 2))
                   + ' ticks/s — the standing dead-monitor claim '
                   'collapsed the scan')
        if entry.get('published') is None:
            nondet('watch', "the " + name + " peer published no counters "
                   'inside the standing window \u2014 the paced '
                   "loop's own evidence never landed")
        elif entry['published'] < entry.get('published_floor', 0):
            failed('cadence', "the " + name + " peer minted "
                   + str(entry['published']) + ' publications inside the '
                   'standing window against the floor of '
                   + str(entry.get('published_floor', 0))
                   + ' — the paced scan collapsed')
        if entry.get('overruns') is None:
            nondet('watch', "the " + name + " peer served no io_health "
                   'overrun counter — the per-scan cost surface is '
                   'pre-contract')
        elif entry['overruns'] > OVERRUN_SLACK:
            failed('overruns', "the " + name + " peer's io_health "
                   'scan_overruns grew by '
                   + str(entry['overruns']) + ' inside the standing '
                   'window, past the bounded probe\'s own cost of '
                   + str(OVERRUN_SLACK) + ' — the probe is paid per '
                   'orphan cycle')
        if entry.get('refused') is None:
            nondet('refused', "the " + name + " peer's durable journal "
                   'carries no refused-probe audit — the pinned release '
                   'predates the contract the bound is read through')
        elif entry['refused'] > REFUSED_PER_WINDOW:
            failed('refused', "the " + name + " peer journaled "
                   + str(entry['refused']) + ' refused-probe records for '
                   'the dead declared monitor inside one probe window, '
                   'against the contract\'s bound of '
                   + str(REFUSED_PER_WINDOW) + ' — the candidate set is '
                   're-probed per scan instead of per window')
        if entry.get('cycles') and entry['refused'] is not None \
                and entry['refused'] >= entry['cycles']:
            failed('refused', "the " + name + " peer's refused-probe "
                   'records (' + str(entry['refused']) + ') number at or '
                   'past the orphan cycles the window spans ('
                   + str(entry['cycles']) + ') — one bounded pull per '
                   'window, not one per scan')
    for name, posture in sorted((record.get('verdicts') or {}).items()):
        if not posture:
            continue
        drifted = next(
            (word for word in posture
             if word not in ('orphaned', 'unsynchronized')), None)
        if drifted is not None:
            failed('verdict', "the " + name + " peer reported "
                   + str(drifted) + ' inside the standing window — the '
                   'ownerless verdict is the posture the claim owes')
    if not record.get('released'):
        nondet('release', "the standing claim never released: "
               + str(record.get('release')))
    elif not record.get('restored'):
        failed('roles', 'the pair never reconverged to one field-owning '
               'active plus one tracking standby after the release — '
               + str(record.get('final')))


def _probe_digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'claim': 'standing' if clean('claim', 'demote', 'orphan')
            else 'unstaged',
        'cadence': 'held' if clean('cadence') else 'collapsed',
        'cost': 'bounded' if clean('overruns') else 'per-cycle',
        'refusals': 'bounded' if clean('refused') else 'per-cycle',
        'verdict': 'orphaned' if clean('verdict') else 'drifted',
        'roles': 'restored'
            if clean('roles', 'release') else 'unrestored'}


def _probe_self_check():
    """The leg's unchecked-diagnostic self-test: replay the cadence
    judge over each planted negative the issue names — a collapsed
    paced cadence, a stalled publication counter, an overrun flood, a
    refusal flood, a verdict drifting off the ownerless posture, the
    unrestored roles — and require the judge to note each. The
    instability shapes must report nondeterministic, not failed. A
    silent judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        return {
            'claim_error': None,
            'surface': {'ok': True},
            'cadence': {'active': 10.0, 'standby': 10.0},
            'claim': {'result': 'claimed_shared'},
            'verdict': {'owner': 12345, 'monitor': '10.0.0.2:9',
                        'declared': True},
            'demoted': True,
            'orphaned': {'active': True, 'standby': True},
            'verdicts': {'active': ['orphaned'] * 8,
                         'standby': ['orphaned'] * 8},
            'window': [{'role': 'standby', 'sync': 'orphaned',
                        'tick': 120, 'published': 90, 'overruns': 0}] * 8,
            'cadence_rows': {
                'active': {'tick_rate': 9.8, 'floor': 4.0,
                           'published': 88, 'published_floor': 70,
                           'overruns': 3, 'refused': 1, 'cycles': 40},
                'standby': {'tick_rate': 9.7, 'floor': 4.0,
                            'published': 86, 'published_floor': 70,
                            'overruns': 2, 'refused': 2, 'cycles': 40}},
            'released': True,
            'release': {'dropped': True},
            'restored': {'active': 'active', 'standby': 'tracking'},
            'final': {'active': 'active', 'standby': 'standby'},
        }

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_probe_cadence(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The issue's named negative first: the standing dead-monitor claim
    # collapsing the paced scan.
    expect('collapsed-cadence', lambda record: record['cadence_rows']
           .__setitem__('active', {'tick_rate': 2.0, 'floor': 4.0,
                                   'published': 88, 'published_floor': 70,
                                   'overruns': 3, 'refused': 1,
                                   'cycles': 40}))
    expect('publication-stalled', lambda record: record['cadence_rows']
           .__setitem__('standby', {'tick_rate': 9.7, 'floor': 4.0,
                                    'published': 12, 'published_floor': 70,
                                    'overruns': 2, 'refused': 2,
                                    'cycles': 40}))
    expect('overrun-flood', lambda record: record['cadence_rows']
           .__setitem__('active', {'tick_rate': 9.8, 'floor': 4.0,
                                   'published': 88, 'published_floor': 70,
                                   'overruns': 400, 'refused': 1,
                                   'cycles': 40}))
    expect('refusal-flood', lambda record: record['cadence_rows']
           .__setitem__('active', {'tick_rate': 9.8, 'floor': 4.0,
                                   'published': 88, 'published_floor': 70,
                                   'overruns': 3, 'refused': 40,
                                   'cycles': 40}))
    expect('verdict-drifted', lambda record: record['verdicts']
           .__setitem__('standby', ['orphaned'] * 7 + ['usurped']))
    expect('roles-unrestored', lambda record: record.update(
        {'restored': None, 'final': {'active': 'standby',
                                     'standby': 'active'}}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a claim that never landed, a
    # fencing verdict declaring no monitor, a pre-contract surface, an
    # unmeasured cadence, a demotion that never happened, a starved
    # watch, a peer that never pinned orphaned, an unreadable overrun
    # counter, an unreleased claim.
    expect('claim-unstaged', lambda record: record.update(
        {'claim_error': 'the attachment refused'}), DIAG_NONDET)
    expect('claim-refused', lambda record: record['claim'].__setitem__(
        'result', 'fenced'), DIAG_NONDET)
    expect('monitor-undeclared', lambda record: record['verdict']
           .__setitem__('declared', False), DIAG_NONDET)
    expect('surface-unstamped', lambda record: record.update(
        {'surface': {'ok': False}}), DIAG_NONDET)
    expect('cadence-unproven', lambda record: record.update(
        {'cadence': {}}), DIAG_NONDET)
    expect('demote-missing', lambda record: record.update(
        {'demoted': False}), DIAG_NONDET)
    expect('watch-starved', lambda record: record.update(
        {'window': [], 'cadence_rows': {}}), DIAG_NONDET)
    expect('never-orphaned', lambda record: record.update(
        {'orphaned': {'active': False, 'standby': False}}), DIAG_NONDET)
    expect('overrun-unreadable', lambda record: record['cadence_rows']
           .__setitem__('active', {'tick_rate': 9.8, 'floor': 4.0,
                                   'published': 88, 'published_floor': 70,
                                   'overruns': None, 'refused': 1,
                                   'cycles': 40}), DIAG_NONDET)
    expect('refused-unreadable', lambda record: record['cadence_rows']
           .__setitem__('active', {'tick_rate': 9.8, 'floor': 4.0,
                                   'published': 88, 'published_floor': 70,
                                   'overruns': 3, 'refused': None,
                                   'cycles': 40}), DIAG_NONDET)
    expect('release-unstaged', lambda record: record.update(
        {'released': False}), DIAG_NONDET)
    return slipped


def _probe_pass(ctx, number, owner, peer, floors):
    """One standing-window pass: measure the pair's cadence, stage the
    standing foreign claim over a dead declared monitor, watch the
    fenced owner's demotion orphan the peers, read the paced cadence and
    its per-scan cost across the window together with the refused-probe
    audit, then release the claim and read the pair back onto its
    launch roles. Returns `(record, evidence)`: the record is what the
    judge replays; an aborted stage leaves its later keys absent for the
    judge to name."""
    record = {'owner': owner, 'peer': peer}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}

    # Phase 1 — the contract's served surface: each peer's role report
    # plus a snapshot carrying the publication and io_health counters the
    # cadence and cost clauses read. A pre-contract surface is the
    # staged run's shape, not a violation.
    surface = {}
    for name in (owner, peer):
        row = _probe_row(ctx, name)
        surface[name] = row is not None and all(
            _int(row[index]) for index in ('tick', 'published',
                                           'overruns'))
    record['surface'] = {'ok': all(surface.values())}
    if not record['surface']['ok']:
        return record, evidence

    # Phase 2 — the measured cadence: the pair's own --scan-ms pacing,
    # read from the served run ticks before any claim stands.
    cadence = _measure_cadence(ctx, (owner, peer), CADENCE_MEASURE)
    record['cadence'] = {name: round(rate, 3)
                         for name, rate in cadence.items()}
    evidence['cadence'] = record['cadence']
    if not cadence:
        return record, evidence

    # Phase 3 — the standing foreign claim: a held attachment issues
    # `claim_writer` declaring a blackholed monitor address, so the
    # fenced owner's next write loses the field and its pairs resolve
    # tracking sources against a claim whose endpoint is undialable.
    try:
        claim = ctx['hold_field_claim']({
            'op': 'claim_writer', 'owner': PROBE_OWNER,
            'controller': True})
    except Exception as exc:
        record['claim_error'] = str(exc)[:300]
        return record, evidence
    monitor = None
    if isinstance(claim, dict):
        reply = claim.get('reply') or {}
        record['claim'] = {'result': reply.get('result'),
                           'monitor': reply.get('monitor')}
        monitor = _blackhole(claim.get('address'))
        if monitor is not None:
            record['claim']['monitor'] = monitor
    else:
        record['claim'] = {'result': None}
    evidence['claim'] = record['claim']
    record['verdict'] = _verdict_fields(_claim_probe(ctx))
    evidence['verdict'] = record['verdict']

    # Phase 4 — the standing window: the claim's fenced write demotes
    # the owner in place, and each peer's paced cadence, publication
    # counter, overrun cost and refused-probe audit are read across it.
    # Until the monitor is declared the claim's own evidence is
    # incomplete, so the pass re-stages it with the dead address.
    if not record['verdict'].get('declared') and monitor:
        try:
            ctx['drop_field_claim']()
        except Exception:
            pass
        claim = ctx['hold_field_claim']({
            'op': 'claim_writer', 'owner': PROBE_OWNER,
            'controller': True, 'monitor': monitor})
        if isinstance(claim, dict):
            record['claim'] = {'result': (claim.get('reply') or {}).get(
                'result'), 'monitor': monitor}
        record['verdict'] = _verdict_fields(_claim_probe(ctx))
        evidence['claim'] = record['claim']
        evidence['verdict'] = record['verdict']

    record['demoted'] = wait_for(
        lambda: (_try_role(ctx, ctx[owner]) or {}).get('role')
        == 'standby' or None,
        time.monotonic() + PROBE_FORM, interval=PROBE_POLL) is not None

    rows = {owner: [], peer: []}
    stamps = {owner: [], peer: []}
    verdicts = {owner: [], peer: []}
    deadline = time.monotonic() + PROBE_HOLD
    while time.monotonic() < deadline:
        at = time.monotonic()
        for name in (owner, peer):
            row = _probe_row(ctx, name)
            rows[name].append(row)
            stamps[name].append(at)
            if isinstance(row, dict):
                verdicts[name].append(row.get('sync'))
    record['window'] = [
        row for name in (owner, peer) for row in rows[name] if row]
    record['verdicts'] = verdicts
    record['orphaned'] = {
        name: any(row and row.get('sync') == 'orphaned'
                  for row in entries)
        for name, entries in rows.items()}
    evidence['window'] = [
        {'peer': name, 'tick': row['tick'], 'sync': row['sync'],
         'published': row['published'], 'overruns': row['overruns']}
        for name in (owner, peer) for row in rows[name] if row]

    # Phase 5 — the cadence and cost readings. Each peer's run-tick
    # advance is measured against the wall span its own samples span and
    # held at the measured cadence's floor; its publication advance is
    # held at the paced scans that floor owes over the window; its
    # overrun growth is held at the bounded probe's own cost; and its
    # refused-probe records are held at the contract's per-window bound
    # and counted beside the orphan cycles the window spans — which is
    # what makes "one bounded pull per window, not one per scan"
    # legible on the served surface.
    audit = {}
    for name in (owner, peer):
        entries = [(at, row) for at, row in zip(stamps[name], rows[name])
                   if isinstance(row, dict)]
        ticks = [row['tick'] for _at, row in entries if _int(row['tick'])]
        span = (entries[-1][0] - entries[0][0]) if len(entries) >= 2 else 0
        advance = ticks[-1] - ticks[0] if len(ticks) >= 2 else None
        floor = cadence[name] * CADENCE_FLOOR
        audit[name] = {
            'tick_rate': round(advance / span, 3)
            if advance is not None and span > 0 else None,
            'floor': round(floor, 3),
            'cycles': advance,
            'published': _advance(rows[name], 1),
            'published_floor': int(floor * PROBE_HOLD),
            'overruns': _advance(rows[name], 2),
            'refused': len(_refused_records(
                ctx, name, floors[name])),
        }
    record['cadence_rows'] = audit
    evidence['cadence_rows'] = audit

    # Phase 6 — the release: the holder drops, the field resolves, and
    # the pair reconverges to one field-owning active plus one tracking
    # standby — the launch layout restored.
    try:
        ctx['drop_field_claim']()
        record['release'] = {'dropped': True}
    except Exception as exc:
        record['release'] = {'dropped': False, 'error': str(exc)[:200]}
    record['released'] = bool((record['release'] or {}).get('dropped'))
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) is not None
                 and _tracking_standby(ctx, peer) is not None) or None,
        time.monotonic() + PROBE_FORM, interval=PROBE_POLL)
    record['final'] = {name: (_try_role(ctx, ctx[name]) or {}).get('role')
                       for name in (owner, peer)}
    return record, evidence


def scenario_orphan_probe_cadence(ctx):
    """Exercise the bounded orphan tracking-source probe cadence on the
    deployed pair: with the pair settled on its launch roles and its
    paced scan cadence measured, a held attachment stands a foreign
    field claim declaring a blackholed monitor; the fenced owner demotes
    in place and the orphaned peers resolve their tracking sources
    against that undialable endpoint for the whole standing window. Each
    peer's run tick and `publication.published` must hold the measured
    cadence's floor — never collapsing toward the per-scan probe cost the
    finding measured — its `io_health.scan_overruns` must grow only by
    the bounded probe's own cost, and its `tracking_source_refused`
    records must stay inside the contract's per-window bound rather than
    numbering one per orphan cycle. Releasing the claim resolves the
    field, the pair reconverges to one active plus one tracking standby,
    and the launch roles are restored. Two passes produce identical
    digests."""
    case = Case(
        'orphan-probe-cadence',
        'A standing dead-monitor claim does not collapse the paced scan',
        'with the pair settled on its launch roles and its paced scan '
        'cadence measured from the served run ticks, each pass stands a '
        'foreign field claim declaring a blackholed monitor through the '
        'lane\'s claim-aware seam; the fenced owner demotes in place and '
        'the orphaned peers resolve their tracking sources against that '
        'undialable endpoint for the whole window; each peer\'s run tick '
        'and publication counter must hold the measured cadence\'s floor '
        '— never collapsing toward the per-scan probe cost — its '
        'io_health scan_overruns must grow only by the bounded probe\'s '
        'own cost, and its tracking_source_refused records must stay '
        'inside the contract\'s per-window bound rather than numbering '
        'one per orphan cycle; releasing the claim resolves the field, '
        'the pair reconverges to one active plus one tracking standby, '
        'and two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the probe-cadence leg needs is absent')
        for action in ('hold_field_claim', 'drop_field_claim'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the standing claim cannot be '
                                   'staged')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-controller journal files — the '
                               'refused-probe audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s monitor '
                                   'is unreachable: ' + str(exc)[:200])

        # The launch layout the passes stage from: the pair settled with
        # the field owner active and its sibling tracking behind it. A
        # swapped layout is walked back before the leg reports.
        if _pair_active(ctx) != 'active':
            _restore_launch(ctx, 'active', 'standby')
        deadline = time.monotonic() + PROBE_FORM
        if wait_for(lambda: _pair_active(ctx) == 'active'
                    and 'active' or None, deadline,
                    interval=PROBE_POLL) != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never settled '
                               'on its launch layout — the standing '
                               'claim the leg stages fences the field '
                               'from the declared owner')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=PROBE_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the standing claim '
                               'the leg stages has no peer to orphan')
        owner, peer = 'active', 'standby'
        floors = {
            name: len(_journal_entries(journals[name]))
            for name in (owner, peer)}
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); orphaned peer: ' + peer + ' (' + ctx[peer]
                     + '); the standing claim is a hold_field_claim '
                     'attachment declaring a blackholed monitor on port '
                     + str(PROBE_MONITOR_PORT))

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _probe_pass(
                    ctx, number, owner, peer, floors)
                _judge_probe_cadence(record, note)
                digest = _probe_digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'orphan-probe-cadence-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'probe-cadence pass '
                              + str(number) + ' — the measured cadence '
                              'and its floor, the staged claim and its '
                              'fencing verdict, the standing window\'s '
                              'paced rows, the overrun growth and the '
                              'refused-probe counts beside the orphan '
                              'cycles, the release, and the normalized '
                              'digest')
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
        finally:
            # The field and the launch layout for the legs behind this
            # one — a clean pass restores both by construction; an
            # aborted pass drops the holder and walks the documented
            # role order again, best-effort.
            try:
                ctx['drop_field_claim']()
            except Exception:
                pass
            _restore_launch(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two standing-window passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the cadence judge replays
        # each planted negative it must name; a silent judge means the
        # leg can no longer catch what it names.
        slipped = _probe_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))