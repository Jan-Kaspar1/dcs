"""The attributed_switch acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: the attributed-switch leg needs any settled pair — it cycles
# every role layout itself — and leaves the launch roles standing, so it
# runs behind the failover leg's own switch regardless of which peer it
# finds holding the field.
RUNS_AFTER = frozenset({'scenario_failover'})


# --------------------------------------------------------------------
# The role-switch journal-attribution contract (WW-ENG-003, WW-OPS-002 —
# #470): an operator-requested `POST /demote` / `POST /promote` carrying
# a declared actor is the most consequential action the pair surface
# exposes, and both peers' durable journals must say who asked — each
# `role_changed` entry of the switch carrying `origin: "request"` and
# the request's actor on the peer that journaled it, the demoted peer's
# checkpoint-adopted journal echoing the same attributed record — while
# the executor's own automatic failover, the armed standby's self-
# promotion at its miss budget, journals `origin: "failover"` with no
# operator actor at all, reading distinguishably from any operator
# request, attributed or not. The leg exercises the attributed demote
# and promote in both directions — the configured-source demotion on
# the launched standby peer and the announced-source demotion on the
# duty peer alike — then severs the field owner and audits the
# automatic record before restoring the launch roles.

ATTRIBUTION_ACTOR = 'qa-attribution'
ATTRIBUTION_POLL = 0.5     # cadence watching roles and the journal file
ATTRIBUTION_SETTLE = 60    # bound on a transition or reconvergence wait
ATTRIBUTION_JOURNAL = 30   # bound on the durable file showing an entry
ATTRIBUTION_FAILOVER = 60  # bound on the armed standby's miss budget


def _role_stream(journal_path, floor=0):
    """The durable `role_changed` entries a peer's journal file gained
    since `floor` — `(seq, tick, from, to, origin, actor)` in seq
    order."""
    stream = []
    for item in _journal_entries(journal_path)[floor:]:
        entry = item.get('entry') or {}
        change = (entry.get('event') or {}).get('role_changed')
        if change is not None:
            stream.append((entry.get('seq'), entry.get('tick'),
                           change.get('from'), change.get('to'),
                           change.get('origin'), change.get('actor'),
                           sorted(change.keys())))
    return stream


def _gained_count(journal_path):
    """The durable journal file's current record count — the floor a
    phase's gained entries are read above."""
    return len(_journal_entries(journal_path))


def _wait_role_stream(journal_path, floor, want, deadline):
    """Poll the durable journal file until its gained `role_changed`
    stream reaches `want` entries — the file's drain is asynchronous,
    so the entries land a moment after the served transition."""
    def ready():
        stream = _role_stream(journal_path, floor)
        return stream if len(stream) >= want else None
    return wait_for(ready, deadline, interval=ATTRIBUTION_POLL)


def _attribution_misses(name, stream, want_pairs, want_origin,
                        want_actor):
    """The attribution violations in a peer's gained `role_changed`
    stream: the transition pairs, the recorded origin, and the
    declared actor the entry must name — or must not carry for the
    peer-initiated origins."""
    misses = []
    pairs = [(frm, to) for _s, _t, frm, to, _o, _a, _k in stream]
    if pairs != want_pairs:
        misses.append(name + ' journaled the role transitions '
                      + json.dumps(pairs) + ', expected '
                      + json.dumps(want_pairs))
        return misses
    for seq, tick, frm, to, origin, actor, keys in stream:
        if origin != want_origin:
            misses.append(name + ' journaled ' + frm + '->' + to
                          + ' at seq ' + str(seq) + ' tick '
                          + str(tick) + ' with origin '
                          + json.dumps(origin) + ', expected '
                          + json.dumps(want_origin))
        if want_actor is None:
            if 'actor' in keys:
                misses.append(name + ' journaled ' + frm + '->' + to
                              + ' at seq ' + str(seq)
                              + ' carrying an operator actor '
                              + json.dumps(actor) + ' — the '
                              'automatic path must never read as '
                              'an operator request')
        elif actor != want_actor:
            misses.append(name + ' journaled ' + frm + '->' + to
                          + ' at seq ' + str(seq) + ' with actor '
                          + json.dumps(actor) + ', expected the '
                          'declared actor '
                          + json.dumps(want_actor) + ' — an '
                          'attributed operator switch must '
                          'journal who asked')
    return misses


