"""The announced_source_verify acceptance leg — one module per leg of
the scenario schedule; see qa_lane/scenarios/__init__.py for the
ordering rule and the shared seam."""
from .common import *

# Ordering: the leg shares the launch-layout window behind
# demote_forged_standby_source — it needs the settled tracking pair,
# opens its own announced-only demotion window per pass (the tracking
# peer stopped, the field owner warm-restarted so no genuine announce
# stands), and restores the launch roles before the tune case's a->b
# switch.
RUNS_AFTER = frozenset({'scenario_demote_forged_standby_source'})
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The announced-source authenticity rule on unkeyed deployments — the
# per-revision lane evidence for the contract #867's fix establishes
# (decision 12's announce contract, WW-LCM-001's takeover-continuity
# clause and WW-FND-004's command integrity): `GET /checkpoint` is
# public and unauthenticated, so on a pair with no `--pair-token` every
# document shape a `?peer=`-announced endpoint could serve is derivable
# from the run's own answers and proves nothing about who serves it.
# An announced-only demotion on such a pair must therefore refuse the
# named `no_tracking_source` — the forged endpoint is never adopted as
# a demotion tracking source and never unblocks the demote guard — and
# the bare hint earns not even a verify pull.
#
# This is the fourth filing of the class (#850, #832, #872, #867): a
# forge replayed the victim's own `/checkpoint` with `owns_field`
# flipped, the `?peer=` announce was accepted, and `POST /demote`
# demoted the live active onto the forged stream. The lazy-verification
# legs (#924, #1111) pin the involuntary path; no leg pinned the
# voluntary announce-and-demote path's forged-document refusal, which
# is what this leg owns.
#
# Each pass opens the announced-only window — the tracking peer
# stopped, the field owner warm-restarted so its recorded-hint set
# begins empty — reads the owner's own served checkpoint, and stages
# the reproductions' two forged documents on the run's bridge-placed
# `dcs-forge` endpoint (`ctx['start_forge']`, launched tokenless: it
# holds no pair key, so every answer it serves is unsigned):
#
#   * `replay` — the victim's own document replayed verbatim with
#     `source_owns_field` rewritten `false`, the standby shape the
#     pre-#872 document checks accepted. Every other field, including
#     the `line_owner` naming the victim itself and its receipt log and
#     internal image, is the victim's own, so the forgery is the
#     maximally replayable one;
#   * `bumped` — the same replay with the tick jumped far ahead, the
#     reproduction's strictly-ahead shape.
#
# Both are announced to the field owner through the endpoint's own
# `GET /checkpoint?peer=` pull, then `POST /demote` runs against it.
# Through the owner's served role, its served checkpoint, the forged
# endpoint's hits ledger, and the owner's durable `--journal-file` the
# leg asserts the named refusal answers, no `tracking_source_adopted`
# record names the forged endpoint anywhere, the active keeps both its
# role and its field claim, the pair reconverges to one active plus one
# tracking standby, and the launch roles are restored.
#
# The run's deployed posture decides which refusal the evidence must
# show, and both are the same contract:
#
#   * unkeyed (the rule's own subject) — the demote answers
#     `no_tracking_source` and the endpoint's ledger records no verify
#     pull at all: a bare hint on an unkeyed run earns not even one;
#   * keyed — the verify pull reaches the endpoint, is answered
#     unsigned, and the proof gate refuses it (`tracking_source_refused`
#     names the endpoint in the owner's journal when the build records
#     probe refusals).
#
# Named diagnostics: announced-source-verify-failed for a contract
# miss — one per leg-and-clause the pass broke, the evidence carrying
# every clause's detail — announced-source-verify-nondeterministic
# when two passes' digests disagree, and
# announced-source-verify-unchecked when the self-check's planted
# negatives slip past the leg's own audits. Two consecutive passes
# produce identical digests.
#
# A staged run that predates the contract reports inconclusive, named
# announced-source-verify-predates. #867's fix is a behavioural guard
# with no new served field and no new durable record, so its
# pre-contract signature is behavioural too: on an unkeyed pair the
# forged announce *armed the demotion* — `POST /demote` answered 200
# instead of the named refusal, and the active walked to standby. Every
# released build before that fix lands presents exactly that shape, so
# the leg cannot call it a failure of a contract the revision never
# had. On a keyed pair the same observation is not that signature: the
# proof gate refused this shape long before #867, so there it is the
# contract failure the judge names.

ANNOUNCED_SETTLE = 45      # bound on the restart, the reconverge, and
                          # the role restore
