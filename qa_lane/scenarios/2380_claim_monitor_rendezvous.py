"""The claim_monitor_rendezvous acceptance leg — one module per leg
of the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg restores what it moves — the field-claim demote
# and the launch-layout restore land the pair back on its launch
# roles — so it needs no declared window.


# --------------------------------------------------------------------
# The wildcard-bind claim-monitor rendezvous contract — the
# per-revision lane evidence for #1135's fix (decision 101 /
# WW-LCM-001's ownership-integrity clause): the field's
# write-ownership claim carries the owner's monitor endpoint as the
# unkeyed pair's only provable rendezvous back into tracking, and on
# a wildcard bind — the deployed rig's 0.0.0.0 listener shape — the
# declared claim monitor must be dialable. The defect #1135 removed
# left the wildcard address stored verbatim: an undialable declared
# monitor, so the field-arbitrated rendezvous could never fire.
#
# The leg reads the standing claim through the plant protocol's
# claim surface — the non-mutating `probe_writer` verdict, which
# names the claim's owner and declared monitor to any foreign
# attachment — and dials the declared endpoint: a foreign dial must
# reach the owner's monitor and the recorded claim state must answer
# (source_owns_field: true on the owner's own checkpoint, its
# line_owner stamp naming the declared monitor's port). Then the
# rendezvous the endpoint exists for: the tracking standby promotes
# through the field-claim path — POST /promote, never POST /demote
# on the owner first — the superseded owner's first fenced write
# demotes it in place, and the demoted peer — the launched owner,
# the half with no configured tracking source — resolves a provable
# successor through the claim-declared monitor, adopts it (journaled
# tracking_source_adopted naming the declared endpoint), and
# reconverges to a tracking standby inside the lane's tick bound.
# The launch roles restore for the next pass and the cases behind.
#
# Named diagnostics: claim-monitor-rendezvous-failed tags the
# contract clauses — an absent, wildcard, undialable, or misnamed
# declared monitor, a dialed endpoint answering without the recorded
# claim state, the demotion never landing or walking off-contract,
# the loss journal missing or misattributed, the adoption absent or
# naming a foreign endpoint, the wedged or over-bound re-join, the
# re-joined peer's own document going foreign, the launch layout
# never restoring — and claim-monitor-rendezvous-nondeterministic
# tags the instability the contract does not answer for: a dropped
# claim-surface read, a dropped served-journal or checkpoint read,
# two passes' digests diverging. The unchecked-diagnostic
# self-check replays the judge over planted negatives and reports
# claim-monitor-rendezvous-unchecked for any that slip through.

RENDEZVOUS_SETTLE = 30        # bound on each settle/rejoin watch
RENDEZVOUS_POLL = 0.4         # cadence polling the peers mid-episode
RENDEZVOUS_DEADLINE = 15      # bound on the demotion/restore waits
RENDEZVOUS_DIAL = 5           # bound on the foreign-endpoint dial
RENDEZVOUS_REJOIN_TICKS = 60  # the lane tick bound on the demoted
                              # peer's re-join through the declared
                              # monitor
PAIR_PORTS = {'active': 8080, 'standby': 8081}
DIAG_FAILED = 'claim-monitor-rendezvous-failed'
DIAG_NONDET = 'claim-monitor-rendezvous-nondeterministic'
DIAG_UNCHECKED = 'claim-monitor-rendezvous-unchecked'


def _verdict_monitor(response):
    """The declared monitor the claim verdict names (None absent)."""
    return ((response or {}).get('error') or {}).get('monitor')


def _verdict_owner(response):
    return ((response or {}).get('error') or {}).get('owner')


def _wildcard(declared):
    """Whether a declared monitor names an unspecified bind — the
    '0.0.0.0:8080' the defect stored, never a dialable rendezvous."""
    host = str(declared or '').rsplit(':', 1)[0]
    return host.strip('[]') in ('0.0.0.0', '::', '')


def _claim_surface(ctx):
    """The field's standing claim read through the plant protocol —
    `probe_writer`, the non-mutating verdict a foreign attachment's
    mutation would meet: owner token plus the declared monitor."""
    return _try_plant(ctx, {'op': 'probe_writer'})


def _dial_monitor(declared, port):
    """The foreign-endpoint reachability the rendezvous contract
    needs: dial the declared monitor's /checkpoint and read the
    recorded claim state back — source_owns_field: true on the
    claim owner's document, the line_owner stamp carrying the
    declared port. A wildcard declaration is never dialed — the
    defect's own shape."""
    if _wildcard(declared):
        return {'answered': False,
                'error': 'wildcard declaration ' + str(declared)}
    try:
        _, doc = http_json('GET', 'http://' + str(declared)
                           + '/checkpoint', timeout=RENDEZVOUS_DIAL)
    except Exception as exc:
        return {'answered': False, 'error': str(exc)[:200]}
    line = doc.get('line_owner')
    return {'answered': True,
            'owns_field': doc.get('source_owns_field'),
            'line_owner': line,
            'line_port': (str(line).endswith(':' + str(port))
                          if line is not None else None)}