def scenario_attributed_switch(ctx):
    """Attributed demote/promote switches journal the declared actor
    on both peers' durable records, and the armed standby's automatic
    failover journals `origin: failover` — never an operator request —
    before the pair's launch roles return."""
    case = Case('attributed-switch',
                'The role journal distinguishes an attributed request '
                'from the executor\'s automatic failover',
                'an attributed POST /demote then POST /promote — each '
                'through the serving monitor declaring the leg\'s '
                'actor — journals the switch\'s role_changed entries '
                'on both peers\' durable records carrying '
                'origin=request and the declared actor ordered after '
                'the request, in both directions across the pair; '
                'severing the field owner lets the armed standby\'s '
                'miss budget expire into a self-promotion whose '
                'journal entries carry origin=failover and no '
                'operator actor — reading distinguishably from any '
                'operator request — and the pair\'s launch roles '
                'return through a last attributed switch')
    try:
        subject = _keyed_subject(ctx)
        if subject is None:
            return case.finish('inconclusive', 'the run carries no '
                               'keyed pair — the announced-source '
                               'demotion contract the attributed '
                               'switch needs cannot verify')
        stop = subject.get('stop_controller')
        start = subject.get('start_controller')
        journals = subject.get('journal_files') or {}
        if stop is None or start is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no controller stop/start '
                               'action — the failover induction has '
                               'no documented seam')
        journal_a = journals.get('active')
        journal_b = journals.get('standby')
        if journal_a is None or journal_b is None:
            return case.finish('inconclusive', 'the run context '
                               'carries no journal-file paths for '
                               'the pair')
        base_a, base_b = subject['active'], subject['standby']
        if base_a is None or base_b is None:
            return case.finish('inconclusive', 'the pair ctx names '
                               'no endpoints to exercise')
        budget = subject.get('failover_misses')
        if not isinstance(budget, int) or budget < 1:
            return case.finish('inconclusive', 'the run declares no '
                               'armed miss budget — the automatic '
                               'half has nothing to exercise')
        case.observe('pair under audit: active=' + str(base_a)
                     + ' standby=' + str(base_b) + ' miss budget '
                     + str(budget))

        def control(url, payload=None):
            """POST a control-plane request returning (status, body)
            with a refused call's named outcome decoded instead of
            raised."""
            try:
                return http_json('POST', url, payload)
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read() or b'null')
                except ValueError:
                    body = None
                finally:
                    exc.close()
                return exc.code, body

        def report(base):
            return _try_role(ctx, base)

        def tracking(base):
            rep = report(base)
            if (rep or {}).get('role') == 'standby' \
                    and 'tracking' in ((rep or {}).get('sync') or {}):
                return rep
            return None

        def attributed_switch(demote_key, promote_key):
            """One attributed switch — demote the owner, promote the
            converged peer — with both peers' durable journal audited
            for the declared-actor record. Returns the failure detail
            or None."""
            demote_base = subject[demote_key]
            promote_base = subject[promote_key]
            demote_journal = journals[demote_key]
            promote_journal = journals[promote_key]
            floor_a = _gained_count(demote_journal)
            floor_b = _gained_count(promote_journal)
            demote_tick = (report(demote_base) or {}).get('tick')
            promote_tick = (report(promote_base) or {}).get('tick')
            status, body = control(demote_base + '/demote',
                                   {'actor': ATTRIBUTION_ACTOR})
            if status != 200:
                return 'the attributed demote on ' + demote_key \
                       + ' answered ' + str(status) + ': ' \
                       + json.dumps(body)[:300]
            promoted = None
            deadline = time.monotonic() + ATTRIBUTION_SETTLE
            while time.monotonic() < deadline and promoted is None:
                status, body = control(promote_base + '/promote',
                                       {'actor': ATTRIBUTION_ACTOR})
                if status == 200:
                    promoted = body
                elif status == 409:
                    time.sleep(ATTRIBUTION_POLL)
                else:
                    return 'the attributed promote on ' + promote_key \
                           + ' answered ' + str(status) + ': ' \
                           + json.dumps(body)[:300]
            if promoted is None:
                return 'the attributed promote on ' + promote_key \
                       + ' never succeeded'
            settled = wait_for(
                lambda: report(promote_base)
                if (report(promote_base) or {}).get('role')
                == 'active' else None,
                time.monotonic() + ATTRIBUTION_SETTLE,
                interval=ATTRIBUTION_POLL)
            if settled is None:
                return 'the promoted peer ' + promote_key \
                       + ' never settled active'
            if wait_for(lambda: tracking(demote_base),
                        time.monotonic() + ATTRIBUTION_SETTLE,
                        interval=ATTRIBUTION_POLL) is None:
                return 'the demoted peer ' + demote_key \
                       + ' never reconverged tracking'
            # The durable audit: each peer's journal gained exactly
            # the switch's two transitions, origin=request carrying
            # the declared actor, ordered after the request.
            stream_d = _wait_role_stream(
                demote_journal, floor_a, 2,
                time.monotonic() + ATTRIBUTION_JOURNAL) or []
            stream_p = _wait_role_stream(
                promote_journal, floor_b, 2,
                time.monotonic() + ATTRIBUTION_JOURNAL) or []
            misses = _attribution_misses(
                demote_key, stream_d,
                [('active', 'demoting'), ('demoting', 'standby')],
                'request', ATTRIBUTION_ACTOR)
            misses += _attribution_misses(
                promote_key, stream_p,
                [('standby', 'promoting'), ('promoting', 'active')],
                'request', ATTRIBUTION_ACTOR)
            # Ordered after the request at its scan boundary: each
            # gained entry attributes to the request's own tick or
            # the boundary that followed it — never to a record the
            # request already stood past.
            if isinstance(demote_tick, int):
                for seq, tick, _f, _t, _o, _a, _k in stream_d:
                    if not isinstance(tick, int) or tick < demote_tick:
                        misses.append(demote_key + ' journaled a '
                                      'switch transition at tick '
                                      + str(tick) + ' ahead of the '
                                      'request boundary '
                                      + str(demote_tick))
            if isinstance(promote_tick, int):
                for seq, tick, _f, _t, _o, _a, _k in stream_p:
                    if not isinstance(tick, int) or tick < promote_tick:
                        misses.append(promote_key + ' journaled a '
                                      'switch transition at tick '
                                      + str(tick) + ' ahead of the '
                                      'request boundary '
                                      + str(promote_tick))
            ref = save_evidence(
                subject['evidence_dir'],
                'attributed-switch-' + demote_key + '-to-'
                + promote_key + '.json',
                {'demote': {'peer': demote_key,
                            'request_tick': demote_tick,
                            'journal': stream_d},
                 'promote': {'peer': promote_key,
                             'request_tick': promote_tick,
                             'journal': stream_p}})
            case.evidence('file', ref, 'the attributed switch\'s '
                          'durable role records on both peers')
            if misses:
                return '; '.join(misses)
            case.observe('attributed switch ' + demote_key + ' -> '
                         + promote_key + ' journaled actor '
                         + ATTRIBUTION_ACTOR + ' on both peers')
            return None

        owner = wait_for(lambda: _settled_active(subject),
                         time.monotonic() + 30)
        if owner is None:
            return case.finish('failed', 'no peer reports '
                               'role=active')
        if owner not in ('active', 'standby'):
            return case.finish('inconclusive', 'the field writer is '
                               + str(owner) + ' — outside the '
                               'launched pair')
        peer = 'standby' if owner == 'active' else 'active'

        # ---- attributed half ----
        # First land ctrl-a on the field: whichever layout the suite
        # hands this leg, one attributed switch puts the launch
        # arrangement up. Then a full attributed round trip — the
        # announced-source demotion on ctrl-a, the configured-source
        # demotion on ctrl-b — ending ctrl-a active and ctrl-b
        # tracking for the automatic half.
        if owner != 'active':
            failure = attributed_switch('standby', 'active')
            if failure:
                return case.finish('failed', failure)
        failure = attributed_switch('active', 'standby')
        if failure:
            return case.finish('failed', failure)
        failure = attributed_switch('standby', 'active')
        if failure:
            return case.finish('failed', failure)

        # ---- automatic half ----
        # Sever the field owner's checkpoint serving: the armed
        # standby's paced pulls miss until the declared budget's
        # scan boundary self-promotes it.
        floor = _gained_count(journal_b)
        try:
            stop('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the owner-stop '
                               'induction never completed: '
                               + str(exc)[:300])
        promoted = wait_for(
            lambda: report(base_b)
            if (report(base_b) or {}).get('role') == 'active'
            else None,
            time.monotonic() + ATTRIBUTION_FAILOVER,
            interval=ATTRIBUTION_POLL)
        if promoted is None:
            return case.finish('failed', 'the armed standby never '
                               'self-promoted after the owner\'s '
                               'checkpoint serving was severed — '
                               'GET /role answers '
                               + json.dumps(report(base_b))[:300])
        # The automatic record's durable shape: `origin: failover`,
        # no operator actor — the entries must read distinguishably
        # from any operator request.
        stream = _wait_role_stream(
            journal_b, floor, 2,
            time.monotonic() + ATTRIBUTION_JOURNAL) or []
        misses = _attribution_misses(
            'standby', stream,
            [('standby', 'promoting'), ('promoting', 'active')],
            'failover', None)
        ref = save_evidence(
            subject['evidence_dir'], 'attributed-switch-failover.json',
            {'budget': budget, 'journal': stream})
        case.evidence('file', ref, 'the self-promotion\'s durable '
                      'role record — origin failover, no actor')
        if misses:
            return case.finish('failed', '; '.join(misses))
        case.observe('the armed standby self-promoted at its miss '
                     'budget; the record reads origin=failover '
                     'with no operator actor')

        # ---- restore ----
        # The returned duty peer reconverges tracking off the
        # promoted standby, then the attributed switch restores the
        # launch roles.
        try:
            start('active')
        except Exception as exc:
            return case.finish('inconclusive', 'the owner restart '
                               'never completed: ' + str(exc)[:300])
        if wait_for(lambda: tracking(base_a),
                    time.monotonic() + ATTRIBUTION_SETTLE,
                    interval=ATTRIBUTION_POLL) is None:
            return case.finish('failed', 'the returned duty peer '
                               'never reconverged tracking on the '
                               'promoted standby')
        failure = attributed_switch('standby', 'active')
        if failure:
            return case.finish('failed', failure)
        settled = wait_for(
            lambda: {'a': report(base_a), 'b': report(base_b)}
            if (report(base_a) or {}).get('role') == 'active'
            and tracking(base_b) else None,
            time.monotonic() + ATTRIBUTION_SETTLE,
            interval=ATTRIBUTION_POLL)
        ref = save_evidence(subject['evidence_dir'],
                            'attributed-switch-restored.json',
                            settled or {'a': report(base_a),
                                        'b': report(base_b)})
        case.evidence('file', ref, 'the pair\'s restored launch '
                      'roles')
        if settled is None:
            return case.finish('failed', 'the pair did not settle '
                               'back to its launch role assignment')
        return case.finish('passed')
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