ANNOUNCED_POLL = 0.4       # wait cadence inside the leg
ANNOUNCED_HINT_SETTLE = 20 # bound on the forged endpoint's announce
                          # landing
REPLAY_LEAD = 90000        # the strictly-ahead tick jump the `bumped`
                          # document carries — the reproduction's shape
ANNOUNCED_FORGE_PORT = 8090  # the bridge-placed forged endpoint's port
                          # the runner launches it on — the address a
                          # journaled adoption names
DIAG_FAILED = 'announced-source-verify-failed'
DIAG_NONDET = 'announced-source-verify-nondeterministic'
DIAG_PREDATES = 'announced-source-verify-predates'
DIAG_UNCHECKED = 'announced-source-verify-unchecked'


# ---- the forged endpoint's staged document and hits ledger ---------

def _announced_hits(path):
    """The parsed records of the forged endpoint's hits ledger —
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


def _announced_hinted(forge):
    """True once the endpoint's ledger holds a `?peer=` announce pull
    the owner answered — the recorded hint `POST /demote` must refuse."""
    return any(record.get('kind') == 'announce' and record.get('ok')
               for record in _announced_hits(forge['hits']))


def _announced_stage_document(forge, document):
    """Rewrite the endpoint's staged checkpoint document — the atomic
    rename lands the next shape without a relaunch or a torn read."""
    staged = Path(forge['dir']) / 'checkpoint.staging.json'
    staged.write_text(json.dumps(document))
    staged.replace(forge['document'])


def _replay_document(victim):
    """The `replay` forgery: the field owner's own served checkpoint
    replayed verbatim with `source_owns_field` rewritten `false` — the
    standby shape, the flipped stamp the pre-#872 checks accepted, on
    a document whose every other field is the victim's own."""
    document = json.loads(json.dumps(victim))
    document['source_owns_field'] = False
    return document


def _bumped_document(victim):
    """The `bumped` forgery: the same replay with the served tick
    jumped far ahead of the victim's — the reproduction's strictly-
    ahead shape, which an unauthenticated endpoint fabricates from the
    public answers alone."""
    document = _replay_document(victim)
    if isinstance(document.get('tick'), int):
        document['tick'] = document['tick'] + REPLAY_LEAD
    return document


def _forgery_record(victim, document):
    """What the staged document changes about the victim's own served
    document: the differing top-level keys with their before/after
    values — the evidence that the leg's forgery is the replay it
    names, derived from the served document rather than from a
    hand-written shape. None when either side is not a document."""
    if not isinstance(victim, dict) or not isinstance(document, dict):
        return None
    changed = {}
    for key in sorted(set(victim) | set(document)):
        if victim.get(key) != document.get(key):
            changed[key] = {'from': victim.get(key),
                            'to': document.get(key)}
    return {'keys': sorted(changed), 'changed': changed,
            'generation': document.get('generation'),
            'tick': document.get('tick'),
            'source_owns_field': document.get('source_owns_field')}


# ---- the served document and durable journal reads ------------------

def _owner_document(ctx, base):
    """The field owner's own served checkpoint document, or None when
    the read dropped — a lost observation, never the leg's verdict."""
    try:
        _, document = http_json('GET', base + '/checkpoint')
    except Exception:
        return None
    return document if isinstance(document, dict) else None


def _field_stamp(document):
    """The ownership stamps the field-claim audit reads off a served
    checkpoint document, or None when the document never answered."""
    if not isinstance(document, dict):
        return None
    return {'source_owns_field': document.get('source_owns_field'),
            'line_owner': document.get('line_owner'),
            'generation': document.get('generation'),
            'tick': document.get('tick')}


def _announced_journal_floor(ctx, name):
    """The durable journal's length — every record after this floor is
    the pass's own."""
    try:
        return len(_journal_entries(ctx['journal_files'][name]))
    except OSError:
        return 0


def _announced_journaled(ctx, name, floor, kind):
    """The bodies of `kind` events the named peer's durable journal
    carries since `floor`, or None when the file cannot be read."""
    try:
        entries = _journal_entries(ctx['journal_files'][name])
    except OSError:
        return None
    return [(item.get('entry') or {}).get('event', {})[kind]
            for item in entries[floor:]
            if kind in ((item.get('entry') or {}).get('event') or {})]


def _announced_durable_adoptions(ctx, name):
    """Every source the named peer's durable journal's
    tracking_source_adopted records name across the whole file, or None
    when it cannot be read — a pin left by an earlier demotion stands
    here even when this pass's window journals none."""
    try:
        entries = _journal_entries(ctx['journal_files'][name])
    except OSError:
        return None
    return [(record.get('entry') or {}).get('event', {})
            .get('tracking_source_adopted', {}).get('source')
            for record in entries
            if 'tracking_source_adopted'
            in ((record.get('entry') or {}).get('event') or {})]


