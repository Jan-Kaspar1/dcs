"""The involuntary_demote_verify leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the leg shares the launch-layout window behind
# demote_forged_standby_source — the same announced-hint seam, the same
# forge staging, and the same restore onto the entry roles — but it
# opens its own episode per pass with no request boundary at all: the
# field claim is preempted, so the owner's demotion is its own fenced
# write.
RUNS_AFTER = frozenset({'scenario_peer_announce',
                        'scenario_demote_forged_standby_source'})


# --------------------------------------------------------------------
# The involuntary-demotion half of decision 92's announced-source
# contract, pinned as per-run lane evidence for WW-LCM-001's
# takeover-continuity clause and WW-FND-004's command integrity.
#
# `POST /demote` crosses a request boundary, so it can hang the
# verification on the call: the demote verify pulls each recorded
# `?peer=` hint once under the keyed `line_proof` the pair's token
# arms, preferring a candidate that serves the line as field owner, and
# a passing hint pins into `adopted` with a journaled
# TrackingSourceAdopted. An involuntary demotion — a field claim's
# mid-run loss, the superseded owner demoting in place on its own
# fenced write — crosses no such boundary: nothing to hang the verify
# on. The announced set is consumed from the tracking path instead,
# lazily, and the leg pins that shape where the voluntary-path forgery
# leg (#882) cannot reach it.
#
# What the leg asserts, on the run's own deployed pair and whichever
# posture it launches under:
#
# - a recorded hint is a verify candidate, never a pull target — the
#   foreign endpoint's ledger shows the bounded verify pass the demote
#   path spends, and the demoted peer never dials it again as its
#   tracking source;
# - on a keyed run a bare announced hint is *not* inert: the refused
#   probe is journaled by name as TrackingSourceRefused, so a refused
#   candidate is durable audit rather than silence, and a verified
#   successor's endpoint pins into TrackingSourceAdopted;
# - on an unkeyed run a bare hint is no tracking source at all — the
#   verify pass is keyed-only outright, so the endpoint's ledger must
#   read *no* pull whatsoever and no refusal can journal either
#   (nothing was served to refuse), while the peer still resolves
#   through the field's own arbitration onto its real successor and
#   reports the honest tracking/orphaned verdict rather than following
#   the foreign document;
# - neither posture adopts the foreign document — the demoted peer's
#   served image keeps the pass's own settled value, never the value the
#   staged forgery plants, and its served line document names the
#   successor that actually holds the field;
# - the pair reconverges to exactly one active plus one tracking
#   standby, and the claim and the entry roles are restored for the
#   legs behind this one.
#
# The hostile endpoint is the run's bridge-placed forge (the
# endpoint_placement 'forge' record): a labeled rig-network container
# out of the shipped `dcs-forge` binary, serving a staged checkpoint
# document and announcing itself to the owner's monitor. The staged
# document is the owner's own checkpoint replayed as a standby-shaped
# continuation with one internal `In` sample planted to a value no
# settled verdict produced — a document that both fails the line's
# command audit and, were it adopted, would be observable in the
# demoted peer's served image.
#
# Named diagnostics demote-hint-verify-failed for a contract miss,
# demote-hint-verify-nondeterministic when two passes disagree, and
# demote-hint-verify-unchecked when a planted negative slips the leg's
# own audits.

HINT_SETTLE = 45            # bound on each preempt, demotion, re-join
                            # and reconvergence
HINT_POLL = 0.4             # wait cadence inside the leg
FORGE_ANNOUNCE_SETTLE = 20  # bound on the forge's announce landing
REJOIN_TICKS = 60           # the lane tick bound on the demoted peer's
                            # re-join — the stranded wedge the contract
                            # names

# The verify pass's allowance for one unproven endpoint across a whole
# episode: `probe_announced_hints` takes newest-announcer first and one
# bounded pull per hint per pass, re-probing an unchanged set only after
# the `ANNOUNCED_VERIFY_RETRY` window. A hint *followed* as a pull
# target is dialed once per scan instead, so any count past this bound
# is a hint chased rather than proved.
VERIFY_PULL_BOUND = 6

# The endpoints' published monitor ports on the rig bridge — the suffix
# a journaled adoption or the served `line_owner` must resolve to.
PAIR_PORTS = {'active': 8080, 'standby': 8081}

# The forge endpoint's rig-bridge port — the suffix no journaled
# adoption may name.
FORGE_PORT = 8090

# The verdicts a demoted peer may legitimately report once it has
# resolved: it followed an endpoint of this line. `degraded` is the
# wedge the contract refuses — a stranded pull on the endpoint it
# followed.
CONVERGED_VERDICTS = ('tracking', 'orphaned')

DIAG_FAILED = 'demote-hint-verify-failed'
DIAG_NONDET = 'demote-hint-verify-nondeterministic'
DIAG_UNCHECKED = 'demote-hint-verify-unchecked'


def _forge_hits(path):
    """The parsed records of the forge endpoint's hits ledger —
    `{'kind': 'serve'|'announce', ...}` JSONL lines the bind-mounted
    file carries. A missing file or a torn final line reads as absent
    records — a lost observation, never the leg's verdict."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _forge_served(forge, since=0):
    """The forge ledger's served checkpoint pulls from index `since` —
    every request a tracking path dialed at the unproven endpoint."""
    return [record for record in _forge_hits(forge['hits'])[since:]
            if record.get('kind') == 'serve']


