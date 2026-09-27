"""The orphan_episode_bound leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg extends the keyed announced-source cluster — the
# demoted-pair island it induces is the announce-verify demote those
# legs stage — and it restores the pair's launch roles for the
# failover case behind it.
RUNS_AFTER = frozenset({'scenario_keyed_announced_source'})


# --------------------------------------------------------------------
# The bounded orphan-episode journal contract — #1041's landed fix
# pinned as per-run lane evidence for WW-LCM-001's peer-health
# journal clause: a tracking peer pinned on a non-advancing adopted
# source — the same `source_owns_field: false` document landing on
# every completed pull while the fetch worker's in-flight cycles
# count produced-nothing misses between them — must journal
# `field_orphaned` exactly once for the whole orphan episode, never
# once per ownerless apply, and the served sync verdict must stay
# `orphaned` through the evidence-free misses instead of flickering
# to `degraded`; the misses still count toward the armed failover
# budget.
#
# The leg stages the island the announced-source legs already prove
# can form: the field owner demotes onto its sibling standby's
# verified announced hint, so each peer tracks a peer that owns
# nothing and the launched standby's configured --standby pull keeps
# landing the sibling's pacing ownerless checkpoint. The
# completed-pull/produced-nothing alternation is driven through the
# runner-owned pause induction: `docker pause` on the source
# container holds its monitor socket open but unanswered, so the
# tracking peer's in-flight fetch stalls until its own timeout drops
# it — the produced-nothing miss — while the thaw lets the held
# ownerless document land again. Through the alternating window the
# peer's served /role is watched row by row: every row must read
# standby + orphaned with the failover miss count advancing, and
# both the durable --journal-file and the served /journal tail must
# hold exactly one field_orphaned entry for the episode. The episode
# then ends on the genuine non-orphaned apply — the owner promoted
# back over the field — a fresh island opens a second episode, and
# exactly one more entry journals: the bound is per-episode, not
# per-run. The pair's launch roles restore. Named diagnostics
# orphan-episode-bound-failed for a contract miss,
# orphan-episode-bound-nondeterministic when two passes disagree or
# the rig answers with instability instead of a verdict — a starved
# watch, the armed failover firing inside the calibrated window, a
# dropped served-journal read, a control-plane refusal that leaves
# the staging short — and orphan-episode-bound-unchecked when the
# self-check's planted negatives slip the leg's own audits.

ORPHAN_SETTLE = 45     # bound on each switch/reconverge/restore
ORPHAN_FORM = 25       # bound on the island's orphaned verdicts —
                       # the sibling's armed failover budget (~12 s of
                       # counted misses on the 100 ms cadence) never
                       # reaches inside it
ORPHAN_POLL = 0.15     # the held-window watch cadence
MISS_HOLD = 1.3        # the frozen-source hold per round — past the
                       # checkpoint fetch timeout, so the stalled
                       # pulls each produce a counted miss
PULL_HOLD = 0.9        # the live-source hold per round — several
                       # ownerless applies land
HOLD_ROUNDS = 2        # miss/pull alternations inside the held episode
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DIAG_FAILED = 'orphan-episode-bound-failed'
DIAG_NONDET = 'orphan-episode-bound-nondeterministic'
DIAG_UNCHECKED = 'orphan-episode-bound-unchecked'


def _orphaned(ctx, name):
    """The endpoint's /role report while it is a standby reporting the
    ownerless-line verdict — the island member's state — else None."""
    report = _try_role(ctx, ctx[name])
    sync = (report or {}).get('sync')
    if (report or {}).get('role') == 'standby' \
            and isinstance(sync, dict) and 'orphaned' in sync:
        return report
    return None


def _row(ctx, name):
    """One normalized watch row off the peer's served /role — the
    observation the held-episode audit replays: the reported role,
    the sync verdict's kind, the alignment the verdict carries, and
    the armed failover evidence (converged/misses/budget). None when
    the peer does not answer — a dropped observation, never a row."""
    report = _try_role(ctx, ctx[name])
    if report is None:
        return None
    sync = report.get('sync')
    kind, detail = (sync if isinstance(sync, str) else 'none'), {}
    if isinstance(sync, dict) and sync:
        kind, detail = next(iter(sync.items()))
        if not isinstance(detail, dict):
            detail = {}
    failover = report.get('failover') or {}
    return {'role': report.get('role'), 'sync': kind,
            'aligned': detail.get('aligned'),
            'converged': failover.get('converged'),
            'misses': failover.get('misses'),
            'budget': failover.get('budget')}


