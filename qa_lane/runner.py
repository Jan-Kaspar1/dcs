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
import ctypes
import ctypes.util
import json
import os
import platform
import shutil
import socket
import stat
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
                           'driven': 424247,
                           # The staged probe pair's pins — each a
                           # distinct claim owner, never sharing the
                           # deployed pair's tokens.
                           'probe_active': 424248,
                           'probe_standby': 424249,
                           'probe_driven': 424250},
    # The pair's shared tracking secret: the announced-source contract
    # is keyed-only, so the rig's redundant pair — the revised peer a
    # model-revision roll promotes, and the driven peer a stale-island
    # leg promotes — carries the same --pair-token,
    # letting a demoted owner's verify pull demand the keyed line_proof
    # only a peer holding the token stamps. A scenario endpoint that
    # merely replays or forges the line's public checkpoints — the
    # bridge-placed forge or interposer — arms nothing in its default
    # tokenless posture; the forged-standby leg also launches the same
    # endpoint keyed so its answers are genuinely signed and only the
    # pulled document's content can convict it — the shape that reaches
    # the demote verify's command-record audit. The labeled foreign
    # peer stays unkeyed on purpose. This pin is the DEPLOYED pair's
    # posture: null/empty runs the pair unkeyed — the posture split the
    # lane-staged probe pair beside it exists to cover.
    'pair_token': 'dcs-qa-pair',
    # The lane-staged keyed probe pair (#1058): the keyed
    # announced-source contract needs per-revision exercise even where
    # the run config deploys its redundant pair unkeyed — the
    # qax-20260926-002 run evidenced the gap: 2050's keyed halves and
    # 2060's keyed precondition kept reporting inconclusive on absent
    # capability rather than on the contract. So every rig also stages
    # a second, always-keyed redundant pair: its own sim-serve plant —
    # bridge-placed, its own declared dynamics, its own field (field
    # ownership arbitration is per-plant, so the probe pair never
    # touches the deployed pair's field or claim tokens) — plus two
    # controllers sharing this block's --pair-token, bound to that
    # plant, tracking each other with line_proof verification on. The
    # keyed legs select it through ctx['probe'] whenever the deployed
    # pair carries no token; 'probe_pair': null stages none and the
    # keyed legs fall back to the absent-capability inconclusive.
    'probe_pair': {
        'pair_token': 'dcs-qa-pair',
        # The probe monitors' published host-loopback ports — the
        # keyed legs' host-side attachment surface — and the probe
        # driven peer's publish for the island leg.
        'active_port': 18085,
        'standby_port': 18086,
        'driven_port': 18087,
        # The probe plant's sim-serve listener on the rig bridge —
        # bridge-placed (no host publish): only the probe pair's
        # rig-dialed --remote and plant-ctl attachments reach it.
        'plant_port': 9002,
        'model_fixture': 'crates/dcs-demo/fixtures/pump_station.json',
        'dynamics_fixture': 'qa_lane/fixtures/probe_dynamics.json',
    },
    # The register-mapped field rig (#1355): a `dcs-sim-bus-device`
    # server plus a controller pair bound to it, so the sim-bus
    # driver's own recovery contract is exercised on the rig rather
    # than only through the deployed pair's sim-tcp link — the two
    # drivers implement their recovery independently, so a rig that
    # stages no register device can say nothing about the point-wise
    # one. The pair attaches to no sim-tcp plant at all: its field is
    # the device's register bank, and the bus device's claim
    # arbitration is per-device, so this rig never touches the
    # deployed pair's plant, field, or claim tokens. The device is
    # bridge-placed and rig-dialed by container name; the pair's two
    # monitors publish on host loopback. 'bus_rig': null stages none
    # and the driver-reattach leg reports inconclusive rather than
    # reading another link's health as this one's.
    'bus_rig': {
        'pair_token': 'dcs-qa-bus',
        'active_port': 18088,
        'standby_port': 18089,
        # The device server's register-protocol listener on the rig
        # bridge — bridge-placed, so no host port publishes it: only
        # the bus pair's drivers and the device-tool exec reach it.
        'device_port': 9010,
        # The register-mapped document the rig serves and the pair
        # assembles. Its `sim-bus` device's `address` and
        # `timeout_ms` are stamped onto the derived copy the rig
        # writes — the placeholder address is the fixture's, never a
        # routable one.
        'model_fixture': 'crates/dcs-assembly/fixtures/mixed_bus.json',
        'device_id': 2,
        # The per-request timeout the rig's drivers hold: under the
        # stall the reattach leg freezes (~2 s), so a frozen device
        # times out mid-exchange and the driver has a failed exchange
        # to recover from — the transient stall the finding records.
        # Without it the stall would sit inside the driver's five-
        # second default and no boundary would fault at all.
        'timeout_ms': 1500,
        # The pair's own --owner-token pins, distinct from every
        # other endpoint's: one controller per token, or the device's
        # single-writer claim could not tell the pair's members apart.
        'owner_tokens': {'active': 424251, 'standby': 424252},
    },
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
        'interposer': 'bridge', 'forge': 'bridge',
        # The probe pair's endpoints: its monitors publish on host
        # loopback like the deployed pair's; its plant is rig-dialed
        # only (bridge) — no host-side attachment exists.
        'probe_active': 'loopback', 'probe_standby': 'loopback',
        'probe_driven': 'loopback', 'probe_plant': 'bridge',
        # The register-mapped rig's endpoints: its pair's monitors
        # publish on host loopback like the deployed pair's; the
        # device server is rig-dialed only (bridge).
        'bus_active': 'loopback', 'bus_standby': 'loopback',
        'bus_device': 'bridge'},
    # The sink-isolation leg's declared impede lever (#999): the
    # controller endpoints whose --state-file mount the runner may
    # stall, each naming the staged-target kind. 'fifo' parks a
    # reader-less FIFO at the sink's write-then-rename temporary
    # sibling of state.json so the drain writer's next open() blocks
    # inside the mount while the scan loop's captures pile into the
    # bounded queue — the impeded mount the isolation contract must
    # absorb — until restore attaches a host reader and the pending
    # write completes. A run config that omits the map leaves the
    # lever unavailable and the leg reports inconclusive rather than
    # probing a mount it was never granted.
    'state_file_mounts': {'active': 'fifo', 'standby': 'fifo'},
    'model_fixture': 'crates/dcs-demo/fixtures/pump_station.json',
    # The lane's own dynamics declaration: the shared fixture leaves
    # the inflow channel to scripted forcing, while the unattended rig
    # needs the declared inflow so the station cycles demand on its own
    # — the duty-rotation case's honest lever.
    'dynamics_fixture': 'qa_lane/fixtures/pump_station_dynamics.json',
    # The foreign-model correspondence leg's staging fixtures (#1309):
    # the born legs' scratch field can instead serve a DIFFERENT model —
    # the dosing skid shares the pump station's low channel ids but
    # declares nothing at 120 and different kinds on shared ids — so a
    # miswired --remote meets the declared-point correspondence refusal
    # the #1302 fix records rather than owning a foreign plant.
    'foreign_model_fixture': 'crates/dcs-demo/fixtures/dosing_skid.json',
    'foreign_dynamics_fixture':
        'crates/dcs-demo/fixtures/dosing_skid_dynamics.json',
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
            '-p dcs-sim-bus '
            '&& cargo build --release --locked '
            '-p dcs-monitor --bin dcs-ctl --bin dcs-forge',
            timeout=cfg['builder_timeout'])
    # Extra binaries each image ships beside its entrypoint: the plant
    # image carries dcs-plant-ctl — the plant-side tool the lane execs
    # inside the container against the server's loopback listener, so
    # the covered plant ops run through the shipped binary rather than
    # a second Python implementation of the wire protocol — plus the
    # register device's own server binary and its field tool, the pair
    # the sim-bus driver-reattach rig stages and the tool the leg
    # reads the device's census through. The controller image carries
    # dcs-forge — the announced-source legs' bridge-placed checkpoint
    # endpoint the runner launches with --entrypoint dcs-forge.
    ship = {'plant': ['dcs-plant-ctl', 'dcs-sim-bus-device',
                      'dcs-sim-bus-ctl'],
            'controller': ['dcs-forge']}
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


def _controller_argv(cfg, pair, name, prefix,
                     model='/model/plant.json', standby=None,
                     track=None, revised=False, driven=False,
                     keyed=True):
    """The complete dcs-controller argv a rig launch hands to docker
    run — the single assembly point every controller launch routes
    through, so the launch contract cannot drift between the sites
    that build it.

    `pair` and `name` name the endpoint the launch serves: the pair's
    --remote plant address (_pair_plant_remote), the endpoint's
    --owner-token pin (_pair_owner_key), the in-container monitor
    port (PAIR_MONITOR_PORTS for the pair's own members,
    DRIVEN_MONITOR_PORT for a third controller), and the CONTAINER_*
    persistence trio all follow from them. `name='standby'` launches
    the pair's tracking member: its --standby target defaults to the
    pair's active and carries the --auto-promote failover budget.

    The named deltas are what the specialty levers pass: `model`
    overrides the mounted document's in-container path (the revised
    and foreign derivations), `standby` hands a non-member tracker
    its explicit --standby target — no --auto-promote, only the
    pair's own standby promotes — `track` doctors a relaunch's
    tracking wiring (--peer on the launched active, the --standby
    target on the launched standby), `driven` runs the externally
    paced mode — --driven in place of --scan-ms, since a driven
    standby scans only inside POST /scan — `revised` opts the launch
    into the revised model's carryover, and `keyed=False` pins the
    tokenless posture the foreign peer keeps even on a keyed run.
    The pair's --pair-token lands whenever the pair carries one and
    the launch is keyed.
    """
    peers = PAIRS[pair]['peers']
    tokens = _plant_owner_tokens(cfg)
    argv = [model,
            '--remote', _pair_plant_remote(cfg, pair, prefix),
            '--owner-token',
            str(tokens[_pair_owner_key(pair, name)])]
    if name == 'standby':
        argv += ['--standby',
                 track if track is not None else
                 prefix + '-' + peers['active'] + ':'
                 + str(PAIR_MONITOR_PORTS['active']),
                 '--auto-promote', str(cfg['failover_misses'])]
    elif track is not None:
        argv += ['--peer', track]
    if standby is not None:
        argv += ['--standby', standby]
    if revised:
        argv += ['--revised']
    if driven:
        argv += ['--driven']
    else:
        argv += ['--scan-ms', '100']
    argv += ['--listen',
             '0.0.0.0:' + str(PAIR_MONITOR_PORTS.get(
                 name, DRIVEN_MONITOR_PORT)),
             '--state-file', CONTAINER_STATE_FILE,
             '--journal-file', CONTAINER_JOURNAL_FILE,
             '--history-file', CONTAINER_HISTORY_FILE]
    token = _pair_token(cfg, pair) if keyed else None
    if token:
        argv += ['--pair-token', str(token)]
    return argv


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
# Decision 102's durable process-history store: --history-file joins
# the mount like the journal's, on the same per-controller directory
# — the declared-duty sample record a restart replays, inspectable
# host-side for the durable-history leg's file audit.
CONTAINER_HISTORY_FILE = CONTAINER_RUN_DIR + '/history.jsonl'

# The endpoint keys whose controllers the runner launches — the pair
# `_start_rig` brings up, the three scenario-action peers, and the
# lane-staged keyed probe pair's three endpoints.
OWNER_TOKEN_ENDPOINTS = ('active', 'standby', 'revised', 'foreign',
                         'driven', 'probe_active', 'probe_standby',
                         'probe_driven')


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
# successors) and the tracking-source/auth evidence place — and the
# probe pair's four endpoints: its monitors host-published
# ('loopback'), its sim-serve plant rig-dialed only ('bridge').
PLACEMENT_ENDPOINTS = ('active', 'standby', 'revised', 'foreign',
                       'driven', 'plant', 'interposer', 'forge',
                       'probe_active', 'probe_standby', 'probe_driven',
                       'probe_plant', 'bus_active', 'bus_standby',
                       'bus_device')
PLACEMENTS = ('loopback', 'bridge')


# The redundant pairs the runner stages and each one's container
# wiring: 'deployed' is the run config's own pair — keyed or unkeyed
# per its pair_token posture — and 'probe' is the lane-staged
# always-keyed pair on its own sim-serve plant. Each maps the scenario
# ctx's endpoint keys ('active'/'standby') to the containers'
# run-name suffixes; in-container monitor ports repeat across pairs
# (separate netns), so the legs' port constants name both.
PAIRS = {
    'deployed': {'peers': {'active': 'a', 'standby': 'b'},
                 'plant': 'plant', 'driven': 'd'},
    'probe': {'peers': {'active': 'probe-a', 'standby': 'probe-b'},
              'plant': 'probe-plant', 'driven': 'probe-d'},
}
# The --listen ports a pair member's monitor binds inside its
# container — identical across pairs since every container owns its
# netns; the island leg's PAIR_PORTS/DRIVEN_PORT constants name the
# same in-container ports for either subject.
PAIR_MONITOR_PORTS = {'active': 8080, 'standby': 8081}
DRIVEN_MONITOR_PORT = 8082

# The probe pair block's required keys: the staged pair's shared
# --pair-token, its published monitor ports and the driven peer's,
# its plant's bridge-side sim-serve port, and its fixtures.
PROBE_PAIR_KEYS = ('pair_token', 'active_port', 'standby_port',
                   'driven_port', 'plant_port', 'model_fixture',
                   'dynamics_fixture')
PROBE_PAIR_PORTS = ('active_port', 'standby_port', 'driven_port',
                    'plant_port')


