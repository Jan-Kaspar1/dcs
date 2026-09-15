"""Lenovo QA run orchestration: reconcile, budget, build, rig, report.

One `cycle` call reconciles state left by dead processes, then starts the
newest queued revision if the daily budget allows. A run extracts the
pinned source archive the WSL dispatcher pushed, builds the controller
and plant images under resource limits, starts the simulated rig on a
dedicated labeled bridge, runs the deterministic scenario set, validates
and persists the report, and always tears its containers down.

Layout on the Lenovo host:
  /srv/homelab/dcs-hwtest/   deployment config: qa_lane/ code, config.json
  /srv/dcs-hwtest/           bounded NVMe run storage:
    state.db                 durable run records (qa_lane.state)
    lock                     cycle flock
    src/<sha>.tar            pinned source archives pushed from WSL
    src/<sha>/               extracted build context
    runs/<run_id>/           report.json, evidence/, timeline.jsonl, logs
    reports/<run_id>.json    completed reports staged for the WSL relay

Container/image contract mirrors docs/packaging.md: the checked-in
Dockerfiles' build stage (rust:1.98.1-bookworm, cargo build --release
--locked) runs inside a cpuset/memory-limited builder container — plain
`docker build` cannot bound BuildKit compile resources — and a generated
runtime stage keeps the debian:bookworm-slim + uid 10001 + entrypoint
contract. CI image pinning does not exist yet; this is the documented
build choice until it does.
"""
import fcntl
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import report as qa_report
from . import scenarios, state as qa_state

MANAGED_LABEL = 'dcs-hwtest.managed'
RUN_LABEL = 'dcs-hwtest.run'

DEFAULT_CONFIG = {
    'state_dir': '/srv/dcs-hwtest',
    'src_dir': '/srv/dcs-hwtest/src',
    'max_runs_per_day': 4,
    'max_attempts_per_sha': 2,
    'hard_timeout_seconds': 7200,
    'min_free_bytes': 10 * 1024 ** 3,
    'active_port': 18080,
    'standby_port': 18081,
    'plant_port': 9001,
    'plant_host_port': 19001,
    'rig_cpus': '1.0',
    'rig_memory': '384m',
    'rig_pids': 128,
    'builder_image': 'rust:1.98.1-bookworm',
    'builder_cpus': '0-3',
    'builder_memory': '6g',
    'builder_pids': 512,
    'builder_timeout': 5400,
    'monitor_timeout': 90,
    'model_fixture': 'crates/dcs-demo/fixtures/pump_station.json',
    'dynamics_fixture': 'crates/dcs-demo/fixtures/pump_station_dynamics.json',
    'capabilities': [
        {'key': 'no-ethercat',
         'detail': 'No EtherCAT driver in this revision; all field I/O '
                   'exercised through the dcs-sim-net remote simulation.',
         'blocking': False},
    ],
}


def load_config(path=None):
    cfg = dict(DEFAULT_CONFIG)
    path = path or os.environ.get(
        'QA_LANE_CONFIG', '/srv/homelab/dcs-hwtest/config.json')
    if Path(path).is_file():
        cfg.update(json.loads(Path(path).read_text()))
    return cfg


def docker(*args, timeout=120, check=True):
    result = subprocess.run(['docker', *args], capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError('docker ' + args[0] + ' failed: '
                           + result.stderr.strip()[:500])
    return result


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _utcnow()).isoformat(timespec='seconds')


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        cmdline = Path('/proc', str(pid), 'cmdline').read_bytes()
    except OSError:
        return True
    return b'qa_lane' in cmdline or b'qa-lane' in cmdline