def _forge_announced(forge):
    """True once the forge's hits ledger holds an announce pull the
    owner answered — the recorded hint the demotion then verifies."""
    return any(record.get('kind') == 'announce' and record.get('ok')
               for record in _forge_hits(forge['hits']))


def _stage_document(forge, document):
    """Rewrite the forge's staged checkpoint document — the atomic
    rename lands the next shape without a relaunch or a torn read."""
    staged = Path(forge['dir']) / 'checkpoint.staging.json'
    staged.write_text(json.dumps(document))
    staged.replace(forge['document'])


def _standby_document(own):
    """The honest standby-shaped document derived from the field
    owner's served checkpoint: the same line's continuation stamped
    `source_owns_field: false` — the shape a real tracking peer's
    checkpoint carries — with the owner-hint and proof decorations
    stripped."""
    document = json.loads(json.dumps(own))
    document['source_owns_field'] = False
    document.pop('line_owner', None)
    document.pop('line_proof', None)
    return document


def _planted_document(own, point):
    """The planted-internal forgery: the honest standby document whose
    internal `In` image claims the held value the pass's settled write
    did not produce — a value no settled verdict on this line ever held.
    Were such a document adopted, the demoted peer would serve it, so
    the plant is the leg's observable proof that it never followed the
    foreign endpoint. Returns None when the checkpoint serves no bool
    sample at the point."""
    document = _standby_document(own)
    sample = (document.get('internal') or {}).get(str(point))
    if not isinstance(sample, dict):
        return None
    value = sample.get('value')
    if not isinstance(value, dict) \
            or not isinstance(value.get('bool'), bool):
        return None
    sample['value'] = {'bool': not value['bool']}
    return document


def _journal_floor(ctx, name):
    """The durable journal's length — every record after this floor is
    the episode's own."""
    try:
        return len(_journal_entries(ctx['journal_files'][name]))
    except (OSError, ValueError):
        return 0


def _journaled(ctx, name, floor, kind):
    """The bodies of `kind` events the named peer's durable journal
    carries since `floor`, or None when the file cannot be read."""
    try:
        entries = _journal_entries(ctx['journal_files'][name])
    except (OSError, ValueError):
        return None
    return [(item.get('entry') or {}).get('event', {})[kind]
            for item in entries[floor:]
            if kind in ((item.get('entry') or {}).get('event') or {})]


