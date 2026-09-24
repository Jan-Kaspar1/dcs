"""The doomed_startup_claim acceptance leg — one module per leg of the scenario schedule; see qa_lane/scenarios/__init__.py for the ordering rule and the shared seam."""
from .common import *

# Ordering: The doomed-startup-claim case shares that foreign seat beside it —
# launched onto a corrupt journal file against the settled pair and
# torn down before either revision case claims the seat.
RUNS_AFTER = frozenset({'scenario_failover'})
RUNS_BEFORE = frozenset({'scenario_incompatible_revision', 'scenario_model_revision'})


# The doomed-startup claim-ordering case's cadence: polls through the
# window a doomed foreign launch runs in — the abort lands inside the
# first polls, and a claim-then-died startup would surface as the
# incumbent demoted by a dead claim within the same window.
DOOMED_STARTUP_POLL = 0.5    # cadence watching the pair mid-attempt
DOOMED_STARTUP_ROUNDS = 10   # observation-window polls once launched
# The corrupt first record the case writes into the foreign peer's
# --journal-file before launch: valid JSON that is not a journal
# record, so the startup replay fails by name on line 1 and the bind —
# and with it any field claim — never happens.
DOOMED_CORRUPT_RECORD = ('{"qa-lane": "corrupt first record — '
                         'the doomed-startup induction"}')