def _managed_containers():
    # NB: `-q` makes docker ignore --format, so use plain `ps -a`.
    result = docker('ps', '-a', '--filter', 'label=' + MANAGED_LABEL + '=1',
                    '--format', '{{.ID}} {{.Label "' + RUN_LABEL + '"}}',
                    check=False)
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def _managed_networks():
    result = docker('network', 'ls', '--filter',
                    'label=' + MANAGED_LABEL + '=1', '--format',
                    '{{.ID}} {{.Label "' + RUN_LABEL + '"}}', check=False)
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def reconcile(st, cfg, log=print):
    """Reap runs whose runner died and remove their leftover containers.

    Runs at every cycle start so a killed runner — timeout, SIGKILL, or
    reboot — never leaves an orphaned rig or a record stuck 'running'.
    A dead run still gets a report built from its persisted timeline so
    the interruption is visible as evidence.
    """
    live = {r['run_id'] for r in st.runs(qa_state.ACTIVE_STATUSES)
            if _pid_alive(r['pid'])}
    for record in st.runs(qa_state.ACTIVE_STATUSES):
        if record['run_id'] not in live:
            st.interrupt(record['run_id'],
                         'runner pid ' + str(record['pid'])
                         + ' gone; reconciled', time.time())
            log('reconcile: marked ' + record['run_id'] + ' interrupted')
            _write_interrupted_report(st, record, cfg, log)
    for cid, run_id in _managed_containers():
        if run_id not in live:
            docker('rm', '-f', cid, check=False)
            log('reconcile: removed orphaned container ' + cid)
    for nid, run_id in _managed_networks():
        if run_id not in live:
            docker('network', 'rm', nid, check=False)
            log('reconcile: removed orphaned network ' + nid)


def _maybe_retry(st, cfg, now, day):
    """Queue one automatic retry for an inconclusive finished run."""
    finished = st.runs(('finished',))
    if not finished:
        return
    last = finished[-1]
    if last['outcome'] != 'inconclusive':
        return
    if st.next_queued() is not None:
        return
    attempts = st.attempts_for(last['attempted_sha'])
    if attempts >= cfg['max_attempts_per_sha']:
        return
    run_id = _new_run_id(st, now)
    st.enqueue(run_id, last['attempted_sha'], now, day,
               attempt=attempts + 1)


def _new_run_id(st, now):
    day = _utcnow().strftime('%Y%m%d')
    seq = 1
    while st.run('qa-' + day + '-' + format(seq, '03d')) is not None:
        seq += 1
    return 'qa-' + day + '-' + format(seq, '03d')


def cycle(cfg, log=print):
    """One supervisor pass: reconcile, retry, then run the newest queued."""
    state_dir = Path(cfg['state_dir'])
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / 'lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('cycle: another run cycle holds the lock; exiting')
        return
    st = qa_state.State(state_dir / 'state.db')
    try:
        reconcile(st, cfg, log)
        now = time.time()
        day = _utcnow().strftime('%Y-%m-%d')
        _maybe_retry(st, cfg, now, day)
        if st.started_today(day) >= cfg['max_runs_per_day']:
            log('cycle: daily run budget reached')
            return
        record = st.next_queued()
        if record is None:
            log('cycle: nothing queued')
            return
        run(st, record, cfg, log)
    finally:
        st.close()
        lock.close()


def _timeline_writer(run_dir):
    events = []

    def append(event, detail=None):
        entry = {'t': _iso(), 'event': event}
        if detail:
            entry['detail'] = detail
        events.append(entry)
        # Persist incrementally: a killed runner leaves its timeline.
        with (run_dir / 'timeline.jsonl').open('a') as stream:
            stream.write(json.dumps(entry) + '\n')

    return append, events


def _free_bytes(path):
    return shutil.disk_usage(str(path)).free