def _probe_pair(cfg):
    """The run's lane-staged keyed probe pair spec, or None when the
    run config stages none ('probe_pair' absent or null).

    The probe pair is keyed by definition — its reason for existing
    is exercising the keyed announced-source contract while the
    deployed pair runs whatever posture the run config gives it —
    so a block missing its pair_token fails the launch loudly rather
    than silently staging an unkeyed twin. Every port must be a
    1..65535 int and both fixtures must name paths, same
    fail-before-launch discipline _plant_owner_tokens applies.
    """
    spec = cfg.get('probe_pair')
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise RuntimeError('probe_pair must map the probe pair\'s '
                           'staging keys, or be null to stage none')
    missing = [key for key in PROBE_PAIR_KEYS if key not in spec]
    if missing:
        raise RuntimeError('probe_pair stages no '
                           + ', '.join(missing))
    bad = {key: spec[key] for key in PROBE_PAIR_PORTS
           if not isinstance(spec[key], int)
           or isinstance(spec[key], bool)
           or not 0 < spec[key] <= 65535}
    if bad:
        raise RuntimeError('probe_pair ports must be int 1..65535: '
                           + json.dumps(bad, sort_keys=True))
    if not spec['pair_token']:
        raise RuntimeError('probe_pair stages the KEYED probe pair — '
                           'pair_token must name its shared secret; '
                           'a null probe_pair stages none')
    for key in ('model_fixture', 'dynamics_fixture'):
        if not isinstance(spec[key], str) or not spec[key]:
            raise RuntimeError('probe_pair ' + key
                               + ' must name a fixture path')
    return dict(spec)


def _pair_token(cfg, pair):
    """The --pair-token pair `pair`'s controllers sign under: the
    deployed pair's run-configured token — None while the run config
    runs it unkeyed — or the staged probe pair's always-keyed token."""
    if pair == 'probe':
        probe = _probe_pair(cfg)
        if probe is None:
            raise RuntimeError('the run config stages no probe pair')
        return probe['pair_token']
    return cfg.get('pair_token')


def _pair_host_port(cfg, pair, key):
    """The host-loopback port pair `pair`'s endpoint `key`
    ('active'/'standby'/'driven') publishes its monitor on."""
    if pair == 'probe':
        probe = _probe_pair(cfg)
        if probe is None:
            raise RuntimeError('the run config stages no probe pair')
        return probe[key + '_port']
    return cfg[key + '_port']


def _pair_owner_key(pair, name):
    """The plant_owner_tokens pin pair `pair`'s endpoint `name`
    carries: 'active' on the deployed pair vs 'probe_active' on the
    probe pair — the probe pair never touches the deployed pair's
    claim tokens."""
    return name if pair == 'deployed' else 'probe_' + name


def _pair_plant_remote(cfg, pair, prefix):
    """The --remote sim-serve address pair `pair` binds, as a
    container name on the run's rig bridge: each pair owns its own
    plant — field ownership arbitration is per-plant, so the probe
    pair's plant never shares the deployed pair's field."""
    if pair == 'probe':
        probe = _probe_pair(cfg)
        if probe is None:
            raise RuntimeError('the run config stages no probe pair')
        return prefix + '-probe-plant:' + str(probe['plant_port'])
    return prefix + '-plant:' + str(cfg['plant_port'])


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
    container ('a'/'b'): its --state-file checkpoint, --journal-file
    audit record, and --history-file durable store live here so a
    container restart resumes the same run and the files stay inside
    the bounded run directory."""
    return Path(run_dir) / 'controllers' / name


def _controller_container(run_id, name, pair='deployed'):
    """The run's controller container for a scenario ctx endpoint key
    on pair `pair`: the deployed pair's 'active' is ctrl-a's
    container, 'standby' ctrl-b's; the staged probe pair's 'active'
    is probe-a's, 'standby' probe-b's — whichever role each
    currently reports."""
    return 'dcs-hw-' + run_id + '-' + PAIRS[pair]['peers'][name]


def restart_controller(run_id, name, timeline, pair='deployed'):
    """The scenario-callable controller restart: `docker stop` then
    `docker start` on one of the run's already-launched controller
    containers — the supervisor-owned lifecycle action a scenario
    triggers through ctx['restart_controller'], never a second writer
    to the field.

    `name` is the scenario ctx's endpoint key: 'active' is ctrl-a's
    container, 'standby' ctrl-b's on the deployed pair (the probe
    pair's ctx binds pair='probe' — 'active' is then probe-a's),
    whichever role each currently reports. The container keeps its
    mounts, labels, published port,
    and bridge name, so the restarted process resumes through the same
    --state-file and rejoins the pair unchanged. Both halves are
    recorded on the run's action timeline; a docker failure raises so
    the calling scenario reports the restart never completed.
    """
    container = _controller_container(run_id, name, pair)
    timeline('controller-restart', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    docker('start', container, timeout=60)
    timeline('controller-restarted', container + ' running')


def cold_restart_controller(run_id, run_dir, name, timeline,
                            pair='deployed'):
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
    container and state dir, 'standby' ctrl-b's (the probe pair's
    ctx binds pair='probe', mapping the same keys onto probe-a/
    probe-b), whichever role each
    currently reports. Only the named controller's state.json is
    removed, and only inside this run's bounded directory. Both
    docker halves are recorded on the run's action timeline; a docker
    or state-file failure raises so the calling scenario reports the
    cold restart never completed rather than silently performing a
    warm restart.
    """
    container = _controller_container(run_id, name, pair)
    peer = PAIRS[pair]['peers'][name]
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


def relaunch_controller(cfg, record, run_dir, model, name, timeline,
                        track=None, pair='deployed'):
    """The scenario-callable flag-doctoring relaunch: `docker rm -f`
    on the pair member's container, then a fresh `docker run`
    rebuilding the member's launch through the same _controller_argv
    spec the rig's launches assemble — same image, model and
    state/journal mounts, published monitor port, owner-token pin,
    failover budget, and pair token — with the tracking-source
    argument optionally doctored. Where `docker start` can only rerun
    the command the container was created with, this lever rewrites
    it: `track` becomes `--peer track` on the launched active — the
    argument through which a field owner names the peer it tracks if
    demoted, the "--standby name" an owning run carries — and
    replaces `--standby`'s target on the launched standby. track=None
    recreates the launch command unchanged: the restore half of a
    doctored pass, constructionally identical to the member's initial
    launch so a flag added to the spec cannot drop here, since no
    start can un-apply a flag a recreate added.

    `name` is the scenario ctx's endpoint key ('active' is ctrl-a,
    'standby' ctrl-b on the deployed pair; the probe pair's ctx maps
    the same keys onto probe-a/probe-b), whichever role each
    currently reports. The recreated container keeps the run's
    managed and run labels so teardown reconciles it, the same
    runner-owned state/journal directory so the process resumes its
    persisted run, and --restart no. An unresolvable `track` name is
    the standby-dns-resume leg's induction: the tracking source
    degrades to pull misses per the deferred-resolution contract —
    never a startup error. The remove tolerates an already-absent
    container so a relaunch interrupted mid-flight can be re-driven;
    the run half still raises on a docker failure so the calling
    scenario reports the relaunch never completed. Both halves are
    recorded on the run's action timeline.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = PAIRS[pair]['peers']
    if name not in peers:
        raise RuntimeError('relaunch_controller expects an endpoint '
                           'key, got ' + repr(name))
    peer = peers[name]
    container = prefix + '-' + peer
    monitor_port = PAIR_MONITOR_PORTS[name]
    command = _controller_argv(cfg, pair, name, prefix, track=track)
    flag = '' if track is None else (
        ('--peer ' if name == 'active' else '--standby ') + track)
    timeline('controller-relaunch', 'docker rm -f ' + container
             + ('; launch ' + flag if flag
                else '; launch flags restored'))
    removed = docker('rm', '-f', container, check=False, timeout=90)
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(_pair_host_port(cfg, pair, name))
           + ':' + str(monitor_port),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, peer))
           + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *command)
    timeline('controller-relaunched', container + ' running'
             + (' with ' + flag if flag else ' with its launch flags')
             + ('' if removed.returncode == 0
                else ' (previous container already absent)'))


def _container_bridge_address(container):
    """The container's current rig-bridge IPv4 — docker inspect over
    the single network every run container joins. Raises on a missing
    container or an empty answer so the calling scenario reports the
    probe never completed rather than staging against a blank
    address."""
    result = docker('inspect', '--format',
                    '{{range .NetworkSettings.Networks}}'
                    '{{.IPAddress}}{{end}}', container, timeout=30)
    address = result.stdout.strip()
    if not address:
        raise RuntimeError('no bridge address on ' + container)
    return address


def _address_placeholder(run_id, pair):
    """The address-move placeholder's container name — one per pair,
    launched with the run's managed and run labels so teardown
    reconciles it even when a pass aborts before
    release_address_placeholder runs."""
    return 'dcs-hw-' + run_id + ('-placeholder' if pair == 'deployed'
                                 else '-probe-placeholder')


def move_controller_address(cfg, record, run_dir, model, name,
                            timeline, pair='deployed'):
    """The scenario-callable address-move staging — the
    track-source-rediscovery leg's reproduction of the
    standby-track-source-stale-ip-pin finding's deployment shape:
    remove the pair member's container, hold its freed bridge
    address with a placeholder container on the rig network, and
    recreate the controller with its launch flags so IPAM assigns a
    different bridge address while the container name — the peer's
    configured DNS tracking source — resolves onward.

    `name` is the scenario ctx's endpoint key ('active' is ctrl-a's
    container, 'standby' ctrl-b's on the deployed pair; the probe
    pair's ctx maps the same keys onto probe-a/probe-b). The
    placeholder is a sleeping container pinned to the freed address
    through `--ip`, carrying the run's managed and run labels; the
    recreate rides relaunch_controller so the launch command is
    byte-identical (flags restored, not doctored). A recreate
    failure removes the placeholder again so an aborted move leaves
    no held address behind. Returns the recorded staging evidence
    {'container', 'placeholder', 'old_address', 'new_address'} —
    old and new differ by construction since the placeholder holds
    the old one. Each step is recorded on the run's action
    timeline; a docker failure raises so the calling scenario
    reports the move never completed.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    container = _controller_container(run_id, name, pair)
    net = 'dcs-hwtest-' + run_id
    placeholder = _address_placeholder(run_id, pair)
    old_address = _container_bridge_address(container)
    timeline('controller-move', 'docker rm -f ' + container
             + '; hold ' + old_address + ' on ' + placeholder)
    docker('rm', '-f', container, timeout=90)
    docker('rm', '-f', placeholder, check=False, timeout=60)
    try:
        docker(*_docker_run_args(cfg, run_id, placeholder),
               '--network', net, '--ip', old_address,
               '--entrypoint', 'sleep',
               IMAGE_PREFIX + 'controller:' + sha, 'infinity')
        relaunch_controller(cfg, record, run_dir, model, name,
                            timeline, pair=pair)
    except Exception:
        # The recreate half failed — the move is incomplete: free
        # the placeholder so a re-driven move starts from a released
        # address rather than wedging it.
        docker('rm', '-f', placeholder, check=False, timeout=60)
        raise
    new_address = _container_bridge_address(container)
    timeline('controller-moved', container + ' moved ' + old_address
             + ' -> ' + new_address + ' (' + placeholder
             + ' holds the old address)')
    return {'container': container, 'placeholder': placeholder,
            'old_address': old_address, 'new_address': new_address}


def release_address_placeholder(run_id, timeline, pair='deployed'):
    """Free the bridge address move_controller_address's placeholder
    holds — `docker rm -f` on the placeholder container. Tolerates an
    already-absent placeholder so an aborted pass's cleanup can be
    re-driven; recorded on the run's action timeline like the other
    lifecycle actions."""
    container = _address_placeholder(run_id, pair)
    timeline('address-release', 'docker rm -f ' + container)
    result = docker('rm', '-f', container, check=False, timeout=60)
    timeline('address-released', container
             + (' removed' if result.returncode == 0
                else ' already absent'))


# The --state-file sink's write-then-rename temporary sibling
# (dcs-monitor's write_state_file writes '<state>.tmp' beside the
# state file, then renames it into place): the path the 'fifo' mount
# lever stages its stall on. Blocking that open leaves state.json
# itself untouched and stalls exactly the writer thread the
# sink-isolation contract isolates behind its bounded queue.
STATE_FILE_TMP = 'state.json.tmp'

# The staged-target kinds _state_file_mounts declarations may name.
STATE_FILE_LEVERS = ('fifo',)

# The restore bounds: STATE_FILE_ATTACH_GRACE is how long the host
# reader waits for a writer to pair a blocked open() before concluding
# no capture is in flight on the staged node (a stalled drain's writer
# attaches within one scan; a dead sink never does — the orphaned node
# is unlinked so the mount frees), and STATE_FILE_BOUND is the bound on
# a paired write completing and on an ordinary state.json returning.
STATE_FILE_ATTACH_GRACE = 3
STATE_FILE_BOUND = 15


