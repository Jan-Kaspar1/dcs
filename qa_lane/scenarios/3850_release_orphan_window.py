"""The release_orphan_window acceptance leg — one module per leg of the
scenario schedule; see qa_lane/scenarios/__init__.py for the ordering
rule and the shared seam."""
from .common import *

# Ordering: the bounded orphan-window leg hands the station's power-fail
# contact to the deployed pair, races the declared release against a
# legal demote, and restores both the field state it drove and the
# pair's launch roles — so it runs on the power-fail leg's own wiring
# and leaves the rig as it found it, ahead of the power-fail-trip leg
# that reads the settled duty-demand baseline it leaves behind.
RUNS_BEFORE = frozenset({'scenario_power_fail_trip'})


# --------------------------------------------------------------------
# The bounded interlock-release orphan-window contract — the
# per-revision lane evidence for the contract #828's fix establishes
# (WW-LCM-001 continuity, and the fail-safe rule that the field must
# never stand energized under a standing protection trip with no
# controller able to take over): a `POST /demote` landing inside the
# interlock-release propagation window must **bound** the energized
# orphan window — either the pending safe-state write lands before the
# demotion completes, or a promotion converges inside the declared
# bound and flushes it — never leaving the field outputs energized
# orphaned past that bound.
#
# The pump station's release propagates over driven scans, not on the
# write: `power-fail` (the journaled station contact) inverts into
# `power-ok`, each pump's `power-ok-in` leg drops that pump's
# aggregated availability, the protection interlock guarding the
# command trips with it, and only then does the group's staged image
# carry the released `p10x-cmd`. A legal operator demote landing
# between the contact write and the scan that writes that release
# abandons the in-flight write: the field keeps the last energized
# outputs while no peer owns them.
#
# The QA finding identities stay as recorded: #828's
# `demote-during-interlock-release-leaves-field-energized-and-wedges-pair`
# and its residual `demote-during-release-orphan-window`. The residual
# had both pumps hand-running with the power-fail contact asserted stay
# energized ~1s through the demote, both peers left
# `standby`/`orphaned`, the pair healing only when an operator promote
# converged and wrote the safe command. #828's fix closes the wedge
# behind that window — the demoted owner's checkpoints stamp
# `source_owns_field: false`, so every tracking pull lands the named
# `orphaned` verdict (promotable on the same convergence proof
# `tracking` stands on) instead of the staged-versus-field divergence
# that answered `409 not_converged` forever. This leg pins the bound
# itself: the field must never read energized past it.
#
# The leg stages the finding's own sequence against the deployed rig,
# on the pair's own field and through the pair's own control plane:
#
# - the pair settles on its launch layout — one member owning the
#   field with its sibling converged `tracking` behind it — and the
#   leg's field attachment joins the owner's pinned writer claim;
# - both pumps are hand-run through the receipted operator path (the
#   writable `p10x-mode`/`p10x-hand` points submitted through
#   `POST /command` and settled applied, attributed to this leg), so
#   both field outputs stand energized on the plant and in the served
#   image — the healthy precondition the trip interrupts;
# - the trip is driven on the field itself: the `power-fail` contact
#   written through the plant protocol under the owner's standing
#   claim, read back asserted while both outputs still read energized
#   (the release lands on the scan sequence, never on the write), and
#   the legal `POST /demote` issued in the same breath — inside the
#   release-propagation window;
# - the bounded orphan window, watched through the plant protocol's
#   own field reads and both peers' serving monitors: its rows record
#   the field's energization, the served trip carriers that attribute
#   the release to the declared interlock chain, and each peer's role,
#   sync verdict and served tick — no peer may report the
#   promote-blocking `diverged`, the orphan transition must be
#   journaled, and the energization must not survive the declared
#   bound;
# - the promote inside the bound: `POST /promote` on the sibling is
#   answered `promoting` (never the `not_converged` wedge the finding
#   recorded), the promoted peer's first field-owning scans write the
#   abandoned release, and the pair reconverges to one active plus
#   one tracking standby;
# - the rig restored: the driven contact released on the field, both
#   pumps returned to the operator state the leg found them in through
#   the receipted path, the trip's consequential alarm latches
#   acknowledged and re-armed, and the pair back on its launch roles
#   with the launch owner owning the field.
#
# Named diagnostics: release-orphan-window-failed tags the contract
# clauses — a hand run that never energized both outputs, a receipted
# write that never settled applied, a demote that never landed, a peer
# reporting the promote-blocking `diverged`, a release the declared
# interlock chain does not explain, an unjournaled orphan transition,
# a promote answered `not_converged` or otherwise refused, field
# outputs still energized past the declared bound, a pair that never
# reconverged, a rig left tripped or hand-run, a launch layout left on
# another owner — and release-orphan-window-nondeterministic tags the
# instability the contract does not answer for: a lost trip write, a
# lost field read, a monitor that stopped answering, a window that
# collected no row, a demotion that never settled, an unread journal,
# and two passes whose digests diverge.
#
# The orphan window's *shape* is deliberately outside the digest: the
# contract accepts either half — the pending write landing before the
# demotion completes, or the promotion flushing it inside the bound —
# and which half a pass observes is a property of the rig's scan
# timing, not of the build under test. The digest therefore records
# the bound's disposition (the energization never outlived it, the
# promote converged, the pair reconverged, the rig restored), while
# the observed shape and the measured window ride the evidence file and
# the case's observations, where the window is named by its own bound.
#
# A staged revision that predates the contract reports inconclusive:
# its divergence gate compares the quiesced peers' staged-released
# image against the energized field and wedges both peers `diverged`,
# so the promote answers `not_converged` forever and the pair stands
# with no active able to write the release. That shape is behavioural
# and mutually exclusive with the contract (which cannot report
# `diverged` at all), so the leg reads it as the pre-#828 signature
# rather than as a failure of a rule the build never carried.
#
# The unchecked-diagnostic self-check replays the judge over planted
# negatives and reports release-orphan-window-unchecked for any that
# slip through — the issue's named doctored case among them: the
# outputs asserted as bounded while they stay energized past the bound.

ROW_SETTLE = 45      # bound on the pair settling to its launch layout
ROW_HAND = 60        # bound on both pumps' hand-run reaching the field
ROW_BOUND = 12.0     # the declared orphan-window bound, in seconds
ROW_ROUNDS = 3       # answered energized rows the window spans
ROW_POLL = 0.4       # cadence polling the field and both monitors
ROW_RESTORE = 60     # bound on the field-state and launch-role restore
ROW_ACTOR = 'qa-lane'
DIAG_FAILED = 'release-orphan-window-failed'
DIAG_NONDET = 'release-orphan-window-nondeterministic'
DIAG_UNCHECKED = 'release-orphan-window-unchecked'