def _build_images(src, cfg, run_dir, timeline, run_id):
    """Compile the pinned source in a resource-limited builder container,
    then package minimal runtime images mirroring the checked-in
    Dockerfile contract. Returns {'controller': digest, 'plant': digest}.

    The build shares a target/ cache across runs so a re-tested revision
    does not recompile the world; the cache is lane-owned state, not
    evidence.
    """
    work = Path(cfg['state_dir']) / 'build-cache'
    work.mkdir(parents=True, exist_ok=True)
    cargo = Path(cfg['state_dir']) / 'cargo-cache'
    cargo.mkdir(exist_ok=True)
    sha = src.name
    timeline('build-start', 'builder ' + cfg['builder_image']
             + ' cpus ' + cfg['builder_cpus'])
    docker('run', '--rm', '--name', 'dcs-hwtest-build-' + sha[:12],
           '--label', MANAGED_LABEL + '=1',
           '--label', RUN_LABEL + '=' + run_id,
           '--cpuset-cpus', cfg['builder_cpus'],
           '--memory', cfg['builder_memory'],
           '--memory-swap', cfg['builder_memory'],
           '--pids-limit', str(cfg['builder_pids']),
           '-v', str(src) + ':/src:ro',
           '-v', str(cargo) + ':/cargo',
           '-v', str(work) + ':/work',
           '-e', 'CARGO_HOME=/cargo',
           '-e', 'CARGO_TARGET_DIR=/work/target',
           cfg['builder_image'], 'bash', '-c',
           'cd /src && cargo build --release --locked '
           '-p dcs-controller -p dcs-plant',
           timeout=cfg['builder_timeout'])
    digests = {}
    for crate, binary, tag in (
            ('controller', 'dcs-controller', 'dcs-hwtest/controller'),
            ('plant', 'dcs-plant-server', 'dcs-hwtest/plant')):
        binary_path = work / 'target' / 'release' / binary
        if not binary_path.is_file():
            raise RuntimeError('build produced no ' + binary)
        context = run_dir / ('image-' + crate)
        context.mkdir(exist_ok=True)
        shutil.copy2(binary_path, context / binary)
        (context / 'Dockerfile').write_text(
            'FROM debian:bookworm-slim\n'
            'RUN useradd --no-create-home --shell /usr/sbin/nologin '
            '--uid 10001 dcs\n'
            'COPY ' + binary + ' /usr/local/bin/' + binary + '\n'
            'USER dcs\n'
            'ENTRYPOINT ["' + binary + '"]\n'
            'CMD ["--help"]\n')
        docker('build', '-t', tag + ':' + sha, str(context), timeout=600)
        image_id = docker('image', 'inspect', tag + ':' + sha,
                          '--format', '{{.Id}}').stdout.strip()
        digests[crate] = image_id
        timeline('image-built', crate + ' ' + image_id[:19])
    return digests


def _docker_run_args(cfg, run_id, name):
    return ['run', '-d', '--name', name,
            '--label', MANAGED_LABEL + '=1',
            '--label', RUN_LABEL + '=' + run_id,
            '--cpus', cfg['rig_cpus'],
            '--memory', cfg['rig_memory'],
            '--memory-swap', cfg['rig_memory'],
            '--pids-limit', str(cfg['rig_pids']),
            '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
            '--restart', 'no']


def _start_rig(cfg, record, src, run_dir, timeline):
    """Start the plant plus redundant pair on a dedicated labeled bridge."""
    run_id, sha = record['run_id'], record['attempted_sha']
    net = 'dcs-hwtest-' + run_id
    prefix = 'dcs-hw-' + run_id
    model = src / cfg['model_fixture']
    dynamics = src / cfg['dynamics_fixture']
    for path in (model, dynamics):
        if not path.is_file():
            raise RuntimeError('model fixture missing: ' + str(path))
    # A dedicated bridge per run. `--internal` is rejected on purpose:
    # it also blocks the loopback-published monitor ports the scenario
    # driver needs. Disabled masquerade gives no NAT egress instead —
    # containers can reach only each other and the published-port DNAT.
    docker('network', 'create',
           '-o', 'com.docker.network.bridge.enable_ip_masquerade=false',
           '--label', MANAGED_LABEL + '=1',
           '--label', RUN_LABEL + '=' + run_id, net)
    docker(*_docker_run_args(cfg, run_id, prefix + '-plant'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['plant_host_port']) + ':'
           + str(cfg['plant_port']),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(dynamics) + ':/model/dynamics.json:ro',
           'dcs-hwtest/plant:' + sha,
           '/model/plant.json', '--dynamics', '/model/dynamics.json',
           '--listen', '0.0.0.0:' + str(cfg['plant_port']))
    # The controllers' --remote attach connects once at startup and exits
    # if the listener is not yet bound: wait for the plant to serve first.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            socket.create_connection(
                ('127.0.0.1', cfg['plant_host_port']), timeout=3).close()
            break
        except OSError:
            time.sleep(1)
    else:
        raise RuntimeError('plant listener never bound')
    docker(*_docker_run_args(cfg, run_id, prefix + '-a'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['active_port']) + ':8080',
           '-v', str(model) + ':/model/plant.json:ro',
           'dcs-hwtest/controller:' + sha,
           '/model/plant.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--scan-ms', '100', '--listen', '0.0.0.0:8080')
    docker(*_docker_run_args(cfg, run_id, prefix + '-b'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['standby_port']) + ':8081',
           '-v', str(model) + ':/model/plant.json:ro',
           'dcs-hwtest/controller:' + sha,
           '/model/plant.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--standby', prefix + '-a:8080',
           '--scan-ms', '100', '--listen', '0.0.0.0:8081')
    timeline('rig-up', 'plant + controller pair on ' + net)