def _state_file_mounts(cfg):
    """The run's declared per-endpoint --state-file impede mounts —
    the 'throttled or stalled mount target the run config declares'
    the sink-isolation leg's lever is built from.

    The map names each controller endpoint's stall kind: the only
    declared kind, 'fifo', stages a reader-less FIFO at the sink's
    write-then-rename temporary path inside the controller's
    runner-owned bind mount, so the drain writer blocks on the mount
    while captures queue — never on the scan's lock. An absent or
    empty map leaves the lever unavailable (the leg reports
    inconclusive rather than probing a mount it was never granted);
    an unknown endpoint or kind fails the launch loudly, same as a
    duplicated owner token.
    """
    mounts = cfg.get('state_file_mounts') or {}
    if not isinstance(mounts, dict):
        raise RuntimeError('state_file_mounts must map endpoint keys '
                           'to mount kinds')
    bad = {key: kind for key, kind in mounts.items()
           if key not in OWNER_TOKEN_ENDPOINTS
           or kind not in STATE_FILE_LEVERS}
    if bad:
        raise RuntimeError('state_file_mounts entries must name a '
                           'controller endpoint and a kind in '
                           + json.dumps(list(STATE_FILE_LEVERS)) + ': '
                           + json.dumps(bad, sort_keys=True))
    return dict(mounts)


def _is_fifo(path):
    try:
        return stat.S_ISFIFO(os.lstat(path).st_mode)
    except OSError:
        return False


def impede_state_file(run_id, run_dir, name, timeline, mounts,
                      pair='deployed'):
    """Stall `name`'s --state-file mount for the sink-isolation leg:
    stage a reader-less FIFO at the sink's write-then-rename
    temporary path inside the controller's runner-owned bind mount.

    `name` is the scenario ctx's endpoint key ('active' is ctrl-a's
    container and state dir, 'standby' ctrl-b's — the probe pair's
    ctx binds pair='probe', naming probe endpoints whose declared
    mounts map their 'probe_' keys); `mounts` is the
    run's validated _state_file_mounts map — an endpoint the config
    never declared has no lever, and the action raises so the
    scenario reports the mount lever unavailable rather than staging
    an ungranted stall. With the FIFO standing, the drain writer's
    next File::create on the temporary path blocks inside open()
    until a reader pairs: captures keep queueing behind the bounded
    handoff, the scan never waits, and publication.state_sink walks
    healthy -> lagging. restore_state_file pairs the reader. A
    capture's regular tmp already in flight clears on its own rename
    before the FIFO stages; an in-place FIFO makes the call
    idempotent. The FIFO is staged world-writable because the
    containerized writer runs uid 10001.
    """
    key = _pair_owner_key(pair, name)
    if key not in mounts:
        raise RuntimeError('the run config declares no state-file '
                           'mount lever for endpoint ' + key)
    container = _controller_container(run_id, name, pair)
    peer = PAIRS[pair]['peers'][name]
    tmp = _controller_dir(run_dir, peer) / STATE_FILE_TMP
    timeline('state-file-impede', 'stall ' + str(tmp) + ' for '
             + container + ' (' + mounts[key] + ')')
    deadline = time.monotonic() + 10
    while True:
        try:
            os.mkfifo(tmp)
            break
        except FileExistsError:
            if _is_fifo(tmp):
                break  # the stall is already staged
            # A capture's write is in flight on a regular tmp: its
            # rename clears the path — retry until it does.
            if time.monotonic() > deadline:
                raise RuntimeError('a regular ' + tmp.name
                                   + ' never cleared for '
                                   + container)
            time.sleep(0.01)
    os.chmod(tmp, 0o666)
    timeline('state-file-impeded', container
             + ' --state-file mount stalled')


def restore_state_file(run_id, run_dir, name, timeline,
                       pair='deployed'):
    """Release the stall impede_state_file staged on `name`: attach a
    host-side reader to the staged FIFO so the drain writer's pending
    open() pairs, drain its bytes until the write's close+rename
    carries the FIFO onto the state path, then wait until a capture's
    regular temporary write has made state.json an ordinary file
    again.

    Holding the read end pairs every writer open on the node — even a
    write that attached after the FIFO staged — so the mount heals
    without touching container or file identity. A readerless node
    whose grace lapses (a stalled drain's writer attaches within a
    scan; only a dead sink never does) is unlinked so the mount frees
    — the orphaned write, if any is still mid-open on the node, keeps
    its pairing through our held read end. Raises when nothing was
    staged or a bounded wait lapses.
    """
    container = _controller_container(run_id, name, pair)
    peer = PAIRS[pair]['peers'][name]
    directory = _controller_dir(run_dir, peer)
    tmp = directory / STATE_FILE_TMP
    if not _is_fifo(tmp):
        raise RuntimeError('no staged state-file stall for '
                           + container)
    timeline('state-file-restore', 'release ' + str(tmp) + ' for '
             + container)
    fd = os.open(tmp, os.O_RDONLY | os.O_NONBLOCK)
    try:
        attached = False
        bound = time.monotonic() + STATE_FILE_BOUND
        grace = time.monotonic() + STATE_FILE_ATTACH_GRACE
        while os.path.lexists(tmp):
            try:
                if os.read(fd, 1 << 16):
                    attached = True
            except BlockingIOError:
                attached = True   # a writer holds the node open
            if attached:
                if time.monotonic() > bound:
                    raise RuntimeError('the stalled state-file write '
                                       'never completed for '
                                       + container)
            elif time.monotonic() > grace:
                tmp.unlink(missing_ok=True)
                break
            else:
                time.sleep(0.005)
    finally:
        os.close(fd)
    # The renamed FIFO now answers as state.json until the next
    # capture's regular temporary replaces it — the mount is restored
    # once an ordinary file stands again.
    state = directory / 'state.json'
    bound = time.monotonic() + STATE_FILE_BOUND
    while not (os.path.isfile(state) and not _is_fifo(state)):
        if time.monotonic() > bound:
            raise RuntimeError('state.json never returned to a '
                               'regular file for ' + container)
        time.sleep(0.01)
    timeline('state-file-restored', container
             + ' --state-file mount released')


# The sink-stall lever the append-mode sinks take where the state
# file's mount trick cannot reach them: a --journal-file or
# --history-file writer opens its path once at bind and appends
# through the held descriptor, so no staged node can ever park a
# mid-run write — the reference plant's journal sink-isolation leg
# parks the drain writer thread itself instead, and the deployed rig
# reaches the same threads through the container's host pid and the
# /proc task surface. `dcs-drain` is every sink writer's comm: the
# legs tell the journal, state-file, and history writers apart by
# observable effect — which sink's depth grows while a candidate
# stands parked — so the lever hands back the candidate set, never a
# verdict.
PTRACE_ATTACH = 16
PTRACE_DETACH = 17
DRAIN_WRITER_COMM = 'dcs-drain'
# Bounds on the lever's own moves: an attached writer reports its
# tracing-stop and a detached one its resume inside this window.
DRAIN_STOP_BOUND = 5


def _tracer():
    """The ptrace surface the drain-stall lever needs — libc loaded
    lazily so the module imports clean where the call is unavailable.
    Raises OSError/AttributeError when ctypes or ptrace cannot be
    bound; the ctx gate turns that into the lever's absence."""
    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6',
                       use_errno=True)
    libc.ptrace.restype = ctypes.c_long
    libc.ptrace.argtypes = [ctypes.c_int, ctypes.c_int,
                            ctypes.c_void_p, ctypes.c_void_p]
    return libc


def _ptrace(libc, request, tid):
    """One ptrace call against thread `tid`, raising OSError on the
    kernel's refusal — an undumpable or foreign-uid task surfaces here
    as EPERM, the lever-absent condition the leg reports inconclusive."""
    if libc.ptrace(request, tid, None, None) == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _thread_state(tid):
    """The thread's one-letter state from /proc/<tid>/stat — 't' is
    the tracing-stop an attached writer reports."""
    with open('/proc/' + str(tid) + '/stat') as handle:
        return handle.read().rsplit(')', 1)[1].split()[0]


def drain_writers(run_id, name, pair='deployed'):
    """The host tids of the named controller container's `dcs-drain`
    writer threads — the sink-stall lever's park candidates, in spawn
    order. `name` is the scenario ctx's endpoint key on pair `pair`,
    resolved to its container like the lifecycle actions'."""
    container = _controller_container(run_id, name, pair)
    result = docker('inspect', '--format', '{{.State.Pid}}', container)
    pid = int(result.stdout.strip())
    tids = []
    for entry in os.listdir('/proc/' + str(pid) + '/task'):
        try:
            with open('/proc/' + str(pid) + '/task/' + entry
                      + '/comm') as handle:
                comm = handle.read().strip()
        except (OSError, ValueError):
            continue
        if comm == DRAIN_WRITER_COMM:
            tids.append(int(entry))
    return sorted(tids)


def park_drain_writer(tid):
    """Attach drain-writer thread `tid` through ptrace and wait for
    its tracing-stop — the writer parked inside its sink operation,
    the stalled-sink contract the bounded queue owes. Raises OSError
    on a refused attach and RuntimeError when the stop never reports;
    the parking leg treats both as the lever being absent, not a rig
    defect."""
    libc = _tracer()
    _ptrace(libc, PTRACE_ATTACH, tid)
    deadline = time.monotonic() + DRAIN_STOP_BOUND
    while time.monotonic() < deadline:
        try:
            if _thread_state(tid) == 't':
                return
        except OSError:
            break
        time.sleep(0.01)
    try:
        _ptrace(libc, PTRACE_DETACH, tid)
    except OSError:
        pass
    raise RuntimeError('the attached drain writer ' + str(tid)
                       + ' never reported tracing-stop')


def release_drain_writer(tid):
    """Detach a parked drain writer — the stall's restore: the writer
    resumes its drain and the standing queue appends in push order.
    A gone thread — the container died under the stall — is already
    released."""
    libc = _tracer()
    _ptrace(libc, PTRACE_DETACH, tid)
    deadline = time.monotonic() + DRAIN_STOP_BOUND
    while time.monotonic() < deadline:
        try:
            if _thread_state(tid) != 't':
                return
        except OSError:
            return
        time.sleep(0.01)
    raise RuntimeError('the detached drain writer ' + str(tid)
                       + ' never resumed')


def _drain_stall_lever():
    """The scenario ctx's drain-stall lever triple — (drain_writers,
    park, release) — or None where the runner admits no tracer: no
    /proc task surface, no bindable ptrace, or a smoke attach refused.
    The leg reports inconclusive on the absent lever rather than
    probing threads it was never granted."""
    if not os.path.isdir('/proc'):
        return None
    try:
        _tracer()
    except (OSError, AttributeError, ImportError):
        return None
    return (drain_writers, park_drain_writer, release_drain_writer)