# The served points the leg drives and reads. The station interlock
# path first, then the two pumps' operator, availability and command
# points, so a model carrying only part of the wiring reports
# inconclusive naming what is missing rather than staging a partial
# episode.
ROW_SIGNALS = {
    'power-fail': 'power_fail', 'power-ok': 'power_ok',
    'p101-cmd': 'cmd1', 'p102-cmd': 'cmd2',
    'p101-avail': 'avail1', 'p102-avail': 'avail2',
    'p101-mode': 'mode1', 'p101-hand': 'hand1',
    'p102-mode': 'mode2', 'p102-hand': 'hand2'}
# The managed alarm latches the trip's consequences raise, and their
# writable ack inputs. Cleanup only: the leg acknowledges and re-arms
# them so the rig's alarm set is left as it found them, and reports
# what it did without making the trip's own alarm lifecycle a clause of
# this contract.
ROW_LATCHES = {
    'power-fail-unacknowledged': 'unack',
    'power-fail-ack': 'ack',
    'none-available-unacknowledged': 'none_unack',
    'none-available-ack': 'none_ack'}
# The four operator points the leg hands the pumps over with and
# returns them through.
ROW_OPERATORS = ('mode1', 'hand1', 'mode2', 'hand2')


def _row_sync(report):
    """The served StandbySync's variant name — 'unsynchronized' and
    'degraded' are bare strings, the rest single-key objects."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    if isinstance(sync, dict) and sync:
        return next(iter(sync))
    return None


def _row_view(ctx, name):
    """One read of a pair member's serving monitor: the role, the sync
    verdict, the served claim observation, and the tick. None where the
    monitor answers nothing: a dropped read is never a view."""
    report = _try_role(ctx, ctx.get(name) or '')
    if report is None:
        return None
    return {'role': report.get('role'), 'sync': _row_sync(report),
            'field_claim': report.get('field_claim'),
            'tick': report.get('tick')}


def _row_open(ctx):
    """The plant-protocol attachment the leg's field writes and reads
    ride — the raw client, because the shipped `dcs-plant-ctl` wraps
    its own mutations in its own claim and exposes no `ensure_writer`
    under a chosen owner token."""
    return _plant_connect(ctx)


def _row_close(stream):
    """Close the leg's field attachment, or the restore's."""
    if stream is None:
        return
    try:
        stream.close()
    except Exception:
        pass


def _row_field(stream, point):
    """One field read through the plant protocol — the stored sample
    the last field-owning scan wrote, or None where the read dropped: a
    lost observation, never the leg's verdict."""
    if stream is None:
        return None
    try:
        answer = _plant_request(stream, {'op': 'read', 'point': point})
    except Exception:
        return None
    if not isinstance(answer, dict) or answer.get('result') != 'sample':
        return None
    return answer.get('sample') or None


def _row_bool(sample):
    """A stored field sample's bool value — None where the sample
    carries none, or where the read dropped."""
    if not isinstance(sample, dict):
        return None
    value = sample.get('value')
    if isinstance(value, dict):
        return value.get('bool')
    return value if isinstance(value, bool) else None


def _row_energized(sample):
    """Whether a stored field sample reads an energized command: the
    bool sample whose value is true. A sample carrying no bool reads as
    not energized — and the row records which reads answered, so a lost
    read is never judged as a released field."""
    return _row_bool(sample) is True


def _row_write(stream, point, boolean):
    """One field write through the plant protocol — the trip's
    `power-fail` assertion and the restore write that releases it —
    under the attachment's standing writer claim. None where the answer
    was lost."""
    if stream is None:
        return None
    try:
        return _plant_request(stream, {'op': 'write', 'point': point,
                                       'value': {'bool': boolean}})
    except Exception:
        return None


def _row_join(stream, owner):
    """Join the field's standing writer claim under a pair member's own
    pinned token — the conditional `ensure_writer` the leg's trip write
    and restore write ride, refused while a different owner's claim
    stands with live holders. None where the answer was lost."""
    if stream is None:
        return None
    try:
        return _plant_request(stream, {'op': 'ensure_writer',
                                       'owner': owner})
    except Exception:
        return None


def _row_served(ctx, base, point):
    """One served image value read off a peer's snapshot — None where
    the read dropped or the point reported no sample."""
    if point is None:
        return None
    snapshot = _try_snapshot(ctx, base)
    if snapshot is None:
        return None
    return _point_value(snapshot, point)


def _row_command(point, boolean):
    """The receipted write's own command body — the identity a settled
    receipt carries, so the settlement audit can find it again."""
    return {'point': point, 'kind': 'bool', 'value': {'bool': boolean}}


def _row_submit(ctx, base, point, boolean):
    """One receipted write through the bounded operator path — the hand
    run the leg stages and the operator state it restores. Answers
    (status, receipt); a lost submission answers 0 with the error as its
    body."""
    try:
        status, receipt = http_json(
            'POST', base + '/command',
            {'command': {'write_value': _row_command(point, boolean)},
             'actor': ROW_ACTOR})
    except Exception as exc:
        return 0, {'error': str(exc)}
    return status, receipt


def _row_applied(ctx, base, command):
    """Whether the peer's journaled settled receipts carry `command`
    applied and attributed to this leg's own actor — the receipted
    hand run's own durable evidence. None where the journal never read,
    False where the settlement never landed."""
    try:
        _, journal = http_json('GET', base + '/journal?since=0')
    except Exception:
        return None
    for receipt in _settled_receipts(journal):
        if (receipt.get('command') or {}).get('write_value') != command:
            continue
        if receipt.get('actor') != ROW_ACTOR:
            return None
        return 'applied' in (receipt.get('outcome') or {})
    return False


def _row_floor(ctx, name):
    """The newest entry seq a pair member's served `GET /journal`
    carries, or None where the read dropped — the cursor the window's
    served audit reads above."""
    try:
        _, journal = http_json('GET', ctx[name] + '/journal')
    except Exception:
        return None
    entries = _journal_list(journal)
    return (entries[-1].get('seq') or 0) if entries else 0


def _row_entries(ctx, name, floor):
    """The member's served journal entries above `floor`, or None where
    the read dropped."""
    if floor is None:
        return None
    try:
        _, journal = http_json('GET', ctx[name] + '/journal?since='
                               + str(floor))
    except Exception:
        return None
    return _journal_list(journal)


def _row_events(entries, kind):
    """The `kind` event bodies a served journal slice carries — an
    unread slice reads as None, never as empty."""
    if entries is None:
        return None
    found = []
    for entry in entries:
        event = (entry or {}).get('event') or {}
        if isinstance(event.get(kind), dict):
            found.append(event[kind])
    return found


def _row_walk(entries):
    """The journaled `role_changed` transitions as (from, to, origin)
    tuples — the walk the demote's and the promote's attribution
    reads."""
    return [(change.get('from'), change.get('to'), change.get('origin'))
            for change in _row_events(entries, 'role_changed') or []]