# ---- the leg audits ------------------------------------------------
# Each leg collects its observation record and a pure judge audits it,
# naming the contract clause every violation breaks; the self-check
# replays each judge over planted negatives.

def _judge_forgery(record, expect, note):
    """Audit the staged document against the victim's own served
    document: the replay must differ from it in exactly the keys the
    leg names (`source_owns_field` alone for `replay`, with the tick
    for `bumped`) and must carry the flipped standby stamp."""
    if not isinstance(record, dict):
        note('shape', 'the staged document is no replay of the '
             'owner\'s own served checkpoint — the forgery the leg '
             'names was never staged')
        return
    if record.get('keys') != sorted(expect):
        note('shape', 'the staged document differs from the victim\'s '
             'served checkpoint in ' + json.dumps(record.get('keys'))
             + ' — the replay this leg stages changes '
             + json.dumps(sorted(expect)))
    if record.get('source_owns_field') is not False:
        note('flip', 'the staged document still claims the field — '
             'the source_owns_field flip this leg exists to exercise '
             'never landed: ' + json.dumps(record.get('changed'))[:200])


def _judge_announced_refusal(record, keyed, forge_port, note):
    """Audit one announced-forgery refusal record: POST /demote
    settles `409 no_tracking_source`, the field owner keeps its field
    claim and its role, no tracking-source adoption names the forged
    endpoint in the journal window or anywhere in the durable file, no
    role change is journaled, and the endpoint's ledger shows the
    verify pull the deployed posture owes — none at all where the hint
    is not even a candidate (unkeyed), one unsigned pull where the
    proof gate does the refusing (keyed)."""
    demote = record.get('demote') or {}
    if demote.get('status') != 409 \
            or demote.get('body') != 'no_tracking_source':
        note('demote-answer', 'POST /demote answered '
             + str(demote.get('status')) + ' '
             + json.dumps(demote.get('body'))[:200]
             + ' instead of settling no_tracking_source')
    stamp = record.get('document_after')
    if not isinstance(stamp, dict):
        note('field-claim', 'the field owner\'s served checkpoint '
             'never answered — the field-claim audit has no evidence '
             'to stand on')
    elif stamp.get('source_owns_field') is not True:
        note('field-claim', 'the field owner released its field claim '
             'on the forged announce: source_owns_field reads '
             + json.dumps(stamp.get('source_owns_field')))
    if (record.get('role_after') or {}).get('role') != 'active':
        note('role', 'the forged announce drove the demotion: the '
             'field owner reports ' + json.dumps(record.get('role_after'))
             [:200] + ' instead of active')
    if record.get('journaled_adoptions'):
        note('adoption', 'the refused demote journaled a '
             'tracking-source adoption: '
             + json.dumps(record['journaled_adoptions'])[:200])
    if record.get('journaled_role_changes'):
        note('role-change', 'the refused demote journaled a role '
             'change: ' + json.dumps(record['journaled_role_changes'])[:200])
    durable = record.get('durable_adoptions')
    if durable is None:
        note('durable-adoption', 'the owner\'s durable journal could '
             'not be read — the no-adoption audit has no record to '
             'stand on')
    else:
        forged = [source for source in durable
                  if str(source).endswith(':' + str(forge_port))]
        if forged:
            note('durable-adoption', 'the durable journal names the '
                 'forged endpoint as an adopted tracking source: '
                 + json.dumps(forged)[:200])
    pulls = record.get('verify_pulls') or []
    if keyed:
        if not pulls:
            note('verify-pull', 'the endpoint ledgered no verify pull '
                 '— the keyed proof gate refused a document the run '
                 'never fetched')
        elif any(pull.get('signed') for pull in pulls):
            note('verify-pull', 'the tokenless endpoint answered a '
                 'signed pull — the proof gate this leg exercises '
                 'never ran')
    elif pulls:
        note('verify-pull', 'the unkeyed demote pulled the forged '
             'endpoint ' + json.dumps(pulls)[:200] + ' — a bare hint '
             'on an unkeyed run earns not even a verify pull')
    if not record.get('announced'):
        note('announce', 'the forged endpoint\'s ?peer= announce never '
             'landed on the owner — nothing was recorded for the '
             'demote to refuse')