def _wait_monitor(cfg, timeline):
    deadline = time.monotonic() + cfg['monitor_timeout']
    while time.monotonic() < deadline:
        try:
            status, _ = scenarios.http_json(
                'GET', 'http://127.0.0.1:' + str(cfg['active_port'])
                + '/role', timeout=5)
            status_b, _ = scenarios.http_json(
                'GET', 'http://127.0.0.1:' + str(cfg['standby_port'])
                + '/role', timeout=5)
            if status == 200 and status_b == 200:
                timeline('monitors-ready')
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _teardown_rig(run_id, timeline):
    for cid, label_run in _managed_containers():
        if label_run == run_id:
            docker('rm', '-f', cid, check=False)
    for nid, label_run in _managed_networks():
        if label_run == run_id:
            docker('network', 'rm', nid, check=False)
    timeline('teardown', 'run containers and network removed')


def _persist_report(st, record, cfg, outcome, completed_sha, images,
                    results, infra, events, log=print):
    """Validate, store, stage, and record a report for a run record."""
    run_dir = Path(cfg['state_dir']) / 'runs' / record['run_id']
    run_dir.mkdir(parents=True, exist_ok=True)
    started = record['started'] or time.time()
    report_doc = {
        'schema_version': qa_report.SCHEMA_VERSION,
        'run_id': record['run_id'],
        'attempted_sha': record['attempted_sha'],
        'completed_sha': completed_sha,
        'image': images,
        'started_at': _iso(datetime.fromtimestamp(started, timezone.utc)),
        'finished_at': _iso(),
        'outcome': outcome,
        'attempt': record['attempt'],
        'host': {'name': 'lenovo', 'os': platform.system().lower()},
        'scenarios': results,
        'capability_limitations': cfg['capabilities'],
        'infrastructure_failures': infra,
        'timeline': events,
    }
    if record.get('range_first'):
        report_doc['changed_range'] = {
            'first': record['range_first'], 'last': record['attempted_sha']}
    qa_report.validate_report(json.dumps(report_doc),
                              run_id=record['run_id'],
                              attempted_sha=record['attempted_sha'])
    path = run_dir / 'report.json'
    path.write_text(json.dumps(report_doc, indent=1) + '\n')
    reports = Path(cfg['state_dir']) / 'reports'
    reports.mkdir(exist_ok=True)
    shutil.copy2(path, reports / (record['run_id'] + '.json'))
    if outcome == 'interrupted':
        # The record already carries the interrupted lifecycle status.
        st.attach_report(record['run_id'], str(path), time.time())
    else:
        st.finish(record['run_id'], outcome, completed_sha, str(path),
                  time.time())
    return path


def _read_timeline(run_dir):
    path = Path(run_dir) / 'timeline.jsonl'
    events = []
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def _write_interrupted_report(st, record, cfg, log=print):
    """Compose the interrupted-run report from the persisted timeline."""
    run_dir = Path(cfg['state_dir']) / 'runs' / record['run_id']
    events = _read_timeline(run_dir)
    events.append({'t': _iso(), 'event': 'reconciled',
                   'detail': 'runner process gone; run marked interrupted '
                             'and its labeled containers removed'})
    try:
        _persist_report(st, record, cfg, 'interrupted', None, None, [],
                        [{'key': 'runner-died',
                          'detail': record.get('error')
                          or 'runner process died mid-run',
                          'phase': 'run'}],
                        events, log)
    except Exception as exc:
        log('interrupted report failed for ' + record['run_id']
            + ': ' + str(exc)[:300])