def _durable_adoptions(ctx, name):
    """Every source the named peer's durable journal's
    TrackingSourceAdopted records name, or None unreadable — the
    process-lifetime pin an earlier demotion leaves behind, which a
    repeat demotion may converge through instead of journaling afresh."""
    try:
        records = _journal_entries(ctx['journal_files'][name])
    except (OSError, ValueError):
        return None
    return [(record.get('entry') or {}).get('event', {})
            .get('tracking_source_adopted', {}).get('source')
            for record in records
            if 'tracking_source_adopted'
            in ((record.get('entry') or {}).get('event') or {})]


def _sync_kind(report):
    """The sync vocabulary a standby's /role report carries —
    `tracking`, `orphaned`, `degraded` — or `unsynchronized` when the
    report carries the string form."""
    sync = (report or {}).get('sync')
    if isinstance(sync, str):
        return sync
    for kind in ('tracking', 'orphaned', 'degraded', 'diverged',
                 'unsynchronized'):
        if isinstance(sync, dict) and kind in sync:
            return kind
    return 'missing'


def _is_converged(report):
    """Whether a served RoleReport carries `standby` under a verdict the
    contract allows a demoted peer to report."""
    return (report or {}).get('role') == 'standby' \
        and _sync_kind(report) in CONVERGED_VERDICTS


def _wait_standby(ctx, name, watch):
    """Poll /role until the peer reports standby; the poll rows land in
    `watch` for the episode's evidence."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if (report or {}).get('role') == 'standby' else None
    return wait_for(found, time.monotonic() + HINT_SETTLE,
                    interval=HINT_POLL)


def _wait_converged(ctx, name, watch):
    """Poll /role until the demoted peer reports a converged verdict —
    the re-join the announced-source contract owes an involuntarily
    demoted owner."""
    def found():
        report = _try_role(ctx, ctx[name])
        watch.append({name: report})
        return report if _is_converged(report) else None
    return wait_for(found, time.monotonic() + HINT_SETTLE,
                    interval=HINT_POLL)


def _role_tick(report):
    """The served tick a RoleReport carries, or None."""
    tick = (report or {}).get('tick')
    return tick if isinstance(tick, int) else None


def _line_document(ctx, name):
    """The peer's served checkpoint document, or None unreadable."""
    try:
        _, document = http_json('GET', ctx[name] + '/checkpoint')
    except Exception:
        return None
    return document if isinstance(document, dict) else None


def _one_active(ctx, expect):
    """The launched pair's field-owning endpoint key — the one-active
    invariant the episode's settle and its restore are both read
    through."""
    active = _pair_active(ctx)
    return active == expect and active is not None


def _restore_layout(ctx, owner, peer):
    """Best-effort launch-layout restore on the deployed pair: demote
    whichever peer still owns the field, promote the entry owner, and
    let the pair reconverge with the entry roles standing."""
    try:
        report = _try_role(ctx, ctx[peer])
        if (report or {}).get('role') in ('active', 'promoting',
                                          'demoting'):
            _settle_call(ctx[peer] + '/demote')
            _wait_standby(ctx, peer, [])
        report = _try_role(ctx, ctx[owner])
        if (report or {}).get('role') != 'active':
            _settle_call(ctx[owner] + '/promote')
        wait_for(lambda: _one_active(ctx, owner) or None,
                 time.monotonic() + HINT_SETTLE, interval=HINT_POLL)
        wait_for(lambda: _tracking_standby(ctx, peer) or None,
                 time.monotonic() + HINT_SETTLE, interval=HINT_POLL)
    except Exception:
        pass


def _judge_unbounded(served, note):
    """The bounded-verify-pass clause: the forge's ledger must show the
    demote path's bounded pull at an unproven endpoint and never a chase
    — a hint followed as a pull target is dialed every scan."""
    if not served:
        note(
            'unbounded',
            'the forge ledgered no checkpoint pull across the '
            'involuntary demotion — the lazy verification never ran '
            'over the recorded hint')
    elif len(served) > VERIFY_PULL_BOUND:
        note(
            'unbounded',
            'the demoted peer spent ' + str(len(served))
            + ' pulls at the unproven endpoint — a recorded hint is a '
            'verify candidate, never a pull target')