def _judge_announced_settle(record, owner, peer, note):
    """Audit the reconvergence: exactly one peer owns the field and it
    is the pass's entry owner, and the other is a tracking standby
    again — the refused demotion left the pair's launch roles intact."""
    actives = sorted(name for name in (owner, peer)
                     if (record.get(name) or {}).get('role') == 'active')
    if actives != [owner]:
        note('actives', 'the pair did not reconverge to one active: '
             + str(actives) + ' own the field, the pass\'s entry '
             'owner being ' + owner)
    for name in (owner, peer):
        if (record.get(name) or {}).get('role') is None:
            note('read', name + '\'s served role never answered after '
                 'the restore — the reconvergence is unobserved')
    report = record.get(peer) or {}
    if report.get('role') is not None \
            and 'tracking' not in (report.get('sync') or {}):
        note('tracking', peer + ' never re-joined tracking behind the '
             'refused demotion: ' + json.dumps(report)[:200])


def _self_check():
    """The leg's unchecked-diagnostic self-test: run every judge over
    the planted negative it must name — a forgery that adopted and
    demoted while the record claims a refusal, a refusal that released
    the field claim, an announce that never landed, a durable adoption
    naming the forge behind a clean journal window, a ledger that
    pulls where the posture owes no pull and stays silent where it
    owes one, a document that was never flipped, and a pair that never
    reconverged — and require the judge to note the violation. A silent
    judge returns the negative names it let through."""
    slipped = []

    def expect(name, judge):
        found = []
        judge(lambda clause, detail: found.append(clause + ': ' + detail))
        if not found:
            slipped.append(name)

    # The doctor's own negative: the forged announce adopted the
    # forged endpoint and demoted the live active while the record
    # carries the refusal's own shape.
    expect('forged-announce-adopted',
           lambda note: _judge_announced_refusal({
               'announced': True,
               'demote': {'status': 200, 'body': {'role': 'demoting'}},
               'verify_pulls': [{'kind': 'serve', 'signed': False,
                                 'query': 'prove=7', 'status': 200}],
               'role_after': {'role': 'standby', 'tick': 41},
               'document_after': {'source_owns_field': False,
                                  'line_owner': 'ctrl-a:8080',
                                  'generation': 9, 'tick': 40},
               'journaled_adoptions': [{'source': '172.18.0.9:8090'}],
               'journaled_role_changes': [{'from': 'active',
                                           'to': 'demoting'}],
               'durable_adoptions': ['172.18.0.9:8090']},
               True, ANNOUNCED_FORGE_PORT, note))
    # The refusal answered but the field owner released its claim.
    expect('field-claim-released',
           lambda note: _judge_announced_refusal({
               'announced': True,
               'demote': {'status': 409, 'body': 'no_tracking_source'},
               'verify_pulls': [{'kind': 'serve', 'signed': False,
                                 'query': 'prove=7', 'status': 200}],
               'role_after': {'role': 'active', 'tick': 41},
               'document_after': {'source_owns_field': False,
                                  'line_owner': None,
                                  'generation': 9, 'tick': 41},
               'journaled_adoptions': [],
               'journaled_role_changes': [],
               'durable_adoptions': []},
               True, ANNOUNCED_FORGE_PORT, note))
    # The announce never landed, so nothing stood for the demote.
    expect('silent-announce',
           lambda note: _judge_announced_refusal({
               'announced': False,
               'demote': {'status': 409, 'body': 'no_tracking_source'},
               'verify_pulls': [], 'role_after': {'role': 'active'},
               'document_after': {'source_owns_field': True},
               'journaled_adoptions': [],
               'journaled_role_changes': [],
               'durable_adoptions': []},
               False, ANNOUNCED_FORGE_PORT, note))
    # The window is clean but the durable file still names the forge.
    expect('durable-adoption',
           lambda note: _judge_announced_refusal({
               'announced': True,
               'demote': {'status': 409, 'body': 'no_tracking_source'},
               'verify_pulls': [], 'role_after': {'role': 'active'},
               'document_after': {'source_owns_field': True},
               'journaled_adoptions': [],
               'journaled_role_changes': [],
               'durable_adoptions': ['172.18.0.9:8090']},
               False, ANNOUNCED_FORGE_PORT, note))
    # The unkeyed posture owes no verify pull at all.
    expect('unkeyed-verify-pulled',
           lambda note: _judge_announced_refusal({
               'announced': True,
               'demote': {'status': 409, 'body': 'no_tracking_source'},
               'verify_pulls': [{'kind': 'serve', 'signed': False,
                                 'query': '', 'status': 200}],
               'role_after': {'role': 'active'},
               'document_after': {'source_owns_field': True},
               'journaled_adoptions': [],
               'journaled_role_changes': [],
               'durable_adoptions': []},
               False, ANNOUNCED_FORGE_PORT, note))
    # The keyed posture owes one unsigned pull, refused at the gate.
    expect('keyed-verify-unpulled',
           lambda note: _judge_announced_refusal({
               'announced': True,
               'demote': {'status': 409, 'body': 'no_tracking_source'},
               'verify_pulls': [], 'role_after': {'role': 'active'},
               'document_after': {'source_owns_field': True},
               'journaled_adoptions': [],
               'journaled_role_changes': [],
               'durable_adoptions': []},
               True, ANNOUNCED_FORGE_PORT, note))
    # The staged document was never flipped.
    expect('unflipped-replay',
           lambda note: _judge_forgery({
               'keys': ['source_owns_field'],
               'changed': {'source_owns_field': {'from': True,
                                                 'to': True}},
               'generation': 9, 'tick': 40,
               'source_owns_field': True},
               ('source_owns_field',), note))
    # The pair never reconverged onto one active plus one tracker.
    expect('pair-not-reconverged',
           lambda note: _judge_announced_settle(
               {'active': {'role': 'active'},
                'standby': {'role': 'active'}}, 'active', 'standby',
               note))
    return slipped