def _row_settle(ctx, name, want, bound):
    """Poll a member's serving monitor until it reports role `want`;
    returns the matching view, else None."""
    accepted = []

    def found():
        view = _row_view(ctx, name)
        if view is not None and view.get('role') == want:
            accepted.append(view)
            return view
        return None

    wait_for(found, time.monotonic() + bound, interval=ROW_POLL)
    return accepted[-1] if accepted else None


def _row_released(row):
    """Whether a window row answered both field reads and found both
    outputs released — the pending safe-state write flushed."""
    commands = (row or {}).get('commands')
    if not isinstance(commands, dict) or len(commands) != 2:
        return False
    return not any(commands.values())


def _row_points(ctx, base):
    """The served points the leg drives and reads, resolved off the
    pair's own signal index. Returns (points, missing): a model
    carrying only part of the interlock wiring reports the names it
    lacks rather than staging a partial episode."""
    _, signals = http_json('GET', base + '/signals')
    entries = {entry.get('name'): entry for entry in signals.get('points', [])
               if isinstance(entry, dict)}
    points = {key: entries[name].get('point')
              for name, key in ROW_SIGNALS.items() if name in entries}
    for name, key in ROW_LATCHES.items():
        if name in entries:
            points[key] = entries[name].get('point')
    return points, sorted(set(ROW_SIGNALS) - set(entries))


def _row_pass(ctx, number, owner, peer, tokens):
    """One pass over the bounded orphan-window contract: settle the
    pair on its launch layout, hand-run both pumps through the
    receipted path with both outputs energized on the field, drive the
    `power-fail` trip and demote the owner in the same breath, watch
    the energized orphan window across the declared bound, promote the
    sibling inside it, and restore the field state and the launch roles
    the leg found. Returns (record, evidence); an aborted stage simply
    leaves its later keys absent."""
    record = {'pass': number, 'owner': owner, 'peer': peer,
              'tokens': {owner: tokens[owner], peer: tokens[peer]},
              'bound': ROW_BOUND}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    stream = None
    contact = None

    try:
        record['settled'] = wait_for(
            lambda: _pair_active(ctx) == owner
                    and _tracking_standby(ctx, peer) or None,
            time.monotonic() + ROW_SETTLE, interval=ROW_POLL) is not None
        if not record['settled']:
            record['stage_error'] = (
                'the pair never settled on its launch layout — ' + owner
                + ' must own the field with ' + peer + ' converged '
                'tracking behind it: '
                + json.dumps({'owner': _row_view(ctx, owner),
                              'peer': _row_view(ctx, peer)})[:300])
            return record, evidence

        # --- the contract surface ----------------------------------
        points, missing = _row_points(ctx, ctx[owner])
        record['points'] = points
        evidence['points'] = points
        if missing:
            record['stage_error'] = (
                'the deployed model lacks the power-fail interlock '
                'wiring this leg races — no signals ' + ', '.join(missing))
            return record, evidence
        contact = points['power_fail']
        stream = _row_open(ctx)
        joined = _row_join(stream, tokens[owner])
        if not isinstance(joined, dict) \
                or joined.get('result') not in ('done', 'claimed_shared'):
            record['stage_error'] = (
                'the writer claim refused the leg\'s field attachment '
                'under ' + owner + '\'s pinned token: '
                + json.dumps(joined)[:300])
            return record, evidence

        # --- the hand run -------------------------------------------
        record['found'] = {key: _row_served(ctx, ctx[owner], points[key])
                           for key in ROW_OPERATORS}
        record['latched'] = {key: _row_served(ctx, ctx[owner], points[key])
                             for key in ('unack', 'none_unack')
                             if points.get(key) is not None}
        evidence['found'] = record['found']
        evidence['latched'] = record['latched']
        standing = sorted(key for key, value in record['found'].items()
                          if value is True)
        if standing:
            record['stage_error'] = (
                'the pumps\' operator points already stand driven — the '
                'rig must hand them over through its own receipted path: '
                + ', '.join(standing))
            return record, evidence

        submissions = []
        for key in ROW_OPERATORS:
            status, receipt = _row_submit(ctx, ctx[owner], points[key],
                                          True)
            submissions.append({'point': points[key], 'key': key,
                                'command': _row_command(points[key],
                                                        True),
                                'status': status, 'receipt': receipt})
            if status != 200 \
                    or 'rejected' in ((receipt or {}).get('outcome') or {}):
                record['stage_error'] = (
                    'the receipted hand-run write on ' + key
                    + ' was refused: ' + str(status) + ' '
                    + json.dumps(receipt)[:300])
                return record, evidence
        record['submissions'] = submissions
        evidence['submissions'] = submissions

        def energized():
            """Whether the served image and the field agree that both
            outputs stand energized on a healthy contact — the hand
            run's own precondition, and the field truth the trip
            interrupts."""
            snapshot = _try_snapshot(ctx, ctx[owner])
            if snapshot is None:
                return None
            if _point_value(snapshot, points['power_fail']) is not False:
                return None
            if [_point_value(snapshot, points['cmd1']),
                _point_value(snapshot, points['cmd2'])] != [True, True]:
                return None
            if not all(_row_energized(_row_field(stream, point))
                       for point in (points['cmd1'], points['cmd2'])):
                return None
            return snapshot

        record['baseline'] = wait_for(energized,
                                      time.monotonic() + ROW_HAND,
                                      interval=ROW_POLL)
        evidence['baseline'] = record['baseline']
        if record['baseline'] is None:
            record['stage_error'] = (
                'the receipted hand run never energized both field '
                'outputs — the pumps must stand hand-run with p101-cmd '
                'and p102-cmd delivered to the field under a released '
                'power-fail contact')
            return record, evidence
        record['settled_receipts'] = {
            item['point']: _row_applied(ctx, ctx[owner], item['command'])
            for item in submissions}
        evidence['settled_receipts'] = record['settled_receipts']

        # --- the trip and the raced demote --------------------------
        record['floor'] = {name: _row_floor(ctx, name)
                           for name in (owner, peer)}
        verdict = _row_write(stream, contact, True)
        record['trip'] = {
            'write': verdict,
            'contact': _row_bool(_row_field(stream, contact)),
            'commands': {key: _row_energized(_row_field(stream,
                                                        points[key]))
                         for key in ('cmd1', 'cmd2')}}
        evidence['trip'] = record['trip']
        if not isinstance(verdict, dict) or verdict.get('result') != 'done':
            record['stage_error'] = (
                'the power-fail write on field point ' + str(contact)
                + ' was refused under the shared claim: '
                + json.dumps(verdict)[:300])
            return record, evidence
        if record['trip']['contact'] is not True:
            record['stage_error'] = (
                'the driven power-fail contact did not read asserted on '
                'the field: ' + json.dumps(record['trip'])[:300])
            return record, evidence
        # The defect's precondition, asserted rather than assumed: the
        # contact stands asserted while the field still reads energized
        # — the release lands on the driven scan sequence, never on the
        # write itself. Where the outputs were already released the
        # demote lands after the release, the contract's first half, and
        # the window is empty by construction.
        record['window_shape'] = 'flushed' \
            if not all(record['trip']['commands'].values()) else 'orphan'

        started = time.monotonic()
        status, demote = _settle_call(ctx[owner] + '/demote')
        record['demote'] = {'status': status, 'report': demote,
                            'seconds': round(time.monotonic() - started, 3)}
        evidence['demote'] = record['demote']
        if status != 200 or (demote or {}).get('role') != 'demoting':
            record['stage_error'] = (
                'POST /demote on ' + owner + ' answered ' + str(status)
                + ' ' + json.dumps(demote)[:300])
            return record, evidence
        record['demoted'] = _row_settle(ctx, owner, 'standby',
                                        (ROW_ROUNDS + 1) * ROW_POLL)

        # --- the bounded orphan window ------------------------------
        rows = []
        answered = 0
        energized_rows = 0
        released = None
        deadline = started + ROW_BOUND
        # The window ends one cadence short of the bound: a row landing
        # after that point is a genuine out-of-bound observation, never
        # a measurement artefact of the leg's own polling.
        watched = deadline - ROW_POLL
        while time.monotonic() < watched:
            row = _row_read(ctx, stream, ctx[owner], points, owner, peer,
                            started)
            rows.append(row)
            if row.get('answers') is None:
                time.sleep(ROW_POLL)
                continue
            answered += 1
            if any(row['commands'].values()):
                energized_rows += 1
            else:
                released = row
                break
            if energized_rows >= ROW_ROUNDS:
                # The orphan window exists and has been observed across
                # its declared rounds: the leg promotes inside the
                # bound, which is the contract's second half.
                break
            time.sleep(ROW_POLL)
        record['rows'] = rows
        record['answered'] = answered
        record['energized_rows'] = energized_rows
        evidence['rows'] = rows

        # --- the promote inside the bound ---------------------------
        status, promote = _settle_call(ctx[peer] + '/promote')
        record['promote'] = {'status': status, 'report': promote,
                             'seconds': round(time.monotonic() - started,
                                              3)}
        evidence['promote'] = record['promote']
        if status == 200 and (promote or {}).get('role') == 'promoting':
            record['promoted'] = _row_settle(ctx, peer, 'active',
                                             3 * ROW_POLL)
        if released is None:
            while time.monotonic() < watched:
                row = _row_read(ctx, stream, ctx[owner], points, owner,
                                peer, started)
                rows.append(row)
                if row.get('answers') is None:
                    time.sleep(ROW_POLL)
                    continue
                answered += 1
                if not any(row['commands'].values()):
                    released = row
                    break
                time.sleep(ROW_POLL)
            record['rows'] = rows
            record['answered'] = answered
            evidence['rows'] = rows
        record['released'] = released
        record['window_seconds'] = (released or {}).get('seconds')
        evidence['released'] = released

        # --- the reconvergence and the durable trail ----------------
        record['final'] = {'owner': _row_view(ctx, owner),
                           'peer': _row_view(ctx, peer)}
        record['served'] = {name: _row_entries(ctx, name,
                                               record['floor'][name])
                            for name in (owner, peer)}
        evidence['final'] = record['final']
        evidence['served'] = record['served']
    finally:
        record['restored'] = _row_restore(
            ctx, owner, peer, contact, record.get('points') or {},
            time.monotonic() + ROW_RESTORE)
        record['restored_owner'] = _pair_active(ctx)
        record['restored_contact'] = _row_field(stream, contact)
        record['restored_operators'] = {
            key: _row_served(ctx, ctx[owner], point)
            for key, point in (record.get('points') or {}).items()
            if key in ROW_OPERATORS}
        _row_close(stream)
    evidence['restored'] = record.get('restored')
    evidence['restored_owner'] = record.get('restored_owner')
    evidence['restored_contact'] = record.get('restored_contact')
    evidence['restored_operators'] = record.get('restored_operators')
    return record, evidence


