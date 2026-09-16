"""Fix-verification runs: replay a finding's original reproduction on a
revision proven to contain its merged fix.

Queue input is <state_dir>/verifications.json, pushed by the WSL relay
from the supervisor's pending_verifications() (finding key, original
case identity, merged fix SHA, reproduction, expected outcome).

A verification run is a dedicated run kind (run ids 'qav-*'). Before
any build it proves the tested revision CONTAINS the fix — `git
merge-base --is-ancestor` inside the lane's bare mirror (git_dir,
populated by the relay) — then replays exactly the original case and
records the verdict, the ancestry check, and the evidence in the
report's verifications channel (schema v2). Pending items dispatch
ahead of the newest-SHA assessment: a merged fix is re-verified before
the lane spends a run on unrelated exploration.

Dedup is per (finding_key, fix_sha, tested_sha): an emitted verdict is
never re-reported for the same pair; an inconclusive one retries
bounded by max_attempts_per_sha; and a fix whose ancestry cannot be
checked yet (mirror or objects absent) simply waits for the next
cycle — no run, no report, and nothing advanced on an unproven
revision.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import report as qa_report
from . import runner, scenarios

QUEUE_NAME = 'verifications.json'
QUEUE_SCHEMA = 'qa-verifications/1'
RUN_PREFIX = 'qav'
REPORTED_KEY = 'verification:reported'
SPEC_PREFIX = 'verification:spec:'
PINS_KEY = 'verification:pins'

KEY = qa_report.KEY
SHA = qa_report.GIT_SHA


def load_queue(cfg, path=None):
    """Read and validate the pending-verification queue document.

    A missing or malformed queue yields no work — the lane never
    invents verification items.
    """
    path = Path(path) if path else Path(cfg['state_dir']) / QUEUE_NAME
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(doc, dict) or doc.get('schema') != QUEUE_SCHEMA:
        return []
    items = doc.get('items')
    if not isinstance(items, list):
        return []
    valid = []
    for item in items:
        try:
            valid.append(validate_item(item))
        except ValueError:
            continue
    return valid


def validate_item(item):
    if not isinstance(item, dict):
        raise ValueError('verification item must be an object')
    if not {'finding_key', 'case', 'fix_sha'} <= set(item):
        raise ValueError('verification item missing fields')
    for field in ('finding_key', 'case'):
        if not isinstance(item[field], str) or not KEY.match(item[field]):
            raise ValueError('invalid verification ' + field)
    if not isinstance(item['fix_sha'], str) or not SHA.match(item['fix_sha']):
        raise ValueError('invalid verification fix_sha')
    return item


def reported(st):
    return st.get(REPORTED_KEY, {})


def _mark_reported(st, key, fix_sha, tested_sha, run_id, outcome):
    marks = reported(st)
    prev = marks.get(key)
    attempts = (prev.get('attempts', 0) + 1
                if prev and prev.get('fix_sha') == fix_sha
                and prev.get('tested_sha') == tested_sha else 1)
    marks[key] = {'fix_sha': fix_sha, 'tested_sha': tested_sha,
                  'run_id': run_id, 'outcome': outcome,
                  'attempts': attempts}
    st.set(REPORTED_KEY, marks)


def fix_ancestry(git_dir, fix_sha, tested_sha, runner_=None):
    """Prove the tested revision contains the fix via git merge-base.

    Returns {'checked': bool, 'contained': bool|None, 'method', 'detail'}.
    'contained' None means the check could not run (mirror or objects
    missing) — a verification never advances on an unproven revision.
    """
    runner_ = runner_ or subprocess.run
    method = 'git merge-base --is-ancestor'
    git_dir = Path(git_dir)
    if not git_dir.is_dir():
        return {'checked': False, 'contained': None, 'method': method,
                'detail': 'lane git mirror absent: ' + str(git_dir)}
    for sha in (fix_sha, tested_sha):
        res = runner_(['git', '-C', str(git_dir), 'cat-file', '-e', sha],
                      capture_output=True, timeout=30)
        if res.returncode != 0:
            return {'checked': False, 'contained': None, 'method': method,
                    'detail': 'git object missing: ' + sha[:12]}
    res = runner_(['git', '-C', str(git_dir), 'merge-base',
                   '--is-ancestor', fix_sha, tested_sha],
                  capture_output=True, timeout=30)
    if res.returncode == 0:
        return {'checked': True, 'contained': True, 'method': method,
                'detail': fix_sha[:12] + ' is an ancestor of '
                + tested_sha[:12]}
    if res.returncode == 1:
        return {'checked': True, 'contained': False, 'method': method,
                'detail': fix_sha[:12] + ' is NOT an ancestor of '
                + tested_sha[:12]}
    return {'checked': False, 'contained': None, 'method': method,
            'detail': 'merge-base failed: '
            + str(getattr(res, 'stderr', ''))[:200]}


def case_function(case_key):
    """Map a stored case identity back to its scenario function."""
    name = 'scenario_' + case_key.replace('-', '_')
    for fn in scenarios.SCENARIOS:
        if fn.__name__ == name:
            return fn
    return None


def _target_sha(st):
    """The revision a verification run tests: the newest revision the
    lane would assess anyway — the queued one, else the last
    attempted."""
    queued = st.next_queued()
    if queued is not None:
        return queued['attempted_sha']
    return st.last_attempted_sha()


def sync_preserves(st, cfg, log=print):
    """Reconcile retention pins with the live verification queue.

    While a finding still sits in the supervisor's pending queue, the
    evidence that proves its verdict stays pinned against reclaim(): the
    qav run dirs (the relay may not have pulled the report yet, and a
    retry may need the case evidence) and the tested revision's source
    tree/images. Once the item leaves the queue — the verdict was
    certified or the finding moved on — the pins release and ordinary
    retention applies. Only pins this lane took (recorded under
    PINS_KEY) are ever released; manual `qa_lane preserve` pins are
    never touched.
    """
    pending = {item['finding_key'] for item in load_queue(cfg)}
    marks = reported(st)
    want_runs, want_shas = set(), set()
    if pending:
        # The next dispatched run tests the current target; keep its
        # source/images alive even before the run record exists.
        target = _target_sha(st)
        if target:
            want_shas.add(target)
    for key in pending:
        mark = marks.get(key)
        if mark and mark.get('tested_sha'):
            want_shas.add(mark['tested_sha'])
    for record in st.runs():
        run_id = record['run_id']
        if not run_id.startswith(RUN_PREFIX + '-'):
            continue
        spec = st.get(SPEC_PREFIX + run_id) or {}
        item = spec.get('item') or {}
        if item.get('finding_key') not in pending:
            continue
        want_runs.add(run_id)
        if record.get('attempted_sha'):
            want_shas.add(record['attempted_sha'])
    prev = st.get(PINS_KEY, {'runs': [], 'shas': []})
    for run_id in want_runs - set(prev['runs']):
        st.set_preserve('run', run_id, True)
    for sha in want_shas - set(prev['shas']):
        st.set_preserve('sha', sha, True)
    for run_id in set(prev['runs']) - want_runs:
        st.set_preserve('run', run_id, False)
    for sha in set(prev['shas']) - want_shas:
        st.set_preserve('sha', sha, False)
    now = {'runs': sorted(want_runs), 'shas': sorted(want_shas)}
    if now != prev:
        st.set(PINS_KEY, now)
        log('verification preserves: %d run(s), %d sha(s) pinned'
            % (len(want_runs), len(want_shas)))


def _teardown(run_id, timeline):
    """Remove this run's labeled containers and network — kept local so
    the verification kind stays independent of runner teardown
    internals. A failed docker listing raises so the caller records the
    cleanup failure rather than silently leaving labeled leftovers."""
    containers_ok, containers = runner._managed_containers()
    networks_ok, networks = runner._managed_networks()
    if not containers_ok or not networks_ok:
        raise RuntimeError('docker listing failed during verification '
                           'teardown')
    for cid, label_run in containers:
        if label_run == run_id:
            runner.docker('rm', '-f', cid, check=False)
    for nid, label_run in networks:
        if label_run == run_id:
            runner.docker('network', 'rm', nid, check=False)
    timeline('teardown', 'verification run containers and network removed')


def next_run(st, cfg, now, log=print):
    """Queue the next due fix-verification run; returns its record.

    Runs ahead of the queued newest-SHA assessment whenever a pending
    verification can be resolved: the source archive for the target
    revision must be staged, and the ancestry check must produce a
    definite verdict — a mirror that lacks the objects leaves the item
    pending for the next cycle without spending a run.
    """
    items = load_queue(cfg)
    if not items:
        return None
    target = _target_sha(st)
    if not target:
        return None
    if not (Path(cfg['src_dir']) / (target + '.tar')).is_file() \
            and not (Path(cfg['src_dir']) / target).is_dir():
        log('verification: source for %s not staged yet; waiting'
            % target[:12])
        return None
    marks = reported(st)
    for item in items:
        key = item['finding_key']
        mark = marks.get(key)
        if mark and mark.get('fix_sha') == item['fix_sha'] \
                and mark.get('tested_sha') == target \
                and (mark.get('outcome') != 'inconclusive'
                     or mark.get('attempts', 1)
                     >= cfg['max_attempts_per_sha']):
            continue  # verdict already reported for this pair
        ancestry = fix_ancestry(cfg['git_dir'], item['fix_sha'], target)
        if ancestry['contained'] is None:
            log('verification %s: ancestry unverifiable (%s); waiting'
                % (key, ancestry['detail']))
            continue
        run_id = runner._new_run_id(st, now, RUN_PREFIX)
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        st.queue_verification(run_id, target, now, day)
        st.set(SPEC_PREFIX + run_id,
               {'item': item, 'ancestry': ancestry, 'tested_sha': target})
        log('verification %s: queued %s against %s (fix %s contained=%s)'
            % (key, run_id, target[:12], item['fix_sha'][:12],
               ancestry['contained']))
        return st.run(run_id)
    return None


def _evidence_for(case_record, run_id, case_key):
    evidence = [{'detail': e.get('detail') or e['ref'],
                 'source': 'run %s case %s evidence %s'
                 % (run_id, case_key, e.get('ref', ''))}
                for e in case_record.get('evidence') or []]
    evidence += [{'detail': o, 'source': 'run ' + run_id}
                 for o in case_record.get('observations') or []]
    return evidence


def run(st, record, cfg, log=print):
    """Execute one fix-verification run.

    The ancestry gate runs before any build: a tested revision that does
    not contain the fix produces a 'blocked' report naming the failed
    check — the reproduction is never run on a non-containing revision.
    A containing revision replays exactly the original case; the report
    carries the verdict, the ancestry record, and the case evidence in
    its verifications channel.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    st.begin(run_id, os.getpid(), time.time())
    record = st.run(run_id)
    spec = st.get(SPEC_PREFIX + run_id) or {}
    item = spec.get('item') or {}
    ancestry = spec.get('ancestry') or {}
    key, case_key = item.get('finding_key'), item.get('case')
    state_dir = Path(cfg['state_dir'])
    run_dir = state_dir / 'runs' / run_id
    evidence_dir = run_dir / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    timeline, events = runner._timeline_writer(run_dir)
    timeline('verification-start',
             'finding %s case %s fix %s on %s'
             % (key, case_key, (item.get('fix_sha') or '')[:12],
                sha[:12]))
    deadline = time.monotonic() + cfg['hard_timeout_seconds']
    results, images, infra = [], None, []

    def entry(outcome, detail=None, evidence=None):
        record_ = {'finding_key': key, 'case': case_key,
                   'fix_sha': item.get('fix_sha'), 'tested_sha': sha,
                   'outcome': outcome, 'fix_ancestry': ancestry}
        if detail:
            record_['detail'] = detail
        if evidence:
            record_['evidence'] = evidence
        return record_

    def blocked(detail):
        timeline('verification-blocked', detail)
        runner._persist_report(
            st, record, cfg, 'blocked', None, None, [],
            [{'key': 'fix-verification', 'detail': detail,
              'phase': 'preflight'}],
            events, log,
            verifications=[entry('inconclusive', detail,
                                 [{'detail': detail}])])

    try:
        if ancestry.get('contained') is not True:
            detail = ('tested revision %s does not contain fix %s; '
                      'reproduction not run'
                      % (sha[:12], (item.get('fix_sha') or '')[:12])
                      if ancestry.get('contained') is False else
                      'fix ancestry unverifiable: '
                      + str(ancestry.get('detail')))
            blocked(detail)
            _mark_reported(st, key, item['fix_sha'], sha, run_id,
                           'not-contained'
                           if ancestry.get('contained') is False
                           else 'inconclusive')
            return
        if runner._free_bytes(state_dir) < cfg['min_free_bytes']:
            blocked('insufficient free space under ' + str(state_dir))
            _mark_reported(st, key, item['fix_sha'], sha, run_id,
                           'inconclusive')
            return
        # Same storage bound as assessment runs: a verification run is
        # still a run, and its evidence consumes the lane footprint.
        try:
            usage = runner.qa_storage_usage(cfg)
        except Exception as exc:
            blocked('cannot measure the lane footprint: '
                    + str(exc)[:300])
            _mark_reported(st, key, item['fix_sha'], sha, run_id,
                           'inconclusive')
            return
        if usage['total'] + cfg['run_headroom_bytes']                 > cfg['qa_storage_max_bytes']:
            blocked('lane footprint ' + str(usage['total'])
                    + ' + headroom ' + str(cfg['run_headroom_bytes'])
                    + ' exceeds ' + str(cfg['qa_storage_max_bytes']))
            _mark_reported(st, key, item['fix_sha'], sha, run_id,
                           'inconclusive')
            return
        src_tar = Path(cfg['src_dir']) / (sha + '.tar')
        src = Path(cfg['src_dir']) / sha
        if not src.is_dir():
            if not src_tar.is_file():
                blocked('source archive missing: ' + src_tar.name)
                _mark_reported(st, key, item['fix_sha'], sha, run_id,
                               'inconclusive')
                return
            src.mkdir(parents=True, exist_ok=True)
            subprocess.run(['tar', '-xf', str(src_tar), '-C', str(src)],
                           check=True, timeout=300)
            timeline('source-extracted', src_tar.name)
        images = runner._build_images(src, cfg, run_dir, timeline, run_id)
        runner._start_rig(cfg, record, src, run_dir, timeline)
        try:
            if not runner._wait_monitor(cfg, timeline):
                raise RuntimeError('monitors did not come up')
            ctx = runner._scenario_ctx(cfg, record, run_dir,
                                       evidence_dir, deadline, timeline)
            fn = case_function(case_key)
            if fn is None:
                result = scenarios.Case(
                    case_key, 'verification case ' + str(case_key),
                    item.get('expected') or 'see finding'
                    ).finish('inconclusive',
                             'no scenario implements case identity '
                             + str(case_key))
            else:
                result = fn(ctx)
                timeline('scenario-' + result['outcome'],
                         result['key'] + ': ' + result['title'])
            results = [result]
        finally:
            _teardown(run_id, timeline)
        verdict = (result['outcome'] if result['outcome']
                   in ('passed', 'failed') else 'inconclusive')
        evidence = _evidence_for(result, run_id, case_key)
        runner._persist_report(
            st, record, cfg,
            verdict if verdict in ('passed', 'failed') else 'inconclusive',
            sha if verdict in ('passed', 'failed') else None,
            images, results, infra, events, log,
            verifications=[entry(verdict, result.get('detail'), evidence)])
        _mark_reported(st, key, item['fix_sha'], sha, run_id, verdict)
        timeline('verification-finished', verdict)
        log('verification ' + run_id + ' (' + str(key) + '): ' + verdict)
    except Exception as exc:
        timeline('run-error', str(exc)[:500])
        infra.append({'key': 'run-error', 'detail': str(exc)[:500],
                      'phase': 'run'})
        try:
            runner._persist_report(
                st, record, cfg, 'inconclusive', None,
                images, results, infra, events, log,
                verifications=[entry('inconclusive', str(exc)[:500],
                                     [{'detail': str(exc)[:500]}])])
            _mark_reported(st, key, item.get('fix_sha'), sha, run_id,
                           'inconclusive')
        except Exception as rep_exc:
            st.interrupt(run_id, str(exc)[:500] + ' | report failed: '
                         + str(rep_exc)[:300], time.time())
            raise
        log('verification ' + run_id + ' inconclusive: ' + str(exc)[:300])
    finally:
        try:
            _teardown(run_id, timeline)
        except Exception as exc:
            log('teardown failed: ' + str(exc)[:300])