def _served_journal(ctx, name, floor):
    """The peer's served journal entries since `floor`, or None."""
    try:
        _, body = http_json('GET', ctx[name] + '/journal?since='
                            + str(floor))
    except Exception:
        return None
    return _journal_list(body)


def _journaled(ctx, name, floor, kind):
    """The bodies of `kind` events the peer journaled since `floor`."""
    entries = _served_journal(ctx, name, floor)
    if entries is None:
        return None
    return [entry['event'][kind] for entry in entries
            if kind in (entry.get('event') or {})]


def _durable_kinds(ctx, name):
    """The event kinds the peer's bind-mounted --journal-file holds."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except Exception:
        return None
    kinds = set()
    for record in records:
        event = ((record.get('entry') or {}).get('event')
                 or record.get('run_boundary') or {})
        kinds.update(event)
    return kinds


def _durable_adoptions(ctx, name):
    """The sources the peer's --journal-file tracking_source_adopted
    records name, or None when the file can't be read."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except Exception:
        return None
    return [(record.get('entry') or {}).get('event', {})
            .get('tracking_source_adopted', {}).get('source')
            for record in records
            if 'tracking_source_adopted'
            in ((record.get('entry') or {}).get('event') or {})]


def _checkpoint(ctx, name):
    _, body = http_json('GET', ctx[name] + '/checkpoint')
    return body


def _sync_kind(report):
    sync = ((report or {}).get('sync') or {})
    for kind in ('tracking', 'orphaned', 'diverged', 'unsynchronized'):
        if kind in sync:
            return kind
    return 'missing'


def _is_tracking(report):
    return _sync_kind(report) == 'tracking'


def _role_tick(report):
    tick = (report or {}).get('tick')
    return tick if isinstance(tick, int) else None


def _wait_standby(ctx, name, watch):
    """Poll /role until the peer reports standby; the poll rows land
    in `watch` for the timeline evidence."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if (report or {}).get('role') == 'standby' \
            else None
    return wait_for(found, time.monotonic() + RENDEZVOUS_DEADLINE,
                    interval=RENDEZVOUS_POLL)


def _wait_tracking(ctx, name, watch):
    """Poll /role until the peer reports the tracking sync."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        if (report or {}).get('role') == 'standby' \
                and _is_tracking(report):
            return report
        return None
    return wait_for(found, time.monotonic() + RENDEZVOUS_SETTLE,
                    interval=RENDEZVOUS_POLL)