def _row_read(ctx, stream, base, points, owner, peer, started):
    """One row of the orphan window: the plant protocol's own field
    reads of both outputs, the served trip carriers that attribute a
    release to the declared interlock chain, and both peers' serving
    views — the served evidence tuple every clause of the bound reads.
    `answers` is None where a field read dropped, so a lost read is
    never judged as a released field."""
    answers = {key: _row_field(stream, points[key])
               for key in ('cmd1', 'cmd2')}
    row = {'seconds': round(time.monotonic() - started, 3)}
    if any(sample is None for sample in answers.values()):
        row.update({'answers': None,
                    'commands': {key: _row_energized(sample)
                                 for key, sample in answers.items()}})
        return row
    row.update({
        'answers': answers,
        'commands': {key: _row_energized(sample)
                     for key, sample in answers.items()},
        'power_ok': _row_served(ctx, base, points['power_ok']),
        'avail': [_row_served(ctx, base, points['avail1']),
                  _row_served(ctx, base, points['avail2'])],
        'owner': _row_view(ctx, owner),
        'peer': _row_view(ctx, peer)})
    return row


def _row_restore(ctx, owner, peer, contact, points, deadline):
    """Best-effort restore inside one bound: put the pair back on its
    launch roles through the documented demote/promote order, release
    the driven `power-fail` contact on the field under the launch
    owner's claim, return both pumps to the operator state the leg
    found them in through the receipted path, and acknowledge the
    consequential alarm latches beside them, re-arming each ack input
    once its latch reads clear. Cleanup, never the contract the leg
    judges — cleanup runs again after an aborted pass, so it is
    idempotent and never reads a state it has not itself written."""
    tokens = ctx.get('plant_owner') or {}
    stream = None
    try:
        stream = _row_open(ctx)
    except Exception:
        stream = None
    try:
        while time.monotonic() < deadline:
            current = _pair_active(ctx)
            if current == peer:
                _settle_call(ctx[peer] + '/demote')
            elif current is None:
                for name in (owner, peer):
                    _settle_call(ctx[name] + '/promote')
            if contact is not None and _row_join(stream,
                                                 tokens.get(owner)):
                verdict = _row_write(stream, contact, False)
                if isinstance(verdict, dict) \
                        and verdict.get('result') == 'done' \
                        and _row_bool(_row_field(stream,
                                                 contact)) is False:
                    contact = None
            for key in ROW_OPERATORS:
                point = (points or {}).get(key)
                if point is not None \
                        and _row_served(ctx, ctx[owner], point) is True:
                    _row_submit(ctx, ctx[owner], point, False)
            _row_latches(ctx, owner, points or {})
            if contact is None \
                    and all(_row_served(ctx, ctx[owner], points[key])
                            is False
                            for key in ROW_OPERATORS
                            if (points or {}).get(key) is not None) \
                    and _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return True
            time.sleep(ROW_POLL)
    finally:
        _row_close(stream)
    return contact is None and _pair_active(ctx) == owner \
        and _tracking_standby(ctx, peer) is not None


