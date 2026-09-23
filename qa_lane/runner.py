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
import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import netpolicy, report as qa_report, revision
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
    # The rolling model-revision case's third controller publishes its
    # monitor here; its sim-net side shares the run's labeled bridge.
    'revised_port': 18082,
    # The checkpoint-negotiation case's foreign-fingerprint peer gets
    # its own published port: it lives beside the pair while the
    # revised container does not exist yet, and the case removes it
    # before the model-revision launch.
    'foreign_port': 18083,
    # The dead-peer-latency case's driven third controller publishes
    # its monitor here; its sim-net side shares the run's labeled
    # bridge.
    'driven_port': 18084,
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
    # The tracking standby's --auto-promote heartbeat budget: that many
    # consecutive failed checkpoint pulls — one per 100 ms scan cycle —
    # self-promote it, so a stopped writer's plant freeze is bounded at
    # ~12 s. The declared field freshness budget (5 ticks) presents
    # stale well inside the window, while a healthy container restart
    # (~3-5 s of misses) never reaches it.
    'failover_misses': 120,
    # Deterministic plant-writer owner tokens pinned per controller
    # endpoint key — every controller the runner launches carries its
    # key's --owner-token, so a scenario plant-protocol attachment can
    # `ensure_writer` under the standing owner's token and share the
    # claim (the designed harness path, answering claimed_shared)
    # instead of preempting the field writer. Each endpoint keeps its
    # own token: the sim's writer claim still fences every other
    # owner, and a standby holds no claim until it promotes. The
    # launch helper refuses a duplicated or missing pin — two
    # controllers on one token would silently defeat the fencing.
    'plant_owner_tokens': {'active': 424243, 'standby': 424244,
                           'revised': 424245, 'foreign': 424246,
                           'driven': 424247},
    # The rig bridge-to-host reachability rule the qax-20260922-001,
    # qax-20260922-005, and qax-20260923-001 exploration runs
    # demonstrated, recorded as the lane's endpoint-placement contract:
    # the host egress policy drops every packet a rig-bridge container
    # aims at the host itself (netpolicy's INPUT rules), so a socket
    # bound on the host — loopback, the LAN address, or another
    # stack's published port reached through it — is unreachable from
    # the rig network. Every lane endpoint carries a recorded
    # placement: 'loopback' marks the services host-side scenario
    # attachments reach through their 127.0.0.1-published ports (the
    # monitor endpoints and the published plant-probe port); 'bridge'
    # marks endpoints a rig peer must dial — the tracking-source/auth
    # legs' checkpoint interposer and forged-checkpoint server — which
    # run in labeled containers on the run's rig network and are
    # dialed by container name, never through a host address.
    'endpoint_placement': {
        'active': 'loopback', 'standby': 'loopback',
        'revised': 'loopback', 'foreign': 'loopback',
        'driven': 'loopback', 'plant': 'loopback',
        'interposer': 'bridge', 'forge': 'bridge'},
    'model_fixture': 'crates/dcs-demo/fixtures/pump_station.json',
    # The lane's own dynamics declaration: the shared fixture leaves
    # the inflow channel to scripted forcing, while the unattended rig
    # needs the declared inflow so the station cycles demand on its own
    # — the duty-rotation case's honest lever.
    'dynamics_fixture': 'qa_lane/fixtures/pump_station_dynamics.json',
    'capabilities': [
        {'key': 'no-ethercat',
         'detail': 'No EtherCAT driver in this revision; all field I/O '
                   'exercised through the dcs-sim-net remote simulation.',
         'blocking': False},
    ],
    # Charter-based exploratory lane (qax-* run ids): when enabled, idle
    # cycles dispatch a time-bounded Devin session against the newest
    # gate-verdicted revision instead of rerunning scenarios. The session
    # runs on the host as the lane user — its working directory lives
    # inside the run dir and it reaches the rig through the loopback
    # monitor ports. `exploration_devin` should be an absolute path: the
    # oneshot unit's PATH lacks ~/.local/bin.
    'exploration_enabled': False,
    'exploration_devin': 'devin',
    'exploration_model': 'swe-2-high',
    'exploration_time_budget_seconds': 5400,
    'exploration_interval_seconds': 7200,
    'max_explorations_per_day': 8,
    'exploration_ledger_keep': 20,
    'exploration_prompt': None,
    'git_dir': '/srv/dcs-hwtest/repo.git',
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
    """Queue one automatic retry for an inconclusive assessment run.
    Dedicated kinds never trigger sha-retries: an inconclusive qav
    already bounds its own attempts and an inconclusive exploration is
    a valid outcome, not a broken gate."""
    finished = [r for r in st.runs(('finished',))
                if r['run_id'].startswith('qa-')]
    if not finished:
        return
    last = finished[-1]
    if last['outcome'] != 'inconclusive':
        return
    if st.next_queued('qa') is not None:
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
    # Lazy: the lane is POSIX-only, but the module must stay importable on
    # Windows so the repository test suite can collect it there.
    import fcntl
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
        # Dispatch order: pending fix verifications first, then the newest
        # queued assessment, then — when enabled and nothing else is due —
        # an exploratory session against the newest gate-verdicted
        # revision. The daily budget bounds assessments only: dedicated
        # kinds carry their own budgets (max_explorations_per_day) or are
        # queue-bound (qav-), and must never starve or be starved by the
        # deterministic gate.
        record = verify.next_run(st, cfg, now, log)
        queued = None
        if st.started_today(day, 'qa') >= cfg['max_runs_per_day']:
            log('cycle: daily assessment budget reached')
        else:
            queued = st.next_queued('qa')
        if record is None and queued is None:
            from . import explorer
            record = explorer.next_run(st, cfg, now, log)
        if record is None and queued is None:
            log('cycle: nothing queued')
            return
        if cfg.get('egress_required'):
            missing = _ensure_egress_policy(log)
            if missing:
                _set_blocked(st, 'egress-policy-missing',
                             'host firewall rules absent: '
                             + '; '.join(missing[:5]), log)
                return
        if record is not None and record['run_id'].startswith('qav-'):
            verify.run(st, record, cfg, log)
        elif queued is not None:
            run(st, queued, cfg, log)
        else:
            from . import explorer
            explorer.run(st, record, cfg, log)
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
           '-p dcs-controller -p dcs-plant -p dcs-sim-net '
           '&& cargo build --release --locked '
           '-p dcs-monitor --bin dcs-ctl',
           timeout=cfg['builder_timeout'])
    # Extra binaries each image ships beside its entrypoint: the plant
    # image carries dcs-plant-ctl — the plant-side tool the lane execs
    # inside the container against the server's loopback listener, so
    # the covered plant ops run through the shipped binary rather than
    # a second Python implementation of the wire protocol.
    ship = {'plant': ['dcs-plant-ctl']}
    digests = {}
    for crate, binary, tag in (
            ('controller', 'dcs-controller', 'dcs-hwtest/controller'),
            ('plant', 'dcs-plant-server', 'dcs-hwtest/plant')):
        binaries = [binary] + ship.get(crate, [])
        for name in binaries:
            binary_path = work / 'target' / 'release' / name
            if not binary_path.is_file():
                raise RuntimeError('build produced no ' + name)
        context = run_dir / ('image-' + crate)
        context.mkdir(exist_ok=True)
        copies = ''
        for name in binaries:
            shutil.copy2(work / 'target' / 'release' / name,
                         context / name)
            copies += 'COPY ' + name + ' /usr/local/bin/' + name + '\n'
        (context / 'Dockerfile').write_text(
            'FROM debian:bookworm-slim\n'
            'RUN useradd --no-create-home --shell /usr/sbin/nologin '
            '--uid 10001 dcs\n'
            + copies +
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
    # The operator CLI ships as a host-side binary, not an image: the
    # same bounded builder compile produces it, and the dcs-ctl
    # scenario execs it against the pair's published monitor ports.
    if not _dcs_ctl_path(cfg).is_file():
        raise RuntimeError('build produced no dcs-ctl')
    timeline('tool-built', 'dcs-ctl ' + str(_dcs_ctl_path(cfg)))
    return digests


def _dcs_ctl_path(cfg):
    """The host-side dcs-ctl binary the lane's image build produces —
    the operator-CLI seam the dcs-ctl scenario consumes through
    ctx['dcs_ctl']."""
    return Path(cfg['state_dir']) / 'build-cache' / 'target' \
        / 'release' / 'dcs-ctl'


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

# The endpoint keys whose controllers the runner launches — the pair
# `_start_rig` brings up plus the three scenario-action peers.
OWNER_TOKEN_ENDPOINTS = ('active', 'standby', 'revised', 'foreign',
                         'driven')


def _plant_owner_tokens(cfg):
    """The run's per-controller --owner-token pins, recorded in the
    run config under 'plant_owner_tokens' and validated before a
    launch trusts them.

    The pins are deterministic per endpoint key so a scenario
    attachment can `ensure_writer` with the standing owner's token —
    the designed shared-claim path for a test harness driving plant
    stimuli (the controller's --owner-token contract). Each endpoint
    keeps its own token: the sim's writer claim still fences every
    other owner, and a standby holds no claim until it promotes. A
    config missing a pin or repeating one across endpoints fails the
    launch loudly — a duplicated token would answer `claimed_shared`
    instead of preempting, silently defeating the single-writer
    fencing the claim exists to provide.
    """
    tokens = cfg.get('plant_owner_tokens') or {}
    missing = [key for key in OWNER_TOKEN_ENDPOINTS
               if key not in tokens]
    if missing:
        raise RuntimeError('plant_owner_tokens pins no --owner-token '
                           'for endpoint(s): ' + ', '.join(missing))
    bad = {key: tokens[key] for key in OWNER_TOKEN_ENDPOINTS
           if not isinstance(tokens[key], int)
           or isinstance(tokens[key], bool)
           or not 0 <= tokens[key] <= 0xFFFFFFFFFFFFFFFF}
    if bad:
        raise RuntimeError('plant_owner_tokens pins must be u64 '
                           'integers: ' + json.dumps(bad))
    pins = {key: tokens[key] for key in OWNER_TOKEN_ENDPOINTS}
    if len(set(pins.values())) != len(pins):
        raise RuntimeError('plant_owner_tokens must pin a distinct '
                           '--owner-token per controller endpoint: '
                           + json.dumps(pins, sort_keys=True))
    return pins


# The endpoint keys the run config records a placement for: the
# monitor/plant services every scenario ctx carries plus the named
# attachment endpoints the takeover-integrity legs (#573 and
# successors) and the tracking-source/auth evidence place.
PLACEMENT_ENDPOINTS = ('active', 'standby', 'revised', 'foreign',
                       'driven', 'plant', 'interposer', 'forge')
PLACEMENTS = ('loopback', 'bridge')


def _endpoint_placement(cfg):
    """The run's recorded endpoint placements, validated before a
    launch trusts them — the rig bridge-to-host reachability rule
    made configuration.

    The host egress policy (qa_lane.netpolicy) drops every packet a
    rig-bridge container aims at the host — the INPUT hook's
    catch-all — so a rig-dialed endpoint can never be a host socket:
    'bridge' placements run in labeled containers on the run's rig
    network and rig peers dial them by container name, while
    'loopback' placements are the host-side scenario-attachment
    views through the 127.0.0.1-published ports. A config missing an
    endpoint's placement or naming an unknown one fails the launch
    loudly, same as a duplicated owner token.
    """
    placements = cfg.get('endpoint_placement') or {}
    missing = [key for key in PLACEMENT_ENDPOINTS
               if key not in placements]
    if missing:
        raise RuntimeError('endpoint_placement records no placement '
                           'for endpoint(s): ' + ', '.join(missing))
    bad = {key: placements[key] for key in PLACEMENT_ENDPOINTS
           if placements[key] not in PLACEMENTS}
    if bad:
        raise RuntimeError('endpoint_placement values must be one of '
                           + json.dumps(list(PLACEMENTS)) + ': '
                           + json.dumps(bad, sort_keys=True))
    return {key: placements[key] for key in PLACEMENT_ENDPOINTS}


def _controller_dir(run_dir, name):
    """The run-dir state directory bind-mounted into controller `name`'s
    container ('a'/'b'): its --state-file checkpoint and --journal-file
    audit record live here so a container restart resumes the same run
    and the files stay inside the bounded run directory."""
    return Path(run_dir) / 'controllers' / name


def _controller_container(run_id, name):
    """The run's controller container for a scenario ctx endpoint key:
    'active' is ctrl-a's container, 'standby' ctrl-b's, whichever role
    each currently reports."""
    return 'dcs-hw-' + run_id + '-' + {'active': 'a', 'standby': 'b'}[name]


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
    container = _controller_container(run_id, name)
    timeline('controller-restart', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    docker('start', container, timeout=60)
    timeline('controller-restarted', container + ' running')


def cold_restart_controller(run_id, run_dir, name, timeline):
    """The scenario-callable cold restart: `docker stop` on one of the
    run's controller containers, remove that controller's host-side
    --state-file inside the bounded run dir, then `docker start` — the
    induction the source-restart scenario needs to produce a
    checkpoint stream whose served tick regresses below the tracking
    peer's last alignment. Where `restart_controller` preserves the
    persisted state and resumes the same run, this leaves the
    restarted process nothing to resume: it binds its --journal-file
    (which stays — the new run-boundary marker at tick 0 is part of
    the evidence the resume was cold) and serves its checkpoint
    stream from the beginning of a fresh run.

    `name` is the scenario ctx's endpoint key: 'active' is ctrl-a's
    container and state dir, 'standby' ctrl-b's, whichever role each
    currently reports. Only the named controller's state.json is
    removed, and only inside this run's bounded directory. Both
    docker halves are recorded on the run's action timeline; a docker
    or state-file failure raises so the calling scenario reports the
    cold restart never completed rather than silently performing a
    warm restart.
    """
    container = _controller_container(run_id, name)
    peer = container.rsplit('-', 1)[1]
    state = _controller_dir(run_dir, peer) / 'state.json'
    timeline('controller-cold-restart', 'docker stop ' + container
             + '; drop ' + str(state))
    docker('stop', '--time', '2', container, timeout=90)
    existed = state.is_file()
    state.unlink(missing_ok=True)
    docker('start', container, timeout=60)
    timeline('controller-cold-restarted', container
             + ' running cold (state file '
             + ('dropped' if existed else 'already absent') + ')')


def stop_controller(run_id, name, timeline):
    """The stop half of the lifecycle action, alone: `docker stop` on
    one of the run's controller containers, held down until the scenario
    issues `start_controller` — the seam the stale-freshness case uses
    to freeze the shared plant's stepping while it polls the surviving
    peer. Recorded on the run's action timeline; a docker failure raises
    so the induction is reported as never completed.
    """
    container = _controller_container(run_id, name)
    timeline('controller-stop', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    timeline('controller-stopped', container + ' down')


def start_controller(run_id, name, timeline):
    """The matching start half: `docker start` on a container
    `stop_controller` stopped — the resumed process reclaims the shared
    plant's writer and the tracking peer's checkpoint stream resumes.
    """
    container = _controller_container(run_id, name)
    timeline('controller-start', 'docker start ' + container)
    docker('start', container, timeout=60)
    timeline('controller-started', container + ' running')


def stop_plant(run_id, timeline):
    """The scenario-callable plant stop: `docker stop` on the run's
    shared-plant container — the field-loss half of the link-loss
    scenario, severing both controllers' remote-driver connections at
    the same boundary. Recorded on the run's action timeline like the
    controller restart; a docker failure raises so the calling
    scenario reports the stop never completed."""
    container = 'dcs-hw-' + run_id + '-plant'
    timeline('plant-stop', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    timeline('plant-stopped', container + ' stopped')


def start_plant(run_id, timeline):
    """The recovery half: `docker start` relaunches the run's plant
    container — a fresh plant-server lifetime, so the single-writer
    claim the old process held is gone and the field owner must
    re-claim it."""
    container = 'dcs-hw-' + run_id + '-plant'
    timeline('plant-start', 'docker start ' + container)
    docker('start', container, timeout=60)
    timeline('plant-started', container + ' running')


def plant_ctl(run_id, port, *args):
    """The scenario-callable plant-tool invocation: `docker exec` runs
    the shipped `dcs-plant-ctl` inside the run's plant container
    against the server's loopback listener — the ticket's honest seam,
    so the lane's covered plant ops drive the binary the image carries
    rather than a second Python implementation of the wire protocol.
    The loopback address binds inside the container's own netns — the
    exchange never leaves the rig bridge the netpolicy closes.
    `check=False` returns the CompletedProcess on a refused request too
    — the tool's nonzero exit is the answer the caller classifies, not
    a docker failure."""
    container = 'dcs-hw-' + run_id + '-plant'
    return docker('exec', container, 'dcs-plant-ctl',
                  '127.0.0.1:' + str(port), *args,
                  check=False, timeout=60)


def _revised_peer_role(cfg):
    """The run's third controller's served RoleReport, or None when
    its monitor is unreachable — the relaunch guard's read of whether
    the existing '-c' container currently owns the field."""
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:' + str(cfg['revised_port'])
                + '/role', timeout=3) as response:
            return json.loads(response.read() or b'null')
    except Exception:
        return None


def start_revised_controller(cfg, record, run_dir, model, active,
                             timeline, incompatible=False):
    """The scenario-callable rolling model-revision action
    (WW-LCM-001's deployment-update clause, the rolling
    model-revision decision): derive the revised model document from
    the run's mounted model through the checked-in recipe
    (qa_lane/revision.py), then launch the run's third controller
    container on it as `--standby <active> --revised`.

    `active` is the scenario ctx key of the peer currently writing the
    field ('active' is ctrl-a, 'standby' ctrl-b) — the revised peer
    pulls that peer's checkpoints until the carryover rule applies
    them to the revised model. The container carries the run's managed
    and run labels so teardown reconciles it with the rest of the rig,
    mounts the revised document read-only at /model/revised.json, and
    gets its own runner-owned state/journal directory: the carryover
    arrives through the standby pull, never through a copied
    checkpoint that the fingerprint gate would reject. Both halves —
    the derivation and the launch — are recorded on the run's action
    timeline; a derivation or docker failure raises so the calling
    scenario reports the action never completed.

    `incompatible=True` runs the refusal half: the derivation applies
    the checked-in post-derivation step (revision-incompatible.json)
    that retypes a carried point so the crossing refuses with the
    named carryover error, and the derived document lands at
    model-revised-incompatible.json instead.

    A second call relaunches the third container: a leftover '-c' —
    the incompatible scenario's degraded standby, or a killed
    earlier attempt — is removed first, but only after its served
    /role proves it does not own the field; an active or promoting
    '-c' refuses removal by name rather than silently orphaning the
    plant's writer claim. Its runner-owned state and journal files
    reset with the container so the new lifetime starts cold: a
    state file left by the old document would fail the fingerprint
    resume gate, and a second run boundary in one journal file would
    misread the new lifetime.

    Returns the derivation summary (revised document path and the
    recipe's added point/signal ids — plus the retyped point on the
    incompatible variant) plus the container name.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = {'active': ('a', 8080), 'standby': ('b', 8081)}
    if active not in peers:
        raise RuntimeError('start_revised expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name, peer_port = peers[active]
    name = ('model-revised-incompatible.json' if incompatible
            else 'model-revised.json')
    revised_doc = Path(run_dir) / name
    info = (revision.derive_incompatible_model(model, revised_doc)
            if incompatible
            else revision.derive_revised_model(model, revised_doc))
    directory = _controller_dir(run_dir, 'c')
    container = prefix + '-c'
    listed = docker('ps', '-a', '--filter',
                    'name=^/' + container + '$', '--format', '{{.ID}}',
                    check=False)
    if listed.returncode != 0:
        raise RuntimeError('start_revised cannot prove the third '
                           'controller is absent: docker ps failed: '
                           + listed.stderr.strip()[:300])
    if listed.stdout.strip():
        report = _revised_peer_role(cfg)
        if report and report.get('role') in ('active', 'promoting'):
            raise RuntimeError('start_revised refuses to replace '
                               + container + ': it reports role '
                               + str(report['role']))
        timeline('model-revision-replace',
                 'docker rm -f ' + container
                 + ' (served role ' + str((report or {}).get('role'))
                 + ')')
        docker('rm', '-f', container, timeout=60)
        directory.mkdir(parents=True, exist_ok=True)
        for artifact in ('state.json', 'journal.jsonl'):
            (directory / artifact).unlink(missing_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    standby = prefix + '-' + peer_name + ':' + str(peer_port)
    owner_token = _plant_owner_tokens(cfg)['revised']
    timeline('model-revision-start',
             'derive ' + revised_doc.name + ' (+points '
             + str(info['added_points']) + ', +signals '
             + str(info['added_signals'])
             + (', retyped point ' + str(info['retyped_point'])
                if incompatible else '')
             + '); launch ' + container
             + ' --standby ' + standby + ' --revised'
             + ' --owner-token ' + str(owner_token))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(cfg['revised_port']) + ':8082',
           '-v', str(revised_doc) + ':/model/revised.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           '/model/revised.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--owner-token', str(owner_token),
           '--standby', standby,
           '--revised',
           '--scan-ms', '100', '--listen', '0.0.0.0:8082',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
    timeline('model-revision-up', container
             + ' running the revised model')
    return dict(info, container=container)


def start_foreign_controller(cfg, record, run_dir, model, active,
                             timeline):
    """The scenario-callable checkpoint-negotiation action
    (WW-LCM-001's named rejection of incompatible state): derive the
    same recipe-revised document the model-revision case rolls in, then
    launch the run's labeled foreign peer on it as `--standby <active>`
    WITHOUT `--revised` — so its fingerprint gate must refuse every
    checkpoint the active serves and it can never converge.

    `active` is the scenario ctx key of the peer currently writing the
    field ('active' is ctrl-a, 'standby' ctrl-b) — the foreign peer
    pulls that peer's checkpoints. The container carries the run's
    managed and run labels so teardown reconciles it with the rest of
    the rig, mounts the derived document read-only at
    /model/foreign.json, publishes its monitor on cfg['foreign_port'],
    and gets its own runner-owned state/journal directory — the derived
    document deliberately never shares the revision case's 'c' paths,
    so a surviving artifact can never seed that launch. Both halves —
    the derivation and the launch — are recorded on the run's action
    timeline; a derivation or docker failure raises so the calling
    scenario reports the action never completed.

    Returns the derivation summary plus the container name.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = {'active': ('a', 8080), 'standby': ('b', 8081)}
    if active not in peers:
        raise RuntimeError('start_foreign expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name, peer_port = peers[active]
    foreign_doc = Path(run_dir) / 'model-foreign.json'
    info = revision.derive_revised_model(model, foreign_doc)
    directory = _controller_dir(run_dir, 'foreign')
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    container = prefix + '-foreign'
    standby = prefix + '-' + peer_name + ':' + str(peer_port)
    owner_token = _plant_owner_tokens(cfg)['foreign']
    timeline('negotiation-start',
             'derive ' + foreign_doc.name + '; launch ' + container
             + ' --standby ' + standby + ' (no --revised)'
             + ' --owner-token ' + str(owner_token))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(cfg['foreign_port']) + ':8082',
           '-v', str(foreign_doc) + ':/model/foreign.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           '/model/foreign.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--owner-token', str(owner_token),
           '--standby', standby,
           '--scan-ms', '100', '--listen', '0.0.0.0:8082',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
    timeline('negotiation-up', container
             + ' running a foreign-fingerprint model')
    return dict(info, container=container)


def stop_foreign_controller(run_id, timeline):
    """The checkpoint-negotiation case's teardown: `docker rm -f` on
    the foreign peer's container — removed outright, not held down, so
    later cases (the model-revision launch above all) see a clean rig.
    Recorded on the run's action timeline like the other lifecycle
    actions; a docker failure raises so the calling scenario reports
    the teardown never completed."""
    container = 'dcs-hw-' + run_id + '-foreign'
    timeline('negotiation-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, timeout=90)
    timeline('negotiation-stopped', container + ' removed')


def start_driven_controller(cfg, record, run_dir, model, active,
                            timeline):
    """The scenario-callable driven-standby launch — the
    dead-peer-latency case's second survivor: the run's labeled
    driven controller on the same mounted model, `--standby <peer>
    --driven`, so every checkpoint pull it ever performs happens
    inside a `POST /scan` request — the per-request pull chain a
    batched scan carries, and the work the serve-pool decision
    confines to the batch's own worker. Fresh and never driven, it
    reports `unsynchronized` — the convergence-grace clock the pair
    health surface reads.

    `active` is the scenario ctx key of the peer the driven standby
    tracks ('active' is ctrl-a, 'standby' ctrl-b) — the checkpoint
    source the case's stop induction then makes unreachable. The
    container carries the run's managed and run labels so teardown
    reconciles it with the rest of the rig, mounts the run's model
    read-only at /model/plant.json, publishes its monitor on
    cfg['driven_port'], and gets its own runner-owned state/journal
    directory. The launch is recorded on the run's action timeline; a
    docker failure raises so the calling scenario reports the action
    never completed.

    Returns the launched container's name.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = {'active': ('a', 8080), 'standby': ('b', 8081)}
    if active not in peers:
        raise RuntimeError('start_driven expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name, peer_port = peers[active]
    directory = _controller_dir(run_dir, 'd')
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    container = prefix + '-d'
    standby = prefix + '-' + peer_name + ':' + str(peer_port)
    owner_token = _plant_owner_tokens(cfg)['driven']
    timeline('driven-start', 'launch ' + container + ' --standby '
             + standby + ' --driven --owner-token '
             + str(owner_token))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(cfg['driven_port']) + ':8082',
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           '/model/plant.json',
           '--remote', prefix + '-plant:' + str(cfg['plant_port']),
           '--owner-token', str(owner_token),
           '--standby', standby,
           '--driven', '--listen', '0.0.0.0:8082',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
    timeline('driven-up', container + ' serving a driven standby')
    return {'container': container}


def stop_driven_controller(run_id, timeline):
    """The dead-peer-latency case's teardown: `docker rm -f` on the
    driven peer's container — removed outright, not held down, so
    later cases see the rig's original pair. Recorded on the run's
    action timeline like the other lifecycle actions; a docker
    failure raises so the calling scenario reports the teardown
    never completed."""
    container = 'dcs-hw-' + run_id + '-d'
    timeline('driven-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, timeout=90)
    timeline('driven-stopped', container + ' removed')


def _scenario_ctx(cfg, record, src, run_dir, evidence_dir, deadline,
                  timeline):
    """The scenario driver's view of the running rig: monitor base URLs
    per endpoint key (the model-revision case's third controller
    answers on 'revised' once launched, the checkpoint-negotiation
    case's foreign peer on 'foreign', the dead-peer-latency case's
    driven standby on 'driven'), the published plant-protocol
    endpoint, the run config's pinned plant-writer owner token per
    endpoint key — the pair's and every third peer's — the run's
    evidence dir and deadline, the runner-owned
    controller restart/cold-restart, plant stop/start,
    model-revision, foreign-peer launch/teardown, and driven-peer
    launch/teardown actions, the shipped plant tool's docker-exec
    invocation, the run config's recorded endpoint placements and the
    run's rig bridge name — the placement rule a scenario attachment
    follows when it needs an endpoint a rig peer must dial — and the
    host-side
    per-controller state/journal files the restart and model-revision
    scenarios read."""
    run_id = record['run_id']
    names = {'active': 'a', 'standby': 'b', 'revised': 'c',
             'foreign': 'foreign', 'driven': 'd'}
    return {
        'active': 'http://127.0.0.1:' + str(cfg['active_port']),
        'standby': 'http://127.0.0.1:' + str(cfg['standby_port']),
        'revised': 'http://127.0.0.1:' + str(cfg['revised_port']),
        'foreign': 'http://127.0.0.1:' + str(cfg['foreign_port']),
        'driven': 'http://127.0.0.1:' + str(cfg['driven_port']),
        'plant': '127.0.0.1:' + str(cfg['plant_host_port']),
        # The run config's pinned --owner-token per endpoint key: a
        # scenario attachment ensures the writer claim under the
        # active's token to drive plant stimuli on the designed
        # shared-claim path.
        'plant_owner': dict(_plant_owner_tokens(cfg)),
        # The run config's recorded endpoint placements — the rig
        # bridge-to-host reachability rule the scenario attachments
        # follow: host-side attachments dial 'loopback' endpoints on
        # their published 127.0.0.1 ports; a 'bridge' endpoint a rig
        # peer must reach runs in a labeled container on
        # ctx['rig_network'] — a host socket is unreachable from the
        # rig bridge, so no rig-dialed endpoint may live on the host.
        'endpoint_placement': dict(_endpoint_placement(cfg)),
        'rig_network': 'dcs-hwtest-' + run_id,
        'evidence_dir': evidence_dir,
        'deadline': deadline,
        'restart_controller': lambda name: restart_controller(
            run_id, name, timeline),
        'cold_restart_controller': lambda name: cold_restart_controller(
            run_id, run_dir, name, timeline),
        'stop_controller': lambda name: stop_controller(
            run_id, name, timeline),
        'start_controller': lambda name: start_controller(
            run_id, name, timeline),
        'failover_misses': cfg['failover_misses'],
        'stop_plant': lambda: stop_plant(run_id, timeline),
        'start_plant': lambda: start_plant(run_id, timeline),
        # The shipped dcs-plant-ctl inside the plant container — the
        # lane's seam for every plant op the tool's subcommands cover.
        'plant_ctl': lambda *args: plant_ctl(
            run_id, cfg['plant_port'], *args),
        'start_revised': lambda name, incompatible=False:
            start_revised_controller(
                cfg, record, run_dir, src / cfg['model_fixture'],
                name, timeline, incompatible),
        'start_foreign': lambda name: start_foreign_controller(
            cfg, record, run_dir, src / cfg['model_fixture'], name,
            timeline),
        'stop_foreign': lambda: stop_foreign_controller(
            run_id, timeline),
        'start_driven': lambda name: start_driven_controller(
            cfg, record, run_dir, src / cfg['model_fixture'], name,
            timeline),
        'stop_driven': lambda: stop_driven_controller(
            run_id, timeline),
        'state_files': {key: str(_controller_dir(run_dir, peer)
                                 / 'state.json')
                        for key, peer in names.items()},
        'journal_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'journal.jsonl')
                          for key, peer in names.items()},
        'dcs_ctl': str(_dcs_ctl_path(cfg)),
    }


def _start_rig(cfg, record, src, run_dir, timeline):
    """Start the plant plus redundant pair on a dedicated labeled bridge."""
    run_id, sha = record['run_id'], record['attempted_sha']
    net = 'dcs-hwtest-' + run_id
    prefix = 'dcs-hw-' + run_id
    tokens = _plant_owner_tokens(cfg)
    placements = _endpoint_placement(cfg)
    # The endpoints this launch publishes on host loopback must be
    # recorded 'loopback' — a config describing them 'bridge' claims
    # a rig this launch does not build.
    for key in ('active', 'standby', 'plant'):
        if placements[key] != 'loopback':
            raise RuntimeError('endpoint_placement records ' + key
                               + ' as ' + repr(placements[key])
                               + ' but the rig publishes it on host '
                               'loopback')
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
    # The bridge-to-host half of that policy bounds endpoint
    # placement: its INPUT drop refuses every packet a rig container
    # aims at a host socket, so an endpoint a rig peer must dial —
    # the tracking-source/auth legs' forge or interposer, a
    # plant-probe listener — runs bridge-placed in a labeled
    # container on this network, dialed by container name (the
    # recorded endpoint_placement selection, validated above), while
    # host-side scenario attachments only ever dial the
    # 127.0.0.1-published ports.
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
           '--owner-token', str(tokens['active']),
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
           '--owner-token', str(tokens['standby']),
           '--standby', prefix + '-a:8080',
           '--auto-promote', str(cfg['failover_misses']),
           '--scan-ms', '100', '--listen', '0.0.0.0:8081',
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE)
    timeline('rig-up', 'plant + controller pair on ' + net
             + ' (owner tokens active=' + str(tokens['active'])
             + ', standby=' + str(tokens['standby']) + ')')


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
                    results, infra, events, log=print, verifications=None,
                    mode=None, exploration=None):
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
    if mode is not None:
        report_doc['mode'] = mode
    if exploration is not None:
        report_doc['exploration'] = exploration
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
            ctx = _scenario_ctx(cfg, record, src, run_dir,
                                evidence_dir, deadline, timeline)
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
        from . import explorer, verify
        runs = st.runs()
        try:
            storage = qa_storage_usage(cfg)
            storage['bound'] = cfg['qa_storage_max_bytes']
        except Exception as exc:
            storage = {'error': str(exc)[:300]}
        day = _utcnow().strftime('%Y-%m-%d')
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
            'exploration': {
                'enabled': bool(cfg.get('exploration_enabled')),
                'devin': explorer.resolve_devin(cfg),
                'today': st.started_today(day, explorer.RUN_PREFIX),
                'ledger': len(explorer.ledger(st)),
            },
        }
    finally:
        st.close()