def _judge_rendezvous(record, note):
    """Audit one pass's record — replayable, so the self-check can
    hand it planted negatives. `note(key, diagnostic, detail)`
    records each clause the record violates: DIAG_FAILED tags the
    rendezvous contract clauses — the declared monitor's presence,
    routability, and recorded-claim-state answer on both the
    baseline owner's and the successor's claims, the demote-in-place,
    the attributed loss journal, the adoption naming the declared
    monitor, the bounded tracking re-join, the re-joined peer's own
    document, the durable journal mirror, the restored launch roles
    — and DIAG_NONDET tags the instability the contract does not
    answer for: a dropped claim-surface read, a dropped journal or
    checkpoint read, a refused control-plane call that leaves the
    staging short. An aborted stage ends the audit where the pass
    ended — the later keys it never wrote are not clauses."""
    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    def nondet(key, detail):
        note(key, DIAG_NONDET, detail)

    def audit_declared(prefix, monitor, dial, port, owner_token,
                       verdict_owner):
        """One declared-monitor clause set: the claim must name an
        owner, declare a routable monitor on the expected listen
        port, and the dialed endpoint must answer the recorded claim
        state."""
        if verdict_owner is not None \
                and verdict_owner != owner_token:
            failed(prefix, 'the claim names foreign owner '
                   + str(verdict_owner) + ' — expected '
                   + str(owner_token))
        if monitor is None:
            failed(prefix, 'the claim declares no monitor — the '
                   'rendezvous surface this leg proves')
            return
        if _wildcard(monitor):
            failed(prefix, 'the claim declares the wildcard bind '
                   + str(monitor) + ' — undialable, the #1135 '
                   'defect this leg convicts')
            return
        if not str(monitor).endswith(':' + str(port)):
            failed(prefix, 'the declared monitor ' + str(monitor)
                   + ' does not name the claim owner on :'
                   + str(port))
        if not isinstance(dial, dict):
            nondet(prefix + '-dial', 'the foreign-endpoint dial of '
                   'the declared monitor never recorded')
            return
        if not dial.get('answered'):
            failed(prefix + '-dial', 'the declared monitor '
                   + str(monitor) + ' refused the foreign dial — '
                   'the rendezvous the field arbitrates could '
                   'never reach it: ' + str(dial.get('error'))[:200])
        elif dial.get('owns_field') is not True:
            failed(prefix + '-dial', 'the endpoint at the declared '
                   'monitor ' + str(monitor) + ' answers without '
                   'the recorded claim state — source_owns_field '
                   + json.dumps(dial.get('owns_field')))
        elif dial.get('line_port') is False:
            failed(prefix + '-dial', 'the serving monitor at '
                   + str(monitor) + ' stamps line_owner '
                   + str(dial.get('line_owner')) + ' — off the '
                   'declared port :' + str(port))

    baseline = record.get('baseline')
    if not isinstance(baseline, dict):
        nondet('baseline', 'the claim-surface read dropped — no '
               'verdict landed for the settled owner')
        return
    audit_declared('baseline', record.get('baseline_monitor'),
                   record.get('baseline_dial'),
                   record.get('owner_port'), record.get('owner_token'),
                   _verdict_owner(baseline))

    promote = record.get('promote') or {}
    if promote.get('status') != 200:
        nondet('promote', 'the field-claim-path demote never staged '
               '— POST /promote on the tracking standby answered '
               + str(promote.get('status')) + ' '
               + json.dumps(promote.get('body'))[:160])
        return
    demoted = record.get('demoted') or {}
    if not demoted.get('walked'):
        failed('demotion', 'the superseded owner never reported '
               'standby — the demotion the rendezvous re-joins '
               'from never landed: ' + json.dumps(demoted)[:200])
        return
    walk = demoted.get('walk') or []
    if any(role is None for role in walk):
        nondet('demotion', 'the demoted peer dropped a /role read '
               'inside the demotion watch — the walk never fully '
               'landed: ' + json.dumps(walk)[:160])
    elif any(role not in ('active', 'promoting', 'demoting',
                          'standby')
             for role in walk):
        failed('demotion', 'the demoted peer walked an unexpected '
               'role sequence ' + json.dumps(walk)[:160])

    losses = record.get('losses')
    if losses is None:
        nondet('loss', 'the served-journal read dropped — the '
               'attributed-loss audit never landed')
    elif not losses:
        failed('loss', 'the demoted peer journaled no '
               'field_claim_lost for the supersede')
    elif len(losses) > 1:
        failed('loss', 'the demoted peer journaled '
               + str(len(losses)) + ' field_claim_lost records for '
               'one supersede')
    else:
        claimant = (losses[0] or {}).get('claimant')
        if claimant is None:
            failed('loss', 'the journaled field_claim_lost carries '
                   'no claimant field: ' + json.dumps(losses)[:200])
        elif claimant != record.get('peer_token'):
            failed('loss', 'the journaled field_claim_lost names '
                   + str(claimant) + ' — the successor\u2019s owner '
                   'token ' + str(record.get('peer_token'))
                   + ' was expected')

    seized = record.get('seized')
    if not isinstance(seized, dict):
        nondet('seized', 'the claim-surface read dropped after the '
               'preempt — the successor\u2019s declared monitor '
               'never landed')
        return
    audit_declared('seized', record.get('seized_monitor'),
                   record.get('seized_dial'),
                   record.get('peer_port'), record.get('peer_token'),
                   _verdict_owner(seized))
    declared = record.get('seized_monitor')

    adoptions = record.get('adoptions')
    if adoptions is None:
        nondet('adopted', 'the served-journal read dropped — the '
               'adoption audit never landed')
    else:
        sources = [(body or {}).get('source') for body in adoptions]
        if len(adoptions) > 1:
            failed('adopted', 'the demoted peer journaled '
                   + str(len(adoptions)) + ' tracking_source_adopted '
                   'records for one re-join: '
                   + json.dumps(sources)[:200])
        elif len(adoptions) == 1:
            if str(sources[0]) != str(declared):
                failed('adopted', 'the journaled adoption names '
                       + str(sources[0]) + ' — not the '
                       'claim-declared monitor ' + str(declared))
        else:
            pinned = declared is not None and any(
                str(source) == str(declared)
                for source in (record.get('durable_sources') or []))
            if not pinned:
                failed('adopted', 'the demoted peer journaled no '
                       'tracking_source_adopted and holds no '
                       'durable pin naming the claim-declared '
                       'monitor ' + str(declared) + ' — the '
                       'rendezvous adoption evidence is absent')

    rejoin = record.get('rejoin') or {}
    if not rejoin.get('tracked'):
        failed('rejoin', 'the demoted peer never reconverged to a '
               'tracking standby through the declared monitor — '
               'the wedge the rendezvous exists to close: '
               + json.dumps(rejoin)[:200])
    else:
        ticks = rejoin.get('ticks')
        if isinstance(ticks, int) \
                and ticks > RENDEZVOUS_REJOIN_TICKS:
            failed('rejoin', 'the demoted peer took ' + str(ticks)
                   + ' ticks to reconverge — beyond the lane bound '
                   + str(RENDEZVOUS_REJOIN_TICKS))

    document = record.get('document')
    if document is None:
        nondet('document', 'the re-joined peer\u2019s checkpoint '
               'read dropped — its line document never landed')
    elif document.get('owns_field') is not True:
        failed('document', 'the re-joined peer serves a checkpoint '
               'whose source_owns_field is '
               + json.dumps(document.get('owns_field'))
               + ' — the adopted line never verified')
    elif document.get('line_port') is False:
        failed('document', 'the re-joined peer stamps line_owner '
               + str(document.get('line_owner')) + ' — off the '
               'successor\u2019s port :' + str(record.get('peer_port')))

    durable = record.get('durable')
    if durable is None:
        nondet('durable', 'the durable journal-file read dropped — '
               'the mirror audit never landed')
    else:
        if 'field_claim_lost' not in durable:
            failed('durable', 'the durable journal file carries no '
                   'field_claim_lost record')
        if 'tracking_source_adopted' not in durable:
            failed('durable', 'the durable journal file carries no '
                   'tracking_source_adopted record')

    if 'restored' in record:
        if not record.get('restored'):
            failed('restore', 'the pair never settled back to its '
                   'launch roles')
        elif record.get('restored_owner') \
                != record.get('owner_token'):
            failed('restore', 'the restored field claim names '
                   + str(record.get('restored_owner'))
                   + ' — the launch owner\u2019s token '
                   + str(record.get('owner_token'))
                   + ' was expected')