def _row_latches(ctx, owner, points):
    """Walk the consequential alarm latches the trip raised toward the
    state the leg found: acknowledge a latched unacknowledged flag
    while its ack input reads released, and re-arm the ack input once
    the flag reads clear. Cleanup, never a clause — a refused write is
    swallowed and the next poll tries again."""
    for flag, ack in (('unack', 'ack'), ('none_unack', 'none_ack')):
        if points.get(flag) is None or points.get(ack) is None:
            continue
        latched = _row_served(ctx, ctx[owner], points[flag])
        held = _row_served(ctx, ctx[owner], points[ack])
        if latched is True and held is not True:
            _row_submit(ctx, ctx[owner], points[ack], True)
        elif latched is False and held is True:
            _row_submit(ctx, ctx[owner], points[ack], False)


def _row_judge(record, note):
    """Audit one pass's record — replayable, so the self-check can hand
    it planted negatives. `note(key, diagnostic, detail)` records each
    clause the record violates: DIAG_FAILED tags the contract clauses
    and DIAG_NONDET the instability the contract does not answer for.
    Each stage's clauses are guarded by that stage's own evidence, so a
    stage that never ran leaves its later keys unread rather than
    reporting an absence as a failure."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    owner, peer = record.get('owner'), record.get('peer')
    if record.get('stage_error') is not None:
        nondet('stage', 'the staging never completed: '
               + str(record['stage_error']))
        return
    if not record.get('settled'):
        failed('settle', 'the pair never settled on its launch layout — '
               + str(owner) + ' must own the field with ' + str(peer)
               + ' converged tracking behind it')
    if record.get('baseline') is None:
        nondet('hand-run', 'the hand run never read both field outputs '
               'energized under a released power-fail contact — the '
               'pumps must stand hand-run with p101-cmd and p102-cmd '
               'delivered to the field')
    elif record.get('submissions') is None:
        nondet('receipt', 'the receipted hand-run submissions never '
               'landed — the bounded operator path carries no evidence')
    else:
        for point, settled in (record.get('settled_receipts')
                               or {}).items():
            if settled is not True:
                failed('receipt', 'the receipted hand-run write on point '
                       + str(point) + ' never settled applied and '
                       'attributed to ' + ROW_ACTOR + ': '
                       + json.dumps(settled))
    if record.get('trip', {}).get('write') is None:
        nondet('trip', 'the power-fail write on the field never answered')
    demote = record.get('demote') or {}
    if demote.get('status') != 200 \
            or (demote.get('report') or {}).get('role') != 'demoting':
        failed('demote', 'POST /demote on ' + str(owner) + ' answered '
               + json.dumps(demote)[:300])
    if record.get('demoted') is None:
        nondet('demote', 'the demoted owner never settled standby — the '
               'demotion the release raced never completed')

    rows = record.get('rows')
    if not rows:
        nondet('window', 'the orphan window collected no row — the '
               'energized field was never observed')
        return
    answered = [row for row in rows if row.get('answers') is not None]
    if not answered:
        nondet('starved', 'the orphan window answered no row whose field '
               'reads both landed — a window of lost reads is not a '
               'verdict')
    for row in answered:
        for key in ('owner', 'peer'):
            view = row.get(key) or {}
            if view.get('sync') == 'diverged':
                failed('diverged', 'the ' + str(key) + ' peer reported '
                       'the promote-blocking diverged the finding left '
                       'both peers in — a staged-versus-field comparison '
                       'on an ownerless line: ' + json.dumps(row)[:300])
    if answered and not any(
            row.get('power_ok') is False
            and any(value is False for value in row.get('avail') or [])
            for row in answered):
        failed('trip', 'no served row read the declared interlock chain '
               '— power-ok down with a pump availability dropped — so '
               'the release the leg watched is not the trip\'s: '
               + json.dumps(answered[-1])[:300])

    # The bound itself: the energization must not outlive it, and the
    # row the bound closed on must read the field released.
    released = record.get('released')
    seconds = record.get('window_seconds')
    if record.get('window_shape') not in ('flushed', 'orphan'):
        failed('window', 'the pass recorded no orphan-window shape for '
               'the demote it staged: '
               + json.dumps(record.get('window_shape')))
    if released is None:
        failed('window-unbounded', 'the field outputs stood energized '
               'orphaned past the declared bound of '
               + str(record.get('bound', ROW_BOUND)) + 's — no promote '
               'inside it flushed the abandoned release: '
               + json.dumps(rows[-1])[:300])
    elif not _row_released(released):
        failed('window-unbounded', 'the row the bound closed on still '
               'reads the field outputs energized: '
               + json.dumps(released)[:300])
    elif not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
            or seconds > float(record.get('bound', ROW_BOUND)):
        failed('window-unbounded', 'the energized orphan window ran '
               + str(seconds) + 's against the declared bound of '
               + str(record.get('bound', ROW_BOUND)) + 's')

    promote = record.get('promote') or {}
    if promote.get('status') != 200 \
            or (promote.get('report') or {}).get('role') != 'promoting':
        failed('promote-wedged', 'POST /promote on ' + str(peer)
               + ' answered ' + json.dumps(promote)[:300]
               + ' — the divergence gate must never refuse the '
               'successor after an ownerless window')
    if record.get('promoted') is None:
        failed('promote-wedged', 'the promoted peer never settled active '
               'holding the field — the successor the bound must '
               'converge inside never took over')

    # The orphan transition's durable half: a peer that reported the
    # ownerless line journaled it by name.
    served = record.get('served') or {}
    if any((row.get('owner') or {}).get('sync') == 'orphaned'
           or (row.get('peer') or {}).get('sync') == 'orphaned'
           for row in answered):
        journaled = any(_row_events(entries, 'field_orphaned')
                        for entries in served.values()
                        if entries is not None)
        if not journaled:
            failed('orphan-journaled', 'a peer reported the ownerless '
                   'line while no served journal carries a '
                   'field_orphaned record naming it: '
                   + json.dumps(answered[-1])[:300])
    for name, entries in served.items():
        if entries is None:
            nondet('starved', 'the ' + str(name)
                   + ' peer\'s served journal never read for the '
                   'window\'s durable audit')
            continue
        walk = _row_walk(entries)
        if name == owner and not any(row[1] == 'demoting'
                                     for row in walk):
            failed('attribution', 'the demoted owner\'s journal carries '
                   'no walk into demoting — the leg\'s own demote left '
                   'no durable record: ' + json.dumps(walk)[:300])
        if name == peer and promote.get('status') == 200 \
                and not any(row[1] == 'promoting' for row in walk):
            failed('attribution', 'the promoted peer\'s journal carries '
                   'no walk into promoting — the recovery left no '
                   'durable record: ' + json.dumps(walk)[:300])

    # The pair closes on exactly one owner with the other tracking it.
    final = record.get('final') or {}
    peer_view, owner_view = final.get('peer') or {}, final.get('owner') or {}
    if peer_view.get('role') != 'active':
        failed('pair', 'the promoted peer reports '
               + str(peer_view.get('role')) + ', expected active: '
               + json.dumps(final)[:300])
    if owner_view.get('role') != 'standby':
        failed('pair', 'the demoted peer reports '
               + str(owner_view.get('role')) + ', expected standby: '
               + json.dumps(final)[:300])
    elif owner_view.get('sync') not in ('tracking', 'orphaned',
                                        'reinitialized'):
        failed('pair', 'the demoted peer did not reconverge on the '
               'successor that flushed the release — it reports '
               + str(owner_view.get('sync')) + ': '
               + json.dumps(final)[:300])

    # --- the rig restored -------------------------------------------
    if record.get('restored') is not True:
        failed('restore', 'the driven field state and the pair\'s '
               'launch roles did not restore after the staged episode')
    elif record.get('restored_owner') not in (None, owner):
        failed('restore', 'the restore left a field-owning member other '
               'than the launch owner ' + str(owner) + ': '
               + str(record.get('restored_owner')))
    if _row_bool(record.get('restored_contact')) is not False:
        failed('restore', 'the driven power-fail contact did not read '
               'released again — the field is left tripped under a leg '
               'that drove it: '
               + json.dumps(record.get('restored_contact'))[:200])
    standing = sorted(key for key, value
                      in (record.get('restored_operators') or {}).items()
                      if value is not False)
    if standing:
        failed('restore', 'the pumps the leg hand-ran were left in a '
               'driven operator state: ' + ', '.join(standing))


def _row_digest(record, violations):
    """The pass's normalized verdict record — identical across clean
    passes, and deliberately blind to which half of the contract the
    pass observed: the declared bound held, the promote converged, the
    pair reconverged, and the rig restored."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'window': 'bounded'
            if clean('window-unbounded', 'window', 'diverged')
            else 'unbounded',
        'promote': 'granted'
            if clean('promote-wedged') else 'wedged',
        'trip': 'propagated' if clean('trip') else 'unattributed',
        'journal': 'clean'
            if clean('orphan-journaled', 'attribution') else 'breached',
        'pair': 'reconverged'
            if clean('pair', 'settle', 'receipt') else 'diverged',
        'rig': 'restored' if clean('restore') else 'unrestored',
        'reads': 'complete'
            if clean('stage', 'demote', 'hand-run', 'window', 'starved')
            else 'partial',
    }