def stop_controller(run_id, name, timeline, pair='deployed'):
    """The stop half of the lifecycle action, alone: `docker stop` on
    one of the run's controller containers, held down until the scenario
    issues `start_controller` — the seam the stale-freshness case uses
    to freeze the shared plant's stepping while it polls the surviving
    peer. Recorded on the run's action timeline; a docker failure raises
    so the induction is reported as never completed.
    """
    container = _controller_container(run_id, name, pair)
    timeline('controller-stop', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    timeline('controller-stopped', container + ' down')


def start_controller(run_id, name, timeline, pair='deployed'):
    """The matching start half: `docker start` on a container
    `stop_controller` stopped — the resumed process reclaims the shared
    plant's writer and the tracking peer's checkpoint stream resumes.
    """
    container = _controller_container(run_id, name, pair)
    timeline('controller-start', 'docker start ' + container)
    docker('start', container, timeout=60)
    timeline('controller-started', container + ' running')


def stop_plant(run_id, timeline, pair='deployed'):
    """The scenario-callable plant stop: `docker stop` on the run's
    shared-plant container — the field-loss half of the link-loss
    scenario, severing both controllers' remote-driver connections at
    the same boundary. `pair` selects which pair's plant —
    'deployed' is the primary field, 'probe' the probe pair's
    bridge-placed plant. Recorded on the run's action timeline like the
    controller restart; a docker failure raises so the calling
    scenario reports the stop never completed."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['plant']
    timeline('plant-stop', 'docker stop ' + container)
    docker('stop', '--time', '2', container, timeout=90)
    timeline('plant-stopped', container + ' stopped')


def start_plant(run_id, timeline, pair='deployed'):
    """The recovery half: `docker start` relaunches the run's plant
    container — a fresh plant-server lifetime, so the single-writer
    claim the old process held is gone and the field owner must
    re-claim it."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['plant']
    timeline('plant-start', 'docker start ' + container)
    docker('start', container, timeout=60)
    timeline('plant-started', container + ' running')


def pause_plant(run_id, timeline, pair='deployed'):
    """The wedged-field half of the bounded-liveness leg:
    `docker pause` freezes the run's plant container in place — the
    controllers' remote-driver sockets stay open, every request the
    scan loop issues just goes unanswered, and each blocked scan only
    completes when its field timeout fires. Unlike `docker stop` this
    holds the connection rather than severing it, so it reproduces the
    docker-pause-shaped wedge the liveness regression models without
    the fencing reconnect the link-loss leg stages. `pair` selects
    which pair's plant — 'deployed' or the probe pair's. A docker
    failure raises so the calling scenario reports the pause never
    landed."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['plant']
    timeline('plant-pause', 'docker pause ' + container)
    docker('pause', container, timeout=30)
    timeline('plant-paused', container + ' paused')


def unpause_plant(run_id, timeline, pair='deployed'):
    """The recovery half: `docker unpause` resumes the frozen plant
    process — the held sockets drain, the wedged scans complete, and
    the field owner keeps its claim since the plant never stopped."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['plant']
    timeline('plant-unpause', 'docker unpause ' + container)
    docker('unpause', container, timeout=30)
    timeline('plant-unpaused', container + ' running')


def pause_controller(run_id, name, timeline, pair='deployed'):
    """The frozen-source induction the orphan-episode leg drives:
    `docker pause` freezes one of the run's controller containers in
    place — its monitor socket stays bound, so a tracking peer's
    checkpoint fetch to it stalls in flight until the pull's own
    timeout drops it, producing the fetch-worker's
    produced-nothing misses rather than the refused-connection
    shape `docker stop` would stage. `name` is the scenario ctx's
    endpoint key ('active'/'standby', pair-selected like the other
    lifecycle actions). Recorded on the run's action timeline; a
    docker failure raises so the calling scenario reports the
    freeze never landed."""
    container = _controller_container(run_id, name, pair)
    timeline('controller-pause', 'docker pause ' + container)
    docker('pause', container, timeout=30)
    timeline('controller-paused', container + ' paused')


def unpause_controller(run_id, name, timeline, pair='deployed'):
    """The matching thaw: `docker unpause` resumes the frozen
    controller process — the stalled checkpoint fetches complete
    with the held document and the peer's pulls land again."""
    container = _controller_container(run_id, name, pair)
    timeline('controller-unpause', 'docker unpause ' + container)
    docker('unpause', container, timeout=30)
    timeline('controller-unpaused', container + ' running')


def plant_ctl(run_id, port, *args, pair='deployed'):
    """The scenario-callable plant-tool invocation: `docker exec` runs
    the shipped `dcs-plant-ctl` inside the run's plant container
    against the server's loopback listener — the ticket's honest seam,
    so the lane's covered plant ops drive the binary the image carries
    rather than a second Python implementation of the wire protocol.
    The loopback address binds inside the container's own netns — the
    exchange never leaves the rig bridge the netpolicy closes.
    `pair` selects which pair's plant container — 'deployed' or the
    probe pair's bridge-placed 'probe-plant'.
    `check=False` returns the CompletedProcess on a refused request too
    — the tool's nonzero exit is the answer the caller classifies, not
    a docker failure."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['plant']
    return docker('exec', container, 'dcs-plant-ctl',
                  '127.0.0.1:' + str(port), *args,
                  check=False, timeout=60)


# The staged-document admission probe's serve-gate bind grace: a
# refused document exits inside the merge — well under a second —
# while an admitted one binds its listener and stays up; the grace
# separates 'refused' from 'serving' without trusting a timeout.
DYNAMICS_BIND_GRACE = 10


def admit_dynamics(cfg, record, run_dir, model, name, document,
                   timeline):
    """The scenario-callable doctored-dynamics admission probe — the
    dynamics-admission leg's per-run variant seam: stage `document`
    (the dynamics declaration list, as JSON text or a Python object)
    inside the bounded run dir, then drive the run's plant image
    through the two admission gates the malformed-dynamics contract
    guards, each in a labeled scratch container mounting the staged
    file read-only beside the run's model:

    - 'check': the released `dcs-plant-server --check-dynamics`
      preflight — a one-shot `docker run --rm` exiting with the merge
      verdict, never serving;
    - 'serve': a detached `--dynamics` plant load on the same staged
      document — polled for the bind grace: a refused document exits
      inside the merge while an admitted one binds its listener and
      stays up, so 'running' past the grace is the accepted verdict.

    Returns {'document': str(staged), 'check': {...}, 'serve': {...}}
    with each gate's exit code and captured output — the leg names the
    refusal; a docker failure on either launch raises so the leg
    reports the probe never ran rather than reading an empty answer.
    Both launches are recorded on the run's action timeline.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    if not isinstance(document, str):
        document = json.dumps(document)
    safe = ''.join(c if c.isalnum() or c == '-' else '-'
                   for c in str(name).lower())
    directory = Path(run_dir) / 'dynamics-probes'
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / (safe + '.json')
    staged.write_text(document)
    result = {'name': safe, 'document': str(staged)}
    # The preflight gate — the released --check-dynamics surface:
    # the same merge-and-validate the serving load applies, reported
    # as 'check ok' plus an element census or one 'dynamics element N
    # (driving point P)' line per refused element.
    timeline('dynamics-admit-check',
             'preflight ' + staged.name + ' (' + safe + ')')
    proc = docker('run', '--rm',
                  '--label', MANAGED_LABEL + '=1',
                  '--label', RUN_LABEL + '=' + run_id,
                  '--cpus', cfg['rig_cpus'],
                  '--memory', cfg['rig_memory'],
                  '--memory-swap', cfg['rig_memory'],
                  '--pids-limit', str(cfg['rig_pids']),
                  '--network', 'none',
                  '-v', str(model) + ':/model/plant.json:ro',
                  '-v', str(staged) + ':/model/dynamics.json:ro',
                  IMAGE_PREFIX + 'plant:' + sha,
                  '/model/plant.json',
                  '--check-dynamics', '/model/dynamics.json',
                  check=False, timeout=120)
    result['check'] = {'exit': proc.returncode,
                       'stdout': proc.stdout,
                       'stderr': proc.stderr}
    # The serving-load gate — a spawned plant carrying the staged
    # document: detached, networkless, and polled across the bind
    # grace. A refused document exits inside the merge; an admitted
    # one binds its loopback listener and stays up — 'listening on'
    # in the logs and Running=true are the accepted verdict. The
    # container is bounded either way and removed after capture.
    container = 'dcs-hw-' + run_id + '-dyn-' + safe
    timeline('dynamics-admit-serve',
             'docker run -d ' + container
             + ' mounting ' + staged.name)
    docker('rm', '-f', container, check=False, timeout=60)
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'none',
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(staged) + ':/model/dynamics.json:ro',
           IMAGE_PREFIX + 'plant:' + sha,
           '/model/plant.json', '--dynamics', '/model/dynamics.json',
           '--listen', '127.0.0.1:0')
    deadline = time.monotonic() + DYNAMICS_BIND_GRACE
    running = True
    while running and time.monotonic() < deadline:
        probe = docker('inspect', '-f', '{{.State.Running}}',
                       container, check=False)
        running = probe.stdout.strip() == 'true'
        if running:
            time.sleep(0.25)
    logs = docker('logs', container, check=False)
    probe = docker('inspect', '-f', '{{.State.ExitCode}}',
                   container, check=False)
    try:
        exit_code = int(probe.stdout.strip())
    except ValueError:
        exit_code = None
    docker('rm', '-f', container, check=False, timeout=60)
    timeline('dynamics-admitted', container + ' '
             + ('still serving past the bind grace'
                if running else 'exited ' + str(exit_code)))
    result['serve'] = {'running': running, 'exit': exit_code,
                       'logs': (logs.stdout or '')
                       + (logs.stderr or '')}
    return result


# The persistence-alias leg's launch bounds: a refused launch exits at
# option-parse — well inside a second — while an admitted one paces its
# --ticks budget to exit 0, so one bounded foreground `docker run`
# captures the whole verdict. The probes never touch the rig's network
# or the deployed members' mounts, so the bound is staging overhead
# plus the paced budget, not a settlement window.
ALIAS_PROBE_TICKS = 5
ALIAS_PROBE_TIMEOUT = 60
ALIAS_PROBE_PORT = 8095  # the probe's in-container --listen port


def admit_persistence(cfg, record, run_dir, model, name, paths,
                      timeline):
    """The scenario-callable persistence-alias launch probe — the
    persistence-path-distinctness leg's per-run variant seam: run the
    run's controller image once, in a labeled networkless scratch
    container, carrying the caller's persistence trio.

    `paths` declares all three sinks —
    {'state_file', 'journal_file', 'history_file'} — as names inside a
    per-probe scratch directory the launch mounts at CONTAINER_RUN_DIR;
    an alias is two keys naming one file. The launch is the rig shape
    minus its pair wiring: the same --scan-ms pacing, --listen, and
    persistence flags, bounded by --ticks so an admitted launch scans
    its budget and exits 0 with the final snapshot on stdout — a
    refused one exits at option-parse with the conflicting flags and
    shared path named on stderr. The probe binds no rig endpoint:
    '--network none' keeps the deployed pair's field and monitor
    endpoints untouched (the persistence trio only requires --listen,
    which binds in-container loopback), the scratch mount never shares
    the members' files, and the deployed members' launches are never
    rebuilt — the launch configuration needs no restoring because no
    member's launch was touched.

    Returns {'name': safe, 'dir': scratch dir, 'paths': declared dict,
    'argv': the launch argv, 'exit': code, 'stdout':, 'stderr':} — the
    leg names the verdict. A docker failure raises so the leg reports
    the launch never ran rather than reading an empty refusal; the
    launch and its captured verdict are recorded on the run's action
    timeline. '--rm' plus a reconciling rm sweep keep a leftover probe
    container from outliving its call.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    flags = {'state_file': '--state-file',
             'journal_file': '--journal-file',
             'history_file': '--history-file'}
    if not isinstance(paths, dict) or sorted(paths) != sorted(flags):
        raise RuntimeError('admit_persistence expects all three '
                           'persistence sinks declared: '
                           + ', '.join(sorted(flags)))
    bad = {key: value for key, value in paths.items()
           if not isinstance(value, str) or not value
           or value.startswith('/')}
    if bad:
        raise RuntimeError('admit_persistence paths must be file names '
                           'inside the probe mount: ' + json.dumps(bad))
    safe = ''.join(c if c.isalnum() or c == '-' else '-'
                   for c in str(name).lower())
    directory = Path(run_dir) / 'persistence-probes' / safe
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    argv = ['/model/plant.json', '--scan-ms', '100',
            '--ticks', str(ALIAS_PROBE_TICKS),
            '--listen', '127.0.0.1:' + str(ALIAS_PROBE_PORT)]
    for key in ('state_file', 'journal_file', 'history_file'):
        argv += [flags[key], CONTAINER_RUN_DIR + '/' + paths[key]]
    container = 'dcs-hw-' + run_id + '-persist-' + safe
    timeline('persistence-probe',
             'docker run ' + container + ' (' + safe + ')')
    docker('rm', '-f', container, check=False, timeout=60)
    try:
        proc = docker('run', '--rm', '--name', container,
                      '--label', MANAGED_LABEL + '=1',
                      '--label', RUN_LABEL + '=' + run_id,
                      '--cpus', cfg['rig_cpus'],
                      '--memory', cfg['rig_memory'],
                      '--memory-swap', cfg['rig_memory'],
                      '--pids-limit', str(cfg['rig_pids']),
                      '--network', 'none',
                      '-v', str(model) + ':/model/plant.json:ro',
                      '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
                      IMAGE_PREFIX + 'controller:' + sha,
                      *argv, check=False, timeout=ALIAS_PROBE_TIMEOUT)
    finally:
        docker('rm', '-f', container, check=False, timeout=60)
    timeline('persistence-probed',
             container + ' exited ' + str(proc.returncode))
    return {'name': safe, 'dir': str(directory),
            'paths': dict(paths), 'argv': argv,
            'exit': proc.returncode,
            'stdout': proc.stdout, 'stderr': proc.stderr}


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
    peers = PAIRS['deployed']['peers']
    if active not in peers:
        raise RuntimeError('start_revised expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name = peers[active]
    peer_port = PAIR_MONITOR_PORTS[active]
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
        for artifact in ('state.json', 'journal.jsonl',
                         'history.jsonl'):
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
    # The roll demotes the field owner toward this peer's announced
    # address — under the keyed announced-source contract only a peer
    # carrying the pair's token can sign the line_proof the demoted
    # peer's verify pull demands, so the launch keeps the pair's keyed
    # posture.
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(cfg['revised_port']) + ':'
           + str(DRIVEN_MONITOR_PORT),
           '-v', str(revised_doc) + ':/model/revised.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *_controller_argv(cfg, 'deployed', 'revised', prefix,
                             model='/model/revised.json',
                             standby=standby, revised=True))
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
    peers = PAIRS['deployed']['peers']
    if active not in peers:
        raise RuntimeError('start_foreign expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name = peers[active]
    peer_port = PAIR_MONITOR_PORTS[active]
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
           '-p', '127.0.0.1:' + str(cfg['foreign_port']) + ':'
           + str(DRIVEN_MONITOR_PORT),
           '-v', str(foreign_doc) + ':/model/foreign.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           # The labeled foreign peer stays unkeyed on purpose — the
           # negotiation-refusal leg's fingerprint gate answers on the
           # document alone.
           *_controller_argv(cfg, 'deployed', 'foreign', prefix,
                             model='/model/foreign.json',
                             standby=standby, keyed=False))
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
                            timeline, pair='deployed'):
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
    tracks ('active' is ctrl-a, 'standby' ctrl-b on the deployed
    pair; pair='probe' names the probe pair's members) — the
    checkpoint source the case's stop induction then makes
    unreachable. The container carries the run's managed and run
    labels so teardown
    reconciles it with the rest of the rig, mounts the run's model
    read-only at /model/plant.json, publishes its monitor on the
    pair's driven_port, gets its own runner-owned state/journal
    directory, binds the pair's own plant's --remote, and carries the
    pair's --pair-token so its checkpoint
    answers sign the keyed line_proof an orphan-resolution probe's
    ?prove= pull demands. The launch is recorded on the run's action
    timeline; a docker failure raises so the calling scenario reports
    the action never completed.

    Returns the launched container's name.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = PAIRS[pair]['peers']
    if active not in peers:
        raise RuntimeError('start_driven expects the active endpoint '
                           'key, got ' + repr(active))
    peer_name = peers[active]
    peer_port = PAIR_MONITOR_PORTS[active]
    directory = _controller_dir(run_dir, PAIRS[pair]['driven'])
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    container = prefix + '-' + PAIRS[pair]['driven']
    standby = prefix + '-' + peer_name + ':' + str(peer_port)
    tokens = _plant_owner_tokens(cfg)
    owner_token = tokens[_pair_owner_key(pair, 'driven')]
    remote = _pair_plant_remote(cfg, pair, prefix)
    timeline('driven-start', 'launch ' + container + ' --standby '
             + standby + ' --driven --remote ' + remote
             + ' --owner-token ' + str(owner_token))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:'
           + str(_pair_host_port(cfg, pair, 'driven'))
           + ':' + str(DRIVEN_MONITOR_PORT),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           # The stale-island leg promotes this peer onto the field
           # and the islanded pair's orphan-resolution probes pull
           # its checkpoint with ?prove= — under the keyed contract
           # only a peer carrying the pair's token can sign the
           # line_proof those verify pulls demand, so the launch keeps
           # the pair's keyed posture.
           *_controller_argv(cfg, pair, 'driven', prefix,
                             standby=standby, driven=True))
    timeline('driven-up', container + ' serving a driven standby')
    return {'container': container}


def stop_driven_controller(run_id, timeline, pair='deployed'):
    """The dead-peer-latency case's teardown: `docker rm -f` on the
    driven peer's container — removed outright, not held down, so
    later cases see the rig's original pair. `pair` selects which
    pair's driven container ('d' on the deployed pair, 'probe-d' on
    the probe pair). Recorded on the run's
    action timeline like the other lifecycle actions; a docker
    failure raises so the calling scenario reports the teardown
    never completed."""
    container = 'dcs-hw-' + run_id + '-' + PAIRS[pair]['driven']
    timeline('driven-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, timeout=90)
    timeline('driven-stopped', container + ' removed')


# The forged-checkpoint endpoint's monitor port inside the rig bridge —
# never published to the host: only the rig's own peers dial it.
FORGE_PORT = 8090


def start_forge_endpoint(cfg, record, run_dir, document, owner,
                         timeline, keyed=True, pair='deployed'):
    """The scenario-callable forged-checkpoint endpoint launch — the
    demote-forged-standby-source leg's hostile announce target: the
    run's labeled rig-bridge container running the shipped `dcs-forge`
    binary out of the controller image, serving `document` as its
    /checkpoint answer while announcing itself to the named owner's
    monitor so its bridge address is the recorded tracking hint a
    POST /demote must verify.

    `document` is the checkpoint-shaped dict the endpoint serves —
    staged to a bind-mounted file inside the run directory that
    dcs-forge re-reads on every pull, so the leg's next forged shape
    lands by rewriting the returned 'document' path between demote
    calls without a relaunch. `owner` is the ctx endpoint key of the
    field-owning peer whose monitor the endpoint announces to
    ('active' is ctrl-a's container, 'standby' ctrl-b's on the
    deployed pair; pair='probe' names the probe pair's members).
    `keyed`
    selects whether the endpoint signs `?prove=` answers under the
    pair's --pair-token: the key-holding shape — every pulled document
    genuinely signed, so only its content can convict it — is what
    the forged legs need to reach the demote verify's command-record
    audit rather than the proof gate; unkeyed is the unproven leg's
    tokenless hostile endpoint. A pair carrying no pair token
    launches unkeyed regardless — matching the contract's keyed-only
    posture.

    The container carries the run's managed and run labels so teardown
    reconciles it with the rig, binds no host port — only the rig's
    peers dial it — and refuses to launch unless the recorded
    endpoint_placement marks 'forge' bridge-placed: the host egress
    policy makes a host socket unreachable from the rig. The launch
    is recorded on the run's action timeline; a docker failure raises
    so the calling scenario reports the action never completed.

    Returns {'container', 'dir', 'document', 'hits', 'keyed', 'port'}:
    the forge's run-dir working directory, the served-document path
    the leg rewrites, the hits ledger the binary appends every served
    pull and announce to — the self-verifying record that the verify
    pull reached the endpoint and whether its answer was signed — the
    keyed posture actually launched, and the endpoint's announced
    bridge port a journaled adoption names.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    peers = PAIRS[pair]['peers']
    if owner not in peers:
        raise RuntimeError('start_forge expects the field-owning '
                           'endpoint key, got ' + repr(owner))
    placements = _endpoint_placement(cfg)
    if placements['forge'] != 'bridge':
        raise RuntimeError('endpoint_placement records forge as '
                           + repr(placements['forge'])
                           + ' but the endpoint must sit on the rig '
                           'bridge — a host socket is unreachable '
                           'from the rig')
    # The probe subject's forge is its own container and staging
    # directory — a leg exercising the probe pair never touches the
    # deployed pair's forged endpoint or its served document.
    suffix = 'forge' if pair == 'deployed' else 'probe-forge'
    directory = Path(run_dir) / suffix
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    document_path = directory / 'checkpoint.json'
    # Host-side atomic staging: the container re-reads this file per
    # pull, so a rename lands the next shape without a torn read.
    staged = directory / 'checkpoint.staging.json'
    staged.write_text(json.dumps(document))
    staged.replace(document_path)
    hits = directory / 'hits.jsonl'
    hits.unlink(missing_ok=True)
    container = prefix + '-' + suffix
    # A leftover forge from an aborted pass leaves the same name; its
    # staged document and hits ledger are refreshed above regardless.
    docker('rm', '-f', container, check=False, timeout=60)
    peer_name = peers[owner]
    peer_port = PAIR_MONITOR_PORTS[owner]
    announce = prefix + '-' + peer_name + ':' + str(peer_port)
    token = _pair_token(cfg, pair)
    keyed = bool(keyed and token)
    timeline('forge-start', 'launch ' + container + ' serving '
             + document_path.name + ', announcing to ' + announce
             + (' (keyed)' if keyed else ' (unkeyed)'))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-v', str(directory) + ':/forge',
           '--entrypoint', 'dcs-forge',
           IMAGE_PREFIX + 'controller:' + sha,
           '--listen', '0.0.0.0:' + str(FORGE_PORT),
           '--document', '/forge/checkpoint.json',
           '--hits', '/forge/hits.jsonl',
           '--announce', announce,
           *(['--pair-token', str(token)] if keyed else []))
    timeline('forge-up', container + ' serving the staged document')
    return {'container': container, 'dir': str(directory),
            'document': str(document_path), 'hits': str(hits),
            'keyed': keyed, 'port': FORGE_PORT}