# ---- the episode pass ----------------------------------------------

def _announced_refusal_leg(ctx, base, owner, floor, forge):
    """One announced-forgery refusal leg: POST /demote on the field
    owner while the endpoint serves its staged document, collecting the
    record `_judge_announced_refusal` audits — the demote answer, the
    endpoint's ledger of the pulls since the call, the owner's served
    role and field claim after it, and the journal window plus the
    whole-file adoption audit a refused demotion must not grow."""
    hits_before = len(_announced_hits(forge['hits']))
    status, body = _settle_call(base + '/demote')
    pulls = [record for record in _announced_hits(forge['hits'])[hits_before:]
             if record.get('kind') == 'serve']
    adoptions, changes = _announced_switch_journaled(ctx, owner, floor)
    return {'announced': _announced_hinted(forge),
            'forge_port': forge['port'],
            'demote': {'status': status, 'body': body},
            'verify_pulls': pulls,
            'role_after': _try_role(ctx, base),
            'document_after': _field_stamp(_owner_document(ctx, base)),
            'journaled_adoptions': adoptions,
            'journaled_role_changes': changes,
            'durable_adoptions': _announced_durable_adoptions(ctx, owner),
            'refusals': _announced_journaled(
                ctx, owner, floor, 'tracking_source_refused')}


def _announced_switch_journaled(ctx, owner, floor):
    """The owner journal's tail since `floor`: (tracking-source
    adoptions, role changes) — the durable record a refused demotion
    must not grow."""
    return (_announced_journaled(ctx, owner, floor,
                                 'tracking_source_adopted') or [],
            _announced_journaled(ctx, owner, floor, 'role_changed') or [])


def _clause_notes(problems):
    """A `note(clause, detail)` sink keeping the first detail each
    contract clause breaks — the violation map is keyed by
    leg-and-clause so no clause's detail is lost behind another."""
    def note(clause, detail):
        problems.setdefault(clause, detail)
    return note


def _announced_verify_pass(ctx, number, owner, keyed):
    """One announced-source-verify pass: the announced-only window,
    the two staged replay forgeries announced through the endpoint's
    own `?peer=` pull, the demote each is verified against, and the
    restore back to the entry roles. Returns (digest, violations,
    evidence): digest is the pass's normalized verdict record,
    identical across clean passes; violations maps each leg-and-clause
    key the pass broke to (diagnostic, detail) and the evidence carries
    the same map with its details; `evidence['inconclusive']` names a
    lost observation, a posture the leg never claimed to exercise, or a
    revision that predates the ownership stamps, and
    `evidence['precontract']` the unkeyed pre-contract signature."""
    digest, violations, evidence = _announced_verify_window(
        ctx, number, owner, keyed)
    evidence['violations'] = {key: diagnostic
                              for key, (diagnostic, _)
                              in violations.items()}
    evidence['details'] = {key: detail for key, (_, detail)
                           in violations.items()}
    return digest, violations, evidence