def _digest(violations):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause."""
    def clean(*keys):
        return not any(key in violations for key in keys)
    return {
        'declared': 'dialable'
            if clean('baseline', 'baseline-dial') else 'refused',
        'demote': 'in-place' if clean('promote', 'demotion') else 'held',
        'loss': 'attributed' if clean('loss') else 'unattributed',
        'successor': 'dialable'
            if clean('seized', 'seized-dial') else 'refused',
        'adopted': 'declared' if clean('adopted') else 'foreign',
        'rejoin': 'tracked' if clean('rejoin') else 'wedged',
        'document': 'verified' if clean('document') else 'foreign',
        'durable': 'journaled' if clean('durable') else 'absent',
        'restore': 'restored' if clean('restore') else 'unrestored'}


def _self_check():
    """The leg's unchecked-diagnostic self-test: replay the
    rendezvous judge over each planted negative the issue names — an
    undialable declared monitor asserted as rendezvous-capable, a
    wildcard declaration, a dialed endpoint answering without the
    recorded claim state, a foreign or silent adoption, a wedged
    re-join, an unrestored pair — and require the judge to note
    each. A silent judge returns the negative names it let through."""
    slipped = []

    def clean_record():
        return {'owner_token': 424243, 'peer_token': 424244,
                'owner_port': 8080, 'peer_port': 8081,
                'baseline': {'result': 'error',
                             'error': {'kind': 'fenced',
                                       'owner': 424243,
                                       'monitor': '172.18.0.2:8080'}},
                'baseline_monitor': '172.18.0.2:8080',
                'baseline_dial': {'answered': True,
                                  'owns_field': True,
                                  'line_owner': '0.0.0.0:8080',
                                  'line_port': True},
                'promote': {'status': 200,
                            'body': {'role': 'promoting'}},
                'demoted': {'walked': True,
                            'walk': ['active', 'demoting', 'standby'],
                            'tick': 40},
                'losses': [{'point': 200, 'claimant': 424244}],
                'seized': {'result': 'error',
                           'error': {'kind': 'fenced',
                                     'owner': 424244,
                                     'monitor': '172.18.0.3:8081'}},
                'seized_monitor': '172.18.0.3:8081',
                'seized_dial': {'answered': True,
                                'owns_field': True,
                                'line_owner': '0.0.0.0:8081',
                                'line_port': True},
                'adoptions': [{'source': '172.18.0.3:8081'}],
                'durable_sources': ['172.18.0.3:8081'],
                'rejoin': {'tracked': True, 'ticks': 9},
                'document': {'owns_field': True,
                             'line_owner': '0.0.0.0:8081',
                             'line_port': True},
                'durable': ['field_claim_lost', 'role_changed',
                            'tracking_source_adopted'],
                'restored': True, 'restored_owner': 424243}

    def expect(name, mutate, diagnostic=DIAG_FAILED):
        record = clean_record()
        mutate(record)
        found = {}
        _judge_rendezvous(
            record,
            lambda key, diag, detail: found.setdefault(key, diag))
        if diagnostic not in found.values():
            slipped.append(name)

    # The doctored negative the issue names first: an undialable
    # declared monitor asserted as rendezvous-capable.
    expect('undialable-declared', lambda record: record.update(
        {'baseline_monitor': '172.18.0.99:8080',
         'baseline_dial': {'answered': False,
                           'error': 'connection refused'}}))
    # The wildcard declaration — the #1135 defect's own shape.
    expect('wildcard-declared', lambda record: record.update(
        {'baseline_monitor': '0.0.0.0:8080',
         'baseline_dial': {'answered': False,
                           'error': 'wildcard declaration'}}))
    # The dialed endpoint answers without the recorded claim state.
    expect('foreign-endpoint', lambda record: record.update(
        {'baseline_dial': {'answered': True, 'owns_field': False,
                           'line_owner': '0.0.0.0:8080',
                           'line_port': True}}))
    # The declared monitor names a foreign port.
    expect('misnamed-declared', lambda record: record.update(
        {'baseline_monitor': '172.18.0.2:9999',
         'baseline_dial': {'answered': True, 'owns_field': True,
                           'line_owner': '0.0.0.0:9999',
                           'line_port': True}}))
    # The successor's claim declares an undialable monitor.
    expect('successor-undialable', lambda record: record.update(
        {'seized_monitor': '0.0.0.0:8081',
         'seized_dial': {'answered': False,
                         'error': 'wildcard declaration'}}))
    # The demotion never landed.
    expect('demotion-held', lambda record: record.update(
        {'demoted': {'walked': False, 'walk': ['active'],
                     'tick': 40}}))
    # The loss journal misattributed to a foreign claimant.
    expect('loss-misattributed', lambda record: record.update(
        {'losses': [{'point': 200, 'claimant': 0xDEAD}]}))
    # The adoption named a foreign endpoint, then none at all.
    expect('adoption-foreign', lambda record: record.update(
        {'adoptions': [{'source': '172.18.0.9:9999'}]}))
    expect('adoption-silent', lambda record: record.update(
        {'adoptions': [], 'durable_sources': []}))
    # The re-join wedged, then over-ran the tick bound.
    expect('rejoin-wedged', lambda record: record.update(
        {'rejoin': {'tracked': False, 'ticks': None}}))
    expect('rejoin-over-bound', lambda record: record.update(
        {'rejoin': {'tracked': True, 'ticks': 200}}))
    # The re-joined peer's own document went foreign.
    expect('document-foreign', lambda record: record.update(
        {'document': {'owns_field': False,
                      'line_owner': '0.0.0.0:8081',
                      'line_port': True}}))
    # The durable mirror dropped the adoption record.
    expect('durable-absent', lambda record: record.update(
        {'durable': ['field_claim_lost', 'role_changed']}))
    # The launch roles never restored.
    expect('unrestored', lambda record: record.update(
        {'restored': False, 'restored_owner': 424244}))
    # The instability the contract does not answer for must report
    # nondeterministic, not failed: dropped reads and a refused
    # control-plane call that leaves the staging short.
    expect('baseline-read-dropped', lambda record: record.update(
        {'baseline': None}), DIAG_NONDET)
    expect('promote-refused', lambda record: record.update(
        {'promote': {'status': 409, 'body': 'not_converged'}}),
        DIAG_NONDET)
    expect('seized-read-dropped', lambda record: record.update(
        {'seized': None}), DIAG_NONDET)
    expect('journal-read-dropped', lambda record: record.update(
        {'losses': None, 'adoptions': None}), DIAG_NONDET)
    expect('document-read-dropped', lambda record: record.update(
        {'document': None}), DIAG_NONDET)
    return slipped


def _rendezvous_pass(ctx, number, owner, peer, tokens):
    """One rendezvous pass: read the settled owner's declared monitor
    through the plant claim surface and dial it as a foreign
    endpoint; demote the owner through the field-claim path — the
    tracking standby's POST /promote, never a POST /demote first —
    and audit the demoted peer's resolution, adoption, bounded
    re-join, and line document through the claim-declared monitor;
    restore the launch roles. Returns (record, evidence): the record
    is what the judge replays; an aborted stage simply leaves its
    later keys absent for the judge to name."""
    record = {'owner_token': tokens[owner],
              'peer_token': tokens[peer],
              'owner_port': PAIR_PORTS[owner],
              'peer_port': PAIR_PORTS[peer]}
    evidence = {'pass': number, 'owner': owner, 'peer': peer}
    floors = {name: _journal_cursor(ctx, ctx[name])
              for name in (owner, peer)}
    evidence['floors'] = floors
    try:
        # Stage A — the settled owner's claim surface: the standing
        # claim names the owner and declares a monitor a foreign
        # endpoint can dial for the recorded claim state.
        verdict = _claim_surface(ctx)
        record['baseline'] = verdict
        evidence['baseline'] = verdict
        if not isinstance(verdict, dict):
            return record, evidence
        monitor = _verdict_monitor(verdict)
        record['baseline_monitor'] = monitor
        dial = _dial_monitor(monitor, PAIR_PORTS[owner]) \
            if monitor else None
        record['baseline_dial'] = dial
        evidence['baseline_dial'] = dial

        # Stage B — the field-claim demote: the tracking standby
        # promotes with no POST /demote on the owner first; the
        # superseded owner's first fenced write demotes it in place.
        status, body = _settle_call(ctx[peer] + '/promote')
        record['promote'] = {'status': status, 'body': body}
        evidence['promote'] = record['promote']
        if status != 200:
            return record, evidence
        demotion = []
        walked = _wait_standby(ctx, owner, demotion)
        evidence['demotion'] = demotion[-8:]
        demote_tick = _role_tick(walked)
        record['demoted'] = {
            'walked': walked is not None,
            'walk': [(row.get(owner) or {}).get('role')
                     for row in demotion],
            'tick': demote_tick}
        if walked is None:
            return record, evidence
        record['losses'] = _journaled(
            ctx, owner, floors[owner], 'field_claim_lost')
        evidence['field_claim_lost'] = record['losses']

        # The successor's claim surface — the declared monitor the
        # demoted peer's rendezvous resolves through.
        seized = _claim_surface(ctx)
        record['seized'] = seized
        evidence['seized'] = seized
        if not isinstance(seized, dict):
            return record, evidence
        declared = _verdict_monitor(seized)
        record['seized_monitor'] = declared
        dial = _dial_monitor(declared, PAIR_PORTS[peer]) \
            if declared else None
        record['seized_dial'] = dial
        evidence['seized_dial'] = dial

        # The re-join: the demoted peer resolves the claim-declared
        # monitor, adopts it (journaled tracking_source_adopted), and
        # reports tracking inside the lane's tick bound.
        rejoin = []
        tracked = _wait_tracking(ctx, owner, rejoin)
        evidence['rejoin'] = rejoin[-8:]
        track_tick = _role_tick(tracked)
        record['rejoin'] = {
            'tracked': tracked is not None,
            'ticks': (track_tick - demote_tick
                      if track_tick is not None
                      and demote_tick is not None else None)}
        record['adoptions'] = _journaled(
            ctx, owner, floors[owner], 'tracking_source_adopted')
        evidence['tracking_source_adopted'] = record['adoptions']
        record['durable_sources'] = _durable_adoptions(ctx, owner)
        evidence['durable_sources'] = record['durable_sources']
        if tracked is None:
            return record, evidence
        try:
            doc = _checkpoint(ctx, owner)
            line = doc.get('line_owner')
            record['document'] = {
                'owns_field': doc.get('source_owns_field'),
                'line_owner': line,
                'line_port': (str(line).endswith(':' + str(
                    PAIR_PORTS[peer])) if line is not None else None)}
            evidence['checkpoint'] = doc
        except Exception:
            record['document'] = None
        durable = _durable_kinds(ctx, owner)
        record['durable'] = sorted(durable) if durable is not None \
            else None
        evidence['durable'] = record['durable']
    finally:
        # The launch roles for the next pass and the cases behind —
        # the demoted owner's promote re-claims the field through the
        # same field-claim path; the sibling reconverges through its
        # configured source.
        _restore_layout(ctx, owner, peer)
        record['restored'] = (
            _pair_active(ctx) == owner
            and _tracking_standby(ctx, peer) is not None)
        verdict = _claim_surface(ctx)
        record['restored_owner'] = _verdict_owner(verdict) \
            if isinstance(verdict, dict) else None
    return record, evidence


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore: demote whichever peer still
    owns the field, promote the launch owner back over it, and let
    the pair reconverge. Every step is retried inside the bound and
    swallowed on refusal."""
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting'):
            _settle_call(ctx[peer] + '/demote')
            _wait_standby(ctx, peer, [])
        deadline = time.monotonic() + RENDEZVOUS_SETTLE
        while time.monotonic() < deadline:
            if (_try_role(ctx, ctx[owner]) or {}).get('role') \
                    != 'active':
                _settle_call(ctx[owner] + '/promote')
            if _pair_active(ctx) == owner \
                    and _tracking_standby(ctx, peer) is not None:
                return
            time.sleep(RENDEZVOUS_POLL)
    except Exception:
        pass