def stop_forge_endpoint(run_id, timeline, pair='deployed'):
    """The forged-checkpoint endpoint's teardown: `docker rm -f` on
    the pair's forge container — removed outright so the hint the demote
    verify dials is a dead endpoint again. `pair` selects which
    pair's forge ('forge' on the deployed pair, 'probe-forge' on the
    probe pair). Recorded on the run's action
    timeline like the other lifecycle actions; a docker failure raises
    so the calling scenario reports the teardown never completed."""
    container = 'dcs-hw-' + run_id + '-' + (
        'forge' if pair == 'deployed' else 'probe-forge')
    timeline('forge-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, timeout=90)
    timeline('forge-stopped', container + ' removed')


# The born-active startup-failure leg's staging surface (decision 103 —
# #985's record, #1017's implementation, #1033's consuming leg): a
# scratch sim-serve field the leg silences, serves, and freezes — never
# the deployed pair's own plant, whose claim arbitration and scan feed
# stay undisturbed — plus the labeled scenario seats the leg launches
# born-active controllers onto. Each born launch is cold by contract:
# the seat's runner-owned state/journal/history artifacts reset with the
# container so the launch exercises the startup claim, never the resume
# path.
BORN_SEATS = {'revised': 'c', 'foreign': 'foreign', 'driven': 'd'}
# The sim-serve port the scratch field binds inside its container —
# bridge-placed only (rig-dialed by name, never host-published): the
# born-active's --remote is the only attachment that dials it.
BORN_FIELD_PORT = 9003
# The --listen port every born seat's monitor binds in-container —
# identical across seats since each container owns its netns.
BORN_MONITOR_PORT = 8082


def _born_seat_container(run_id, seat):
    """The labeled container a born launch occupies — the scenario
    ctx's endpoint key mapped to the seat's container suffix."""
    if seat not in BORN_SEATS:
        raise RuntimeError('born-active launches run on the scenario '
                           'seats ' + json.dumps(sorted(BORN_SEATS))
                           + ', got ' + repr(seat))
    return 'dcs-hw-' + run_id + '-' + BORN_SEATS[seat]


def _born_target(run_id, value):
    """The --peer/--standby argument a born launch carries: a seat key
    resolves to that seat's rig-bridge monitor address; anything else —
    the leg's deliberately unresolvable peer name — passes through
    verbatim."""
    if value in BORN_SEATS:
        return 'dcs-hw-' + run_id + '-' + BORN_SEATS[value] \
            + ':' + str(BORN_MONITOR_PORT)
    return value


def _born_seat_role(cfg, seat):
    """The seat's served RoleReport through its published monitor port,
    or None while unreachable — the refuse-to-replace guard's read of
    whether an existing seat container currently owns a field."""
    try:
        with urllib.request.urlopen(
                'http://127.0.0.1:' + str(cfg[seat + '_port'])
                + '/role', timeout=3) as response:
            return json.loads(response.read() or b'null')
    except Exception:
        return None


def start_born_field(cfg, record, run_dir, model, dynamics, timeline,
                     mode):
    """The scenario-callable born-active staging field: the leg's own
    scratch sim-serve container on the rig bridge, mode-selected to
    reproduce each field-side startup condition decision 103 records:

    - 'silent' launches a sleeping placeholder under the field's
      container name — the address resolves but nothing listens, the
      unreachable-field class (a)'s resolvable-but-dead transport;
    - 'serving' launches the run's plant server on the run model and
      dynamics — the field whose answered contact resolves the pending
      state, and whose held claim refuses the class (b) launches;
      `pause_born_field` on top of it stages the class (c)
      attach-without-verdict inconclusive claim;
    - 'foreign' launches the same plant server on the run config's
      foreign fixtures (`foreign_model_fixture`/`foreign_dynamics_fixture`
      — the dosing skid) — the miswired-remote field the
      foreign-model correspondence leg stages: its served point set
      and kinds differ from the rig model's, so a --remote born-active
      declaring the run model must meet the #1302 startup refusal.

    The container carries the run's managed and run labels so teardown
    reconciles it; a previous born field — either mode — is removed
    first. The launch is recorded on the run's action timeline; a docker
    failure raises so the calling scenario reports the staging never
    completed. Returns {'container', 'remote', 'mode'} — `remote` is
    the container-name sim-serve address a born controller's --remote
    dials.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    if mode not in ('serving', 'silent', 'foreign'):
        raise RuntimeError('start_born_field modes are '
                           "'serving'/'silent'/'foreign', got "
                           + repr(mode))
    container = 'dcs-hw-' + run_id + '-born-plant'
    docker('rm', '-f', container, check=False, timeout=60)
    timeline('born-field-start', 'launch ' + container + ' (' + mode
             + ')')
    if mode == 'silent':
        docker(*_docker_run_args(cfg, run_id, container),
               '--network', 'dcs-hwtest-' + run_id,
               '--entrypoint', 'sleep',
               IMAGE_PREFIX + 'controller:' + sha, 'infinity')
    else:
        docker(*_docker_run_args(cfg, run_id, container),
               '--network', 'dcs-hwtest-' + run_id,
               '-v', str(model) + ':/model/plant.json:ro',
               '-v', str(dynamics) + ':/model/dynamics.json:ro',
               IMAGE_PREFIX + 'plant:' + sha,
               '/model/plant.json', '--dynamics', '/model/dynamics.json',
               '--listen', '0.0.0.0:' + str(BORN_FIELD_PORT))
        # The deferred attach tolerates a not-yet-bound listener, but
        # the refusal classes need the claim arbitration live: wait for
        # the server to serve before handing the address out — probed
        # through the shipped tool inside the container's own netns
        # since nothing host-side reaches the bridge.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            probe = docker('exec', container, 'dcs-plant-ctl',
                           '127.0.0.1:' + str(BORN_FIELD_PORT), 'list',
                           check=False, timeout=10)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('born field listener never bound')
    timeline('born-field-up', container + ' ' + mode)
    return {'container': container,
            'remote': container + ':' + str(BORN_FIELD_PORT),
            'mode': mode}


def pause_born_field(run_id, timeline):
    """The inconclusive-claim half of the born-active staging:
    `docker pause` freezes the scratch field in place — a born-active's
    attach completes into the listener's backlog but the claim request
    never answers, the verdict-free `Err` leg class (c) records.
    `unpause_born_field` lets the deferred grant land. Recorded on the
    run's action timeline; a docker failure raises so the calling
    scenario reports the freeze never landed."""
    container = 'dcs-hw-' + run_id + '-born-plant'
    timeline('born-field-pause', 'docker pause ' + container)
    docker('pause', container, timeout=30)
    timeline('born-field-paused', container + ' paused')


def unpause_born_field(run_id, timeline):
    """The recovery half: `docker unpause` resumes the frozen scratch
    field — the pending born-active's next answered contact re-issues
    the conditional startup grant."""
    container = 'dcs-hw-' + run_id + '-born-plant'
    timeline('born-field-unpause', 'docker unpause ' + container)
    docker('unpause', container, timeout=30)
    timeline('born-field-unpaused', container + ' running')


def stop_born_field(run_id, timeline):
    """Tear down the leg's scratch field — `docker rm -f`, tolerating an
    already-absent container so a failed staging's cleanup can re-run.
    Removing the field drops every claim its attachments held."""
    container = 'dcs-hw-' + run_id + '-born-plant'
    timeline('born-field-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, check=False, timeout=60)
    timeline('born-field-stopped', container + ' removed')


def start_born_controller(cfg, record, run_dir, model, seat, remote,
                          timeline, peer=None, standby=None):
    """The scenario-callable born-active launch — the born-active
    startup-failure leg's per-class launcher: runs a controller on one
    of the labeled scenario seats (`revised`/`foreign`/`driven` — the
    run's third-controller containers 'c'/'foreign'/'d') bound to
    `remote`, the scratch field's sim-serve address.

    `peer` and `standby` name the tracking wiring the launch carries:
    `peer` launches a born-active declaring its pair member — the
    `--peer` whose declared rejoin the refused-claim class exercises —
    while `standby` launches the pair's tracking member. Both take a
    seat key resolved to its rig-bridge monitor address, or a verbatim
    `host:port` — the unreachable-peer edge's deliberately dead name.
    A leftover seat container is removed first, but only after its
    served /role proves it does not own a field — an active or
    promoting seat refuses removal by name. The seat's runner-owned
    state, journal, and history reset with the launch: a born launch
    is cold by contract, and a stale --state-file would run the resume
    path — a different class entirely.

    The container carries the run's managed and run labels, mounts the
    run's model read-only, publishes its monitor on the seat's recorded
    port, and carries the seat's pinned --owner-token plus the run's
    --pair-token. The launch is recorded on the run's action timeline;
    a docker failure raises so the calling scenario reports the launch
    never completed. Returns {'container', 'seat', 'address', 'remote',
    'peer', 'standby', 'monitor'} — `address` is the rig-bridge
    monitor endpoint a peer's tracking declaration dials, `monitor`
    the published host-loopback URL the scenario reads.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    if peer is not None and standby is not None:
        raise RuntimeError('a born launch is either the pair\'s '
                           'born-active (--peer) or its tracking '
                           'standby (--standby), never both')
    container = _born_seat_container(run_id, seat)
    listed = docker('ps', '-a', '--filter',
                    'name=^/' + container + '$', '--format', '{{.ID}}',
                    check=False)
    if listed.returncode != 0:
        raise RuntimeError('start_born_controller cannot prove the '
                           'seat is absent: docker ps failed: '
                           + listed.stderr.strip()[:300])
    if listed.stdout.strip():
        report = _born_seat_role(cfg, seat)
        if report and report.get('role') in ('active', 'promoting'):
            raise RuntimeError('start_born_controller refuses to '
                               'replace ' + container + ': it reports '
                               'role ' + str(report['role']))
        timeline('born-replace', 'docker rm -f ' + container
                 + ' (served role ' + str((report or {}).get('role'))
                 + ')')
        docker('rm', '-f', container, timeout=60)
    directory = _controller_dir(run_dir, BORN_SEATS[seat])
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o777)
    for artifact in ('state.json', 'journal.jsonl', 'history.jsonl'):
        (directory / artifact).unlink(missing_ok=True)
    owner_token = _plant_owner_tokens(cfg)[seat]
    peer_flag = _born_target(run_id, peer) if peer is not None else None
    standby_flag = (_born_target(run_id, standby)
                    if standby is not None else None)
    timeline('born-start',
             'launch ' + container + ' --remote ' + remote
             + (' --peer ' + peer_flag if peer_flag else '')
             + (' --standby ' + standby_flag if standby_flag else '')
             + ' --owner-token ' + str(owner_token))
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', 'dcs-hwtest-' + run_id,
           '-p', '127.0.0.1:' + str(cfg[seat + '_port']) + ':'
           + str(BORN_MONITOR_PORT),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(directory) + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           '/model/plant.json',
           '--remote', remote,
           '--owner-token', str(owner_token),
           *(['--peer', peer_flag] if peer_flag else []),
           *(['--standby', standby_flag] if standby_flag else []),
           '--scan-ms', '100', '--listen', '0.0.0.0:'
           + str(BORN_MONITOR_PORT),
           '--state-file', CONTAINER_STATE_FILE,
           '--journal-file', CONTAINER_JOURNAL_FILE,
           '--history-file', CONTAINER_HISTORY_FILE,
           *(['--pair-token', str(cfg['pair_token'])]
             if cfg.get('pair_token') else []))
    timeline('born-up', container + ' launched')
    return {'container': container, 'seat': seat,
            'address': container + ':' + str(BORN_MONITOR_PORT),
            'remote': remote, 'peer': peer_flag,
            'standby': standby_flag,
            'monitor': 'http://127.0.0.1:' + str(cfg[seat + '_port'])}