def _judge_inert(served, refusals, note):
    """The inert-hint clause on an unkeyed run: the announced contract is
    keyed-only outright, so a bare hint earns not even a verify pass —
    no pull is served and no refusal can journal either, because nothing
    was served to refuse."""
    if served:
        note(
            'inert',
            'the unkeyed peer spent ' + str(len(served))
            + ' pulls at the announced endpoint — a bare hint on an '
            'unkeyed run is no tracking source at all, so it earns not '
            'even a verify pass')
    elif refusals:
        note(
            'inert',
            'the unkeyed peer journaled ' + str(len(refusals))
            + ' tracking_source_refused records — it never probed the '
            'hint, so nothing can have been served to refuse')


def _judge_refused(refusals, note):
    """The auditability clause on a keyed run: a refused candidate is
    durable audit rather than silence."""
    if not refusals:
        note(
            'auditable',
            'the keyed peer journaled no tracking_source_refused record '
            'naming the refused endpoint — a refused probe is auditable, '
            'never silent')


def _judge_adoption(adoptions, port, note):
    """The no-adoption clause: no journaled adoption may name the foreign
    endpoint's port, and at least one must stand — a fresh adoption or a
    durable pin an earlier demotion left both count as a resolved
    source, neither at all is the strand the contract closes."""
    named = [str(source) for source in adoptions]
    if any(source.endswith(':' + str(port)) for source in named):
        note(
            'adopted',
            'the demoted peer journaled a tracking_source_adopted naming '
            'the forge endpoint on :' + str(port)
            + ' — the announced hint was adopted')
    elif not named:
        note(
            'adopted',
            'the demoted peer resolved no verified tracking source at '
            'all — neither a journaled adoption this demotion nor a '
            'durable pin from an earlier one')


def _judge_document(document, port, want, note):
    """The no-following clause read off the demoted peer's own served
    surface: the served line document names the successor that actually
    holds the field, and the served image keeps the pass's own settled
    value rather than the one the staged forgery plants."""
    if not isinstance(document, dict):
        note(
            'document',
            'the demoted peer serves no checkpoint document — the '
            'document it landed on cannot be audited')
        return
    line_owner = str(document.get('line_owner') or '')
    if not line_owner.endswith(':' + str(port)):
        note(
            'document',
            'the demoted peer serves line_owner '
            + json.dumps(document.get('line_owner'))[:120]
            + ' where the promoted successor on :' + str(port)
            + ' was expected — the pulls moved somewhere else')
    planted = ((document.get('internal') or {}).get(str(want['point']))
               or {}).get('value')
    if isinstance(planted, dict) and planted.get('bool') == want['planted']:
        note(
            'document',
            'the demoted peer serves the planted internal sample the '
            'staged forgery carries — the foreign document was adopted')


def _judge_pair(ctx, owner, peer, note):
    """The reconvergence clause: exactly one field owner — the promoted
    successor — with the demoted owner converged behind it."""
    actives = [name for name in (owner, peer)
               if (_try_role(ctx, ctx[name]) or {}).get('role') == 'active']
    if actives != [peer]:
        note(
            'pair',
            'the episode settled with ' + repr(actives) + ' active — '
            'the promoted successor alone was expected')
    elif not _is_converged(_try_role(ctx, ctx[owner])):
        note(
            'pair',
            'the demoted owner reports '
            + json.dumps(_try_role(ctx, ctx[owner]))[:200]
            + ' — the pair never reconverged to one active plus one '
            'tracking standby')


def _digest_hint_verify(problems, keyed):
    """The pass's normalized verdict record — identical across clean
    passes; each field is the clean value only while no violation —
    contract or instability — names its clause. Named for the leg so no
    sibling's patch.object(scenarios, '_digest', ...) seam rewrites it
    through the facade."""
    def clean(*keys):
        return not any(key in problems for key in keys)

    return {
        # The posture's own reading of the recorded hint: a bounded
        # verify pass under keying, outright inert without it.
        'hints': ('proved' if clean('unbounded') else 'chased') if keyed
                 else ('inert' if clean('inert') else 'pulled'),
        # A refusal is only ever journaled where the probe ran: the
        # announced contract is keyed-only outright, so an unkeyed run
        # serves nothing to refuse.
        'audit': ('journaled' if clean('auditable') else 'silent')
                 if keyed else 'never-earned',
        'source': 'verified' if clean('adopted', 'document') else 'foreign',
        'pair': 'settled' if clean('pair') else 'split',
        'roles': 'restored' if clean('roles') else 'unrestored',
    }


