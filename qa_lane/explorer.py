"""Charter-based exploratory QA runs driven by an ephemeral Devin session.

The deterministic lane (qa-*) answers "does this revision still satisfy the
checked-in scenarios". The exploratory lane (qax-*) answers a different
question: "what is weak in this revision that nobody wrote a scenario for".
A run builds the exact target revision, starts the same simulated rig, then
hands a time-bounded Devin session (swe-2-high, dangerous mode) a rendered
copy of qa_lane/explorer_prompt.md plus this run's context: source worktree,
monitor endpoints, UI URL, rig manifest, recent changes, open backlog,
pending fix verifications, and the exploration ledger. The agent chooses a
charter itself — there is no fixed scenario list — and reports through
{{RESULTS_DIR}}/agent-result.json, which this module validates and folds
into the normal versioned run report:

  results[]                -> report.scenarios (schema v3 explicit finding
                              fields: module, mode, reproduction, severity,
                              confidence, test_requirements, product_cause)
  capability_limitations[] -> report.capability_limitations
  infrastructure_failures[]-> report.infrastructure_failures
  verifications[]          -> report.verifications, with the fix ancestry
                              recomputed here against the lane's bare mirror
                              (the agent never proves ancestry itself)
  ledger                   -> the durable exploration ledger in state.db
                              that feeds the next run's novelty rule

Dispatch order in runner.cycle is verification > assessment > exploration:
pending fix verifications and new-SHA gates always preempt; exploration
fills the idle cycles on its own interval and daily cap. Findings route
through the unchanged WSL coordinator — the agent never sees GitHub.

The Devin session runs on the Lenovo host as the lane user (the checked-in
systemd unit) with its working directory inside the run dir: the rig's
monitor ports are published on host loopback only, so the agent needs host
loopback reachability plus outbound HTTPS to the Devin API. An ephemeral
agent container remains future hardening work — it would need its own
egress-bearing bridge and a route to the loopback-published ports.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from . import report as qa_report
from . import runner, verify

RUN_PREFIX = 'qax'
LEDGER_KEY = 'exploration:ledger'
RESULT_NAME = 'agent-result.json'
TEMPLATE = Path(__file__).resolve().parent / 'explorer_prompt.md'
MODE = 'simulation'

KEY = qa_report.KEY
SHA = qa_report.GIT_SHA

RESULT_LIMITS = {
    'results': qa_report.MAX_SCENARIOS,
    'capability_limitations': qa_report.MAX_LIMITATIONS,
    'infrastructure_failures': qa_report.MAX_INFRA_FAILURES,
    'verifications': qa_report.MAX_VERIFICATIONS,
    'timeline': qa_report.MAX_TIMELINE,
}

FORBIDDEN_ACTIONS = """\
- Do not use sudo or attempt privilege escalation of any kind.
- Do not edit, commit, or push product source; {{SOURCE_DIR}} is read-only
  and its git worktree is lane-owned.
- Do not modify the host QA supervisor, its state, this prompt, or the
  lane's Docker images; do not alter host networking, firewall rules,
  storage, mounts, or unrelated user files.
- Do not touch other services on this host (Immich, Nextcloud, Paperless,
  Caddy, Pi-hole, systemd units, cron) or their ports and data.
- Do not access GitHub or any credential store; you hold no GitHub
  credentials and must not acquire any.
- Never start a second EtherCAT master and never touch the dedicated
  hardware NIC; no physical I/O is commissioned on this host.
- Any Docker object you create must carry labels dcs-hwtest.managed=1 and
  dcs-hwtest.run={{RUN_ID}} and attach only to this run's labeled network;
  the lane reaps them at teardown. Do not remove or rename objects you did
  not create.
- Do not read or copy personal documents, browser profiles, ssh keys, or
  other secrets; never include them in results or evidence."""

RIG_MANIFEST = """\
Simulated rig (mode: simulation). One dcs-plant-server plus a redundant
dcs-controller pair (active + standby) on this run's dedicated Docker
bridge; all plant I/O flows through the dcs-sim-net remote simulation
socket. Approved actions: HTTP requests to the published monitor/command
endpoints, plant-side manipulation offered by the simulation API, restart
or stop of this run's labeled rig containers, and disposable probes under
the results directory. No physical channels exist: the Wago rig, EtherCAT
path, and feedback loop are not commissioned — never claim hardware
behavior from simulation evidence.