def stop_born_controller(run_id, seat, timeline):
    """The born seat's teardown — `docker rm -f`, tolerating an
    already-absent container so a failed staging's cleanup can re-run.
    Removing an incumbent drops its field claim with the attachment."""
    container = _born_seat_container(run_id, seat)
    timeline('born-stop', 'docker rm -f ' + container)
    docker('rm', '-f', container, check=False, timeout=90)
    timeline('born-stopped', container + ' removed')


def born_controller_state(run_id, seat):
    """The born seat container's process verdict — the
    undeclared-refusal class's evidence: `{'container', 'running',
    'exit', 'logs', 'absent'}` — Running=false with a nonzero exit and
    the named refusal on the log tail is the recorded disposition; an
    absent container reports `absent` rather than raising, since the
    read itself is the leg's evidence collection."""
    container = _born_seat_container(run_id, seat)
    probe = docker('inspect', '-f', '{{.State.Running}} {{.State.ExitCode}}',
                   container, check=False)
    if probe.returncode != 0:
        return {'container': container, 'running': False, 'exit': None,
                'logs': '', 'absent': True}
    parts = probe.stdout.split()
    running = parts[:1] == ['true']
    try:
        exit_code = int(parts[1])
    except (IndexError, ValueError):
        exit_code = None
    logs = docker('logs', '--tail', '60', container, check=False)
    return {'container': container, 'running': running,
            'exit': exit_code, 'absent': False,
            'logs': (logs.stdout or '') + (logs.stderr or '')}


def born_field_ctl(run_id, *args):
    """The scenario-callable plant-tool invocation against the born
    legs' scratch field: `docker exec` runs the shipped `dcs-plant-ctl`
    inside the field's own container against its loopback listener —
    the born plant is rig-bridge-placed, reachable only as a --remote
    address, so its census and claim evidence reach the lane through
    the container's own netns, the same seam `plant_ctl` gives the
    deployed pair's plant. `check=False` returns the CompletedProcess
    on a refused request — the tool's nonzero exit and stderr are the
    answer the caller classifies, not a docker failure."""
    container = 'dcs-hw-' + run_id + '-born-plant'
    return docker('exec', container, 'dcs-plant-ctl',
                  '127.0.0.1:' + str(BORN_FIELD_PORT), *args,
                  check=False, timeout=60)


# The register-mapped field rig (#1355): the bus device's staged
# document, its device-server container, the pair bound to it, and the
# levers the sim-bus driver-reattach leg drives. The device is
# bridge-placed and dialed by container name — no host publish — so
# its own census reaches the lane through the shipped field tool
# exec'd inside the container's netns, exactly like the born legs'
# scratch field.
BUS_RIG_REQUIRED = ('pair_token', 'active_port', 'standby_port',
                    'device_port', 'model_fixture', 'device_id',
                    'timeout_ms', 'owner_tokens')
BUS_RIG_PORTS = ('active_port', 'standby_port', 'device_port')
BUS_RIG_SUFFIX = {'active': 'bus-a', 'standby': 'bus-b',
                  'device': 'bus-device'}


def _bus_rig(cfg):
    """The run config's register-mapped field rig block, validated
    before a launch trusts it — or None when the run stages none
    ('bus_rig' absent or null), which leaves the driver-reattach leg
    without a subject and its verdict inconclusive.

    Same fail-before-launch discipline `_plant_owner_tokens` and
    `_probe_pair` apply: every staging key present, the two published
    monitor ports and the device's bridge port 1..65535, a
    non-negative integer device id and request timeout, a shared
    --pair-token, and the pair's own two distinct u64 --owner-token
    pins. A duplicated pin would answer the device's single-writer
    claim as a shared attachment and silently defeat the fencing the
    rig-reattach leg's promote probe reads.
    """
    spec = cfg.get('bus_rig')
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise RuntimeError('bus_rig must map the bus rig\'s staging '
                           'keys, or be null to stage none')
    missing = [key for key in BUS_RIG_REQUIRED if key not in spec]
    if missing:
        raise RuntimeError('bus_rig stages no ' + ', '.join(missing))
    bad = {key: spec[key] for key in BUS_RIG_PORTS
           if not isinstance(spec[key], int)
           or isinstance(spec[key], bool)
           or not 0 < spec[key] <= 65535}
    if bad:
        raise RuntimeError('bus_rig ports must be int 1..65535: '
                           + json.dumps(bad, sort_keys=True))
    for key in ('device_id', 'timeout_ms'):
        if not isinstance(spec[key], int) or isinstance(spec[key], bool) \
                or spec[key] < 0:
            raise RuntimeError('bus_rig ' + key
                               + ' must be a non-negative integer')
    if not spec['pair_token']:
        raise RuntimeError('bus_rig pair_token must name the rig '
                           'pair\'s shared secret')
    tokens = spec['owner_tokens']
    if not isinstance(tokens, dict) or sorted(tokens) != ['active',
                                                           'standby']:
        raise RuntimeError("bus_rig owner_tokens must pin an 'active' "
                           "and a 'standby' --owner-token")
    bad = {key: tokens[key] for key in ('active', 'standby')
           if not isinstance(tokens[key], int)
           or isinstance(tokens[key], bool)
           or not 0 <= tokens[key] <= 0xFFFFFFFFFFFFFFFF}
    if bad:
        raise RuntimeError('bus_rig owner_tokens must be u64 integers: '
                           + json.dumps(bad, sort_keys=True))
    if tokens['active'] == tokens['standby']:
        raise RuntimeError('bus_rig must pin a distinct --owner-token '
                           'per bus pair member: '
                           + json.dumps(tokens, sort_keys=True))
    return dict(spec)


def _bus_container(run_id, name):
    """The labeled container a bus-rig role occupies: the pair's two
    members or the device server."""
    if name not in BUS_RIG_SUFFIX:
        raise RuntimeError('the bus rig has no seat ' + repr(name))
    return 'dcs-hw-' + run_id + '-' + BUS_RIG_SUFFIX[name]


def derive_bus_model(model_path, out_path, address, device_id,
                     timeout_ms):
    """Write the rig's register-mapped model: the checked-in fixture
    with its `sim-bus` (or `sim-cyclic`) device's `address` and
    `timeout_ms` stamped onto the derived copy.

    The checked-in fixture declares a `__BUS_ADDR__` placeholder —
    the same convention `dcs-assembly`'s own bus fixtures carry, since
    no source tree has a routable device address — and no timeout, so
    the drivers would run on the five-second default. Both are
    per-run facts, so both land on the derived document the device
    server and both controllers mount: one declaration, one address,
    one timeout. Deterministic serialization, matching
    `revision.derive_revised_model`. Returns the derivation summary
    {'document', 'device', 'kind', 'address', 'timeout_ms'}; a
    fixture naming no register device raises, so a mistyped
    `device_id` fails before anything binds a port.
    """
    try:
        document = json.loads(Path(model_path).read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError('cannot read the bus-rig fixture '
                           + str(model_path) + ': ' + str(exc))
    devices = document.get('devices')
    if not isinstance(devices, list):
        raise RuntimeError('the bus-rig fixture declares no devices '
                           'list: ' + str(model_path))
    served = next((entry for entry in devices
                   if isinstance(entry, dict)
                   and entry.get('id') == device_id), None)
    if served is None:
        raise RuntimeError('the bus-rig fixture declares no device '
                           + str(device_id) + ': ' + str(model_path))
    kind = served.get('kind')
    if kind not in ('sim-bus', 'sim-cyclic'):
        raise RuntimeError('the bus rig serves a '
                           + repr(kind) + ' device ' + str(device_id)
                           + ' — only sim-bus and sim-cyclic are '
                           'register devices')
    parameters = served.setdefault('parameters', {})
    parameters['address'] = address
    parameters['timeout_ms'] = timeout_ms
    Path(out_path).write_text(json.dumps(document, indent=1,
                                         sort_keys=True) + '\n')
    return {'document': str(out_path), 'device': device_id,
            'kind': kind, 'address': address, 'timeout_ms': timeout_ms}


def _bus_controller_argv(bus, name, prefix):
    """The dcs-controller argv one bus-rig member launches with.

    The rig's own assembly, for the shape the shared
    `_controller_argv` cannot express: the pair attaches to no
    `--remote` plant — its field is the device's register bank, so a
    sim-tcp attachment would be a second, unrelated field — and its
    in-container `--listen` ports are the standard pair members' 8080
    and 8081 (separate netns, so the numbers repeat across rigs).
    `name='standby'` launches the tracking member: its `--standby`
    target is the pair's own active on the bridge, with the run's
    --auto-promote budget. Both members carry the block's shared
    --pair-token, so the rig's announced-source posture matches the
    deployed pair's keyed one.
    """
    argv = ['/model/bus.json', '--owner-token',
            str(bus['owner_tokens'][name]), '--scan-ms', '100',
            '--listen', '0.0.0.0:' + str(PAIR_MONITOR_PORTS[name])]
    if name == 'standby':
        argv += ['--standby',
                 prefix + '-' + BUS_RIG_SUFFIX['active'] + ':'
                 + str(PAIR_MONITOR_PORTS['active']),
                 '--auto-promote', '120']
    argv += ['--state-file', CONTAINER_STATE_FILE,
             '--journal-file', CONTAINER_JOURNAL_FILE,
             '--history-file', CONTAINER_HISTORY_FILE]
    if bus['pair_token']:
        argv += ['--pair-token', str(bus['pair_token'])]
    return argv


def _start_bus_rig(cfg, record, src, run_dir, net, timeline):
    """Stage the register-mapped field rig (#1355): one
    `dcs-sim-bus-device` server plus a redundant controller pair bound
    to the derived model, so a scenario can sever the point-wise
    BusDriver's link and watch it recover on a rig instead of on a
    unit test's loopback pair.

    The device server runs the run's own image (built with the
    register device's binaries) under `--entrypoint`, mounts the
    derived document read-only, and is bridge-placed: nothing
    host-side dials it, so readiness and every later census run
    through the shipped `dcs-sim-bus-ctl` exec'd inside the
    container's own netns. The pair's monitors publish on host
    loopback. Field ownership arbitration is per-device, so this rig
    never touches the deployed pair's plant, claim tokens, or files;
    each member gets its own runner-owned state/journal directory
    like every other launched controller.
    """
    bus = _bus_rig(cfg)
    if bus is None:
        return None
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    fixture = src / bus['model_fixture']
    if not fixture.is_file():
        raise RuntimeError('bus-rig fixture missing: ' + str(fixture))
    placements = _endpoint_placement(cfg)
    for key in ('bus_active', 'bus_standby'):
        if placements[key] != 'loopback':
            raise RuntimeError('endpoint_placement records ' + key
                               + ' as ' + repr(placements[key])
                               + ' but the bus rig publishes it on '
                               'host loopback')
    if placements['bus_device'] != 'bridge':
        raise RuntimeError('endpoint_placement records bus_device as '
                           + repr(placements['bus_device'])
                           + ' but the register device is rig-dialed '
                           'only — no host-side attachment exists')
    document = derive_bus_model(
        fixture, Path(run_dir) / 'model-bus.json',
        prefix + '-' + BUS_RIG_SUFFIX['device'] + ':'
        + str(bus['device_port']), bus['device_id'], bus['timeout_ms'])
    for name in ('bus-a', 'bus-b'):
        directory = _controller_dir(run_dir, name)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o777)
    container = _bus_container(run_id, 'device')
    timeline('bus-rig-start', 'derive ' + Path(document['document']).name
             + ' (' + document['kind'] + ' device '
             + str(document['device']) + ' at ' + document['address']
             + ', timeout ' + str(document['timeout_ms']) + 'ms); launch '
             + container)
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', net,
           '-v', document['document'] + ':/model/bus.json:ro',
           '--entrypoint', 'dcs-sim-bus-device',
           IMAGE_PREFIX + 'plant:' + sha,
           '/model/bus.json', '--device', str(bus['device_id']),
           '--listen', '0.0.0.0:' + str(bus['device_port']))
    # The pair's drivers connect at assembly and the device's listener
    # may not be bound yet: readiness runs through the shipped field
    # tool inside the container's own netns — no host port publishes
    # this device.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        probe = bus_device_ctl(run_id, bus['device_port'], 'list')
        if probe.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('the bus device server never served')
    for name in ('active', 'standby'):
        docker(*_docker_run_args(cfg, run_id,
                                 _bus_container(run_id, name)),
               '--network', net,
               '-p', '127.0.0.1:' + str(bus[name + '_port']) + ':'
               + str(PAIR_MONITOR_PORTS[name]),
               '-v', document['document'] + ':/model/bus.json:ro',
               '-v', str(_controller_dir(run_dir, BUS_RIG_SUFFIX[name]))
               + ':' + CONTAINER_RUN_DIR,
               IMAGE_PREFIX + 'controller:' + sha,
               *_bus_controller_argv(bus, name, prefix))
    timeline('bus-rig-up', 'register device + bus controller pair on '
             + net + ' (owner tokens bus_active='
             + str(bus['owner_tokens']['active']) + ', bus_standby='
             + str(bus['owner_tokens']['standby'])
             + ', pair-token ' + str(bus['pair_token']) + ')')
    return document