def _pass_problems(problems):
    """The pass's recorded clauses with their detail, in a stable order —
    the failure line the leg reports names the clauses, not the first
    one to trip."""
    return [detail for _clause, detail in sorted(problems.items())
            if detail]


def _self_check():
    """The planted negatives that must be caught by this leg's own
    judges — the check proving the audits are live rather than merely
    present. Each case plants the pre-contract shape the contract
    closed; a judge that reported nothing for it is a judge the leg
    cannot claim to have exercised. Returns the names that slipped."""
    slipped = []

    def expect(name, judge):
        found = {}
        judge(lambda clause, detail: found.setdefault(clause, detail))
        if not found:
            slipped.append(name)

    forge = 'forge:8090'
    successor = str(PAIR_PORTS['standby'])
    honest = {'line_owner': 'peer:' + successor,
              'internal': {'101': {'value': {'bool': False}}}}
    planted = {'line_owner': 'peer:' + successor,
               'internal': {'101': {'value': {'bool': True}}}}

    # A recorded hint followed as a pull target: a chase past the bounded
    # verify pass is exactly what a demoted peer dialing its pull source
    # every scan looks like.
    expect('unbounded-chase', lambda note: _judge_unbounded(
        [{'kind': 'serve'}] * (VERIFY_PULL_BOUND + 1), note))
    expect('unverified-hint', lambda note: _judge_unbounded([], note))
    # An unkeyed run that probed the bare hint anyway, and one that
    # journaled a refusal it never earned.
    expect('unkeyed-pull', lambda note: _judge_inert(
        [{'kind': 'serve'}], None, note))
    expect('unkeyed-refusal', lambda note: _judge_inert(
        None, [{'source': forge, 'detail': 'detail'}], note))
    # A keyed run whose refused probe vanished from the journal.
    expect('silent-refusal', lambda note: _judge_refused(None, note))
    # The foreign endpoint adopted as the tracking source, and a
    # demotion that resolved no verified source at all.
    expect('adopted-hint', lambda note: _judge_adoption(
        [forge], str(FORGE_PORT), note))
    expect('source-less', lambda note: _judge_adoption(
        [], str(FORGE_PORT), note))
    # The demoted peer serving the staged forgery's own document: its
    # line stamp gone, and its planted internal sample applied.
    expect('following-forgery', lambda note: _judge_document(
        {'line_owner': None}, PAIR_PORTS['standby'],
        {'point': 101, 'planted': True}, note))
    expect('planted-sample', lambda note: _judge_document(
        planted, PAIR_PORTS['standby'],
        {'point': 101, 'planted': True}, note))
    # A demoted peer whose pulls moved off the successor, and a pair that
    # never reconverged to one field owner.
    expect('retargeted', lambda note: _judge_document(
        honest, PAIR_PORTS['active'], {'point': 101, 'planted': True},
        note))
    return slipped