def _row_self_check():
    """The leg's unchecked-diagnostic self-test: replay the judge over
    each planted negative it must name — the issue's named doctored
    record (the outputs asserted as bounded while they stay energized
    past the bound), the wedged promote, the promote-blocking diverged,
    an unjournaled orphan transition, a hand run that never energized,
    a pair that never reconverged, a rig left tripped or hand-run, and
    the instability shapes — and require the judge to note each. A
    silent judge returns the negative names it let through."""
    slipped = []

    def audit(record):
        found = {}
        _row_judge(record, lambda key, diagnostic, detail:
                   found.setdefault(key, diagnostic))
        return found

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = _row_clean_record()
        mutate(record)
        if diagnostic not in audit(record).values():
            slipped.append(name)

    if audit(_row_clean_record()):
        slipped.append('clean-overstrict')

    # The issue's named doctored negative: the bound asserted as held
    # while the outputs stay energized past it.
    expect('energized-past-the-bound', lambda record:
           record.update({'released': None,
                          'window_seconds': ROW_BOUND + 1.0}))
    expect('energized-on-the-closing-row', lambda record:
           record['released'].update({'commands': {'cmd1': True,
                                                   'cmd2': False}}))
    expect('promote-wedged-not-converged', lambda record:
           record['promote'].update(
               {'status': 409,
                'report': {'error': {'not_converged': {'detail': 'x'}}}}))
    expect('promote-never-took-the-field', lambda record:
           record.update({'promoted': None}))
    expect('diverged-reported', lambda record:
           record['rows'][1]['peer'].update({'sync': 'diverged'}))
    expect('orphan-unjournaled', lambda record:
           record['served'].update({'active': [], 'standby': [
               {'seq': 7, 'tick': 22, 'event': {'role_changed': {
                   'from': 'standby', 'to': 'promoting',
                   'origin': 'request'}}}]}))
    expect('hand-run-never-energized', lambda record:
           record.update({'baseline': None}), DIAG_NONDET)
    expect('hand-run-receipt-unsettled', lambda record:
           record['settled_receipts'].update({301: False}))
    expect('release-not-the-declared-trip', lambda record:
           [row.update({'power_ok': True, 'avail': [True, True]})
            for row in record['rows']])
    expect('pair-never-reconverged', lambda record:
           record['final']['owner'].update({'sync': 'diverged'}))
    expect('promoted-peer-not-active', lambda record:
           record['final']['peer'].update({'role': 'demoting'}))
    expect('demote-unjournaled', lambda record:
           record['served'].update({'active': [
               {'seq': 7, 'tick': 21, 'event': {
                   'field_orphaned': {'aligned': 18}}}]}))
    expect('rig-left-tripped', lambda record:
           record.update({'restored_contact': {
               'value': {'bool': True}, 'quality': 'good', 'tick': 44}}))
    expect('rig-left-hand-run', lambda record:
           record['restored_operators'].update({'hand1': True}))
    expect('rig-left-another-owner', lambda record:
           record.update({'restored_owner': 'standby'}))
    expect('demote-never-landed', lambda record:
           record['demote'].update({'status': 409,
                                    'report': {'error': 'not_active'}}))

    # The instability shapes must report nondeterministic: a lost trip
    # write, a demotion that never settled, a window that collected no
    # row, a window of lost reads, an unread journal, and a staging
    # call that never completed.
    expect('trip-write-lost', lambda record:
           record.update({'trip': {'write': None, 'contact': None,
                                   'commands': {}}}), DIAG_NONDET)
    expect('demotion-never-settled', lambda record:
           record.update({'demoted': None}), DIAG_NONDET)
    expect('window-starved', lambda record:
           record.update({'rows': [], 'answered': 0, 'released': None}),
           DIAG_NONDET)
    expect('reads-all-lost', lambda record:
           [row.update({'answers': None}) for row in record['rows']],
           DIAG_NONDET)
    expect('journal-unreadable', lambda record:
           record.update({'served': {'active': None, 'standby': None}}),
           DIAG_NONDET)
    expect('pair-never-settled', lambda record:
           record.update({'settled': False}))
    expect('staging-refused', lambda record:
           record.update({'stage_error': 'the plant write was refused'}),
           DIAG_NONDET)
    return slipped