def restart_bus_device(run_id, timeline):
    """The reattach leg's device restart: `docker restart` on the
    rig's register device server — the field-device-restart half of
    the contract, the server dying and returning on its bound address
    with its claim table empty. The controllers keep running: nothing
    here restarts a controller, so what recovers can only be the
    driver's own lazy re-attach. Recorded on the run's action
    timeline; a docker failure raises so the leg reports the staging
    never completed."""
    container = _bus_container(run_id, 'device')
    timeline('bus-device-restart', 'docker restart ' + container)
    docker('restart', '--time', '2', container, timeout=90)
    timeline('bus-device-restarted', container + ' serving again')


def freeze_bus_device(run_id, timeline):
    """The reattach leg's stall staging: `docker pause` freezes the
    register device in place — the socket stays open and unanswered,
    so the driver's in-flight exchange runs into its declared
    per-request timeout and the field is transiently unreachable
    without anything dying. `thaw_bus_device` resumes it.
    Recorded on the run's action timeline; a docker failure raises so
    the leg reports the freeze never landed."""
    container = _bus_container(run_id, 'device')
    timeline('bus-device-freeze', 'docker pause ' + container)
    docker('pause', container, timeout=30)
    timeline('bus-device-frozen', container + ' frozen')


def thaw_bus_device(run_id, timeline):
    """The recovery half of freeze_bus_device: `docker unpause`
    resumes the frozen register device, so the driver's next
    re-attached exchange finds it answering again. Recorded on the
    run's action timeline; a docker failure raises so the leg reports
    the thaw never landed."""
    container = _bus_container(run_id, 'device')
    timeline('bus-device-thaw', 'docker unpause ' + container)
    docker('unpause', container, timeout=30)
    timeline('bus-device-thawed', container + ' running')