def scenario_doomed_startup_claim(ctx):
    """A foreign peer whose startup aborts on an unreplayable journal
    file never strands a claim fencing the incumbent."""
    case = Case('doomed-startup-claim',
                'A doomed startup never fences the incumbent',
                'with the pair settled and the active holding the '
                'field claim, a foreign peer launched onto a journal '
                'file whose first record cannot be replayed aborts '
                'before its preemptive claim can run — the incumbent '
                'keeps role=active with its tick, field writes, and '
                'receipted command path undisturbed, every third-party '
                'mutation probe stays fenced under the standing claim '
                '(never unclaimed, never silently writable), no peer '
                'reports a spurious role change, and the rig returns '
                'clean once the peer is removed')
    start = ctx.get('start_foreign')
    stop = ctx.get('stop_foreign')
    foreign = ctx.get('foreign')
    journal = (ctx.get('journal_files') or {}).get('foreign')
    state_file = (ctx.get('state_files') or {}).get('foreign')
    if start is None or stop is None or foreign is None \
            or journal is None or not ctx.get('plant'):
        return case.finish('inconclusive', 'the run context carries no '
                           'foreign-peer launch/teardown action, '
                           'endpoint, journal-file path, or plant '
                           'address')
    try:
        active = wait_for(lambda: _settled_active(ctx),
                          time.monotonic() + 30)
        if active is None:
            return case.finish('failed', 'no peer reports role=active')
        if active not in ('active', 'standby'):
            return case.finish('inconclusive', 'the settled field '
                               'owner is not a pair peer the foreign '
                               'launch can stand by on: ' + str(active))
        peer = 'standby' if active == 'active' else 'active'
        base, peer_base = ctx[active], ctx[peer]
        expected = {active: 'active', peer: 'standby'}
        case.observe('field owner: ' + active + ' (' + base + ')')

        # The audit positions the doomed startup must leave untouched:
        # the incumbent's role and advancing tick, its receipted
        # command path, the standing claim's fencing verdict, and the
        # field output it keeps writing.
        _, signals = http_json('GET', base + '/signals')
        ref = save_evidence(ctx['evidence_dir'],
                            'doomed-startup-claim-signals.json', signals)
        case.evidence('file', ref, 'SignalIndex naming the '
                      'receipted-path target')
        target = _writable_bool_point(signals)
        if target is None:
            return case.finish('inconclusive',
                               'no writable bool point in the model — '
                               'the incumbent\'s receipted path cannot '
                               'be probed')
        point = target['point']
        census = _try_plant_ctl(ctx, 'list')
        if census is None:
            return case.finish('inconclusive', 'the simulated plant '
                               'did not answer its point census')
        points = (census or {}).get('points') or []
        field_out = sorted(entry['point'] for entry in points
                           if isinstance(entry, dict)
                           and entry.get('direction') == 'out')
        if not field_out:
            return case.finish('inconclusive', 'the simulated plant '
                               'serves no field output to watch')
        watch = min(field_out)
        probe0 = _try_plant(ctx, {'op': 'step', 'dt': 0})
        if probe0 is None:
            return case.finish('inconclusive', 'the simulated plant '
                               'did not answer a fencing probe')
        if not _fenced(probe0):
            return case.finish('failed', 'the field held no writer '
                               'claim before the doomed launch — a '
                               'third attachment\'s mutation probe '
                               'answered ' + json.dumps(probe0)[:300])
        role0 = _try_role(ctx, base)
        peer_role0 = _try_role(ctx, peer_base)
        snap0 = _try_snapshot(ctx, base)
        if snap0 is None:
            return case.finish('inconclusive', 'the incumbent never '
                               'served a snapshot for the baseline')
        tick0 = snap0.get('tick')
        sample0 = _probe_sample(ctx, watch)
        _, body = http_json('GET', base + '/receipts')
        receipts0 = [(r.get('command'), r.get('actor'))
                     for r in _receipt_list(body)]
        ref = save_evidence(ctx['evidence_dir'],
                            'doomed-startup-claim-before.json',
                            {'active': active, 'tick': tick0,
                             'incumbent': role0, 'partner': peer_role0,
                             'receipts': len(receipts0),
                             'probe': probe0, 'watch': watch,
                             'field': sample0})
        case.evidence('file', ref, 'the pre-launch audit positions')
        case.observe('baseline: incumbent tick ' + str(tick0) + ', '
                     + str(len(receipts0)) + ' receipts, field point '
                     + str(watch) + ' fenced under its claim')

        # The induction: the foreign peer's --journal-file gains a
        # first record its startup replay cannot read. The file lives
        # in the runner-owned state/journal directory the launch
        # bind-mounts, so the corrupt record is the file's line 1 when
        # the container replays it.
        journal_path = Path(journal)
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(DOOMED_CORRUPT_RECORD + '\n')
        case.observe('corrupt first record written to '
                     + str(journal_path))
        try:
            info = start(active)
        except Exception as exc:
            try:
                journal_path.unlink(missing_ok=True)
            except OSError:
                pass  # restore is best-effort; the launch never ran
            return case.finish('inconclusive', 'the foreign-peer '
                               'launch action never completed: '
                               + str(exc)[:300])
        container = str(info.get('container'))
        case.observe('foreign peer ' + container + ' launched onto '
                     'the corrupt journal file')

        command = {'command': {'write_value': {
            'point': point, 'kind': 'bool', 'value': {'bool': True}}},
            'actor': 'qa-lane'}

        def attempt():
            """Everything the case asserts across the doomed startup —
            the observation window over the pair's roles, the
            incumbent's tick and field writes, the fencing probes, the
            receipted command — and the post-attempt audit."""
            document = json.loads(Path(info['document']).read_text())
            ref = save_evidence(
                ctx['evidence_dir'],
                'doomed-startup-claim-induction.json',
                {'corrupt_record': DOOMED_CORRUPT_RECORD,
                 'document': document, 'container': container})
            case.evidence('file', ref, 'the corrupt first record and '
                          'the derived document the doomed launch ran')

            # The observation window: the startup's abort lands inside
            # the first polls — a startup that claimed the field on
            # its way out would show here as the incumbent demoted by
            # the dead claim, its fenced writes, or the field going
            # unclaimed.
            window = []
            last_tick = tick0
            field_tick = (sample0 or {}).get('tick')
            served = None
            violation = None
            unanswered = 0
            probes = 0
            reads = 0
            submission = None
            for round_no in range(DOOMED_STARTUP_ROUNDS):
                foreign_report = _try_role(ctx, foreign)
                if foreign_report is not None and served is None:
                    served = foreign_report
                incumbent = _try_role(ctx, base)
                partner = _try_role(ctx, peer_base)
                snap = _try_snapshot(ctx, base)
                probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
                sample = _probe_sample(ctx, watch)
                tick = (snap or {}).get('tick')
                ftick = (sample or {}).get('tick')
                window.append({'round': round_no, 'incumbent': incumbent,
                               'partner': partner,
                               'foreign': foreign_report, 'tick': tick,
                               'probe': probe, 'field_tick': ftick})
                # The first violation wins the detail — the root
                # cause, not the cascade of legs it tripped.
                if incumbent is None:
                    unanswered += 1
                elif violation is None \
                        and incumbent.get('role') != expected[active]:
                    violation = ('the incumbent left role=active — the '
                                 'doomed startup disturbed the field '
                                 'owner: ' + json.dumps(incumbent)[:300])
                if violation is None and partner is not None \
                        and partner.get('role') != expected[peer]:
                    violation = ('the tracking peer reported a '
                                 'spurious role change: '
                                 + json.dumps(partner)[:300])
                if probe is not None:
                    probes += 1
                    if violation is None:
                        kind = _probe_error(probe)
                        if kind == 'unclaimed':
                            violation = ('the field entered the '
                                         'unclaimed window — no '
                                         'writer claim stands: '
                                         + json.dumps(probe)[:300])
                        elif kind != 'fenced':
                            violation = ('the field answered a third '
                                         'attachment\'s mutation '
                                         'probe without the standing '
                                         'claim\'s fencing: '
                                         + json.dumps(probe)[:300])
                if tick is not None:
                    if violation is None and last_tick is not None \
                            and tick <= last_tick:
                        violation = ('the incumbent\'s tick stalled at '
                                     + str(tick))
                    last_tick = tick
                if sample is not None:
                    reads += 1
                if ftick is not None:
                    if violation is None and field_tick is not None \
                            and ftick <= field_tick:
                        violation = ('the field stopped receiving the '
                                     'incumbent\'s writes at tick '
                                     + str(ftick))
                    field_tick = ftick
                # The receipted-path probe: one write_value submitted
                # to the incumbent inside the window must be admitted
                # and answered — a dropped connection retries next
                # round, a refusal is the violation.
                if submission is None:
                    try:
                        status, receipt = http_json(
                            'POST', base + '/command', command)
                        submission = {'status': status,
                                      'receipt': receipt}
                        outcome = (receipt or {}).get('outcome') or {}
                        if violation is None and (
                                status != 200 or 'rejected' in outcome):
                            violation = ('the incumbent refused the '
                                         'mid-window command: '
                                         + str(status) + ' '
                                         + json.dumps(receipt)[:300])
                    except urllib.error.HTTPError as exc:
                        body = exc.read()
                        try:
                            receipt = json.loads(body or b'null')
                        except ValueError:
                            receipt = None
                        finally:
                            exc.close()
                        submission = {'status': exc.code,
                                      'receipt': receipt}
                        if violation is None:
                            violation = ('the incumbent refused the '
                                         'mid-window command: HTTP '
                                         + str(exc.code) + ' '
                                         + json.dumps(receipt)[:300])
                    except Exception:
                        pass  # one dropped poll — retried next round
                if violation or served is not None:
                    break
                time.sleep(DOOMED_STARTUP_POLL)
            ref = save_evidence(ctx['evidence_dir'],
                                'doomed-startup-claim-window.json',
                                {'watch': watch, 'command': command,
                                 'submission': submission,
                                 'window': window})
            case.evidence('file', ref, 'the observation window: pair '
                          'roles, incumbent ticks, fencing probes, and '
                          'the field\'s own writes')

            # The post-attempt audit: the incumbent's receipt log must
            # carry the mid-window command once more than the baseline
            # did, the doomed peer's journal file must still hold
            # exactly the corrupt record — the replay failed before
            # this run's boundary could append — and its state file
            # must never have appeared.
            try:
                _, body = http_json('GET', base + '/receipts')
                receipts1 = [(r.get('command'), r.get('actor'))
                             for r in _receipt_list(body)]
            except Exception:
                receipts1 = None
            key = (command['command'], 'qa-lane')
            landed = receipts1 is not None \
                and receipts1.count(key) > receipts0.count(key)
            journal_after = (journal_path.read_text()
                             if journal_path.is_file() else None)
            state_written = (Path(state_file).is_file()
                             if state_file else None)
            snap1 = _try_snapshot(ctx, base)
            probe1 = _try_plant(ctx, {'op': 'step', 'dt': 0})
            ref = save_evidence(
                ctx['evidence_dir'],
                'doomed-startup-claim-after.json',
                {'tick': (snap1 or {}).get('tick'),
                 'receipts': len(receipts1)
                 if receipts1 is not None else None,
                 'command_landed': landed,
                 'probe': probe1, 'journal': journal_after,
                 'state_file': state_written})
            case.evidence('file', ref, 'the post-attempt audit '
                          'positions — the receipt log, the fencing '
                          'probe, and the doomed peer\'s files')

            corrupt_line = DOOMED_CORRUPT_RECORD + '\n'
            if served is not None:
                if journal_after is not None \
                        and journal_after.startswith(corrupt_line):
                    return case.finish('failed', 'the doomed startup '
                                       'served its monitor — the '
                                       'corrupt first record did not '
                                       'fail its journal replay: '
                                       + json.dumps(served)[:300])
                return case.finish('inconclusive', 'the foreign peer '
                                   'served its monitor — the corrupt '
                                   'record never reached the journal '
                                   'file it replayed: '
                                   + str(journal_after)[:300])
            if violation:
                return case.finish('failed', violation)
            if unanswered >= len(window):
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered during the window')
            if probes == 0:
                return case.finish('inconclusive', 'the plant never '
                                   'answered a fencing probe')
            if reads == 0:
                return case.finish('inconclusive', 'the field never '
                                   'answered a read during the window')
            if submission is None:
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered a command '
                                   'submission during the window')
            if journal_after is None:
                return case.finish('inconclusive', 'the foreign '
                                   'journal file vanished mid-attempt')
            if journal_after != corrupt_line:
                return case.finish('failed', 'the doomed startup ran '
                                   'past the failed replay — its '
                                   'journal file gained records: '
                                   + journal_after[:300])
            if state_written:
                return case.finish('failed', 'the doomed startup '
                                   'persisted a checkpoint — it ran '
                                   'past the failed journal replay')
            if receipts1 is None:
                return case.finish('inconclusive', 'the incumbent '
                                   'never answered the post-attempt '
                                   'receipt audit')
            if not landed:
                return case.finish('failed', 'the incumbent\'s '
                                   'receipt log never carried the '
                                   'mid-window command')
            if not _fenced(probe1):
                return case.finish('failed', 'the field\'s fencing '
                                   'changed across the attempt — a '
                                   'third attachment\'s probe answered '
                                   + json.dumps(probe1)[:300])
            case.observe('the incumbent held role=active across '
                         + str(len(window)) + ' polls — tick '
                         + str(tick0) + ' -> '
                         + str((snap1 or {}).get('tick')) + ', '
                         + str(probes) + ' probes fenced, the '
                         'mid-window command receipted')
            return case.finish('passed')

        try:
            record = attempt()
        except Exception as exc:
            record = case.finish('inconclusive', str(exc))
        # Teardown is unconditional once the peer is up: the foreign
        # seat must be clean for later cases, and the induction
        # artifact comes back out with it — the seat returns to the
        # never-written state the launch found.
        try:
            stop()
            case.observe('foreign peer ' + container + ' removed — '
                         'the seat is clean for later cases')
        except Exception as exc:
            case.observe('the foreign peer teardown failed: '
                         + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the foreign peer was never '
                                    'removed: ' + str(exc)[:300])
        try:
            journal_path.unlink(missing_ok=True)
        except OSError as exc:
            case.observe('the corrupt journal record could not be '
                         'removed: ' + str(exc)[:200])
            if record['outcome'] == 'passed':
                record['outcome'] = 'inconclusive'
                record['detail'] = ('the induction record was left in '
                                    'the foreign journal file: '
                                    + str(exc)[:300])
        # The clean-rig leg: the pair still serves its settled roles
        # and the field still fences third-party mutation under the
        # incumbent's standing claim.
        if record['outcome'] == 'passed':
            try:
                roles = {name: _try_role(ctx, ctx[name])
                         for name in (active, peer)}
                probe = _try_plant(ctx, {'op': 'step', 'dt': 0})
                ref = save_evidence(
                    ctx['evidence_dir'],
                    'doomed-startup-claim-teardown.json',
                    {'roles': roles, 'probe': probe})
                case.evidence('file', ref, 'the rig after teardown')
                if (roles.get(active) or {}).get('role') != 'active' \
                        or (roles.get(peer) or {}).get('role') \
                        != 'standby':
                    record = case.finish('failed', 'the pair did not '
                                         'return to its settled '
                                         'roles: '
                                         + json.dumps(roles)[:300])
                elif not _fenced(probe):
                    record = case.finish('failed', 'the field no '
                                         'longer fences under the '
                                         'incumbent\'s claim after '
                                         'teardown: '
                                         + json.dumps(probe)[:300])
                else:
                    case.observe('the rig returned clean — settled '
                                 'roles, the standing claim fencing')
            except Exception as exc:
                record = case.finish('inconclusive', 'the '
                                     'post-teardown rig could not be '
                                     'verified: ' + str(exc)[:300])
        return record
    except Exception as exc:
        return case.finish('inconclusive', str(exc))