Known capability limits carried by the lane:
{{CAPABILITIES}}"""


def resolve_devin(cfg):
    """The Devin CLI to run, or None. The systemd unit's PATH lacks
    ~/.local/bin, so deployment config should pin the absolute path."""
    candidate = cfg.get('exploration_devin') or 'devin'
    if os.path.sep in candidate:
        return candidate if os.path.isfile(candidate) else None
    extra = str(Path.home() / '.local' / 'bin')
    return shutil.which(candidate, path=extra + os.pathsep
                        + os.environ.get('PATH', ''))


def ledger(st):
    entries = st.get(LEDGER_KEY, [])
    return entries if isinstance(entries, list) else []


def record_ledger(st, entry, keep):
    entries = ledger(st)
    entries.append(entry)
    st.set(LEDGER_KEY, entries[-keep:])


def last_started(st):
    return st.last_started(RUN_PREFIX)


def next_run(st, cfg, now, log=print):
    """Queue one exploration run against the newest gate-verdicted
    revision, or None. Exploration never preempts verification or
    assessment work; it fills idle cycles on its own interval."""
    if not cfg.get('exploration_enabled'):
        return None
    # A record stranded queued by a failed pre-dispatch gate (egress,
    # budget) is reused, not duplicated.
    existing = st.next_queued(RUN_PREFIX)
    if existing is not None:
        return existing
    day = time.strftime('%Y-%m-%d', time.gmtime(now))
    if st.started_today(day, RUN_PREFIX) >= cfg['max_explorations_per_day']:
        return None
    previous = last_started(st)
    interval = cfg['exploration_interval_seconds']
    if previous and now - previous < interval:
        return None
    target = st.latest_verdicted_sha()
    if not target:
        log('exploration: no gate-verdicted revision yet; waiting')
        return None
    # The staged source tar/extraction is retention-bounded; the lane's
    # bare mirror is authoritative — run() re-archives from it on demand.
    if not _commit_known(cfg, target):
        log('exploration: %s not in lane mirror; waiting' % target[:12])
        return None
    if resolve_devin(cfg) is None:
        log('exploration: devin CLI not found (%s); waiting'
            % (cfg.get('exploration_devin') or 'devin'))
        return None
    run_id = runner._new_run_id(st, now, RUN_PREFIX)
    st.queue_dedicated(run_id, target, now, day)
    log('exploration: queued %s against verdicted %s'
        % (run_id, target[:12]))
    return st.run(run_id)


def _commit_known(cfg, sha):
    """True when the lane's bare mirror contains the revision."""
    res = subprocess.run(
        ['git', '-C', cfg['git_dir'], 'cat-file', '-e',
         sha + '^{commit}'], capture_output=True, timeout=30)
    return res.returncode == 0


