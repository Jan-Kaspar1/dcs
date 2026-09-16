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

from . import netpolicy, report as qa_report
from . import scenarios, state as qa_state

MANAGED_LABEL = 'dcs-hwtest.managed'
RUN_LABEL = 'dcs-hwtest.run'
IMAGE_PREFIX = 'dcs-hwtest/'

DEFAULT_CONFIG = {
    'state_dir': '/srv/dcs-hwtest',
    'src_dir': '/srv/dcs-hwtest/src',
    'max_runs_per_day': 4,
    'max_attempts_per_sha': 2,
    'hard_timeout_seconds': 7200,
    'min_free_bytes': 10 * 1024 ** 3,
    # Total lane footprint bound: everything under state_dir (source
    # archives/extractions, cargo+build caches, run evidence, staged
    # reports, state.db) plus lane-owned Docker images, the builder
    # image, and Docker build cache. Chosen as enforced accounting +
    # hard-fail preflight rather than a dedicated filesystem: Docker
    # image storage lives in the shared daemon and cannot be bounded
    # by a filesystem quota, so a filesystem bound would only ever
    # cover part of the footprint. A run refuses to start when the
    # measured footprint plus run_headroom_bytes would exceed this.
    'qa_storage_max_bytes': 60 * 1024 ** 3,
    # Conservative estimate of one run's growth: two ~120 MB runtime
    # images, extracted source, incremental target/ output, evidence.
    'run_headroom_bytes': 8 * 1024 ** 3,
    # Retention (reclaim runs every cycle): terminal run dirs kept,
    # distinct recently-attempted SHAs kept under src/, and distinct
    # recent SHAs whose images stay cached.
    'runs_keep': 20,
    'src_keep': 2,
    'images_keep': 3,
    # Staged reports are kept while their run record exists; files
    # whose record is gone are reaped only after this age.
    'reports_orphan_days': 30,
    'cargo_cache_max_bytes': 8 * 1024 ** 3,
    'build_cache_max_bytes': 20 * 1024 ** 3,
    'docker_build_cache_max_bytes': 4 * 1024 ** 3,
    # Fail closed when the host egress policy (qa_lane.netpolicy) is
    # absent. The dcs-hwtest-netpolicy systemd unit installs it at
    # boot; each cycle verifies and self-heals via sudo -n.
    'egress_required': True,
    'builder_ifname': netpolicy.BUILDER_IFACE,
    'rig_ifname': netpolicy.RIG_IFACE,
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
    # Bare mirror the relay pushes to; verification runs check fix
    # ancestry (`git merge-base --is-ancestor`) against it.
    cfg.setdefault('git_dir', str(Path(cfg['state_dir']) / 'repo.git'))
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


def _labeled_rows(*args):
    """(ok, [(object_id, run_label)]) for managed docker objects.

    ok=False means the listing itself failed — callers must treat that
    as 'cannot prove no leftovers', not as 'no leftovers'.
    """
    result = docker(*args, '--filter', 'label=' + MANAGED_LABEL + '=1',
                    '--format', '{{.ID}} {{.Label "' + RUN_LABEL + '"}}',
                    check=False)
    rows = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2:
                rows.append((parts[0], parts[1]))
    return result.returncode == 0, rows


def _managed_containers():
    # NB: `-q` makes docker ignore --format, so use plain `ps -a`.
    return _labeled_rows('ps', '-a')


def _managed_networks():
    return _labeled_rows('network', 'ls')


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
    containers_ok, containers = _managed_containers()
    networks_ok, networks = _managed_networks()
    for cid, run_id in containers:
        if run_id not in live:
            res = docker('rm', '-f', cid, check=False)
            if res.returncode != 0:
                st.record_cleanup_error(
                    'cleanup-container-' + cid,
                    'docker rm failed: ' + res.stderr.strip()[:300])
                log('reconcile: container ' + cid + ' removal FAILED')
            else:
                st.clear_cleanup_error('cleanup-container-' + cid)
                log('reconcile: removed orphaned container ' + cid)
    for nid, run_id in networks:
        if run_id not in live:
            res = docker('network', 'rm', nid, check=False)
            if res.returncode != 0:
                st.record_cleanup_error(
                    'cleanup-network-' + nid,
                    'docker network rm failed: ' + res.stderr.strip()[:300])
                log('reconcile: network ' + nid + ' removal FAILED')
            else:
                st.clear_cleanup_error('cleanup-network-' + nid)
                log('reconcile: removed orphaned network ' + nid)
    # Sweep ledger entries for docker objects that no longer exist —
    # e.g. a leftover removed manually between cycles. The entry did
    # its job (blocked cycles, surfaced in reports); it must not
    # outlive the object it describes. Only sweep when the listing
    # itself succeeded — a failed listing proves nothing.
    if containers_ok and networks_ok:
        extant = {cid for cid, _ in containers} \
            | {nid for nid, _ in networks}
        for key in list(st.cleanup_errors()):
            for prefix in ('cleanup-container-', 'cleanup-network-'):
                if key.startswith(prefix) \
                        and key[len(prefix):] not in extant:
                    st.clear_cleanup_error(key)


def ownership_block(st, log=print):
    """Fail-closed gate after reconcile: while exclusive ownership of
    the lane's resources is uncertain, no new run may start.

    Two conditions block, each with a named reason:
      active-run-conflict  a 'running' record points at a live process
                           that is not this cycle — the flock should
                           make that impossible, so the state is
                           inconsistent. Self-heals once the foreign
                           pid exits and reconcile interrupts it.
      cleanup-incomplete   managed containers/networks from dead runs
                           survived reconcile — teardown or host
                           docker is misbehaving, and starting a run
                           on top of leftovers is unsafe.
      docker-listing-failed the managed-object listing itself failed —
                           'no leftovers found' cannot be proven.
    Returns (reason, detail) or None.
    """
    active = st.runs(qa_state.ACTIVE_STATUSES)
    if active:
        ids = ', '.join(r['run_id'] for r in active)
        return ('active-run-conflict',
                'run record(s) still active under another pid: ' + ids)
    containers_ok, containers = _managed_containers()
    networks_ok, networks = _managed_networks()
    if not (containers_ok and networks_ok):
        return ('docker-listing-failed',
                'cannot enumerate managed containers/networks — '
                'cannot prove exclusive ownership')
    leftovers = ([('container', cid, rid) for cid, rid in containers]
                 + [('network', nid, rid) for nid, rid in networks])
    if leftovers:
        detail = '; '.join(kind + ' ' + ref + ' (run ' + rid + ')'
                           for kind, ref, rid in leftovers[:8])
        return ('cleanup-incomplete',
                'unreconciled managed objects: ' + detail[:400])
    return None


# --------------------------------------------------------------------------
# Storage accounting and retention
#
# The lane's footprint = everything under state_dir (archives,
# extractions, caches, run evidence, staged reports, state.db) plus
# lane-owned Docker objects (dcs-hwtest/* images, the builder image,
# Docker build cache — the daemon keeps those outside state_dir, so a
# filesystem quota alone cannot bound them). qa_storage_max_bytes is
# enforced by accounting + a hard-fail preflight; reclaim() is the
# retention reconciler that keeps the footprint inside the bound by
# removing only QA-owned artifacts, preserving evidence pinned for
# unresolved findings/verifications (qa_lane preserve).

_SIZE_UNITS = {'b': 1, 'kb': 1000, 'mb': 1000 ** 2, 'gb': 1000 ** 3,
               'tb': 1000 ** 4, 'kib': 1024, 'mib': 1024 ** 2,
               'gib': 1024 ** 3, 'tib': 1024 ** 4}


def _parse_size(text):
    """Parse docker's human sizes ('20.11GB', '823.6MB', '0B')."""
    text = text.strip()
    for unit in sorted(_SIZE_UNITS, key=len, reverse=True):
        if text.lower().endswith(unit):
            try:
                return int(float(text[:-len(unit)]) * _SIZE_UNITS[unit])
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _qa_images(cfg):
    """Lane-owned images: dcs-hwtest/* build outputs plus the builder
    image. Listed by repository prefix — never matched by name
    collision with other stacks (their repos don't share the prefix).
    """
    result = docker('image', 'ls', '--format',
                    '{{.Repository}}|{{.Tag}}|{{.ID}}|{{.Size}}',
                    check=False)
    rows = []
    if result.returncode != 0:
        return rows
    for line in result.stdout.splitlines():
        parts = line.split('|')
        if len(parts) != 4:
            continue
        repo, tag, iid, size = parts
        if repo.startswith(IMAGE_PREFIX) or \
                repo + ':' + tag == cfg['builder_image']:
            rows.append({'repo': repo, 'tag': tag, 'id': iid,
                         'size': _parse_size(size)})
    return rows


def _docker_build_cache_bytes():
    result = docker('system', 'df', '--format', '{{json .}}',
                    check=False)
    if result.returncode != 0:
        return 0
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if 'build' in str(row.get('Type', '')).lower():
            return _parse_size(str(row.get('Size', '0')))
    return 0


def qa_storage_usage(cfg):
    """Measured lane footprint in bytes."""
    files = _dir_size(cfg['state_dir'])
    images = sum(i['size'] for i in _qa_images(cfg))
    build_cache = _docker_build_cache_bytes()
    return {'files': files, 'images': images,
            'docker_build_cache': build_cache,
            'total': files + images + build_cache}


def _record_reclaim_error(st, key, detail):
    st.record_cleanup_error('reclaim-' + key, str(detail)[:400])


def _remove_path(path, st, key, log):
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        st.clear_cleanup_error('reclaim-' + key)
        return True
    except OSError as exc:
        _record_reclaim_error(st, key, str(exc))
        log('reclaim: failed to remove ' + path.name + ': '
            + str(exc)[:200])
        return False


def _trim_dir(path, cap):
    """Delete oldest-mtime files under path until it fits cap bytes.
    Used for the cargo registry cache — entries are re-fetched on
    demand, so dropping old ones is safe."""
    files = []
    total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            p = Path(root) / name
            try:
                st_ = p.stat()
            except OSError:
                continue
            files.append((st_.st_mtime, st_.st_size, p))
            total += st_.st_size
    removed = 0
    for _mtime, size, p in sorted(files):
        if total <= cap:
            break
        try:
            p.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return removed


def reclaim(st, cfg, log=print, docker_ok=True):
    """Retention reconciler: remove only QA-owned artifacts.

    Preserved without exception: artifacts of non-terminal runs
    (queued/running), run ids and SHAs pinned via `qa_lane preserve`
    (unresolved findings / queued verification), and runs whose record
    has no persisted report yet (evidence still being written).
    """
    now = time.time()
    preserved = st.preserved()
    all_runs = st.runs()
    active_shas = {r['attempted_sha']
                   for r in all_runs if r['status'] in ('queued', 'running')}
    recent_shas = []
    for r in reversed(all_runs):
        sha = r['attempted_sha']
        if sha not in recent_shas:
            recent_shas.append(sha)
    keep_shas = (active_shas | set(preserved['shas'])
                 | set(recent_shas[:cfg['src_keep']]))

    # 1. src/<sha>.tar and src/<sha>/ for SHAs nothing references.
    src_dir = Path(cfg['src_dir'])
    if src_dir.is_dir():
        for entry in sorted(src_dir.iterdir()):
            sha = entry.name[:-4] if entry.name.endswith('.tar') \
                else entry.name
            if len(sha) != 40 or any(c not in '0123456789abcdef'
                                     for c in sha):
                continue  # foreign file — never touch
            if sha in keep_shas:
                continue
            if _remove_path(entry, st, 'src-' + sha, log):
                log('reclaim: removed src ' + entry.name)

    # 2. runs/<id>/ beyond the retention window.
    keep_runs = ({r['run_id'] for r in all_runs
                  if r['status'] in ('queued', 'running')}
                 | set(preserved['runs'])
                 | {r['run_id'] for r in all_runs if not r['report']
                    and r['status'] != 'superseded'})
    terminal = [r for r in all_runs
                if r['status'] in ('finished', 'interrupted')
                and r['run_id'] not in keep_runs]
    terminal.sort(key=lambda r: r['finished'] or 0, reverse=True)
    keep_runs |= {r['run_id'] for r in terminal[:cfg['runs_keep']]}
    runs_dir = Path(cfg['state_dir']) / 'runs'
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            if not entry.name.startswith(('qa-', 'qav-')):
                continue  # foreign dir — never touch
            if entry.name in keep_runs:
                continue
            if _remove_path(entry, st, 'run-' + entry.name, log):
                log('reclaim: removed run dir ' + entry.name)

    # 3. reports/<id>.json: keep while the run record exists (the WSL
    #    relay pulls from here); reap only old orphans.
    reports_dir = Path(cfg['state_dir']) / 'reports'
    record_ids = {r['run_id'] for r in all_runs}
    orphan_age = cfg['reports_orphan_days'] * 86400
    if reports_dir.is_dir():
        for entry in sorted(reports_dir.iterdir()):
            if entry.suffix != '.json' or \
                    not entry.name.startswith(('qa-', 'qav-')):
                continue
            if entry.stem in record_ids:
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age > orphan_age and \
                    _remove_path(entry, st, 'report-' + entry.stem, log):
                log('reclaim: removed orphan report ' + entry.name)

    # 4. cargo cache: trim oldest entries to the cap (safe — cargo
    #    re-fetches missing registry entries on demand).
    cargo = Path(cfg['state_dir']) / 'cargo-cache'
    if cargo.is_dir() and _dir_size(cargo) > cfg['cargo_cache_max_bytes']:
        removed = _trim_dir(cargo, cfg['cargo_cache_max_bytes'])
        log('reclaim: trimmed cargo-cache by ' + str(removed) + ' files')
        if _dir_size(cargo) > cfg['cargo_cache_max_bytes']:
            _record_reclaim_error(st, 'cargo-cache',
                                  'still over cap after trim')

    # 5. build cache (shared cargo target/): wiping forces a clean
    #    rebuild, which is safe but slow — only when over the cap.
    work = Path(cfg['state_dir']) / 'build-cache'
    if work.is_dir() and _dir_size(work) > cfg['build_cache_max_bytes']:
        for entry in sorted(work.iterdir()):
            _remove_path(entry, st, 'build-cache-' + entry.name, log)
        log('reclaim: wiped build-cache (was over cap)')

    if not docker_ok:
        log('reclaim: skipping docker objects while ownership is '
            'uncertain')
        return

    # 6. lane images for SHAs beyond images_keep recent ones.
    keep_image_shas = (active_shas | set(preserved['shas'])
                       | set(recent_shas[:cfg['images_keep']]))
    for image in _qa_images(cfg):
        if not image['repo'].startswith(IMAGE_PREFIX):
            continue  # builder image is never reaped
        if image['tag'] in keep_image_shas:
            continue
        # Remove by repo:tag, not image id: identical binaries produce a
        # shared image id across SHAs, and `image rm <id>` refuses while
        # multiple repositories reference it. Untagging each stale ref
        # frees the layers once the last tag goes.
        key = ('image-' + image['repo'].split('/')[-1]
               + '-' + image['tag'][:12])
        res = docker('image', 'rm',
                     image['repo'] + ':' + image['tag'], check=False)
        if res.returncode != 0:
            _record_reclaim_error(st, key,
                                  'docker image rm failed: '
                                  + res.stderr.strip()[:300])
        else:
            st.clear_cleanup_error('reclaim-' + key)
            log('reclaim: removed image ' + image['repo']
                + ':' + image['tag'])

    # 7. docker build cache: only the lane builds on this host, so the
    #    cache is lane-attributable; bound it conservatively.
    if _docker_build_cache_bytes() > cfg['docker_build_cache_max_bytes']:
        res = docker('builder', 'prune', '-f', '--keep-storage',
                     str(cfg['docker_build_cache_max_bytes']),
                     check=False)
        if res.returncode != 0:
            _record_reclaim_error(st, 'docker-build-cache',
                                  'builder prune failed: '
                                  + res.stderr.strip()[:300])
        else:
            st.clear_cleanup_error('reclaim-docker-build-cache')
            log('reclaim: pruned docker build cache')


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


def _new_run_id(st, now, prefix='qa'):
    day = _utcnow().strftime('%Y%m%d')
    seq = 1
    while st.run(prefix + '-' + day + '-'
                 + format(seq, '03d')) is not None:
        seq += 1
    return prefix + '-' + day + '-' + format(seq, '03d')


def _ensure_egress_policy(log):
    """Verify the host egress policy; self-heal once via sudo. Returns
    the list of still-missing rules ([] = enforced)."""
    try:
        missing = netpolicy.verify()
    except Exception as exc:
        return ['verify failed: ' + str(exc)[:200]]
    if not missing:
        return []
    log('cycle: egress policy incomplete; re-applying')
    try:
        netpolicy.apply()
        missing = netpolicy.verify()
    except Exception as exc:
        return ['apply failed: ' + str(exc)[:200]]
    return missing


def _set_blocked(st, reason, detail, log):
    if reason is None:
        if st.get('blocked'):
            st.set('blocked', None)
        return False
    st.set('blocked', {'reason': reason, 'detail': str(detail)[:400],
                     'since': _iso()})
    log('cycle: ' + reason + ' — refusing to start runs: '
        + str(detail)[:200])
    return True


def cycle(cfg, log=print):
    """One supervisor pass: reconcile, gate, reclaim, then run the
    newest queued revision."""
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
        now = time.time()
        reconcile(st, cfg, log)
        block = ownership_block(st, log)
        # Pending-verification evidence pins must be in place before the
        # retention reconciler runs; settled items release their pins.
        from . import verify
        verify.sync_preserves(st, cfg, log)
        reclaim(st, cfg, log, docker_ok=block is None)
        day = _utcnow().strftime('%Y-%m-%d')
        _maybe_retry(st, cfg, now, day)
        if _set_blocked(st, *(block or (None, None)), log):
            return
        if st.started_today(day) >= cfg['max_runs_per_day']:
            log('cycle: daily run budget reached')
            return
        # Pending fix verifications dispatch ahead of the newest-SHA
        # assessment: a merged fix is re-verified on a revision proven to
        # contain it before the lane spends a run on fresh exploration.
        record = verify.next_run(st, cfg, now, log)
        queued = record if record is not None else st.next_queued()
        if queued is None:
            log('cycle: nothing queued')
            return
        # The egress gate covers both run kinds: a verification run
        # builds images and starts a rig exactly like an assessment.
        if cfg.get('egress_required'):
            missing = _ensure_egress_policy(log)
            if missing:
                _set_blocked(st, 'egress-policy-missing',
                             'host firewall rules absent: '
                             + '; '.join(missing[:5]), log)
                return
        if record is not None:
            verify.run(st, record, cfg, log)
        else:
            run(st, queued, cfg, log)
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
    # The builder needs crates.io egress (sparse index + crate files);
    # it runs on its own labeled bridge so the host egress policy can
    # allowlist web traffic for it while the rig bridge stays closed.
    net = 'dcs-hwtest-build-' + run_id
    docker('network', 'create',
           '-o', 'com.docker.network.bridge.name='
           + cfg['builder_ifname'],
           '--label', MANAGED_LABEL + '=1',
           '--label', RUN_LABEL + '=' + run_id, net)
    docker('run', '--rm', '--name', 'dcs-hwtest-build-' + sha[:12],
           '--label', MANAGED_LABEL + '=1',
           '--label', RUN_LABEL + '=' + run_id,
           '--network', net,
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
        docker('build', '-t', tag + ':' + sha,
               '--label', MANAGED_LABEL + '=1',
               '--label', RUN_LABEL + '=' + run_id,
               str(context), timeout=600)
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


# Restart recovery (decisions 35/36): each controller runs with
# --state-file and --journal-file on a runner-owned per-controller
# directory inside the run dir, bind-mounted into the container at
# CONTAINER_RUN_DIR. The mount keeps both artifacts inspectable on the
# host — the restart scenario reads the journal file's run-boundary
# record — and inside the bounded run directory the retention
# reconciler removes with the run.
CONTAINER_RUN_DIR = '/var/lib/dcs-run'
CONTAINER_STATE_FILE = CONTAINER_RUN_DIR + '/state.json'
CONTAINER_JOURNAL_FILE = CONTAINER_RUN_DIR + '/journal.jsonl'


def _controller_dir(run_dir, name):
    """The run-dir state directory bind-mounted into controller `name`'s
    container ('a'/'b'): its --state-file checkpoint and --journal-file
    audit record live here so a container restart resumes the same run
    and the files stay inside the bounded run directory."""
    return Path(run_dir) / 'controllers' / name


def restart_controller(run_id, name, timeline):
    """The scenario-callable controller restart: `docker stop` then
    `docker start` on one of the run's already-launched controller
    containers — the supervisor-owned lifecycle action a scenario
    triggers through ctx['restart_controller'], never a second writer
    to the field.

    `name` is the scenario ctx's endpoint key: 'active' is ctrl-a's
    container, 'standby' ctrl-b's, whichever role each currently
    reports. The container keeps its mounts, labels, published port,
    and bridge name, so the restarted process resumes through the same
    --state-file and rejoins the pair unchanged. Both halves are
    recorded on the run's action timeline; a docker failure raises so
    the calling scenario reports the restart never completed.
    """
    container = ('dcs-hw-' + run_id + '-'
                 + {'active': 'a', 'standby': 'b'}[name])
    timeline('controller-restart', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    docker('start', container, timeout=60)
    timeline('controller-restarted', container + ' running')


def _scenario_ctx(cfg, record, run_dir, evidence_dir, deadline,
                  timeline):
    """The scenario driver's view of the running rig: monitor base URLs
    per endpoint key, the run's evidence dir and deadline, the
    runner-owned controller-restart action, and the host-side
    per-controller state/journal files the restart scenario reads."""
    run_id = record['run_id']
    names = {'active': 'a', 'standby': 'b'}
    return {
        'active': 'http://127.0.0.1:' + str(cfg['active_port']),
        'standby': 'http://127.0.0.1:' + str(cfg['standby_port']),
        'evidence_dir': evidence_dir,
        'deadline': deadline,
        'restart_controller': lambda name: restart_controller(
            run_id, name, timeline),
        'state_files': {key: str(_controller_dir(run_dir, peer)
                                 / 'state.json')
                        for key, peer in names.items()},
        'journal_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'journal.jsonl')
                          for key, peer in names.items()},
    }


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
    # Each controller's restart-recovery artifacts live in its own
    # runner-owned directory inside the run dir. Mode 0777 lets the
    # container's uid-10001 process create its state/journal files
    # under the runner-owned run directory.
    for name in ('a', 'b'):
        directory = _controller_dir(run_dir, name)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o777)
    # A dedicated bridge per run on a fixed interface name the host
    # egress policy (qa_lane.netpolicy) matches: no new outbound
    # connections leave it — no LAN, no other containers, no IPv6;
    # only replies to loopback-published monitor connections pass.
    # `--internal` is still rejected on purpose: it also blocks the
    # published ports the scenario driver needs. Disabled masquerade
    # remains as defense in depth beneath the firewall policy.
    docker('network', 'create',
           '-o', 'com.docker.network.bridge.name=' + cfg['rig_ifname'],
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
           '-v', str(_controller_dir(run_dir, 'a'))
           + ':' + CONTAINER_RUN_DIR,
           'dcs-hwtest/controller:' + sha,
           '/model/plant.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--scan-ms', '100', '--listen', '0.0.0.0:8080',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
    docker(*_docker_run_args(cfg, run_id, prefix + '-b'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['standby_port']) + ':8081',
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, 'b'))
           + ':' + CONTAINER_RUN_DIR,
           'dcs-hwtest/controller:' + sha,
           '/model/plant.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--standby', prefix + '-a:8080',
           '--scan-ms', '100', '--listen', '0.0.0.0:8081',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
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


def _teardown_rig(run_id, timeline, st=None):
    """Remove a run's labeled containers and networks. Returns a list
    of report-shaped infra failures; failures are also written to the
    cleanup ledger so they stay visible until a later removal succeeds
    and the ownership gate can refuse new runs while they persist."""
    failures = []

    def _remove(kind, ref, args):
        res = docker(*args, check=False)
        if res.returncode != 0:
            detail = res.stderr.strip()[:300] or 'exit ' \
                + str(res.returncode)
            failures.append({'key': 'cleanup-' + kind + '-' + ref,
                             'detail': kind + ' ' + ref + ': ' + detail,
                             'phase': 'cleanup'})
            if st is not None:
                st.record_cleanup_error(
                    'cleanup-' + kind + '-' + ref,
                    'docker ' + args[0] + ' failed: ' + detail)
        elif st is not None:
            st.clear_cleanup_error('cleanup-' + kind + '-' + ref)

    containers_ok, containers = _managed_containers()
    networks_ok, networks = _managed_networks()
    for ok, kind in ((containers_ok, 'containers'),
                     (networks_ok, 'networks')):
        if ok:
            if st is not None:
                st.clear_cleanup_error('cleanup-listing-' + kind)
        else:
            failures.append({'key': 'cleanup-listing-' + kind,
                             'detail': 'docker listing of managed '
                             + kind + ' failed — teardown may be '
                             'incomplete', 'phase': 'cleanup'})
            if st is not None:
                st.record_cleanup_error(
                    'cleanup-listing-' + kind,
                    'docker listing of managed ' + kind + ' failed')
    for cid, label_run in containers:
        if label_run == run_id:
            _remove('container', cid, ('rm', '-f', cid))
    for nid, label_run in networks:
        if label_run == run_id:
            _remove('network', nid, ('network', 'rm', nid))
    detail = 'run containers and network removed'
    if failures:
        detail += ' — ' + str(len(failures)) + ' removal(s) FAILED'
    timeline('teardown', detail)
    return failures


def _persist_report(st, record, cfg, outcome, completed_sha, images,
                    results, infra, events, log=print, verifications=None):
    """Validate, store, stage, and record a report for a run record.

    The durable cleanup ledger is merged into infrastructure_failures
    so teardown/reclaim failures that outlived a single run stay
    visible in every report until resolved.
    """
    infra = list(infra)
    seen = {i.get('key') for i in infra if isinstance(i, dict)}
    for key, entry in sorted(st.cleanup_errors().items()):
        if key not in seen:
            infra.append({'key': key,
                          'detail': str(entry.get('detail', ''))[:3900]
                          + ' (first seen ' + str(entry.get('first_seen'))
                          + ')', 'phase': 'cleanup'})
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
    if verifications is not None:
        report_doc['verifications'] = verifications
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


def _preflight_blocked(st, record, cfg, timeline, events, log):
    """Resource gates that refuse a run before it touches the rig.
    Returns True when the run was closed out as 'blocked' — a blocked
    run never attempted its revision, so the dispatcher may requeue
    the same SHA once the condition clears."""
    state_dir = Path(cfg['state_dir'])
    if _free_bytes(state_dir) < cfg['min_free_bytes']:
        timeline('run-blocked', 'insufficient free space')
        _persist_report(st, record, cfg, 'blocked', None, None, [],
                        [{'key': 'preflight-disk',
                          'detail': 'insufficient free space under '
                          + str(state_dir), 'phase': 'preflight'}],
                        events, log)
        return True
    try:
        usage = qa_storage_usage(cfg)
    except Exception as exc:
        timeline('run-blocked', 'storage accounting failed')
        _persist_report(st, record, cfg, 'blocked', None, None, [],
                        [{'key': 'preflight-storage-error',
                          'detail': 'cannot measure the lane footprint: '
                          + str(exc)[:300], 'phase': 'preflight'}],
                        events, log)
        return True
    projected = usage['total'] + cfg['run_headroom_bytes']
    if projected > cfg['qa_storage_max_bytes']:
        timeline('run-blocked', 'qa storage bound would be exceeded')
        _persist_report(st, record, cfg, 'blocked', None, None, [],
                        [{'key': 'preflight-storage-bound',
                          'detail': 'lane footprint ' + str(usage['total'])
                          + ' + headroom '
                          + str(cfg['run_headroom_bytes']) + ' exceeds '
                          + str(cfg['qa_storage_max_bytes'])
                          + ' (files ' + str(usage['files'])
                          + ', images ' + str(usage['images'])
                          + ', docker build cache '
                          + str(usage['docker_build_cache']) + ')',
                          'phase': 'preflight'}],
                        events, log)
        return True
    return False


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
        if _preflight_blocked(st, record, cfg, timeline, events, log):
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
            ctx = _scenario_ctx(cfg, record, run_dir, evidence_dir,
                                deadline, timeline)
            results = scenarios.run_all(ctx, timeline)
        finally:
            infra += _teardown_rig(run_id, timeline, st)
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
            leftover = _teardown_rig(run_id, timeline, st)
            if leftover:
                log('teardown reported ' + str(len(leftover))
                    + ' failure(s); recorded in the cleanup ledger')
        except Exception as exc:
            log('teardown failed: ' + str(exc)[:300])


def status(cfg):
    st = qa_state.State(Path(cfg['state_dir']) / 'state.db')
    try:
        from . import verify
        runs = st.runs()
        try:
            storage = qa_storage_usage(cfg)
            storage['bound'] = cfg['qa_storage_max_bytes']
        except Exception as exc:
            storage = {'error': str(exc)[:300]}
        return {
            'last_attempted_sha': st.last_attempted_sha(),
            'queued': [r['attempted_sha'] for r in st.runs(('queued',))],
            'running': [{'run_id': r['run_id'],
                         'attempted_sha': r['attempted_sha'],
                         'started': r['started']}
                        for r in st.runs(('running',))],
            'blocked': st.get('blocked'),
            'cleanup_errors': st.cleanup_errors(),
            'preserve': st.preserved(),
            'storage': storage,
            'recent': [{'run_id': r['run_id'], 'sha': r['attempted_sha'],
                        'status': r['status'], 'outcome': r['outcome'],
                        'attempt': r['attempt'], 'day': r['day']}
                       for r in runs[-10:]],
            'pending_verifications': len(verify.load_queue(cfg)),
        }
    finally:
        st.close()