def _watch(ctx, name, hold, rows):
    """Poll the peer's served /role for `hold` seconds, appending a
    normalized row per answered read."""
    deadline = time.monotonic() + hold
    while time.monotonic() < deadline:
        row = _row(ctx, name)
        if row is not None:
            rows.append(row)
        time.sleep(ORPHAN_POLL)


def _durable_events(ctx, name, floor, kind):
    """The `kind` event bodies the peer's durable --journal-file
    carries since `floor` records — the audit surface the
    field_orphaned bound is asserted on."""
    out = []
    for item in _journal_entries(ctx['journal_files'][name])[floor:]:
        event = (item.get('entry') or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _served_events(ctx, name, floor, kind):
    """The `kind` event bodies the peer's served journal carries
    since `floor` — None when the read itself dropped."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    out = []
    for entry in _journal_list(journal):
        event = entry.get('event') or {}
        if isinstance(event.get(kind), dict):
            out.append(event[kind])
    return out


def _judge_episode(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    orphan-episode contract clauses — the verdict that must not
    flicker, the miss accounting that must advance, the one entry
    per episode, the episode end and its genuinely-new successor,
    the restored launch roles — and DIAG_NONDET tags the instability
    the contract does not answer for: a starved watch, the armed
    failover firing inside the calibrated window, a dropped served
    read, a refused control-plane call that leaves the staging
    short, an alternation that never landed a completed pull. An
    aborted stage ends the audit where the pass ended — the later
    keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    budget = record.get('budget')
    if budget is None:
        nondet('armed', 'the tracking peer\'s first read dropped — '
               'the failover evidence the leg audits was never '
               'observed')
    elif not isinstance(budget, int) or isinstance(budget, bool) \
            or budget <= 0:
        failed('armed', 'the tracking peer serves no armed failover '
               'evidence — the miss accounting the leg audits is '
               'off: ' + json.dumps(budget))
    demote = record.get('demote') or {}
    if demote.get('status') != 200:
        nondet('demote', 'the island-inducing POST /demote answered '
               + str(demote.get('status')) + ' '
               + json.dumps(demote.get('body'))[:160] + ' — the '
               'staging never opened an episode')
        return
    adoptions = record.get('adoptions')
    if not isinstance(adoptions, list) or len(adoptions) != 1 \
            or not str((adoptions[0] or {}).get('source', '')) \
            .endswith(':' + str(PAIR_PORTS['standby'])):
        nondet('adoption', 'the demotion journaled '
               + json.dumps(adoptions)[:200] + ' instead of one '
               'tracking-source adoption naming the sibling standby '
               'on :' + str(PAIR_PORTS['standby']))
    if not isinstance(record.get('island'), dict):
        failed('island', 'the demoted pair never reported the '
               'orphaned verdict on each other — the island never '
               'formed')
        return
    if not record.get('first_orphan'):
        failed('orphan-journal', 'the peer reported orphaned but '
               'its first field_orphaned entry never journaled — '
               'the transition evidence the episode owes is absent')
        return
    window = record.get('window') or []
    rows = [row for phase in window for row in (phase.get('rows')
                                                or [])]
    fired = next((row for row in rows
                  if row.get('role') != 'standby'), None)
    if not rows:
        nondet('watch', 'the held-episode watch collected no served '
               'rows — the starved monitor gave the audit nothing '
               'to read')
    else:
        if fired is not None:
            nondet('failover', 'the pinned peer reported '
                   + json.dumps(fired)[:200] + ' inside the held '
                   'episode — the armed failover boundary was '
                   'reached inside the calibrated window and the '
                   'peer promoted out from under the episode')
        # The pinned-posture audits run on the peer's standby rows —
        # once the failover fires, the episode the leg pinned is
        # legitimately over and its verdict evidence ends there.
        standing = [row for row in rows
                    if row.get('role') == 'standby']
        flickered = next(
            (row for row in standing
             if row.get('sync') != 'orphaned'), None)
        if flickered is not None:
            failed('verdict', 'the orphaned peer reported '
                   + json.dumps(flickered)[:200] + ' inside the held '
                   'episode — the standing verdict flickered off '
                   'orphaned')
        misses = [row.get('misses') for row in standing]
        if len(standing) >= 2:
            if any(not isinstance(m, int) or isinstance(m, bool)
                   for m in misses):
                failed('misses', 'the peer serves no armed failover '
                       'miss count through the held episode')
            elif misses[-1] <= misses[0] \
                    or any(cur < prev for prev, cur
                           in zip(misses, misses[1:])):
                failed('misses', 'the failover miss accounting did '
                       'not advance monotonically across the held '
                       'window: ' + json.dumps(misses[:14])[:200])
            else:
                flat = next(
                    (phase_rows for phase in window
                     for phase_rows in [phase.get('rows') or []]
                     if phase.get('phase') == 'miss'
                     and len([row for row in phase_rows
                              if row.get('role') == 'standby']) >= 2
                     and [row for row in phase_rows
                          if row.get('role') == 'standby'][-1]
                     .get('misses')
                     <= [row for row in phase_rows
                         if row.get('role') == 'standby'][0]
                     .get('misses')), None)
                if flat is not None:
                    failed('misses', 'a frozen-source window '
                           'produced no counted misses — the '
                           'evidence-free cycles never reached the '
                           'failover budget')
            aligned_first = next(
                (row.get('aligned') for row in standing
                 if isinstance(row.get('aligned'), int)
                 and not isinstance(row.get('aligned'), bool)),
                None)
            pull_aligned = [
                row.get('aligned')
                for phase in window if phase.get('phase') == 'pull'
                for row in (phase.get('rows') or [])
                if row.get('role') == 'standby'
                and isinstance(row.get('aligned'), int)
                and not isinstance(row.get('aligned'), bool)]
            if aligned_first is None or not pull_aligned \
                    or max(pull_aligned) <= aligned_first:
                nondet('pulls', 'no completed ownerless pull '
                       'landed during the held window — the '
                       'freeze/thaw alternation never staged the '
                       'missed-pull window the bound is exercised '
                       'across')
    if record.get('durable_orphans') != 1:
        failed('orphans', 'durable_orphans holds '
               + json.dumps(record.get('durable_orphans'))
               + ' field_orphaned entries for the held episode '
               'instead of exactly one — the bound is per-episode')
    served = record.get('served_orphans')
    if served is None:
        nondet('orphans-served', 'the served-journal read dropped '
               'inside the held episode — the tail count never '
               'landed')
    elif served != 1:
        failed('orphans', 'served_orphans holds '
               + json.dumps(served) + ' field_orphaned entries for '
               'the held episode instead of exactly one — the '
               'bound is per-episode')
    if fired is not None:
        # The episode ended on the armed failover's own promotion,
        # not the leg's staged apply — the remaining stages are the
        # script the pass could no longer run, not clauses.
        return
    promote = record.get('promote') or {}
    if promote.get('status') != 200:
        nondet('promote', 'the episode-ending POST /promote '
               'answered ' + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160] + ' — the '
               'non-orphaned apply never landed')
        return
    ended = record.get('ended')
    if not isinstance(ended, dict) \
            or 'tracking' not in (ended.get('sync') or {}):
        failed('ended', 'the peer never left orphaned on the '
               'non-orphaned apply — the episode never ended: '
               + json.dumps(ended)[:160])
        return
    redemote = record.get('redemote') or {}
    if redemote.get('status') != 200:
        nondet('redemote', 'the episode-reopening POST /demote '
               'answered ' + str(redemote.get('status')) + ' '
               + json.dumps(redemote.get('body'))[:160] + ' — the '
               'second episode never staged')
    elif not isinstance(record.get('reorphaned'), dict):
        failed('reorphan', 'the peer never re-orphaned — the '
               'second episode never began')
    else:
        if record.get('durable_orphans_2') != 2:
            failed('orphans_2', 'durable_orphans_2 holds '
                   + json.dumps(record.get('durable_orphans_2'))
                   + ' field_orphaned entries after the second '
                   'episode began instead of exactly two — a '
                   'genuinely new episode owes its own entry')
        served = record.get('served_orphans_2')
        if served is None:
            nondet('orphans-served-2', 'the served-journal read '
                   'dropped after the second episode began — the '
                   'tail count never landed')
        elif served != 2:
            failed('orphans_2', 'served_orphans_2 holds '
                   + json.dumps(served) + ' field_orphaned entries '
                   'after the second episode began instead of '
                   'exactly two — a genuinely new episode owes its '
                   'own entry')
    if not record.get('restored'):
        failed('restored', 'the pair never settled back to its '
               'launch roles')


def _digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'adopted': 'sibling' if clean('demote', 'adoption') else 'none',
        'island': 'formed' if clean('island') else 'absent',
        'episode': 'one-entry'
            if clean('orphan-journal', 'orphans', 'orphans-served')
            else 'flooded',
        'verdict': 'held' if clean('verdict', 'watch', 'failover')
            else 'flickered',
        'misses': 'advanced' if clean('armed', 'misses') else 'stalled',
        'pulls': 'landed' if clean('pulls') else 'none',
        'ended': 'tracking' if clean('promote', 'ended') else 'orphaned',
        'episode_two': 'second-entry'
            if clean('redemote', 'reorphan', 'orphans_2',
                     'orphans-served-2') else 'absent',
        'roles': 'restored' if clean('restored') else 'unrestored'}


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay the episode
    judge over each planted negative the issue names — a second
    field_orphaned inside the held episode, a served degraded
    verdict on a miss cycle, stalled miss accounting, no completed
    pull landing, the episode never ending, the second episode's
    entry absent — and require the judge to note each. A silent
    judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        def rows(aligned, misses):
            return [{'role': 'standby', 'sync': 'orphaned',
                     'aligned': aligned, 'converged': True,
                     'misses': m, 'budget': 120}
                    for aligned, m in zip(aligned, misses)]
        return {'budget': 120,
                'demote': {'status': 200, 'body': {'role': 'demoting'}},
                'adoptions': [{'source': 'probe-b:8081'}],
                'island': {'active': {'role': 'standby'},
                           'standby': {'role': 'standby'}},
                'first_orphan': [{'aligned': 98}],
                'window': [
                    {'phase': 'miss',
                     'rows': rows([100, 100, 100], [3, 5, 7])},
                    {'phase': 'pull',
                     'rows': rows([104, 109], [9, 11])},
                    {'phase': 'miss',
                     'rows': rows([109, 109], [14, 16])},
                    {'phase': 'pull',
                     'rows': rows([114, 118], [18, 20])}],
                'durable_orphans': 1, 'served_orphans': 1,
                'promote': {'status': 200, 'body': {'role': 'promoting'}},
                'ended': {'role': 'standby',
                          'sync': {'tracking': {'aligned': 150}}},
                'redemote': {'status': 200, 'body': {'role': 'demoting'}},
                'reorphaned': {'role': 'standby',
                               'sync': {'orphaned': {'aligned': 210}}},
                'durable_orphans_2': 2, 'served_orphans_2': 2,
                'restore_promote': {'body': {'role': 'promoting'}},
                'restored': {'role': 'standby',
                             'sync': {'tracking': {'aligned': 240}}}}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_episode(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: a second
    # field_orphaned asserted inside the held episode.
    expect('journal-flooded', lambda record:
           record.update({'durable_orphans': 2}))
    # ... and the orphaned verdict asserted as flickered to degraded.
    expect('verdict-flickered', lambda record:
           record['window'][0]['rows'][1].update(
               {'sync': 'degraded'}))
    # The evidence-free misses never reached the budget accounting.
    expect('misses-stalled', lambda record: [
        row.update({'misses': 7})
        for phase in record['window'] for row in phase['rows']])
    # The non-orphaned apply never ended the episode.
    expect('episode-unended', lambda record:
           record.update({'ended': None}))
    # The second episode journaled nothing new.
    expect('second-episode-silent', lambda record: record.update(
        {'durable_orphans_2': 1, 'served_orphans_2': 1}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: a starved watch, the failover
    # firing inside the window, a dropped served read, a refused
    # control-plane call, an alternation that landed no pull.
    expect('watch-starved', lambda record:
           record.update({'window': []}), DIAG_NONDET)
    expect('failover-fired', lambda record:
           record['window'][0]['rows'][1].update(
               {'role': 'promoting'}), DIAG_NONDET)
    expect('served-read-dropped', lambda record:
           record.update({'served_orphans': None}), DIAG_NONDET)
    expect('pulls-never-landed', lambda record: [
        row.update({'aligned': 100})
        for phase in record['window'] for row in phase['rows']],
        DIAG_NONDET)
    expect('demote-refused', lambda record:
           record.update({'demote': {'status': 409, 'body':
                                     'no_tracking_source'}}),
           DIAG_NONDET)
    return slipped


def _episode_pass(ctx, number, owner, peer):
    """One held-episode pass: island the demoted pair, freeze and
    thaw the source so completed ownerless pulls alternate with
    produced-nothing misses, audit the served verdicts and both
    journal surfaces, end the episode on the non-orphaned apply,
    re-orphan for the second entry, and restore the launch roles.
    Returns (record, evidence): the record is what the judge
    replays; an aborted stage simply leaves its later keys absent
    for the judge to name."""
    record = {'window': []}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    base, peer_base = ctx[owner], ctx[peer]

    row = _row(ctx, peer)
    record['budget'] = (row or {}).get('budget')
    floors = {
        'peer_file':
            len(_journal_entries(ctx['journal_files'][peer])),
        'peer_served': _journal_cursor(ctx, peer_base),
        'owner_file':
            len(_journal_entries(ctx['journal_files'][owner]))}

    # Induce the island: demote the field owner — the announced-source
    # verify adopts the newest tracking hint, the sibling standby —
    # and each peer ends up tracking a peer that owns nothing.
    status, body = _settle_call(base + '/demote')
    record['demote'] = {'status': status, 'body': body}
    record['adoptions'] = _durable_events(
        ctx, owner, floors['owner_file'], 'tracking_source_adopted')
    if status != 200:
        return record, evidence

    def islanded():
        pair = {name: _orphaned(ctx, name) for name in (owner, peer)}
        return pair if all(pair.values()) else None

    record['island'] = wait_for(islanded,
                                time.monotonic() + ORPHAN_FORM,
                                interval=ORPHAN_POLL)
    if not isinstance(record['island'], dict):
        return record, evidence

    # The episode's one allowed entry — the durable file and the
    # served tail must each land exactly this transition.
    record['first_orphan'] = wait_for(
        lambda: _durable_events(ctx, peer, floors['peer_file'],
                                'field_orphaned') or None,
        time.monotonic() + ORPHAN_SETTLE, interval=ORPHAN_POLL)

    # The held window: freeze the source past the fetch timeout so
    # the tracking peer's pulls produce only misses, then thaw so
    # the ownerless checkpoint lands again — alternating across the
    # multi-cycle window.
    paused = False
    try:
        for _round in range(HOLD_ROUNDS):
            ctx['pause_controller'](owner)
            paused = True
            rows = []
            _watch(ctx, peer, MISS_HOLD, rows)
            record['window'].append({'phase': 'miss', 'rows': rows})
            ctx['unpause_controller'](owner)
            paused = False
            rows = []
            _watch(ctx, peer, PULL_HOLD, rows)
            record['window'].append({'phase': 'pull', 'rows': rows})
    finally:
        if paused:
            try:
                ctx['unpause_controller'](owner)
            except Exception:
                pass
    record['durable_orphans'] = len(_durable_events(
        ctx, peer, floors['peer_file'], 'field_orphaned'))
    served = _served_events(ctx, peer, floors['peer_served'],
                            'field_orphaned')
    record['served_orphans'] = None if served is None else len(served)

    # End the episode on a genuinely non-orphaned apply: the owner's
    # promote re-arms its field claim, so the peer's next completed
    # pull lands source_owns_field: true and realigns to tracking.
    promoted, last = None, None
    deadline = time.monotonic() + ORPHAN_SETTLE
    while promoted is None and time.monotonic() < deadline:
        status, body = _settle_call(base + '/promote')
        if status == 200:
            promoted = body
        else:
            last = (status, body)
            time.sleep(ORPHAN_POLL)
    record['promote'] = {'status': 200 if promoted is not None
                         else last[0] if last else None,
                         'body': promoted if promoted is not None
                         else last[1] if last else None}
    if promoted is not None:
        record['ended'] = wait_for(
            lambda: _tracking_standby(ctx, peer),
            time.monotonic() + ORPHAN_SETTLE, interval=ORPHAN_POLL)
    if promoted is None or record.get('ended') is None:
        return record, evidence

    # The second episode: demote the owner onto the sibling again —
    # the fresh island is a genuinely new orphan episode, so exactly
    # one more field_orphaned entry may journal.
    status, body = _settle_call(base + '/demote')
    record['redemote'] = {'status': status, 'body': body}
    if status == 200:
        record['reorphaned'] = wait_for(
            lambda: _orphaned(ctx, peer),
            time.monotonic() + ORPHAN_FORM, interval=ORPHAN_POLL)
    if isinstance(record.get('reorphaned'), dict):
        wait_for(
            lambda: len(_durable_events(
                ctx, peer, floors['peer_file'],
                'field_orphaned')) >= 2 or None,
            time.monotonic() + ORPHAN_SETTLE, interval=ORPHAN_POLL)
        record['durable_orphans_2'] = len(_durable_events(
            ctx, peer, floors['peer_file'], 'field_orphaned'))
        served = _served_events(ctx, peer, floors['peer_served'],
                                'field_orphaned')
        record['served_orphans_2'] = \
            None if served is None else len(served)

    # Restore the launch roles: the owner's promote over the field it
    # was demoted from, then the pair settles owner-active +
    # peer-tracking — the layout the next pass and the cases behind
    # this one meet.
    promoted, last = None, None
    deadline = time.monotonic() + ORPHAN_SETTLE
    while promoted is None and time.monotonic() < deadline:
        status, body = _settle_call(base + '/promote')
        if status == 200:
            promoted = body
        else:
            last = (status, body)
            time.sleep(ORPHAN_POLL)
    record['restore_promote'] = {'body': promoted,
                                 'last_refusal': last}
    record['restored'] = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + ORPHAN_SETTLE, interval=ORPHAN_POLL)
    return record, evidence


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore on the exercised pair: the
    documented switchover order run again — the named owner promoted
    back over the field with its sibling tracking behind it. Used by
    the layout gate and the cleanup path alike; every step is
    retried inside the bound and swallowed on refusal."""
    try:
        if (_try_role(ctx, ctx[peer]) or {}).get('role') \
                in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
        deadline = time.monotonic() + ORPHAN_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(ORPHAN_POLL)
    except Exception:
        pass


def scenario_orphan_episode_bound(ctx):
    """Exercise the bounded orphan-episode field_orphaned journal
    contract on the run's keyed pair: the demoted-pair island pins
    the tracking standby onto its sibling's pacing ownerless
    checkpoint; the source container frozen and thawed alternates
    produced-nothing fetch misses with completed ownerless pulls
    across a held window; the peer must keep serving orphaned
    through every miss — never degraded — the armed failover misses
    still advancing, journal exactly one field_orphaned entry for
    the held episode on both the durable file and the served tail,
    and journal exactly one more only when a genuinely new episode
    begins after a non-orphaned apply. The subject is the deployed
    pair while the run config keys it, else the lane-staged keyed
    probe pair."""
    case = Case(
        'orphan-episode-bound',
        'Bounded field_orphaned journaling across a held orphan '
        'episode',
        'with the keyed pair settled — the deployed pair, or the '
        'lane-staged probe pair while the deployed pair runs '
        'unkeyed — each pass demotes the field owner onto the '
        'sibling standby\'s verified announced hint so the demoted '
        'pair islands on each other; the tracking peer keeps '
        'pulling the sibling\'s ownerless checkpoint while the '
        'source container is frozen past the fetch timeout and '
        'thawed again, so produced-nothing pull misses alternate '
        'with completed ownerless pulls across a multi-cycle '
        'window; the peer\'s served sync must stay orphaned through '
        'every evidence-free miss — never degraded — the armed '
        'failover misses still advancing, and the durable journal '
        'and served tail must each hold exactly one field_orphaned '
        'entry for the whole episode; the owner\'s promote ends the '
        'episode on a non-orphaned apply, a fresh island opens a '
        'second episode that journals exactly one more entry, the '
        'pair restores its launch roles, and two passes produce '
        'identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the orphan-episode leg needs is absent')
        # The keyed subject: the deployed pair while the run config
        # keys it, else the lane-staged probe pair — the island's
        # announced-source demote is keyed-only, so an unkeyed
        # deployment with no staged probe pair is inconclusive on
        # capability, not on the contract.
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the deployed pair '
                               'carries no --pair-token and no keyed '
                               'probe pair is staged — the '
                               'announced-source island the leg '
                               'induces is off')
        if subject is not ctx:
            case.observe('exercised on the lane-staged keyed probe '
                         'pair — the deployed pair runs unkeyed')
            ctx = subject
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the keyed subject '
                               'carries only one endpoint — the pair '
                               'the leg needs is absent')
        for action in ('pause_controller', 'unpause_controller'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the frozen-source miss '
                                   'induction cannot be driven')
        journals = ctx.get('journal_files') or {}
        if not all(journals.get(name)
                   and Path(journals[name]).is_file()
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the field_orphaned count '
                               'audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])

        # The launch layout the passes stage from: the unconfigured
        # peer — launched without a tracking source — must hold the
        # field so its demote owes the announced adoption the island
        # forms on; a swapped layout is restored before the leg
        # reports.
        deadline = time.monotonic() + ORPHAN_SETTLE
        if _pair_active(ctx) != 'active':
            _restore_layout(ctx, 'active', 'standby')
        owner = wait_for(lambda: _pair_active(ctx) == 'active'
                         and 'active' or None, deadline,
                         interval=ORPHAN_POLL)
        if owner != 'active':
            reports = {name: _try_role(ctx, ctx[name])
                       for name in ('active', 'standby')}
            if all(report is None for report in reports.values()):
                return case.finish('inconclusive', 'the pair is '
                                   'unreachable — monitor endpoints '
                                   + ctx['active'] + ' and '
                                   + ctx['standby'])
            return case.finish('inconclusive', 'the pair never '
                               'settled on its launch layout — the '
                               'unconfigured peer must hold the '
                               'field for the announced-source '
                               'island the leg induces')
        if wait_for(lambda: _tracking_standby(ctx, 'standby'),
                    deadline, interval=ORPHAN_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg restores to was never reached')
        owner, peer = 'active', 'standby'
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); pinned tracking peer: ' + peer)

        digests = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _episode_pass(
                    ctx, number, owner, peer)
                _judge_episode(record, note)
                digest = _digest(violations)
                evidence['record'] = record
                evidence['digest'] = dict(digest)
                evidence['violations'] = {
                    key: diagnostic for key, (diagnostic, _)
                    in violations.items()}
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'orphan-episode-bound-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'orphan-episode pass '
                              + str(number) + ' — the demote\'s '
                              'journaled adoption, the island\'s '
                              'orphaned verdicts, the alternating '
                              'miss/pull watch rows, the durable and '
                              'served field_orphaned counts, the '
                              'episode end and re-orphan, the '
                              'restore, and the normalized digest')
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
            # The launch layout for the cases behind this one — a
            # clean pass restores it by construction; an aborted pass
            # gets the source thawed and the documented role order
            # run again, best-effort.
            try:
                ctx['unpause_controller'](owner)
            except Exception:
                pass
            _restore_layout(ctx, owner, peer)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two held-episode passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the episode judge
        # replays each planted negative it must name; a silent judge
        # means the leg can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg\u2019s own audits: '
                               + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