def _row_clean_record():
    """A pass record for the contract's own shape: both pumps hand-run
    and energized, the `power-fail` contact asserted, the owner demoted
    inside the release window, both peers reporting the named
    `orphaned` verdict while the field reads energized, the sibling's
    promote answered `promoting`, the promoted peer's first scans
    writing the abandoned release inside the declared bound, the pair
    reconverged on one active plus one tracking standby, and the rig
    restored to the operator state and launch roles the leg found."""
    return {
        'pass': 1,
        'owner': 'active',
        'peer': 'standby',
        'tokens': {'active': 424243, 'standby': 424244},
        'bound': ROW_BOUND,
        'settled': True,
        'points': {'power_fail': 120, 'power_ok': 206, 'cmd1': 100,
                   'cmd2': 101, 'avail1': 328, 'avail2': 360,
                   'mode1': 300, 'hand1': 301, 'mode2': 332,
                   'hand2': 333},
        'found': {'mode1': False, 'hand1': False, 'mode2': False,
                  'hand2': False},
        'latched': {'unack': False, 'none_unack': False},
        'submissions': [
            {'point': 300, 'key': 'mode1',
             'command': _row_command(300, True), 'status': 200,
             'receipt': {'outcome': {'accepted': {}}}},
            {'point': 332, 'key': 'mode2',
             'command': _row_command(332, True), 'status': 200,
             'receipt': {'outcome': {'accepted': {}}}},
            {'point': 301, 'key': 'hand1',
             'command': _row_command(301, True), 'status': 200,
             'receipt': {'outcome': {'accepted': {}}}},
            {'point': 333, 'key': 'hand2',
             'command': _row_command(333, True), 'status': 200,
             'receipt': {'outcome': {'accepted': {}}}}],
        'settled_receipts': {300: True, 332: True, 301: True, 333: True},
        'baseline': {'tick': 18, 'points': [
            {'point': 100, 'sample': {'value': {'bool': True},
                                       'quality': 'good'}},
            {'point': 101, 'sample': {'value': {'bool': True},
                                       'quality': 'good'}}]},
        'floor': {'active': 6, 'standby': 5},
        'trip': {'write': {'result': 'done'}, 'contact': True,
                 'commands': {'cmd1': True, 'cmd2': True}},
        'window_shape': 'orphan',
        'demote': {'status': 200, 'report': {'role': 'demoting',
                                             'tick': 19},
                   'seconds': 0.126},
        'demoted': {'role': 'standby', 'sync': 'orphaned',
                    'field_claim': 'held', 'tick': 22},
        'rows': [
            {'commands': {'cmd1': True, 'cmd2': True},
             'answers': {'cmd1': {'value': {'bool': True}},
                         'cmd2': {'value': {'bool': True}}},
             'power_ok': False, 'avail': [False, False],
             'owner': {'role': 'standby', 'sync': 'orphaned',
                       'field_claim': 'held', 'tick': 20},
             'peer': {'role': 'standby', 'sync': 'orphaned',
                      'field_claim': 'held', 'tick': 19},
             'seconds': round(ROW_BOUND / 10.0, 3)},
            {'commands': {'cmd1': True, 'cmd2': True},
             'answers': {'cmd1': {'value': {'bool': True}},
                         'cmd2': {'value': {'bool': True}}},
             'power_ok': False, 'avail': [False, False],
             'owner': {'role': 'standby', 'sync': 'orphaned',
                       'field_claim': 'held', 'tick': 21},
             'peer': {'role': 'standby', 'sync': 'orphaned',
                      'field_claim': 'held', 'tick': 20},
             'seconds': round(ROW_BOUND / 5.0, 3)},
            {'commands': {'cmd1': True, 'cmd2': True},
             'answers': {'cmd1': {'value': {'bool': True}},
                         'cmd2': {'value': {'bool': True}}},
             'power_ok': False, 'avail': [False, False],
             'owner': {'role': 'standby', 'sync': 'orphaned',
                       'field_claim': 'held', 'tick': 22},
             'peer': {'role': 'standby', 'sync': 'orphaned',
                      'field_claim': 'held', 'tick': 21},
             'seconds': round(ROW_BOUND * 0.3, 3)}],
        'answered': 3,
        'energized_rows': 3,
        'promote': {'status': 200, 'report': {'role': 'promoting',
                                              'tick': 22},
                    'seconds': 1.34},
        'promoted': {'role': 'active', 'sync': None,
                     'field_claim': 'held', 'tick': 25},
        'released': {'commands': {'cmd1': False, 'cmd2': False},
                     'answers': {'cmd1': {'value': {'bool': False}},
                                 'cmd2': {'value': {'bool': False}}},
                     'power_ok': False, 'avail': [False, False],
                     'owner': {'role': 'standby', 'sync': 'orphaned',
                               'field_claim': 'held', 'tick': 23},
                     'peer': {'role': 'active', 'sync': None,
                              'field_claim': 'held', 'tick': 26},
                     'seconds': round(ROW_BOUND / 2.0, 3)},
        'window_seconds': round(ROW_BOUND / 2.0, 3),
        'final': {'owner': {'role': 'standby', 'sync': 'tracking',
                            'field_claim': 'held', 'tick': 27},
                  'peer': {'role': 'active', 'sync': None,
                           'field_claim': 'held', 'tick': 28}},
        'served': {
            'active': [{'seq': 7, 'tick': 19, 'event': {'role_changed': {
                'from': 'active', 'to': 'demoting',
                'origin': 'request'}}},
                {'seq': 8, 'tick': 21, 'event': {
                    'field_orphaned': {'aligned': 18}}}],
            'standby': [{'seq': 6, 'tick': 20, 'event': {
                'field_orphaned': {'aligned': 18}}},
                {'seq': 7, 'tick': 22, 'event': {'role_changed': {
                    'from': 'standby', 'to': 'promoting',
                    'origin': 'request'}}},
                {'seq': 8, 'tick': 25, 'event': {'role_changed': {
                    'from': 'promoting', 'to': 'active',
                    'origin': 'request'}}}]},
        'restored': True,
        'restored_owner': 'active',
        'restored_contact': {'value': {'bool': False}, 'quality': 'good',
                             'tick': 60},
        'restored_operators': {'mode1': False, 'hand1': False,
                               'mode2': False, 'hand2': False},
    }