def scenario_claim_monitor_rendezvous(ctx):
    """With the deployed pair settled and the field owner holding its
    claim, read the claim's declared monitor endpoint through the
    plant protocol's claim surface and assert it is dialable under
    the wildcard bind — a foreign endpoint reaches the owner's
    monitor and the recorded claim state answers — then exercise the
    rendezvous it exists for: demote the owner through the
    field-claim path and assert the demoted peer resolves a provable
    successor through the claim-declared monitor, adopts it, and
    reconverges to a tracking standby with the journaled adoption
    evidence; restore the pair's launch roles."""
    case = Case(
        'claim-monitor-rendezvous',
        'The claim-declared monitor is dialable under the wildcard '
        'bind and carries the demoted peer\u2019s rendezvous back '
        'into tracking',
        'the standing claim\u2019s declared monitor answers a foreign '
        'dial with the recorded claim state — never the wildcard '
        'bind the #1135 defect stored; the tracking standby\u2019s '
        'POST /promote demotes the owner through the field-claim '
        'path; the demoted peer journals the attributed '
        'field_claim_lost, adopts the endpoint the standing claim '
        'declares (journaled tracking_source_adopted naming it), '
        'and reconverges to a tracking standby inside the bound; '
        'the durable journal mirrors the records; the launch roles '
        'restore; two passes produce identical digests')
    try:
        active, standby = ctx.get('active'), ctx.get('standby')
        if not active or not standby:
            return case.finish(
                'inconclusive', 'the run config did not record both '
                'peer monitor endpoints — the leg cannot drive the '
                'deployed pair')
        placement = ctx.get('endpoint_placement') or {}
        if any(placement.get(name) != 'loopback'
               for name in ('active', 'standby', 'plant')):
            return case.finish(
                'inconclusive', 'the run config does not publish '
                'loopback endpoints for both peers and the plant — '
                'the rig\u2019s declared placements are '
                + str(placement)[:200] + ' so the leg cannot drive '
                'the pair or read the claim surface host-side')
        if not ctx.get('plant'):
            return case.finish(
                'inconclusive', 'the run config did not publish the '
                'simulated plant endpoint — the leg cannot read the '
                'field\u2019s claim surface')
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name)
                   for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config does not bind-mount '
                'per-controller journal files — the leg cannot '
                'audit the durable field_claim_lost and '
                'tracking_source_adopted records')
        tokens = ctx.get('plant_owner') or {}
        if not all(tokens.get(name) for name in ('active', 'standby')):
            return case.finish(
                'inconclusive', 'the run config records no pinned '
                '--owner-token for the pair — the standing claim\u2019s '
                'owner is not attributable')
        keyed = bool(ctx.get('pair_token'))
        case.observe('subject pair ' + ('keyed' if keyed
                                        else 'unkeyed')
                     + ' — active ' + active + ', standby ' + standby)

        # The launch layout the pass stages from: the unconfigured
        # peer — launched without a tracking source — must hold the
        # field so its demote owes the claim-declared rendezvous; a
        # swapped layout is restored before the leg reports.
        reports = {name: _try_role(ctx, ctx[name])
                   for name in ('active', 'standby')}
        if all(report is None for report in reports.values()):
            return case.finish(
                'inconclusive', 'the pair is unreachable — monitor '
                'endpoints ' + active + ' and ' + standby)
        owner = _pair_active(ctx)
        if owner is None:
            return case.finish(
                'failed', 'no settled active peer — /role reports '
                + str(reports)[:300])
        if owner != 'active':
            _restore_layout(ctx, 'active', 'standby')
            owner = wait_for(lambda: _pair_active(ctx) == 'active'
                             and 'active' or None,
                             time.monotonic() + RENDEZVOUS_SETTLE,
                             interval=RENDEZVOUS_POLL)
        if owner != 'active':
            return case.finish(
                'inconclusive', 'the pair never settled on its '
                'launch layout — the unconfigured peer must hold '
                'the field for the rendezvous the leg exercises')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer) or None,
                    time.monotonic() + RENDEZVOUS_SETTLE,
                    interval=RENDEZVOUS_POLL) is None:
            return case.finish(
                'inconclusive', 'the pair never reported a settled '
                'tracking standby — the rendezvous episode has no '
                'settled baseline')
        try:
            doc = _checkpoint(ctx, owner)
            if 'source_owns_field' not in doc \
                    or 'line_owner' not in doc:
                raise ValueError('missing fields')
        except Exception:
            return case.finish(
                'inconclusive', 'the pair serves a checkpoint '
                'document without the field-ownership stamps — the '
                'rig predates the field-arbitrated monitor '
                'contract')
        baseline = _claim_surface(ctx)
        if baseline is None:
            return case.finish(
                'inconclusive', 'the plant\u2019s claim surface '
                'answered no probe — the leg cannot read the '
                'standing claim')
        if not _fenced(baseline):
            return case.finish(
                'inconclusive', 'the claim surface answered no '
                'fenced verdict — the field is open or the claim '
                'read surface is absent: ' + str(baseline)[:200])
        if _verdict_owner(baseline) != tokens[owner]:
            return case.finish(
                'inconclusive', 'the standing claim names '
                + str(_verdict_owner(baseline)) + ' — the launch '
                'owner\u2019s token ' + str(tokens[owner])
                + ' was expected')
        if _verdict_monitor(baseline) is None:
            return case.finish(
                'inconclusive', 'the claim surface names no declared '
                'monitor — the staged run predates the '
                'field-arbitrated monitor contract')
        case.observe('settled baseline: ' + owner + ' owns the '
                     'field under ' + str(tokens[owner])
                     + ', declared monitor '
                     + str(_verdict_monitor(baseline)) + ', and '
                     + peer + ' tracks it')

        digests = []
        for number in (1, 2):
            violations = {}

            def note(key, diagnostic, detail):
                violations.setdefault(key, (diagnostic, detail))

            record, evidence = _rendezvous_pass(
                ctx, number, owner, peer, tokens)
            _judge_rendezvous(record, note)
            digest = _digest(violations)
            evidence['record'] = record
            evidence['digest'] = dict(digest)
            evidence['violations'] = {
                key: diagnostic for key, (diagnostic, _)
                in violations.items()}
            ref = save_evidence(
                ctx['evidence_dir'],
                'claim-monitor-rendezvous-pass-' + str(number)
                + '.json', evidence)
            case.evidence('file', ref, 'claim-monitor rendezvous '
                          'pass ' + str(number) + ' — the claim '
                          'surface reads, the foreign-endpoint '
                          'dials, the demotion walk, the adoption '
                          'and loss journals, the re-join watch, '
                          'the restore, and the normalized digest')
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
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two rendezvous passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))

        # The unchecked-diagnostic self-check: the rendezvous judge
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
        return case.finish('inconclusive',
                           'the leg could not complete on this '
                           'rig: ' + str(exc)[:500])