def _announced_verify_window(ctx, number, owner, keyed):
    """The pass's episode: the announced-only window, the two staged
    replay forgeries, the demote each is verified against, and the
    restore back to the entry roles."""
    violations = {}
    evidence = {'entry_owner': owner, 'pass': number,
                'posture': 'keyed' if keyed else 'unkeyed'}
    digest = {'announce': 'missing', 'replay': 'adopted',
              'bumped': 'adopted', 'adoption': 'forged',
              'field': 'released', 'verify': 'unpulled',
              'roles': 'unrestored'}
    peer = 'standby' if owner == 'active' else 'active'
    base = ctx[owner]
    evidence['peer'] = peer

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, DIAG_FAILED, detail)

    # The settle gate: the entry owner holds the field and the other
    # launched peer tracks it — the layout the pass's restore owes.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled — ' + owner
               + ' holds no active role with ' + peer
               + ' tracking behind it')
        return None, violations, evidence

    # The announced-only window: the tracking peer stopped, the field
    # owner warm-restarted so its recorded-hint set begins empty — the
    # forged endpoint's announce is then the only hint the demote has.
    try:
        ctx['stop_controller'](peer)
        ctx['restart_controller'](owner)
    except Exception as exc:
        failed('window', 'the lifecycle actions never opened the '
               'announced-only window: ' + str(exc)[:200])
        return None, violations, evidence
    restored = wait_for(
        lambda: ((_try_role(ctx, base) or {}).get('role') == 'active'
                 and _try_role(ctx, base)) or None,
        time.monotonic() + ANNOUNCED_SETTLE, interval=ANNOUNCED_POLL)
    evidence['window'] = {'peer': 'stopped', 'owner_restart': restored}
    if restored is None:
        failed('window', 'the restarted owner never reported active '
               'again')
        return None, violations, evidence

    # The contract surface: the owner's own served document is the
    # replay the forgeries derive from, and the stamp they rewrite must
    # be present on it.
    victim = _owner_document(ctx, base)
    if victim is None:
        evidence['inconclusive'] = \
            'the restarted owner\'s checkpoint never answered — the '\
            'replay the forgeries derive from is unavailable'
        return None, violations, evidence
    if 'source_owns_field' not in victim:
        evidence['inconclusive'] = \
            'the served checkpoint carries no source_owns_field ' \
            'stamp — the rig predates the announced-source ' \
            'ownership stamps'
        return None, violations, evidence
    if victim.get('source_owns_field') is not True:
        failed('victim', 'the field owner serves no field-owning '
               'checkpoint document: ' + json.dumps(victim)[:200])
        return None, violations, evidence

    replay = _replay_document(victim)
    bumped = _bumped_document(victim)
    evidence['victim'] = {'generation': victim.get('generation'),
                          'tick': victim.get('tick'),
                          'line_owner': victim.get('line_owner'),
                          'source_owns_field': victim.get(
                              'source_owns_field')}
    floor = _announced_journal_floor(ctx, owner)

    refusals = {}
    evidence['legs'] = refusals
    forge = None
    try:
        # The tokenless forged endpoint, announcing itself to the
        # field owner through its own `?peer=` pull — the same public
        # channel a real tracking peer uses, and the only recorded hint
        # the announced-only window leaves.
        forge = ctx['start_forge'](replay, owner, keyed=False)
        if wait_for(lambda: _announced_hinted(forge) or None,
                    time.monotonic() + ANNOUNCED_HINT_SETTLE,
                    interval=ANNOUNCED_POLL) is None:
            failed('announce', 'the forged endpoint\'s ?peer= '
                   'announce never landed on the owner — its hint '
                   'was never recorded')
            return None, violations, evidence
        digest['announce'] = 'landed'

        for label, document, expect in (
                ('replay', replay, ('source_owns_field',)),
                ('bumped', bumped, ('source_owns_field', 'tick'))):
            if label == 'bumped':
                _announced_stage_document(forge, document)
            record = _announced_refusal_leg(ctx, base, owner, floor, forge)
            record['forgery'] = _forgery_record(victim, document)
            refusals[label] = record
            problems = {}
            note_problem = _clause_notes(problems)
            _judge_forgery(record['forgery'], expect, note_problem)
            _judge_announced_refusal(record, keyed, forge['port'],
                                     note_problem)
            if not problems:
                digest[label] = 'refused'
            for clause, detail in problems.items():
                failed(label + ':' + clause, detail)
    finally:
        if forge is not None:
            try:
                ctx['stop_forge']()
            except Exception:
                pass

    # A dropped served read is a lost observation, never the verdict:
    # the refusal audit cannot be read as a result.
    lost = sorted(label for label, record in refusals.items()
                  if record['role_after'] is None
                  or record['document_after'] is None)
    if lost:
        evidence['inconclusive'] = \
            'the owner\'s served role or checkpoint read dropped after '\
            + ', '.join(lost) + ' — the refusal cannot be audited'
        return None, violations, evidence
    if not any(record['journaled_adoptions'] is None
               or record['durable_adoptions'] is None
               for record in refusals.values()):
        digest['adoption'] = 'none'
    if all((record['document_after'] or {}).get('source_owns_field')
           is True for record in refusals.values()):
        digest['field'] = 'kept'
    if keyed:
        digest['verify'] = 'unsigned-pull' \
            if any(record['verify_pulls'] for record in
                   refusals.values()) else 'unpulled'
    else:
        digest['verify'] = 'no-pull' \
            if not any(record['verify_pulls'] for record in
                       refusals.values()) else 'pulled'

    # The configured-source posture: a field owner carrying a
    # `--standby` slot demotes toward that source and never consults an
    # announced hint, so the announced-only window this leg stages is
    # not the demotion path it can exercise.
    configured = sorted(
        label for label, record in refusals.items()
        if any(not str((body or {}).get('source')).endswith(
                   ':' + str(record['forge_port']))
               for body in (record['journaled_adoptions'] or [])))
    if configured:
        evidence['inconclusive'] = (
            'the field owner carries a configured --standby source: '
            'POST /demote on ' + ', '.join(configured) + ' adopted '
            + json.dumps([refusals[label]['journaled_adoptions']
                          for label in configured])[:200]
            + ' instead of refusing the announced hint — the '
            'announced-only window the leg stages is not that '
            'demotion path')
        return None, violations, evidence

    # The pre-contract signature: on an unkeyed pair the forged
    # announce armed the demotion — `POST /demote` answered 200 where
    # the named refusal belongs. Every build before #867's fix
    # presents exactly that shape, so the leg reports it as a revision
    # predating the contract, not as a defect in it. A demote that
    # answered the named refusal while journaling an adoption or
    # moving a role contradicts its own answer: that is a defect in
    # the contract, not a stale revision.
    armed = sorted(label for label, record in refusals.items()
                   if (record['demote'] or {}).get('status', 0) in
                   (200, 201, 202, 204))
    if not keyed and armed:
        evidence['precontract'] = (
            'the unkeyed forged announce armed the demotion (' + ', '
            .join(armed) + '): POST /demote answered '
            + json.dumps({label: refusals[label]['demote']
                          for label in armed}, sort_keys=True)[:200]
            + ' and the active left the field — the announced-source '
            'authenticity rule #867 establishes is absent, so the '
            'staged revision predates the contract')

    # The restore: the tracking peer's container back up — its
    # configured standby pull re-announces and re-tracks the owner — so
    # the next pass and the legs behind this one meet the launch roles.
    try:
        ctx['start_controller'](peer)
    except Exception as exc:
        failed('restore:container', 'the stopped peer never came '
               'back: ' + str(exc)[:200])
        return None, violations, evidence
    settled = wait_for(
        lambda: (_pair_active(ctx) == owner or None)
        and _tracking_standby(ctx, peer),
        time.monotonic() + ANNOUNCED_SETTLE, interval=ANNOUNCED_POLL)
    reports = {name: _try_role(ctx, ctx[name]) for name in (owner, peer)}
    evidence['restored'] = reports
    problems = {}
    note_problem = _clause_notes(problems)
    _judge_announced_settle(reports, owner, peer, note_problem)
    if settled is None:
        note_problem('settle', 'the pair never settled back to its '
                    'entry role layout')
    if not problems:
        digest['roles'] = 'restored'
    for clause, detail in problems.items():
        failed('restore:' + clause, detail)

    evidence['digest'] = dict(digest)
    return digest, violations, evidence