def scenario_release_orphan_window(ctx):
    """Exercise the bounded interlock-release orphan-window contract on
    the deployed pair: settle the pair on its launch layout, hand-run
    both pumps through the receipted operator path with both field
    outputs energized, drive the journaled power-fail contact on the
    field and POST /demote the owner in the same breath — inside the
    release-propagation window — then assert through the plant
    protocol's own field reads and both peers' serving monitors that
    the energization never outlives the declared bound: no peer reports
    the promote-blocking diverged, the orphan transition is journaled,
    POST /promote is answered promoting and its first field-owning
    scans write the abandoned release, the pair reconverges to one
    active plus one tracking standby, and the driven field state and
    launch roles restore; two passes produce identical digests."""
    case = Case(
        'release-orphan-window',
        'A demote inside the interlock-release window bounds the '
        'energized orphan window',
        'with the deployed pair settled and tracking, both pumps '
        'hand-run through the receipted operator path with p101-cmd '
        'and p102-cmd standing energized on the field, a '
        'plant-protocol write driving the journaled power-fail '
        'contact asserted under the settled owner\'s writer claim '
        'and POST /demote issued in the same breath: through the '
        'plant protocol\'s own field reads and both peers\' serving '
        'monitors the energized orphan window stays inside the '
        'declared bound — neither peer reporting the promote-blocking '
        'diverged, the orphan transition journaled by name, the '
        'promote answered promoting (never not_converged) and its '
        'first field-owning scans writing the abandoned release, and '
        'the pair reconverging to one active plus one tracking '
        'standby — while the driven contact is released, both pumps '
        'return to the operator state the leg found, and the launch '
        'roles restore; two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context carries '
                               'only one endpoint — the pair the '
                               'orphan-window leg needs is absent')
        if not ctx.get('plant'):
            return case.finish('inconclusive', 'the run context carries '
                               'no plant endpoint — the field reads the '
                               'bound is judged on ride that attachment')
        tokens = ctx.get('plant_owner') or {}
        if not tokens.get('active') or not tokens.get('standby'):
            return case.finish('inconclusive', 'the run pins no '
                               'plant-writer owner tokens for the pair '
                               'endpoints — the field write the trip '
                               'rides cannot join the owner\'s claim')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s monitor '
                                   'is unreachable: ' + str(exc)[:200])
        owner = wait_for(lambda: _pair_active(ctx),
                         time.monotonic() + ROW_SETTLE, interval=ROW_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        # The contract surface: the deployed model must carry the whole
        # interlock path and both pumps' operator points — a rig that
        # serves only part of the wiring never presents this contract.
        try:
            _points, missing = _row_points(ctx, ctx[owner])
        except Exception as exc:
            return case.finish('inconclusive', 'the served signal index '
                               'never answered: ' + str(exc)[:200])
        if missing:
            return case.finish('inconclusive', 'the deployed model lacks '
                               'the power-fail interlock wiring this leg '
                               'races — no signals ' + ', '.join(missing))
        peer = 'standby' if owner == 'active' else 'active'
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + ') under pinned token ' + hex(tokens[owner])
                     + '; tracking peer: ' + peer + ' (' + ctx[peer]
                     + ') under ' + hex(tokens[peer]))
        digests = []
        windows = []
        try:
            for number in (1, 2):
                violations = {}

                def note(key, diagnostic, detail):
                    violations.setdefault(key, (diagnostic, detail))

                record, evidence = _row_pass(ctx, number, owner, peer,
                                             tokens)
                evidence['record'] = record
                # A staged revision predating the contract presents the
                # pre-fix shape instead of a verdict the contract's own
                # judgement could read: the leg reports that
                # inconclusive rather than calling the wedge a failure
                # of a rule the build never carried.
                pre_contract = _row_pre_contract(record)
                if pre_contract:
                    evidence['pre_contract'] = pre_contract
                    ref = save_evidence(
                        ctx['evidence_dir'],
                        'release-orphan-window-pass-' + str(number)
                        + '.json', evidence)
                    case.evidence('file', ref, 'release-orphan-window '
                                  'pass ' + str(number) + ' — the '
                                  'pre-contract shape the staged '
                                  'revision presented')
                    return case.finish('inconclusive', pre_contract)
                _row_judge(record, note)
                digest = _row_digest(record, violations)
                record['digest'] = dict(digest)
                record['violations'] = {
                    key: diagnostic
                    for key, (diagnostic, _) in violations.items()}
                evidence['digest'] = dict(digest)
                evidence['violations'] = record['violations']
                windows.append({'pass': number,
                                'shape': record.get('window_shape'),
                                'seconds': record.get('window_seconds')})
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'release-orphan-window-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'release-orphan-window pass '
                              + str(number) + ' — the settled launch '
                              'layout and the hand-run baseline, the '
                              'driven trip and the demote it raced, '
                              'the orphan window\'s field-read and '
                              'serving-monitor rows, the promote and '
                              'the release it flushed, both journals\' '
                              'durable walks, the restored field state '
                              'and launch roles, and the normalized '
                              'digest')
                if violations:
                    name = DIAG_FAILED if any(
                        diagnostic == DIAG_FAILED
                        for diagnostic, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', name + ': ' + '; '.join(
                            detail for _, detail
                            in list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The field state and launch roles for the legs behind this
            # one: a clean pass restores them by construction; an aborted
            # pass may have left the field tripped or the ownership on
            # the promoted peer, so the documented restore runs again.
            try:
                points, _missing = _row_points(ctx, ctx[owner])
            except Exception:
                points = {}
            if not _row_restore(ctx, owner, peer, points.get('power_fail'),
                                points, time.monotonic() + ROW_RESTORE):
                case.observe('cleanup: the driven field state and the '
                             'pair\'s launch roles did not restore')
        if len(digests) < 2:
            return case.finish('inconclusive', 'the orphan-window '
                               'contract never produced two clean passes')
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two release-orphan-window passes, identical '
                     'digests: '
                     + json.dumps(digests[0], sort_keys=True))
        case.observe('the energized orphan window, bounded by name '
                     'against the declared bound of '
                     + str(ROW_BOUND) + 's: '
                     + json.dumps(windows, sort_keys=True)
                     + ' — flushed before the demotion completed or '
                     'flushed by the promote inside the bound, never '
                     'past it')

        # The unchecked-diagnostic self-check: the judge replays each
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _row_self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED
                               + ': planted negatives slipped the '
                               'leg’s own audits: '
                               + ', '.join(slipped))
        case.observe('the self-check leg’s planted negatives each '
                     'reported their named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive',
                           'the leg could not complete on this rig: '
                           + str(exc)[:500])


def _row_pre_contract(record):
    """The pre-#828 signature, read off one pass's record: the
    ownerless window's own rows show the quiesced peers reporting
    `diverged` — their staged released image against the still
    energized field — and the sibling's promote answered the
    `not_converged` refusal that verdict gates, so no controller could
    take the field and flush the release. The shape is behavioural and
    mutually exclusive with the contract, which cannot report
    `diverged` at all, so it reads as a staged revision predating the
    fix rather than as a defect."""
    if record.get('stage_error') is not None:
        return None
    rows = [row for row in record.get('rows') or []
            if row.get('answers') is not None]
    if not rows:
        return None
    diverged = [row for row in rows
                if (row.get('owner') or {}).get('sync') == 'diverged'
                or (row.get('peer') or {}).get('sync') == 'diverged']
    if not diverged:
        return None
    promote = record.get('promote') or {}
    if promote.get('status') == 200 \
            or 'not_converged' not in json.dumps(promote):
        return None
    return ('the ownerless window left both peers reporting the '
            'staged-versus-field divergence and the sibling\'s '
            'promote answered not_converged — the wedge #828 closed: '
            'a staged revision predating the bounded '
            'interlock-release orphan-window contract')