def _git(cfg, *args, timeout=60):
    """Bounded read against the lane's bare mirror; '' on any failure."""
    try:
        res = subprocess.run(
            ['git', '-C', cfg['git_dir'], *args],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ''
    return res.stdout if res.returncode == 0 else ''


def _changed_range(st, cfg, sha):
    """Commits between the previously verdicted revision and the target."""
    verdicted = [r['completed_sha'] for r in st.runs(('finished',))
                 if r['run_id'].startswith('qa-')
                 and r['outcome'] in ('passed', 'failed')
                 and r['completed_sha'] and r['completed_sha'] != sha]
    base = verdicted[-1] if verdicted else None
    if base:
        out = _git(cfg, 'log', '--oneline', '--no-decorate',
                   base + '..' + sha)
        if out.strip():
            return ('%s..%s\n%s' % (base[:12], sha[:12], out.strip()))[:8000]
    out = _git(cfg, 'log', '--oneline', '--no-decorate', '-20', sha)
    return ('last 20 commits on %s\n%s'
            % (sha[:12], out.strip()))[:8000] or 'unavailable'


def _recent_changes(cfg, sha):
    out = _git(cfg, 'log', '--oneline', '--no-decorate', '-25', sha)
    return out.strip()[:8000] or 'unavailable'


def _backlog(cfg):
    """The open-issue snapshot the WSL relay pushes (qa-backlog/1); the
    lane holds no GitHub credentials, so absence degrades to a note."""
    path = Path(cfg['state_dir']) / 'backlog.json'
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return ('unavailable on this host — the coordinator owns '
                'deduplication and roadmap fit')
    items = doc.get('items')
    if not isinstance(items, list):
        return 'unavailable (unrecognized backlog document)'
    lines = []
    for item in items[:150]:
        labels = ','.join(item.get('labels') or [])
        lines.append('#%s %s%s'
                     % (item.get('number'), item.get('title'),
                        ' [' + labels + ']' if labels else ''))
    return '\n'.join(lines) or 'empty'


def _pending_verifications(cfg):
    items = []
    for item in verify.load_queue(cfg):
        entry = dict(item)
        if entry.get('replay') == 'agent':
            entry['note'] = ('this reproduction is an exploratory case — '
                             're-run it yourself on the tested revision '
                             'and report the verdict in verifications[]')
        items.append(entry)
    return json.dumps(items, indent=1)[:8000] or '[]'


def _history(st):
    entries = ledger(st)[-10:]
    if not entries:
        return 'empty — this is among the first exploratory runs'
    lines = []
    for entry in entries:
        lines.append('- %s run %s on %s: charter %s — %s'
                     % (entry.get('t', '?'), entry.get('run_id', '?'),
                        str(entry.get('sha', ''))[:12],
                        entry.get('charter', '?'),
                        entry.get('note', '')))
        for nxt in entry.get('next') or []:
            lines.append('    next: ' + nxt)
    return '\n'.join(lines)[:8000]


def build_context(st, record, cfg, run_dir, workspace):
    """The {{VAR}} substitutions for explorer_prompt.md."""
    run_dir = Path(run_dir)
    sha = record['attempted_sha']
    caps = '\n'.join('- %s: %s' % (c.get('key'), c.get('detail'))
                     for c in cfg.get('capabilities') or []) or '- none'
    budget = cfg['exploration_time_budget_seconds']
    endpoints = {
        'active_monitor': 'http://127.0.0.1:%s' % cfg['active_port'],
        'standby_monitor': 'http://127.0.0.1:%s' % cfg['standby_port'],
        'plant_sim_socket': '127.0.0.1:%s (dcs-sim-net remote)'
        % cfg['plant_host_port'],
    }
    return {
        'RUN_ID': record['run_id'],
        'ATTEMPTED_SHA': sha,
        'CHANGED_RANGE': _changed_range(st, cfg, sha),
        'MODE': MODE,
        'TIME_BUDGET': '%d minutes (hard limit — the lane terminates the '
                       'session at the deadline; persist evidence '
                       'continuously)' % (budget // 60),
        'SOURCE_DIR': str(workspace / 'src'),
        'RESULTS_DIR': str(run_dir / 'results'),
        'ENDPOINTS': json.dumps(endpoints, indent=1)
        + '\n(monitor ports are published on host loopback only)',
        'UI_URL': 'http://127.0.0.1:%s/' % cfg['active_port'],
        'RIG_MANIFEST': RIG_MANIFEST.replace('{{CAPABILITIES}}', caps)
                      .replace('{{RUN_ID}}', record['run_id']),
        'RECENT_CHANGES': _recent_changes(cfg, sha),
        'OPEN_BACKLOG': _backlog(cfg),
        'PENDING_VERIFICATIONS': _pending_verifications(cfg),
        'EXPLORATION_HISTORY': _history(st),
        'FORBIDDEN_ACTIONS': FORBIDDEN_ACTIONS
                              .replace('{{SOURCE_DIR}}',
                                       str(workspace / 'src'))
                              .replace('{{RUN_ID}}', record['run_id']),
    }


def render_prompt(template_text, context):
    prompt = template_text
    for key, value in context.items():
        prompt = prompt.replace('{{' + key + '}}', str(value))
    prompt = re.sub(r'\{\{[A-Z_]+\}\}', '(not provided by the runner)',
                    prompt)
    return prompt


def _bounded(value, limit):
    return value if isinstance(value, str) and value.strip() \
        and len(value) <= limit else None


def _scenario(item, warnings):
    """Normalize one agent-reported case into a schema-v3 scenario."""
    if not isinstance(item, dict):
        warnings.append('results entry is not an object')
        return None
    key = item.get('key')
    if not isinstance(key, str) or not KEY.match(key):
        warnings.append('results entry dropped: invalid key')
        return None
    outcome = item.get('outcome')
    if outcome not in qa_report.CASE_OUTCOMES:
        warnings.append('%s dropped: invalid outcome' % key)
        return None
    record = {'key': key,
              'title': _bounded(item.get('title'), 200) or key,
              'expected': _bounded(item.get('expected'), 4000)
              or 'see exploration summary',
              'outcome': outcome,
              'observations': [o[:2000] for o in
                               (item.get('observations') or [])
                               if isinstance(o, str)][:40]}
    for field, limit in (('detail', 4000), ('reproduction', 8000),
                         ('test_requirements', 4000), ('module', 500)):
        if _bounded(item.get(field), limit) is not None:
            record[field] = item[field]
    if item.get('mode') in qa_report.RUN_MODES:
        record['mode'] = item['mode']
    if item.get('severity') in qa_report.SEVERITIES:
        record['severity'] = item['severity']
    if item.get('confidence') in qa_report.CONFIDENCES:
        record['confidence'] = item['confidence']
    if type(item.get('product_cause')) is bool:
        record['product_cause'] = item['product_cause']
    evidence = []
    for entry in (item.get('evidence') or [])[:qa_report.MAX_EVIDENCE]:
        if not isinstance(entry, dict):
            continue
        kind, ref = entry.get('kind'), entry.get('ref')
        if kind not in qa_report.EVIDENCE_KINDS or not _bounded(ref, 500):
            continue
        if kind == 'file' and not ref.startswith('results/'):
            ref = 'results/' + ref
        ev = {'kind': kind, 'ref': ref}
        if _bounded(entry.get('detail'), 2000):
            ev['detail'] = entry['detail']
        evidence.append(ev)
    if evidence:
        record['evidence'] = evidence
    return record


def _verification(item, record, cfg, warnings):
    """Normalize one agent replay verdict; the runner — never the agent —
    attaches the fix-ancestry proof against the lane mirror."""
    if not isinstance(item, dict):
        warnings.append('verifications entry is not an object')
        return None
    finding_key, case = item.get('finding_key'), item.get('case')
    fix_sha = item.get('fix_sha')
    if not (isinstance(finding_key, str) and KEY.match(finding_key)
            and isinstance(case, str) and KEY.match(case)
            and isinstance(fix_sha, str) and SHA.match(fix_sha)):
        warnings.append('verifications entry dropped: invalid identity')
        return None
    outcome = item.get('outcome')
    if outcome not in qa_report.VERIFICATION_OUTCOMES:
        warnings.append('%s verification dropped: invalid outcome'
                        % finding_key)
        return None
    tested = record['attempted_sha']
    entry = {'finding_key': finding_key, 'case': case, 'fix_sha': fix_sha,
             'tested_sha': tested, 'outcome': outcome,
             'fix_ancestry': verify.fix_ancestry(cfg['git_dir'], fix_sha,
                                                 tested)}
    if _bounded(item.get('detail'), 4000):
        entry['detail'] = item['detail']
    evidence = []
    for ev in (item.get('evidence') or [])[:qa_report.MAX_EVIDENCE]:
        if isinstance(ev, str) and ev.strip():
            evidence.append({'detail': ev[:2000]})
        elif isinstance(ev, dict) and _bounded(ev.get('detail'), 2000):
            entry_ev = {'detail': ev['detail']}
            if _bounded(ev.get('source'), 500):
                entry_ev['source'] = ev['source']
            evidence.append(entry_ev)
    if evidence:
        entry['evidence'] = evidence
    return entry


def _keyed(item, warnings, channel):
    if not isinstance(item, dict) or not KEY.match(str(item.get('key'))):
        warnings.append(channel + ' entry dropped: invalid key')
        return None
    detail = _bounded(item.get('detail'), 4000)
    if detail is None:
        warnings.append('%s dropped: missing detail' % item['key'])
        return None
    record = {'key': item['key'], 'detail': detail}
    if channel == 'capability_limitations' \
            and type(item.get('blocking')) is bool:
        record['blocking'] = item['blocking']
    if channel == 'infrastructure_failures':
        record['phase'] = _bounded(item.get('phase'), 200) or 'exploration'
    return record


def parse_result(run_dir, record, cfg, log=print):
    """Read and normalize the agent's result document.

    Returns (scenarios, capabilities, infra, verifications, exploration,
    ledger_entry, warnings). A missing or unparseable document raises
    ValueError; individually malformed entries are dropped and named in
    warnings so one bad record never sinks the run's evidence.
    """
    warnings = []
    path = Path(run_dir) / 'results' / RESULT_NAME
    try:
        doc = json.loads(path.read_text(errors='replace'))
    except (OSError, ValueError) as exc:
        raise ValueError('agent result unreadable: ' + str(exc)[:300])
    if not isinstance(doc, dict):
        raise ValueError('agent result is not an object')

    scenarios, seen = [], set()
    for item in (doc.get('results') or [])[:RESULT_LIMITS['results']]:
        entry = _scenario(item, warnings)
        if entry is None or entry['key'] in seen:
            if entry is not None:
                warnings.append('%s dropped: duplicate key' % entry['key'])
            continue
        seen.add(entry['key'])
        scenarios.append(entry)

    capabilities = [c for c in
                    (_keyed(i, warnings, 'capability_limitations')
                     for i in (doc.get('capability_limitations') or [])
                     [:RESULT_LIMITS['capability_limitations']])
                    if c is not None]
    infra = [c for c in
             (_keyed(i, warnings, 'infrastructure_failures')
              for i in (doc.get('infrastructure_failures') or [])
              [:RESULT_LIMITS['infrastructure_failures']])
             if c is not None]
    verifications = [v for v in
                     (_verification(i, record, cfg, warnings)
                      for i in (doc.get('verifications') or [])
                      [:RESULT_LIMITS['verifications']])
                     if v is not None]

    exploration = {}
    if _bounded(doc.get('charter'), 500):
        exploration['charter'] = doc['charter']
    if _bounded(doc.get('novelty_rationale'), 4000):
        exploration['rationale'] = doc['novelty_rationale']
    ledger_doc = doc.get('ledger') if isinstance(doc.get('ledger'), dict) \
        else {}
    frontiers = [f[:500] for f in (ledger_doc.get('next') or [])
                 if isinstance(f, str)][:10]
    if frontiers:
        exploration['next_frontiers'] = frontiers
    exploration['agent'] = 'devin/' + str(cfg.get('exploration_model'))

    ledger_entry = {'t': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    'run_id': record['run_id'],
                    'sha': record['attempted_sha'],
                    'charter': exploration.get('charter') or 'unrecorded',
                    'note': (_bounded(ledger_doc.get('note'), 2000) or ''),
                    'explored': [e[:500] for e in
                                 (ledger_doc.get('explored') or [])
                                 if isinstance(e, str)][:20],
                    'next': frontiers}
    return scenarios, capabilities, infra, verifications, exploration, \
        ledger_entry, warnings


def _spawn(command, cwd, log_path, env, timeout):
    """Run the agent to completion or deadline; returns
    ('completed'|'failed'|'timeout', exit_code, elapsed_seconds)."""
    started = time.monotonic()
    with open(log_path, 'ab', buffering=0) as log:
        child = subprocess.Popen(command, cwd=str(cwd), env=env,
                                 stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=log, start_new_session=True)
        while child.poll() is None:
            if time.monotonic() - started >= timeout:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
                return 'timeout', child.returncode, \
                    time.monotonic() - started
            time.sleep(1)
        code = child.returncode
    return ('completed' if code == 0 else 'failed'), code, \
        time.monotonic() - started


def _checkout_source(cfg, sha, workspace, timeline):
    """Give the agent a read-only, history-enabled source view: a detached
    worktree of the lane's bare mirror when possible, else a plain copy of
    the extracted archive. The mirror's worktree metadata is lane-owned
    and pruned after the run."""
    ws_src = Path(workspace) / 'src'
    git_dir = Path(cfg['git_dir'])
    if git_dir.is_dir():
        res = subprocess.run(
            ['git', '-C', str(git_dir), 'worktree', 'add', '--detach',
             str(ws_src), sha], capture_output=True, text=True,
            timeout=120)
        if res.returncode == 0:
            timeline('source-worktree', str(ws_src))
            return ws_src
        timeline('worktree-fallback', res.stderr.strip()[:200])
    src = Path(cfg['src_dir']) / sha
    shutil.copytree(src, ws_src)
    timeline('source-copied', str(src))
    return ws_src


def _drop_worktree(cfg, workspace):
    ws_src = Path(workspace) / 'src'
    git_dir = Path(cfg['git_dir'])
    if git_dir.is_dir() and (ws_src / '.git').exists():
        subprocess.run(['git', '-C', str(git_dir), 'worktree', 'remove',
                        '--force', str(ws_src)],
                       capture_output=True, timeout=60)
        subprocess.run(['git', '-C', str(git_dir), 'worktree', 'prune'],
                       capture_output=True, timeout=60)


def _session_env(cfg):
    env = dict(os.environ)
    devin = resolve_devin(cfg) or 'devin'
    env['PATH'] = str(Path(devin).parent) + os.pathsep \
        + env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    return env


def run(st, record, cfg, log=print, spawn=None):
    """Execute one exploratory run against record['attempted_sha'].

    Same skeleton as runner.run — preflight, extract, build, rig — but the
    scenario driver is replaced by a bounded Devin session whose result
    document is validated and folded into the report."""
    spawn = spawn or _spawn
    run_id, sha = record['run_id'], record['attempted_sha']
    st.begin(run_id, os.getpid(), time.time())
    record = st.run(run_id)
    state_dir = Path(cfg['state_dir'])
    run_dir = state_dir / 'runs' / run_id
    evidence_dir = run_dir / 'evidence'
    results_dir = run_dir / 'results'
    workspace = run_dir / 'workspace'
    for path in (evidence_dir, results_dir, workspace):
        path.mkdir(parents=True, exist_ok=True)
    timeline, events = runner._timeline_writer(run_dir)
    timeline('exploration-start', 'exploring ' + sha)
    deadline = time.monotonic() + cfg['hard_timeout_seconds']
    results, images, infra = [], None, []
    verifications, exploration, ledger_entry = None, None, None
    try:
        if runner._preflight_blocked(st, record, cfg, timeline, events, log):
            return
        src_tar = Path(cfg['src_dir']) / (sha + '.tar')
        src = Path(cfg['src_dir']) / sha
        if not src.is_dir():
            if not src_tar.is_file():
                # Assessment sources are retention-bounded; the mirror is
                # authoritative — re-archive the verdicted revision.
                res = subprocess.run(
                    ['git', '-C', cfg['git_dir'], 'archive',
                     '-o', str(src_tar), sha],
                    capture_output=True, text=True, timeout=300)
                if res.returncode != 0:
                    timeline('run-blocked', 'source archive missing')
                    runner._persist_report(
                        st, record, cfg, 'blocked', None, None, [],
                        [{'key': 'preflight-source',
                          'detail': ('source unavailable: ' + src_tar.name
                                     + ' and mirror archive failed: '
                                     + res.stderr.strip()[:200]),
                          'phase': 'preflight'}], events, log)
                    return
                timeline('source-archived', 'mirror -> ' + src_tar.name)
            src.mkdir(parents=True, exist_ok=True)
            subprocess.run(['tar', '-xf', str(src_tar), '-C', str(src)],
                           check=True, timeout=300)
            timeline('source-extracted', src_tar.name)
        images = runner._build_images(src, cfg, run_dir, timeline, run_id)
        runner._start_rig(cfg, record, src, run_dir, timeline)
        try:
            if not runner._wait_monitor(cfg, timeline):
                raise RuntimeError('monitors did not come up')
            ws_src = _checkout_source(cfg, sha, workspace, timeline)
            for root, dirs, files in os.walk(ws_src):
                for name in files:
                    try:
                        os.chmod(Path(root) / name, 0o444)
                    except OSError:
                        pass
            template = Path(cfg.get('exploration_prompt')
                            or TEMPLATE).read_text()
            context = build_context(st, record, cfg, run_dir, workspace)
            prompt_path = run_dir / 'prompt.txt'
            prompt_path.write_text(render_prompt(template, context))
            devin = resolve_devin(cfg)
            if devin is None:
                raise RuntimeError('devin CLI not found: '
                                   + str(cfg.get('exploration_devin')))
            command = [devin, '-p', '--model',
                       cfg.get('exploration_model') or 'swe-2-high',
                       '--permission-mode', 'dangerous',
                       '--respect-workspace-trust', 'false',
                       '--prompt-file', str(prompt_path),
                       '--export', str(run_dir / 'conversation.json')]
            budget = min(cfg['exploration_time_budget_seconds'],
                         max(300, deadline - time.monotonic() - 300))
            timeline('agent-start', 'budget %ds' % budget)
            status, code, elapsed = spawn(command, workspace,
                                          run_dir / 'devin.log',
                                          _session_env(cfg), budget)
            timeline('agent-finished',
                     '%s (exit %s) after %ds' % (status, code, elapsed))
            if status == 'timeout':
                infra.append({'key': 'agent-timeout',
                              'detail': 'exploratory session hit the %ds '
                              'budget and was terminated' % budget,
                              'phase': 'exploration'})
            try:
                scenarios, caps, agent_infra, vers, exploration, \
                    ledger_entry, warnings = parse_result(run_dir, record,
                                                          cfg, log)
            except ValueError as exc:
                # The session produced no valid result document: the run
                # still carries its raw evidence/summary/log, reported as
                # inconclusive with a named infrastructure failure.
                infra.append({'key': 'agent-result-invalid',
                              'detail': str(exc)[:3900],
                              'phase': 'exploration'})
                scenarios, caps, agent_infra, vers, warnings = \
                    [], [], [], None, []
                ledger_entry = None
            results = scenarios
            verifications = vers or None
            infra += agent_infra
            for warning in warnings:
                infra.append({'key': 'agent-result-field',
                              'detail': warning[:3900],
                              'phase': 'exploration'})
            # Agent-reported capability gaps merge into the report channel
            # the coordinator already routes to planning.
            agent_caps = [c for c in caps
                          if all(k['key'] != c['key']
                                 for k in (cfg.get('capabilities') or []))]
            cfg = dict(cfg)
            cfg['capabilities'] = (cfg.get('capabilities') or []) \
                + agent_caps
            for entry in parse_result_timeline(run_dir):
                events.append(entry)
            if ledger_entry:
                record_ledger(st, ledger_entry,
                              cfg.get('exploration_ledger_keep') or 20)
        finally:
            infra += runner._teardown_rig(run_id, timeline, st)
            _drop_worktree(cfg, workspace)
        outcome = ('failed' if any(r['outcome'] == 'failed' for r in results)
                   else 'passed' if results and all(
                       r['outcome'] == 'passed' for r in results)
                   else 'inconclusive')
        runner._persist_report(
            st, record, cfg, outcome,
            sha if outcome in ('passed', 'failed') else None,
            images, results, infra, events, log,
            verifications=verifications, mode=MODE,
            exploration=exploration)
        timeline('run-finished', outcome)
        log('exploration ' + run_id + ': ' + outcome)
    except Exception as exc:
        timeline('run-error', str(exc)[:500])
        infra.append({'key': 'run-error', 'detail': str(exc)[:500],
                      'phase': 'run'})
        try:
            runner._persist_report(
                st, record, cfg, 'inconclusive', None, images, results,
                infra, events, log, verifications=verifications,
                mode=MODE, exploration=exploration)
        except Exception as rep_exc:
            st.interrupt(run_id, str(exc)[:500] + ' | report failed: '
                         + str(rep_exc)[:300], time.time())
            raise
        log('exploration ' + run_id + ' inconclusive: ' + str(exc)[:300])
    finally:
        try:
            leftover = runner._teardown_rig(run_id, timeline, st)
            if leftover:
                log('teardown reported ' + str(len(leftover))
                    + ' failure(s); recorded in the cleanup ledger')
        except Exception as exc:
            log('teardown failed: ' + str(exc)[:300])


def parse_result_timeline(run_dir):
    """Agent-supplied timeline entries, normalized and bounded."""
    try:
        doc = json.loads(
            (Path(run_dir) / 'results' / RESULT_NAME)
            .read_text(errors='replace'))
    except (OSError, ValueError):
        return []
    events = []
    for entry in (doc.get('timeline') or [])[:RESULT_LIMITS['timeline']]:
        if not isinstance(entry, dict):
            continue
        t, event = entry.get('t'), entry.get('event')
        if not (isinstance(t, str) and isinstance(event, str)):
            continue
        row = {'t': t[:40], 'event': 'agent:' + event[:190]}
        if isinstance(entry.get('detail'), str):
            row['detail'] = entry['detail'][:2000]
        events.append(row)
    return events