def bus_device_ctl(run_id, device_port, *args):
    """The scenario-callable field-tool invocation against the
    register device: `docker exec` runs the shipped `dcs-sim-bus-ctl`
    inside the device container against its loopback listener —
    the device is bridge-placed, reachable only by container name, so
    its own census reaches the lane through the container's netns, the
    same seam `plant_ctl` gives the deployed pair's plant and
    `born_field_ctl` the born legs' scratch field. `check=False`
    returns the CompletedProcess on a refused request: the tool's
    nonzero exit and stderr are the answer the caller classifies —
    how it tells a frozen or restarted device from a serving one —
    not a docker failure."""
    container = 'dcs-hw-' + run_id + '-' + BUS_RIG_SUFFIX['device']
    return docker('exec', container, 'dcs-sim-bus-ctl',
                  '127.0.0.1:' + str(device_port), *args,
                  check=False, timeout=60)


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
    controller restart/cold-restart/relaunch, plant stop/start,
    model-revision, foreign-peer launch/teardown, driven-peer
    launch/teardown, born-active launch/teardown/state reads, and
    born-field serve/silence/freeze actions, and
    forged-checkpoint-endpoint
    launch/teardown actions, the run's shared --pair-token the
    announced-source legs' keyed posture answers, the shipped plant
    tool's docker-exec invocation, the run config's recorded endpoint
    placements and the
    run's rig bridge name — the placement rule a scenario attachment
    follows when it needs an endpoint a rig peer must dial — the
    drain-stall tracer lever the durable-history leg's parked-writer
    induction drives (None where the runner admits no tracer) — and the
    host-side
    per-controller state/journal/history files the restart and
    model-revision
    scenarios read — and, under 'probe', the same ctx shape
    re-pointed at the lane-staged keyed probe pair so the keyed
    announced-source legs can name it their subject while the
    deployed pair runs whichever posture the run config gives it —
    and, under 'bus', the lane-staged register-mapped field rig the
    sim-bus driver-reattach leg names its subject (None where the run
    config stages no bus rig)."""
    run_id = record['run_id']
    names = {'active': 'a', 'standby': 'b', 'revised': 'c',
             'foreign': 'foreign', 'driven': 'd'}
    mounts = _state_file_mounts(cfg)
    lever = _drain_stall_lever()
    ctx = {
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
        # The flag-doctoring relaunch — docker rm + a recreated launch
        # with `track` naming the member's tracking-source argument
        # (--peer on the launched active, --standby on the launched
        # standby); track=None restores the launch command. The
        # standby-dns-resume leg's seam: a plain docker start could
        # never stage the doctored name.
        'relaunch_controller': lambda name, track=None:
            relaunch_controller(
                cfg, record, run_dir, src / cfg['model_fixture'], name,
                timeline, track),
        # The tracking-source address-move staging — the
        # rediscovery leg's reproduction of the stale-IP-pin
        # finding: the runner removes the named member's container,
        # holds its freed bridge address on a placeholder, and
        # recreates the launch so the configured DNS name resolves
        # to a NEW address. release_address_placeholder frees the
        # held address again.
        'move_controller_address': lambda name: move_controller_address(
            cfg, record, run_dir, src / cfg['model_fixture'], name,
            timeline),
        'release_address_placeholder': lambda:
            release_address_placeholder(run_id, timeline),
        # The sink-isolation leg's mount lever — the impede/restore
        # pair on the endpoints the run config declares a stalled
        # mount kind for. No declaration means no lever, and the leg
        # reports inconclusive rather than probing a mount it was
        # never granted.
        'impede_state_file': (lambda name: impede_state_file(
            run_id, run_dir, name, timeline, mounts))
            if mounts else None,
        'restore_state_file': (lambda name: restore_state_file(
            run_id, run_dir, name, timeline))
            if mounts else None,
        'stop_controller': lambda name: stop_controller(
            run_id, name, timeline),
        'start_controller': lambda name: start_controller(
            run_id, name, timeline),
        # The frozen-source induction — docker pause/unpause on a
        # controller container, the orphan-episode leg's lever for
        # produced-nothing checkpoint-pull misses.
        'pause_controller': lambda name: pause_controller(
            run_id, name, timeline),
        'unpause_controller': lambda name: unpause_controller(
            run_id, name, timeline),
        'failover_misses': cfg['failover_misses'],
        'stop_plant': lambda: stop_plant(run_id, timeline),
        'start_plant': lambda: start_plant(run_id, timeline),
        # The wedged-field levers — docker pause/unpause freeze the
        # plant container in place so the remote driver's open socket
        # just stops answering, the bounded-liveness leg's wedge.
        'pause_plant': lambda: pause_plant(run_id, timeline),
        'unpause_plant': lambda: unpause_plant(run_id, timeline),
        # The shipped dcs-plant-ctl inside the plant container — the
        # lane's seam for every plant op the tool's subcommands cover.
        'plant_ctl': lambda *args: plant_ctl(
            run_id, cfg['plant_port'], *args),
        # The doctored-dynamics admission lever — a scenario stages a
        # document by name and gets each admission gate's verdict
        # back; see admit_dynamics for the seam's shape.
        'admit_dynamics': lambda name, document:
            admit_dynamics(cfg, record, run_dir,
                           src / cfg['model_fixture'], name, document,
                           timeline),
        # The persistence-alias launch lever — a scenario declares the
        # three persistence sinks' names and gets the doctored launch's
        # verdict back; see admit_persistence for the seam's shape.
        'admit_persistence': lambda name, paths:
            admit_persistence(cfg, record, run_dir,
                              src / cfg['model_fixture'], name, paths,
                              timeline),
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
        # The born-active startup-failure leg's staging surface
        # (decision 103, #1033): the scratch sim-serve field the leg
        # silences, serves, and freezes — never the deployed pair's own
        # plant — and the labeled scenario seats it launches born-active
        # controllers onto. born_controller_state is the read-only
        # process verdict the undeclared-refusal class's exit evidence
        # comes from.
        'start_born_field': lambda mode: start_born_field(
            cfg, record, run_dir,
            src / (cfg['foreign_model_fixture']
                   if mode == 'foreign' else cfg['model_fixture']),
            src / (cfg['foreign_dynamics_fixture']
                   if mode == 'foreign' else cfg['dynamics_fixture']),
            timeline, mode),
        'pause_born_field': lambda: pause_born_field(run_id, timeline),
        'unpause_born_field': lambda: unpause_born_field(
            run_id, timeline),
        'stop_born_field': lambda: stop_born_field(run_id, timeline),
        'start_born_controller': lambda seat, remote, peer=None,
                standby=None: start_born_controller(
                    cfg, record, run_dir, src / cfg['model_fixture'],
                    seat, remote, timeline, peer=peer, standby=standby),
        'stop_born_controller': lambda seat: stop_born_controller(
            run_id, seat, timeline),
        'born_controller_state': lambda seat: born_controller_state(
            run_id, seat),
        'born_field_ctl': lambda *args: born_field_ctl(run_id, *args),
        # The run's shared --pair-token — the keyed posture the
        # announced-source legs need: absent means the rig verifies
        # nothing and the legs report inconclusive rather than failed.
        'pair_token': cfg.get('pair_token'),
        # The forged-checkpoint endpoint's launch/teardown — the
        # demote-forged-standby leg's hostile announced source. The
        # staged document path the launch returns is the leg's rewrite
        # seam between demote calls.
        'start_forge': lambda document, owner, keyed=True:
            start_forge_endpoint(cfg, record, run_dir, document,
                                 owner, timeline, keyed),
        'stop_forge': lambda: stop_forge_endpoint(run_id, timeline),
        'state_files': {key: str(_controller_dir(run_dir, peer)
                                 / 'state.json')
                        for key, peer in names.items()},
        'journal_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'journal.jsonl')
                          for key, peer in names.items()},
        'history_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'history.jsonl')
                          for key, peer in names.items()},
        # The drain-stall lever — the durable-history leg's parked-
        # writer induction: the container's `dcs-drain` tids, and the
        # tracer park/release pair. None where the runner admits no
        # tracer; the leg reports inconclusive rather than probing
        # threads it was never granted.
        'drain_writers': (lambda name: drain_writers(run_id, name))
                         if lever else None,
        'park_drain_writer': lever[1] if lever else None,
        'release_drain_writer': lever[2] if lever else None,
        'dcs_ctl': str(_dcs_ctl_path(cfg)),
    }
    probe = _probe_pair(cfg)
    if probe is not None:
        subject = _probe_ctx(ctx, cfg, record, src, run_dir, probe,
                             mounts, timeline)
        ctx['probe'] = subject
    else:
        ctx['probe'] = None
    ctx['bus'] = _bus_ctx(cfg, run_id, timeline)
    return ctx


def _bus_ctx(cfg, run_id, timeline):
    """The scenario ctx's register-mapped field subject (#1355) — the
    device rig and the pair bound to it, with the levers the
    sim-bus driver-reattach leg drives.

    Its own small shape rather than a re-pointed copy of the deployed
    ctx: the bus pair is not the deployed pair and none of the
    deployed pair's levers apply to it (it owns no sim-tcp plant to
    stop, no state files to stall, no forge to stand). The subject
    carries the two monitor base URLs, the device-side field-tool
    invocation, and the restart/freeze/thaw levers — every one of
    them None where the run config stages no bus rig, so the leg
    reports inconclusive rather than reading another rig's endpoints.
    """
    bus = _bus_rig(cfg)
    if bus is None:
        return None
    return {
        'active': 'http://127.0.0.1:' + str(bus['active_port']),
        'standby': 'http://127.0.0.1:' + str(bus['standby_port']),
        'device_port': bus['device_port'],
        'device_ctl': lambda *args: bus_device_ctl(
            run_id, bus['device_port'], *args),
        'restart_device': lambda: restart_bus_device(run_id, timeline),
        'freeze_device': lambda: freeze_bus_device(run_id, timeline),
        'thaw_device': lambda: thaw_bus_device(run_id, timeline),
    }


def _probe_ctx(ctx, cfg, record, src, run_dir, probe, mounts,
               timeline):
    """The scenario ctx re-pointed at the run's staged probe pair —
    the subject keyed announced-source legs exercise through
    _keyed_subject while the deployed pair runs unkeyed.

    Same keys the deployed ctx carries, rebound to the probe pair's
    own containers, published monitor ports, and plant: the lifecycle
    actions take the probe pair's containers, start_driven/
    start_forge bind the probe pair's --remote and --pair-token, and
    the host-side state/journal paths live under controllers/probe-*.
    'plant' is None — the probe field is bridge-placed (rig-dialed
    only); plant-side work goes through the probe pair's own
    plant_ctl exec. The revised/foreign launch actions are deployed-
    pair actions, not offered here — a leg needing them declines on
    absence rather than launching on the wrong pair.
    """
    run_id = record['run_id']
    probe_names = {'active': 'probe-a', 'standby': 'probe-b',
                   'driven': 'probe-d'}
    probe_mounts = any(key.startswith('probe_') for key in mounts)
    tokens = _plant_owner_tokens(cfg)
    subject = dict(ctx)
    subject.update({
        'active': 'http://127.0.0.1:' + str(probe['active_port']),
        'standby': 'http://127.0.0.1:' + str(probe['standby_port']),
        'revised': None,
        'foreign': None,
        'driven': 'http://127.0.0.1:' + str(probe['driven_port']),
        'plant': None,
        # The probe pair's own pinned claim tokens — never the
        # deployed pair's.
        'plant_owner': {'active': tokens['probe_active'],
                        'standby': tokens['probe_standby'],
                        'driven': tokens['probe_driven']},
        'restart_controller': lambda name: restart_controller(
            run_id, name, timeline, pair='probe'),
        'cold_restart_controller': lambda name:
            cold_restart_controller(run_id, run_dir, name, timeline,
                                    pair='probe'),
        'relaunch_controller': lambda name, track=None:
            relaunch_controller(
                cfg, record, run_dir, src / probe['model_fixture'],
                name, timeline, track, pair='probe'),
        'move_controller_address': lambda name: move_controller_address(
            cfg, record, run_dir, src / probe['model_fixture'], name,
            timeline, pair='probe'),
        'release_address_placeholder': lambda:
            release_address_placeholder(run_id, timeline,
                                        pair='probe'),
        'impede_state_file': (lambda name: impede_state_file(
            run_id, run_dir, name, timeline, mounts, pair='probe'))
            if probe_mounts else None,
        'restore_state_file': (lambda name: restore_state_file(
            run_id, run_dir, name, timeline, pair='probe'))
            if probe_mounts else None,
        'stop_controller': lambda name: stop_controller(
            run_id, name, timeline, pair='probe'),
        'start_controller': lambda name: start_controller(
            run_id, name, timeline, pair='probe'),
        'pause_controller': lambda name: pause_controller(
            run_id, name, timeline, pair='probe'),
        'unpause_controller': lambda name: unpause_controller(
            run_id, name, timeline, pair='probe'),
        'stop_plant': lambda: stop_plant(run_id, timeline,
                                         pair='probe'),
        'start_plant': lambda: start_plant(run_id, timeline,
                                           pair='probe'),
        'pause_plant': lambda: pause_plant(run_id, timeline,
                                           pair='probe'),
        'unpause_plant': lambda: unpause_plant(run_id, timeline,
                                               pair='probe'),
        'plant_ctl': lambda *args: plant_ctl(
            run_id, probe['plant_port'], *args, pair='probe'),
        # The doctored-dynamics admission lever, bound to the probe
        # pair's own model fixture.
        'admit_dynamics': lambda name, document:
            admit_dynamics(cfg, record, run_dir,
                           src / probe['model_fixture'], name,
                           document, timeline),
        # The persistence-alias launch lever, bound to the probe
        # pair's own model fixture — the scratch launch is pair-blind:
        # networkless, unlabeled with any peer, its sinks under the
        # probe's own persistence-probes directory.
        'admit_persistence': lambda name, paths:
            admit_persistence(cfg, record, run_dir,
                              src / probe['model_fixture'], name,
                              paths, timeline),
        'start_revised': None,
        'start_foreign': None,
        'stop_foreign': None,
        'start_driven': lambda name: start_driven_controller(
            cfg, record, run_dir, src / probe['model_fixture'],
            name, timeline, pair='probe'),
        'stop_driven': lambda: stop_driven_controller(
            run_id, timeline, pair='probe'),
        # The probe pair's shared --pair-token — always set: the
        # keyed legs the deployed pair's posture cannot serve run
        # against this subject.
        'pair_token': probe['pair_token'],
        'start_forge': lambda document, owner, keyed=True:
            start_forge_endpoint(cfg, record, run_dir, document,
                                 owner, timeline, keyed, pair='probe'),
        'stop_forge': lambda: stop_forge_endpoint(run_id, timeline,
                                                  pair='probe'),
        'state_files': {key: str(_controller_dir(run_dir, peer)
                                 / 'state.json')
                        for key, peer in probe_names.items()},
        'journal_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'journal.jsonl')
                          for key, peer in probe_names.items()},
        'history_files': {key: str(_controller_dir(run_dir, peer)
                                   / 'history.jsonl')
                          for key, peer in probe_names.items()},
        # The tracer park/release pair is tid-addressed — pair-agnostic;
        # only the tid listing binds the probe pair's containers.
        'drain_writers': (lambda name: drain_writers(
            run_id, name, pair='probe'))
                         if ctx.get('park_drain_writer') else None,
        'park_drain_writer': ctx.get('park_drain_writer'),
        'release_drain_writer': ctx.get('release_drain_writer'),
    })
    return subject


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
    probe = _probe_pair(cfg)
    if probe is not None:
        # The staged probe pair's placements: its monitors publish
        # on host loopback like the deployed pair's; its sim-serve
        # plant is rig-dialed only — recorded 'bridge', since only
        # the probe pair's own rig peers attach to it.
        for key in ('probe_active', 'probe_standby',
                    'probe_driven'):
            if placements[key] != 'loopback':
                raise RuntimeError('endpoint_placement records '
                                   + key + ' as '
                                   + repr(placements[key])
                                   + ' but the rig publishes it on '
                                   'host loopback')
        if placements['probe_plant'] != 'bridge':
            raise RuntimeError('endpoint_placement records '
                               'probe_plant as '
                               + repr(placements['probe_plant'])
                               + ' but the probe field is rig-dialed '
                               'only — no host-side attachment '
                               'exists')
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
    # The announced-source contract is keyed-only: both pair members
    # carry the run config's shared --pair-token so a demoted owner's
    # verify pull can demand the keyed line_proof. An empty token runs
    # the rig unkeyed — where every announced-only demotion refuses.
    keyed = bool(cfg.get('pair_token'))
    docker(*_docker_run_args(cfg, run_id, prefix + '-a'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['active_port']) + ':'
           + str(PAIR_MONITOR_PORTS['active']),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, 'a'))
           + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *_controller_argv(cfg, 'deployed', 'active', prefix))
    docker(*_docker_run_args(cfg, run_id, prefix + '-b'),
           '--network', net,
           '-p', '127.0.0.1:' + str(cfg['standby_port']) + ':'
           + str(PAIR_MONITOR_PORTS['standby']),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, 'b'))
           + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *_controller_argv(cfg, 'deployed', 'standby', prefix))
    timeline('rig-up', 'plant + controller pair on ' + net
             + ' (owner tokens active=' + str(tokens['active'])
             + ', standby=' + str(tokens['standby'])
             + (', pair-keyed' if keyed else ', unkeyed') + ')')
    if probe is not None:
        _start_probe_pair(cfg, record, src, run_dir, net, probe,
                          timeline)
    _start_bus_rig(cfg, record, src, run_dir, net, timeline)


def _start_probe_pair(cfg, record, src, run_dir, net, probe,
                      timeline):
    """Stage the run's lane-staged keyed probe pair (#1058): its own
    sim-serve plant plus a redundant controller pair bound to it —
    the subject the keyed announced-source legs exercise while the
    deployed pair runs whatever posture the run config gives it.

    The probe plant is bridge-placed: nothing host-side dials it —
    readiness is probed through the shipped dcs-plant-ctl exec'd
    inside the container's own netns, and every field attachment is
    a rig peer on this run's bridge. Field ownership arbitration is
    per-plant: the pair's --remote and its distinct --owner-token
    pins never touch the deployed pair's field or claim tokens.
    Both controllers carry the probe block's --pair-token, so their
    tracking pulls and checkpoint answers exercise line_proof
    verification regardless of the deployed pair's keyed posture.
    """
    run_id, sha = record['run_id'], record['attempted_sha']
    prefix = 'dcs-hw-' + run_id
    tokens = _plant_owner_tokens(cfg)
    model = src / probe['model_fixture']
    dynamics = src / probe['dynamics_fixture']
    for path in (model, dynamics):
        if not path.is_file():
            raise RuntimeError('probe fixture missing: ' + str(path))
    for name in ('probe-a', 'probe-b'):
        directory = _controller_dir(run_dir, name)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o777)
    container = prefix + '-probe-plant'
    docker(*_docker_run_args(cfg, run_id, container),
           '--network', net,
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(dynamics) + ':/model/dynamics.json:ro',
           'dcs-hwtest/plant:' + sha,
           '/model/plant.json', '--dynamics', '/model/dynamics.json',
           '--listen', '0.0.0.0:' + str(probe['plant_port']))
    # The controllers' --remote attach connects once at startup and
    # exits if the listener is not yet bound. No host port publishes
    # this plant, so readiness runs through the shipped tool inside
    # the container's own netns.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        res = docker('exec', container, 'dcs-plant-ctl',
                     '127.0.0.1:' + str(probe['plant_port']), 'list',
                     check=False, timeout=10)
        if res.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('probe plant listener never bound')
    token = str(probe['pair_token'])
    docker(*_docker_run_args(cfg, run_id, prefix + '-probe-a'),
           '--network', net,
           '-p', '127.0.0.1:' + str(probe['active_port']) + ':'
           + str(PAIR_MONITOR_PORTS['active']),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, 'probe-a'))
           + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *_controller_argv(cfg, 'probe', 'active', prefix))
    docker(*_docker_run_args(cfg, run_id, prefix + '-probe-b'),
           '--network', net,
           '-p', '127.0.0.1:' + str(probe['standby_port']) + ':'
           + str(PAIR_MONITOR_PORTS['standby']),
           '-v', str(model) + ':/model/plant.json:ro',
           '-v', str(_controller_dir(run_dir, 'probe-b'))
           + ':' + CONTAINER_RUN_DIR,
           IMAGE_PREFIX + 'controller:' + sha,
           *_controller_argv(cfg, 'probe', 'standby', prefix))
    timeline('probe-rig-up', 'probe plant + keyed probe pair on '
             + net + ' (owner tokens probe_active='
             + str(tokens['probe_active']) + ', probe_standby='
             + str(tokens['probe_standby']) + ', pair-token '
             + token + ')')


def _wait_monitor(cfg, timeline):
    """Poll every staged pair's monitor until all serve /role: the
    deployed pair plus the staged probe and register-device pairs — a
    launched controller that never answers is a rig defect worth
    surfacing as the run's inconclusive verdict rather than the keyed
    or reattach legs' silent incapacity."""
    urls = [str(cfg['active_port']), str(cfg['standby_port'])]
    probe = _probe_pair(cfg)
    if probe is not None:
        urls += [str(probe['active_port']),
                 str(probe['standby_port'])]
    bus = _bus_rig(cfg)
    if bus is not None:
        urls += [str(bus['active_port']), str(bus['standby_port'])]
    deadline = time.monotonic() + cfg['monitor_timeout']
    while time.monotonic() < deadline:
        try:
            statuses = [scenarios.http_json(
                'GET', 'http://127.0.0.1:' + port + '/role',
                timeout=5)[0] for port in urls]
            if all(status == 200 for status in statuses):
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