def run(st, record, cfg, log=print):
    """Execute one QA run against record['attempted_sha']."""
    run_id, sha = record['run_id'], record['attempted_sha']
    st.begin(run_id, os.getpid(), time.time())
    record = st.run(run_id)
    state_dir = Path(cfg['state_dir'])
    run_dir = state_dir / 'runs' / run_id
    evidence_dir = run_dir / 'evidence'
    evidence_dir.mkdir(parents=True, exist_ok=True)
    timeline, events = _timeline_writer(run_dir)
    timeline('run-start', 'attempting ' + sha)
    deadline = time.monotonic() + cfg['hard_timeout_seconds']
    results, images, infra = [], None, []
    try:
        if _free_bytes(state_dir) < cfg['min_free_bytes']:
            timeline('run-blocked', 'insufficient free space')
            _persist_report(st, record, cfg, 'blocked', None, None, [],
                            [{'key': 'preflight-disk',
                              'detail': 'insufficient free space under '
                              + str(state_dir), 'phase': 'preflight'}],
                            events, log)
            return
        src_tar = Path(cfg['src_dir']) / (sha + '.tar')
        src = Path(cfg['src_dir']) / sha
        if not src.is_dir():
            if not src_tar.is_file():
                timeline('run-blocked', 'source archive missing')
                _persist_report(st, record, cfg, 'blocked', None, None, [],
                                [{'key': 'preflight-source',
                                  'detail': 'source archive missing: '
                                  + src_tar.name, 'phase': 'preflight'}],
                                events, log)
                return
            src.mkdir(parents=True, exist_ok=True)
            subprocess.run(['tar', '-xf', str(src_tar), '-C', str(src)],
                           check=True, timeout=300)
            timeline('source-extracted', src_tar.name)
        images = _build_images(src, cfg, run_dir, timeline, run_id)
        _start_rig(cfg, record, src, run_dir, timeline)
        try:
            if not _wait_monitor(cfg, timeline):
                raise RuntimeError('monitors did not come up')
            ctx = {'active': 'http://127.0.0.1:' + str(cfg['active_port']),
                   'standby': 'http://127.0.0.1:' + str(cfg['standby_port']),
                   'evidence_dir': evidence_dir,
                   'deadline': deadline}
            results = scenarios.run_all(ctx, timeline)
        finally:
            _teardown_rig(run_id, timeline)
        outcome = ('passed' if all(r['outcome'] == 'passed' for r in results)
                   else 'failed' if any(r['outcome'] == 'failed'
                                        for r in results)
                   else 'inconclusive')
        _persist_report(st, record, cfg, outcome,
                        sha if outcome in ('passed', 'failed') else None,
                        images, results, infra, events, log)
        timeline('run-finished', outcome)
        log('run ' + run_id + ': ' + outcome)
    except Exception as exc:
        # A run that errored mid-flight produced no verdict: record it
        # inconclusive with the infrastructure failure named, so the one
        # automatic retry applies. Only process death stays 'interrupted'.
        timeline('run-error', str(exc)[:500])
        infra.append({'key': 'run-error', 'detail': str(exc)[:500],
                      'phase': 'run'})
        try:
            _persist_report(st, record, cfg, 'inconclusive', None,
                            images, results, infra, events, log)
        except Exception as rep_exc:
            st.interrupt(run_id, str(exc)[:500] + ' | report failed: '
                         + str(rep_exc)[:300], time.time())
            raise
        log('run ' + run_id + ' inconclusive: ' + str(exc)[:300])
    finally:
        try:
            _teardown_rig(run_id, timeline)
        except Exception as exc:
            log('teardown failed: ' + str(exc)[:300])


def status(cfg):
    st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
    try:
        runs = st.runs()
        return {
            'last_attempted_sha': st.last_attempted_sha(),
            'queued': [r['attempted_sha'] for r in st.runs(('queued',))],
            'running': [{'run_id': r['run_id'],
                         'attempted_sha': r['attempted_sha'],
                         'started': r['started']}
                        for r in st.runs(('running',))],
            'recent': [{'run_id': r['run_id'], 'sha': r['attempted_sha'],
                        'status': r['status'], 'outcome': r['outcome'],
                        'attempt': r['attempt']}
                       for r in runs[-10:]],
        }
    finally:
        st.close()
