"""The standby_dns_resume acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the standby-dns-resume leg shares the launch-layout
# window — it relaunches the launched-active member through the
# runner's relaunch lever with a doctored tracking source, demotes
# it onto that source for the named degraded evidence, and lands the
# pair back on the launch roles and flags before the tune case's
# a->b switch.
RUNS_BEFORE = frozenset({'scenario_parameter_tune_carryover'})


# --------------------------------------------------------------------
# The unresolvable-standby-name resume contract — the lane evidence
# for #1081's fix serving WW-LCM-001's restart-resume clause: a
# controller resuming as field owner whose standby-naming DNS name
# does not resolve must not exit — the standby-resolution failure is
# a named degraded standby condition, not a fatal boot error, when
# the run owns the field. The pair's restart legs relaunch the pair's
# own containers with their configured flags, so no leg ever staged a
# name-resolution failure at resume until this one.
#
# Staging: with the deployed pair settled — the launched-active
# member owning the field, its sibling tracking — the runner's
# relaunch lever recreates the field owner's container with its
# tracking-source argument doctored to an unresolvable name. On the
# launched active that argument is `--peer` — the flag through which
# an owner names the standby it would track if demoted, whose own
# help text pledges it "degrades like --standby when the name does
# not resolve". The resumed process must stay up owning the field —
# scans advancing, a receipted write landing, the served surface
# answering — and when the leg demotes it onto that very source, the
# standby resolution failure must surface as the named `sync.degraded`
# evidence on the still-serving monitor, never the observed exit(1)
# a pre-contract startup produced. The pass then relaunches the
# launch flags back: the demote released the field's claim, the
# recreated active's conditional startup grant takes it, and the
# sibling's pulls reconverge — the pair's launch roles and flags
# restored for the cases behind this one. Two passes must produce
# identical digests; inconclusive where the runner's relaunch lever
# cannot doctor the tracking-source argument or the rig never
# presents the settled launch layout.

DNS_RESUME_SETTLE = 45   # bound on each settle/demote/restore wait
DNS_RESUME_AUDIT = 30    # bound on the degraded-evidence wait
DNS_RESUME_POLL = 0.4    # cadence on the pair posture waits
DNS_RESUME_WATCH = 0.05  # cadence on the resume and demote watches

# The doctored tracking source: an RFC-2606 `.invalid` name no rig
# bridge alias or DNS answer ever resolves — the same unresolvable
# shape #1081's controller tests bind — so every checkpoint pull the
# demoted peer runs on it counts a resolution miss. The argument is
# spelled `--peer` on the launched active: the flag through which a
# field owner names its standby.
UNRESOLVABLE_TRACK = 'dcs-peer-down.invalid:8080'


def _degraded_verdict(report, target, note):
    """The demoted resumed peer's degraded-standby audit: the served
    /role must carry `sync.degraded` naming the doctored tracking
    source's resolution failure — the named degraded standby evidence
    the contract puts in place of the observed exit(1). Returns
    'named' for the pass digest; 'silent' after noting the violation.
    A dead monitor and a served-but-foreign detail are both silence —
    one is the process exit the contract forbids, the other evidence
    that never named the doctored resolution."""
    if report is None:
        note('degraded', 'standby-dns-resume-failed',
             'the demoted resumed peer stopped answering /role — the '
             'unresolvable tracking source took the process down '
             'instead of reporting as named degraded evidence')
        return 'silent'
    detail = ((report.get('sync') or {}).get('degraded') or {}) \
        .get('detail')
    if not isinstance(detail, str) or target not in detail \
            or 'resolve' not in detail:
        note('degraded', 'standby-dns-resume-failed',
             'the demoted resumed peer\'s standby evidence never '
             'named the doctored tracking source\'s resolution '
             'failure — the contract\'s named degraded condition — '
             'served ' + json.dumps(report.get('sync'))[:200])
        return 'silent'
    return 'named'


def _dns_resume_pass(ctx, number, owner, peer, point, value):
    """One standby-dns-resume pass: relaunch the field owner with its
    tracking-source argument doctored to the unresolvable name, prove
    the resumed process stays up owning the field — scans advancing,
    the served surface answering, a receipted write landing — demote
    it onto the doctored source for the named degraded standby
    evidence, then relaunch the launch flags back and reconverge the
    pair. Returns (digest, violations, evidence): the digest is the
    pass's normalized verdict record, identical across clean
    passes."""
    violations = {}
    evidence = {'pass': number, 'entry_owner': owner,
                'track': UNRESOLVABLE_TRACK}
    digest = {'resume': 'unstaged', 'scans': 'stalled',
              'served': 'unanswered', 'write': 'unlanded',
              'degraded': 'unproven', 'roles': 'unrestored'}
    base, peer_base = ctx[owner], ctx[peer]

    def note(key, diagnostic, detail):
        violations.setdefault(key, (diagnostic, detail))

    def failed(key, detail):
        note(key, 'standby-dns-resume-failed', detail)

    def inconclusive(key, detail):
        evidence['inconclusive'] = key + ': ' + detail
        return finish(None)

    def finish(result):
        evidence['violations'] = {key: {'diagnostic': name,
                                        'detail': detail}
                                  for key, (name, detail)
                                  in violations.items()}
        evidence['digest'] = result
        return result, violations, evidence

    # The settled gate: the launched-active owner holds the field and
    # the sibling tracks it — the layout the pass's restore owes.
    if _pair_active(ctx) != owner \
            or _tracking_standby(ctx, peer) is None:
        failed('settle', 'the pair never settled on the launch roles '
               '— ' + owner + ' holds no active role with ' + peer
               + ' tracking behind it')
        return finish(None)

    # The doctored relaunch: the runner's lever recreates the field
    # owner's container with --peer naming the unresolvable standby —
    # the flag shape a plain docker start could never stage.
    try:
        ctx['relaunch_controller'](owner, UNRESOLVABLE_TRACK)
    except Exception as exc:
        return inconclusive('relaunch', 'the runner\'s relaunch lever '
                            'never staged the doctored tracking '
                            'source on ' + owner + ': '
                            + str(exc)[:200])
    evidence['relaunched'] = {'flag': '--peer',
                              'track': UNRESOLVABLE_TRACK}

    # The resume watch: the relaunched monitor answers /role as the
    # field owner while the sibling never takes the field inside the
    # relaunch window. A monitor that never answers is the observed
    # exit(1) shape — the contract violation the leg exists to name.
    promoted = []
    back = None
    deadline = time.monotonic() + DNS_RESUME_SETTLE
    while back is None and time.monotonic() < deadline:
        report = _try_role(ctx, peer_base)
        if (report or {}).get('role') == 'active':
            promoted.append(report)
        report = _try_role(ctx, base)
        if (report or {}).get('role') == 'active':
            back = report
        else:
            time.sleep(DNS_RESUME_WATCH)
    evidence['resumed'] = back
    evidence['peer_promoted'] = promoted[0] if promoted else None
    if promoted:
        failed('peer-promoted', 'the tracking sibling took the field '
               'inside the relaunch window — the resumed process\'s '
               'refused grant exited it before the contract could '
               'run: ' + json.dumps(promoted[0])[:300])
        return finish(digest)
    if back is None:
        failed('resumed', 'the relaunched owner never answered /role '
               '— the unresolvable standby name ended the process '
               'rather than degrading to pull misses')
        return finish(digest)
    digest['resume'] = 'up'

    # The owning assertions: scans advancing — the resumed run's tick
    # continues past its first answer.
    tick0 = back.get('tick') or 0

    def advanced():
        report = _try_role(ctx, base)
        return report if report is not None \
            and (report.get('tick') or 0) > tick0 else None

    grown = wait_for(advanced, time.monotonic() + DNS_RESUME_SETTLE,
                     interval=DNS_RESUME_POLL)
    evidence['tick_advanced'] = grown
    if grown is None:
        failed('scans', 'the resumed owner\'s tick never advanced '
               'past ' + str(tick0) + ' — the process serves but '
               'does not scan')
        return finish(digest)
    digest['scans'] = 'advanced'

    # The served surface answering — the same monitor reads every
    # consumer makes.
    try:
        _, signals = http_json('GET', base + '/signals')
        _snapshot(ctx, base)
    except Exception as exc:
        failed('served', 'the resumed owner\'s served surface never '
               'answered: ' + str(exc)[:200])
        return finish(digest)
    digest['served'] = 'answered'

    # A receipted write landing — the field stays writable through
    # the owner the relaunch resumed.
    command = {'command': {'write_value': {
        'point': point, 'kind': 'bool', 'value': {'bool': value}}},
        'actor': 'qa-dns-resume-' + str(number),
        'reason': 'standby-dns-resume'}
    try:
        status, receipt = http_json('POST', base + '/command', command)
    except Exception as exc:
        status, receipt = None, str(exc)
    evidence['write'] = {'status': status, 'receipt': receipt}
    if status != 200 or not isinstance(receipt, dict) \
            or _outcome_key(receipt) not in ('accepted', 'applied'):
        failed('write', 'the resumed owner\'s command path refused '
               'the receipted write: ' + str(status) + ' '
               + json.dumps(receipt)[:200])
        return finish(digest)
    landed = wait_for(
        lambda: _point_value(_try_snapshot(ctx, base) or {}, point)
        == value or None,
        time.monotonic() + DNS_RESUME_SETTLE, interval=DNS_RESUME_POLL)
    evidence['write_landed'] = bool(landed)
    if not landed:
        failed('write-landed', 'the resumed owner\'s write never '
               'reached the served point — ' + json.dumps(value)
               + ' at point ' + str(point))
        return finish(digest)
    digest['write'] = 'landed'

    # The named degraded half: demoting the resumed owner lands it on
    # its doctored tracking source — a configured --peer the demote
    # guard accepts — where every checkpoint pull resolves the name
    # fresh and misses. The standby resolution failure must read as
    # the named sync.degraded evidence on the still-serving monitor.
    try:
        status, body = _settle_call(base + '/demote')
    except Exception as exc:
        status, body = None, str(exc)
    evidence['demote'] = {'status': status, 'body': body}
    if status != 200:
        failed('demote', 'POST /demote on the resumed owner answered '
               + str(status) + ' ' + json.dumps(body)[:200])
        return finish(digest)

    def landed_standby():
        report = _try_role(ctx, base)
        return report if report is not None \
            and report.get('role') == 'standby' else None

    demoted = wait_for(landed_standby,
                       time.monotonic() + DNS_RESUME_SETTLE,
                       interval=DNS_RESUME_WATCH)
    evidence['demoted'] = demoted
    if demoted is None:
        failed('demote-settle', 'the resumed owner never landed the '
               'standby role after its demote')
        return finish(digest)

    def degraded():
        report = _try_role(ctx, base)
        detail = ((report or {}).get('sync') or {}) \
            .get('degraded', {}).get('detail')
        return report if isinstance(detail, str) else None

    seen = wait_for(degraded, time.monotonic() + DNS_RESUME_AUDIT,
                    interval=DNS_RESUME_POLL)
    last = _try_role(ctx, base)
    evidence['degraded'] = seen if seen is not None else last
    digest['degraded'] = _degraded_verdict(
        seen if seen is not None else last,
        UNRESOLVABLE_TRACK, note)

    # The restore half: relaunch the launch command back — the demote
    # released the field's claim, so the recreated active's
    # conditional startup grant takes it, and the sibling's tracking
    # pulls reconverge behind the restored owner once its name
    # answers again.
    try:
        ctx['relaunch_controller'](owner)
    except Exception as exc:
        failed('restore', 'the restore relaunch on ' + owner
               + ' never completed: ' + str(exc)[:200])
        return finish(digest)

    def settled():
        if _pair_active(ctx) != owner:
            return None
        return _tracking_standby(ctx, peer)

    restored = wait_for(settled, time.monotonic() + DNS_RESUME_SETTLE,
                        interval=DNS_RESUME_POLL)
    evidence['restored'] = restored
    if restored is None:
        failed('restore', 'the pair never settled back to the launch '
               'roles — ' + owner + ' owning with ' + peer
               + ' tracking')
        return finish(digest)
    digest['roles'] = 'restored'
    return finish(digest)


def scenario_standby_dns_resume(ctx):
    """Exercise the #1081 unresolvable-standby-name resume contract on
    the deployed pair: relaunch the field-owning launched-active
    through the runner's relaunch lever with its tracking-source
    argument doctored to an unresolvable name, and audit that the
    resumed process stays up owning the field — scans advancing,
    writes landing, the served surface answering — with the standby
    resolution surfacing as named degraded standby evidence on the
    demote, never the observed exit(1); the pair's launch roles and
    flags restore, and two passes produce identical digests."""
    case = Case(
        'standby-dns-resume',
        'Resumed field owner survives an unresolvable standby name',
        'with the deployed pair settled on the launched-active '
        'owner, the runner\'s relaunch lever recreates the field '
        'owner\'s container with its tracking-source argument '
        'doctored to an unresolvable name (`--peer '
        + UNRESOLVABLE_TRACK + '` — the flag through which an owner '
        'names the standby it would track if demoted); the resumed '
        'process stays up owning the field — its /role answering '
        'active, scans advancing, a receipted write landing, the '
        'served surface answering — and on demote the standby '
        'resolution failure reads as the named `sync.degraded` '
        'evidence on the still-serving monitor rather than the '
        'observed exit(1); the launch flags and roles restore, and '
        'two passes produce identical digests')
    owner = peer = None
    restore_pending = False
    try:
        if ctx.get('active') is None or ctx.get('standby') is None:
            return case.finish('inconclusive', 'the run context '
                               'carries only one endpoint — the pair '
                               'the resume leg needs is absent')
        if ctx.get('relaunch_controller') is None:
            return case.finish(
                'inconclusive', 'the run context carries no '
                'relaunch_controller action — the runner\'s restart '
                'machinery cannot doctor the tracking-source '
                'argument')
        for name in ('active', 'standby'):
            try:
                _role(ctx, ctx[name])
            except Exception as exc:
                return case.finish('inconclusive', name + '\'s '
                                   'monitor is unreachable: '
                                   + str(exc)[:200])
        deadline = time.monotonic() + DNS_RESUME_SETTLE
        owner = wait_for(lambda: _pair_active(ctx), deadline,
                         interval=DNS_RESUME_POLL)
        if owner != 'active':
            return case.finish(
                'inconclusive', 'the pair\'s field owner is not the '
                'launched-active peer the doctored relaunch needs — '
                'the leg cannot stage the owning resume')
        peer = 'standby'
        if wait_for(lambda: _tracking_standby(ctx, peer), deadline,
                    interval=DNS_RESUME_POLL) is None:
            return case.finish('inconclusive', 'the pair has no '
                               'tracking standby — the settle the '
                               'leg relaunches inside was never '
                               'reached')
        _, signals = http_json('GET', ctx[owner] + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'standby-dns-resume-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the writable '
                      'command point')
        points = _writable_bool_points(signals, 1)
        if not points:
            return case.finish('inconclusive', 'the model declares '
                               'no writable bool command point')
        point = points[0]
        snapshot = _snapshot(ctx, ctx[owner])
        baseline = _point_value(snapshot, point)
        if not isinstance(baseline, bool):
            baseline = False
        value = not baseline
        case.observe('field owner: ' + owner + ' (' + ctx[owner]
                     + '); sibling standby: ' + peer
                     + '; doctored track ' + UNRESOLVABLE_TRACK
                     + '; command point ' + str(point))
        digests = []
        for number in (1, 2):
            restore_pending = True
            digest, violations, evidence = _dns_resume_pass(
                ctx, number, owner, peer, point, value)
            if (digest or {}).get('roles') == 'restored':
                restore_pending = False
            ref = save_evidence(
                ctx['evidence_dir'],
                'standby-dns-resume-pass-' + str(number) + '.json',
                evidence)
            case.evidence('file', ref, 'standby-dns-resume pass '
                          + str(number) + ' — the doctored relaunch, '
                          'the owning assertions, the named degraded '
                          'standby evidence, and the normalized '
                          'digest')
            if evidence.get('inconclusive'):
                return case.finish('inconclusive',
                                   evidence['inconclusive'])
            if violations or digest is None:
                diagnostic = 'standby-dns-resume-failed' \
                    if digest is None or any(
                        name == 'standby-dns-resume-failed'
                        for name, _ in violations.values()) \
                    else 'standby-dns-resume-nondeterministic'
                return case.finish(
                    'failed', diagnostic + ': ' + '; '.join(
                        detail for _, detail in
                        list(violations.values())[:4]))
            digests.append(digest)
        if digests[0] != digests[1]:
            return case.finish(
                'failed', 'standby-dns-resume-nondeterministic: '
                'the two passes\' digests diverged: '
                + json.dumps(digests[0], sort_keys=True) + ' vs '
                + json.dumps(digests[1], sort_keys=True))
        case.observe('two standby-dns-resume passes, identical '
                     'digests')

        # The unchecked self-check: the degraded-standby audit, run
        # over a planted foreign-source degraded record, must name
        # its violation — a silent audit can no longer be trusted to
        # catch what it names.
        planted = {}
        verdict = _degraded_verdict(
            {'role': 'standby',
             'sync': {'degraded': {
                 'detail': 'fetch from dcs-hw-qa-a:8080: '
                           'connection refused'}}},
            UNRESOLVABLE_TRACK,
            lambda key, diagnostic, detail:
                planted.setdefault(key, (diagnostic, detail)))
        if verdict != 'silent' \
                or planted.get('degraded', ('',))[0] \
                != 'standby-dns-resume-failed':
            return case.finish(
                'failed', 'standby-dns-resume-unchecked: the '
                'degraded-standby audit stayed silent on a planted '
                'wrong-source record')
        case.observe('the self-check leg\'s planted negative '
                     'reported its named diagnostic')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
    finally:
        # The launch layout for the cases behind this one: a clean
        # pass restores it by construction; an aborted pass may have
        # left the owner dead, demoted, or still carrying the doctored
        # flags while owning the field, and the sibling promoted —
        # free the field for the launched-active's conditional grant,
        # re-drive the launch-flagged relaunch until the owner serves
        # (the doctored flag only leaves through a recreate, so an
        # owner still answering 'active' needs it too), and converge
        # the sibling back to tracking.
        if restore_pending and owner is not None and peer is not None \
                and ctx.get('relaunch_controller') is not None:
            deadline = time.monotonic() + DNS_RESUME_SETTLE * 2
            relaunched_at = 0.0
            try:
                while time.monotonic() < deadline:
                    reports = {name: _try_role(ctx, ctx[name])
                               for name in (owner, peer)}
                    if (reports.get(peer) or {}).get('role') \
                            == 'active':
                        _settle_call(ctx[peer] + '/demote')
                        # The field freed — the next relaunch's
                        # conditional grant can land.
                        relaunched_at = 0.0
                    elif not relaunched_at \
                            or ((reports.get(owner) or {})
                                .get('role') != 'active'
                                and time.monotonic() - relaunched_at
                                > 10):
                        try:
                            ctx['relaunch_controller'](owner)
                        except Exception:
                            pass
                        relaunched_at = time.monotonic()
                    elif (reports.get(owner) or {}).get('role') \
                            == 'active' \
                            and _tracking_standby(ctx, peer) \
                            is not None:
                        break
                    time.sleep(DNS_RESUME_POLL)
            except Exception:
                pass