def scenario_announced_source_verify(ctx):
    """Exercise the announced-source authenticity rule on the deployed
    pair: a `?peer=`-announced forge serving the field owner's own
    checkpoint with `source_owns_field` flipped — and the same replay
    with its tick jumped ahead — cannot arm a demotion. The named
    `no_tracking_source` refusal answers, no adoption names the forged
    endpoint, the active keeps its role and its field claim, and the
    pair reconverges to one active plus one tracking standby with its
    launch roles restored."""
    case = Case(
        'announced-source-verify',
        'An announced forged checkpoint cannot arm a demotion',
        'with the deployed pair settled and tracking, each pass opens '
        'the announced-only window — the tracking peer stopped, the '
        'field owner warm-restarted so no genuine announce stands — '
        'stages the field owner\'s own served checkpoint replayed '
        'verbatim with source_owns_field flipped false and that same '
        'replay with its tick jumped far ahead on the run\'s '
        'bridge-placed tokenless forge endpoint, announces it to the '
        'owner through its own GET /checkpoint?peer= pull, and runs '
        'POST /demote against each; the demote answers the named '
        'no_tracking_source refusal — with no verify pull at all on '
        'the unkeyed run, where a bare hint earns none, and with one '
        'unsigned pull refused by the proof gate on the keyed run — '
        'no tracking_source_adopted names the forged endpoint in the '
        'journal window or anywhere in the durable journal, no role '
        'change is journaled, the active keeps both its role and its '
        'source_owns_field field claim, and the pair reconverges to '
        'one active plus one tracking standby with its launch roles '
        'restored; two passes produce identical digests')
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the announced-source verify needs is '
                               'absent')
        for action in ('stop_controller', 'start_controller',
                       'restart_controller', 'start_forge',
                       'stop_forge'):
            if ctx.get(action) is None:
                return case.finish('inconclusive', 'the run context '
                                   'carries no ' + action + ' action '
                                   '— the announced-only window and '
                                   'the forged endpoint cannot be '
                                   'driven')
        placements = ctx.get('endpoint_placement') or {}
        if placements.get('forge') != 'bridge':
            return case.finish('inconclusive', 'endpoint_placement '
                               'does not place forge on the rig bridge '
                               '— a rig-dialed endpoint cannot stand '
                               'on the host')
        journal_files = ctx.get('journal_files') or {}
        if not all(journal_files.get(name)
                   for name in ('active', 'standby')):
            return case.finish('inconclusive', 'the run context '
                               'carries no per-controller journal '
                               'files — the no-adoption audit cannot '
                               'run')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + ANNOUNCED_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=ANNOUNCED_POLL)
        if owner is None:
            return case.finish('failed', 'no peer reports role=active')
        peer = 'standby' if owner == 'active' else 'active'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=ANNOUNCED_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the leg '
                               'opens its announced-only window from '
                               'was never reached')
        keyed = bool(ctx.get('pair_token'))
        case.observe('field owner: ' + owner + ' (' + ctx[owner] + '); '
                     'tracking peer: ' + peer + '; deployed posture: '
                     + ('keyed' if keyed else 'unkeyed'))
        digests = []
        try:
            for number in (1, 2):
                digest, violations, evidence = _announced_verify_pass(
                    ctx, number, owner, keyed)
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'announced-source-verify-pass-' + str(number)
                    + '.json', evidence)
                case.evidence('file', ref, 'announced-source-verify '
                              'pass ' + str(number) + ' — the '
                              'announced-only window, the two staged '
                              'replay forgeries, each demote answer '
                              'with the endpoint pull ledger, the '
                              'served role and field claim, the '
                              'journal-window and whole-file adoption '
                              'audits, the reconvergence, and the '
                              'normalized digest')
                if evidence.get('inconclusive'):
                    return case.finish('inconclusive',
                                       evidence['inconclusive'])
                if evidence.get('precontract'):
                    return case.finish('inconclusive',
                                       DIAG_PREDATES + ': '
                                       + evidence['precontract'])
                if violations or digest is None:
                    diagnostic = DIAG_FAILED \
                        if digest is None or any(
                            name == DIAG_FAILED
                            for name, _ in violations.values()) \
                        else DIAG_NONDET
                    return case.finish(
                        'failed', diagnostic + ': ' + '; '.join(
                            detail for _, detail in
                            list(violations.values())[:4]))
                digests.append(digest)
        finally:
            # The launch layout for the legs behind this one: a clean
            # pass restores it by construction; an aborted pass gets
            # the forged endpoint removed, the peer's container back,
            # and the documented role order run again, best-effort.
            try:
                ctx['stop_forge']()
            except Exception:
                pass
            try:
                ctx['start_controller'](peer)
            except Exception:
                pass
            try:
                if _pair_active(ctx) != owner:
                    if _pair_active(ctx) is not None:
                        _settle_call(ctx[_pair_active(ctx)] + '/demote')
                    _settle_call(ctx[owner] + '/promote')
                wait_for(lambda: (_pair_active(ctx) == owner or None)
                         and _tracking_standby(ctx, peer),
                         time.monotonic() + ANNOUNCED_SETTLE,
                         interval=ANNOUNCED_POLL)
            except Exception as exc:
                case.observe('cleanup: role restore failed: '
                             + str(exc)[:200])
        if digests[0] != digests[1]:
            return case.finish(
                'failed', DIAG_NONDET + ': the two passes\' digests '
                'diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two announced-source-verify passes, identical '
                     'digests: ' + json.dumps(digests[0], sort_keys=True))
        # The unchecked-diagnostic self-check: every judge replays the
        # planted negative it must name; a silent judge means the leg
        # can no longer catch what it names.
        slipped = _self_check()
        if slipped:
            return case.finish('failed', DIAG_UNCHECKED + ': planted '
                               'negatives slipped the leg\'s own '
                               'audits: ' + ', '.join(slipped))
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))