def _involuntary_pass(ctx, number, owner, peer, point, baseline):
    """One involuntary-demote pass: the settled write the planted
    document contradicts, the foreign endpoint's landing announce, the
    claim preempt that demotes the owner in place, the lazy-verification
    audit, and the launch-layout restore. Returns
    `(digest, violations, evidence)` — digest the pass's normalized
    verdict record, identical across clean passes; violations
    `{clause: detail}` in the order the clauses first failed."""
    violations = {}

    def note(clause, detail):
        """Record the clause's violation — a satisfied clause records
        nothing, so the map carries failures alone."""
        violations.setdefault(clause, detail)

    evidence = {'pass': number, 'owner': owner, 'peer': peer,
                'posture': 'keyed' if ctx.get('pair_token') else 'unkeyed'}
    keyed = bool(ctx.get('pair_token'))
    forge = None
    try:
        # The settled gate: the entry owner holds the field and the other
        # launched peer tracks it — the episode's restore lands the same.
        if not _one_active(ctx, owner) or not _tracking_standby(ctx, peer):
            violations['settle'] = (
                'the pair never settled — ' + owner + ' holds no active '
                'role with ' + peer + ' tracking behind it')
            return None, violations, evidence

        # The pass's settled write: the held value the staged forgery
        # plants against, so adoption would be observable in the demoted
        # peer's served image.
        want = not baseline
        index = _next_receipt_index(ctx, ctx[owner])
        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': want}}},
            'actor': 'qa-lane-demote-hint-' + str(number)}
        try:
            command_status, receipt = http_json(
                'POST', ctx[owner] + '/command', command)
        except Exception as exc:
            violations['command'] = ('the field owner never answered the '
                                   'pass command: ' + str(exc)[:200])
            return None, violations, evidence
        settled = wait_for(
            lambda: _settled_outcome(ctx, ctx[owner], index),
            time.monotonic() + HINT_SETTLE, interval=HINT_POLL)
        evidence['command'] = {'index': index, 'status': command_status,
                               'receipt': receipt, 'settled': settled}
        if settled != 'applied':
            violations['command'] = ('the pass command never settled '
                                   'applied — the plant has no settled '
                                   'value for the forgery to contradict'
                                   + ': ' + json.dumps(receipt)[:200])
            return None, violations, evidence
        planted = not want

        # The staged document: the owner's own checkpoint replayed as a
        # standby-shaped continuation with one internal sample planted
        # against the pass's settled write.
        own = _line_document(ctx, owner)
        document = _planted_document(own or {}, point)
        if document is None:
            violations['document'] = (
                'the owner serves no field-owning checkpoint with a bool '
                'sample at point ' + str(point) + ' — the hostile '
                'document cannot be staged')
            return None, violations, evidence
        evidence['document'] = {
            'tick': document.get('tick'),
            'source_owns_field': document.get('source_owns_field'),
            'planted': planted}

        # The foreign endpoint: a process outside the line, so it holds
        # no pair token whatever the rig's posture — its announce is the
        # recorded hint and its ledger the record of what the tracking
        # path dialed there.
        forge = ctx['start_forge'](document, owner, keyed=False)
        if wait_for(lambda: _forge_announced(forge) or None,
                    time.monotonic() + FORGE_ANNOUNCE_SETTLE,
                    interval=HINT_POLL) is None:
            violations['announce'] = (
                "the foreign endpoint's announce never landed on the "
                'owner — no hint was recorded to verify')
            return None, violations, evidence
        floor = _journal_floor(ctx, owner)
        served_before = len(_forge_served(forge))

        # The claim preempt: `POST /promote` on the tracking standby —
        # never `POST /demote` on the owner first, so the demotion is the
        # superseded owner's own fenced write and the announced set is
        # consumed with no request boundary to hang a verify on.
        status, body = _settle_call(ctx[peer] + '/promote')
        evidence['promote'] = {'status': status, 'body': body}
        if status != 200:
            violations['promote'] = (
                'POST /promote on the tracking standby answered '
                + str(status) + ' ' + json.dumps(body)[:200]
                + ' — the field claim could not be preempted')
            return None, violations, evidence

        # The demote-in-place watch: the superseded owner must walk to
        # standby on its own fenced write — never disappear, never hold
        # `active` beside the promoted peer.
        demotion = []
        walked = _wait_standby(ctx, owner, demotion)
        evidence['demotion'] = demotion[-8:]
        if walked is None:
            violations['demotion'] = (
                'the superseded owner never reported standby — its own '
                'fenced write never demoted it in place; last /role '
                + json.dumps(demotion[-1] if demotion else None)[:200])
            return None, violations, evidence
        demote_tick = _role_tick(walked)

        # The re-join watch: the demoted peer resolves a source of this
        # line and reports a converged verdict inside the lane's bound.
        rejoin = []
        converged = _wait_converged(ctx, owner, rejoin)
        evidence['rejoin'] = rejoin[-8:]
        evidence['rejoin_ticks'] = (
            _role_tick(converged) - demote_tick
            if converged is not None and demote_tick is not None
            else None)
        if converged is None:
            violations['rejoin'] = (
                'the demoted peer never re-joined — the stranded wedge '
                'this contract names; last /role '
                + json.dumps(rejoin[-1] if rejoin else None)[:200])
            return None, violations, evidence
        if evidence['rejoin_ticks'] is not None \
                and evidence['rejoin_ticks'] > REJOIN_TICKS:
            violations['rejoin'] = (
                'the demoted peer took ' + str(evidence['rejoin_ticks'])
                + ' ticks to re-join — beyond the lane bound '
                + str(REJOIN_TICKS))

        # The lazy-verification audit, read through the demoted peer's
        # served surface and its durable journal.
        served = _forge_served(forge, served_before)
        refusals = [body for body in _journaled(
            ctx, owner, floor, 'tracking_source_refused') or []
            if str(body.get('source', '')).endswith(
                ':' + str(forge['port']))]
        adoptions = _journaled(ctx, owner, floor,
                               'tracking_source_adopted') or []
        durable = _durable_adoptions(ctx, owner) or []
        sources = [str(body.get('source')) for body in adoptions]
        record = {
            'served': len(served),
            'refused': [body.get('detail') for body in refusals],
            'adopted': sources,
            'durable_adoptions': durable,
            'sync': _sync_kind(_try_role(ctx, ctx[owner])),
            'document': _line_document(ctx, owner),
        }
        evidence['audit'] = record
        if keyed:
            _judge_unbounded(served, note)
            _judge_refused(refusals, note)
        else:
            _judge_inert(served, refusals, note)
        _judge_adoption([str(source) for source in sources + durable],
                        str(forge['port']), note)
        _judge_document(record['document'], PAIR_PORTS[peer],
                        {'point': point, 'planted': planted}, note)
        _judge_pair(ctx, owner, peer, note)
    finally:
        if forge is not None:
            try:
                ctx['stop_forge']()
            except Exception:
                pass

    # The restore: the entry owner back on the field with the other peer
    # tracking it — the claim and the launch roles the legs behind this
    # one inherit.
    _restore_layout(ctx, owner, peer)
    restored = wait_for(
        lambda: _one_active(ctx, owner) and _tracking_standby(ctx, peer),
        time.monotonic() + HINT_SETTLE, interval=HINT_POLL)
    evidence['restored'] = bool(restored)
    if not restored:
        violations['roles'] = (
            'the pair never settled back to its entry role layout — '
            + owner + ' reports ' + json.dumps(_try_role(ctx, ctx[owner]))[:160]
            + ', ' + peer + ' reports '
            + json.dumps(_try_role(ctx, ctx[peer]))[:160])
    evidence['violations'] = sorted(violations)
    evidence['digest'] = _digest_hint_verify(violations, keyed)
    return evidence['digest'], violations, evidence


def scenario_involuntary_demote_verify(ctx):
    """Exercise the involuntary demotion's lazy hint verification on the
    deployed pair: a foreign endpoint records itself on the active
    through `?peer=` announces, the field claim is preempted so the
    active demotes in place with no request boundary to verify on, and
    the demoted peer must treat the recorded hint as a verify candidate
    alone — a bounded verify pass under the run's keyed posture, no pull
    at all on an unkeyed one — journal the refused probe by name where
    it probed at all, never adopt the staged forgery, pin a verified
    successor instead, and leave the pair one active plus one tracking
    standby with its launch roles restored."""
    case = Case(
        'involuntary-demote-verify',
        'Involuntary-demotion lazy hint verification on the deployed '
        'pair',
        'with the deployed pair settled and tracking — the entry owner '
        'holding the field, the other launched peer tracking it — a '
        'foreign endpoint records itself on the owner through a '
        '?peer= announce; the field claim is then preempted by a '
        'documented POST /promote on the tracking standby, so the '
        'owner demotes in place on its own fenced write and no request '
        'boundary runs a verify; through the demoted peer\'s served '
        'surface and durable journal the recorded hint is then a verify '
        'candidate alone — the forge\'s ledger showing the bounded '
        'verify pass under the run\'s keyed posture and *no* pull at all '
        'on an unkeyed one, the refused probe journaled by name as '
        'tracking_source_refused where it probed at all, and no '
        'tracking_source_adopted ever naming the foreign endpoint — '
        'while the demoted peer\'s served line document names the '
        'promoted successor and its served image keeps the pass\'s own '
        'settled value rather than the planted one, a verified '
        'successor\'s endpoint pins into TrackingSourceAdopted, and the '
        'pair reconverges to exactly one active plus one tracking '
        'standby with the claim and the launch roles restored; two '
        'consecutive passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context carries '
                               'only one endpoint — the pair the '
                               'involuntary-demote leg needs is absent')
        for action in ('start_forge', 'stop_forge'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the foreign endpoint cannot be '
                                   'driven')
        placements = ctx.get('endpoint_placement') or {}
        if placements.get('forge') != 'bridge':
            return case.finish('inconclusive', 'endpoint_placement does '
                               'not place forge on the rig bridge — a '
                               'rig-dialed endpoint cannot stand on the '
                               'host')
        if not (ctx.get('journal_files') or {}).get('active') \
                or not (ctx.get('journal_files') or {}).get('standby'):
            return case.finish('inconclusive', 'the run context carries '
                               'no per-controller journal files — the '
                               'no-adoption audit cannot run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s monitor '
                                   'is unreachable: ' + str(exc)[:200])
        deadline = time.monotonic() + HINT_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=HINT_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=HINT_POLL) is None:
            return case.finish('inconclusive', 'the pair has no tracking '
                               'standby — the settle the leg restores '
                               'to was never reached')
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); tracking peer: ' + peer + '; posture: '
                     + ('keyed' if ctx.get('pair_token') else 'unkeyed'))
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'demote-hint-verify-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the settled '
                      'write the staged document plants against')
        target = _writable_bool_point(signals)
        if target is None or target.get('point') is None:
            return case.finish('inconclusive', 'the model declares no '
                               'writable bool in-point for the staged '
                               'document to contradict')
        point = target['point']
        baseline = _point_value(_snapshot(ctx, ctx[owner]), point)
        if not isinstance(baseline, bool):
            return case.finish('inconclusive', 'point ' + str(point)
                               + ' serves no bool baseline to write '
                               'against')
        digests = []
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED + ': planted '
                               'negatives slipped the leg\'s own audits: '
                               + ', '.join(slipped))
        try:
            for number in (1, 2):
                digest, found, evidence = _involuntary_pass(
                    ctx, number, owner, peer, point, baseline)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'demote-hint-verify-pass-' + str(number) + '.json',
                    evidence)
                case.evidence('file', ref, 'involuntary-demote pass '
                              + str(number) + ' — the pass\'s settled '
                              'write, the staged document, the claim '
                              'preempt and its demotion watch, the '
                              'lazy-verification audit, the pair\'s '
                              'reconvergence, and the normalized digest')
                if found or digest is None:
                    return case.finish(
                        'failed', DIAG_FAILED + ': '
                        + '; '.join(_pass_problems(found)[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the cases behind this one — a clean
            # pass restores it by construction; an aborted pass gets the
            # forge removed and the entry role order run again,
            # best-effort.
            try:
                ctx['stop_forge']()
            except Exception:
                pass
            _restore_layout(ctx, owner, peer)
            current = _pair_active(ctx)
            if current != owner:
                try:
                    case.observe('cleanup: restored the entry role '
                                 'layout')
                except Exception:
                    pass
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two involuntary-demote passes, identical digests: '
                     + json.dumps(digests[0], sort_keys=True))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
