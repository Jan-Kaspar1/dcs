//! The QA finding `demoted-peer-strands-unsynchronized` at the process
//! boundary its reproduction ran against: a healthy *unkeyed* redundant
//! pair — no `--pair-token`, so the announced-source contract's keyed
//! verification cannot authenticate a tracking peer's `?peer=` hint —
//! where `POST /promote` on the converged standby preempts the field
//! claim and the superseded active's first fenced write demotes it in
//! place. On the defective build the ex-active then reports
//! `standby`/`unsynchronized` forever: every recovery needs a verified
//! tracking source, and an unkeyed run could never prove any endpoint —
//! only a container restart or operator reconfiguration restored the
//! pair, while `/role` kept serving a plausible-looking `standby`.
//!
//! The contract the finding demands: after any loss of active
//! ownership the demoted peer must be able to re-join tracking so the
//! pair retains failover redundancy. The shipped shape is the
//! field-arbitrated successor: a controller declares its monitor
//! address on the field's write-ownership claim, the fencing verdicts
//! report the standing claim's declared monitor, and a demoted peer
//! whose tracking path finds no proven source pulls the endpoint the
//! field's own arbitration names as owner — an identity no announced
//! hint could ever carry, since only actually holding the claim puts
//! a monitor under it — verifies the served checkpoint is this line's
//! field-owning continuation, and adopts it into the journal.
//!
//! Same-host tests cannot see the defect the failed verification
//! recorded: every same-stack dial of `0.0.0.0:<port>` resolves to
//! loopback, where the wildcard-bound successor answers, so a fencing
//! verdict naming the wildcard verbatim is wrong only where the peers
//! cannot share a stack — the documented deployment, whose containers
//! bind `0.0.0.0` behind a routed bridge. The namespace reproduction
//! below runs the issue's own topology — each controller in a private
//! network namespace, wildcard-bound, unkeyed, paced — so a verbatim
//! `0.0.0.0` verdict would dial the demoted peer's own netns and
//! refuse forever, exactly the `qax-20260927-005` evidence.

use dcs_core::{FieldClaim, JournalEvent, Role, StandbySync, SwitchError};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

mod support;

use support::{CONTROLLER, Spawned, listening_on, spawn_logged, spawn_plant, workspace_binary};

/// The shared plant's model — the QA rig's own pump station: the same
/// checked-in document the lane's plant server and both controllers
/// run.
const STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The plant-side physics the lane runs.
const DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../qa_lane/fixtures/pump_station_dynamics.json"
);
const DT: &str = "0.1";
/// The driven-tick bound on the standby reaching `tracking` — the
/// lane converges it in a handful.
const CONVERGE_BOUND: u64 = 24;
/// The driven ticks the demoted peer gets to re-join tracking once
/// the new owner's claim declares its monitor — the resolution runs
/// inside the ordinary tracking cycle, so the bound is small.
const REJOIN_TICKS: u64 = 8;

/// A `--driven` controller *without* `--pair-token` — the unkeyed pair
/// shape the finding's reproduction runs: `support::spawn_controller*`
/// helpers always inject the keyed token, so this spawns the binary
/// directly with the same argument layout minus it.
fn spawn_unkeyed(model: &str, extra: &[String], dt: &str) -> Spawned {
    let mut args = vec![model.to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on).0
}

/// The reproduction's unkeyed pair: one plant server, a launched
/// active claiming the field, and a `--standby` peer converged onto
/// its checkpoints.
struct Pair {
    _plant: Spawned,
    _active_process: Spawned,
    _standby_process: Spawned,
    active: MonitorClient,
    standby: MonitorClient,
}

fn converged_unkeyed_pair() -> Pair {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let active_process = spawn_unkeyed(STATION, &remote, DT);
    let mut standby_args = remote.to_vec();
    standby_args.extend(["--standby".to_string(), active_process.addr.to_string()]);
    let standby_process = spawn_unkeyed(STATION, &standby_args, DT);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            converged = true;
            break;
        }
    }
    assert!(converged, "the standby never converged");

    Pair {
        _plant: plant,
        _active_process: active_process,
        _standby_process: standby_process,
        active,
        standby,
    }
}

/// The issue's reproduction: `POST /promote` on the converged standby
/// of a healthy unkeyed pair. The promoted peer's claim declares its
/// monitor; the ex-active's first fenced write demotes it in place,
/// and its tracking path adopts the field-arbitrated successor — the
/// demoted peer reconverges `tracking` and stays promotable, so the
/// pair's failover redundancy survives the switchover.
#[test]
fn a_remote_promotion_lets_the_demoted_peer_rejoin_tracking() {
    let pair = converged_unkeyed_pair();

    // The evidence's second defect surface stays closed by design:
    // `POST /demote` on the unkeyed owner still needs a proven source
    // to demote onto, and an unverified `?peer=` hint can never be
    // one — the recovery the finding demands is the demoted peer's
    // re-join, not an unproven demotion.
    let (status, body) = pair.active.request("POST", "/demote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NoTrackingSource
    );

    // The takeover: `POST /promote` on the standby — no `POST /demote`
    // boundary ever runs on the old owner, so the tracking-source
    // recovery is the involuntary path's. The ex-active's next field
    // write is fenced and demotes it in place; the fenced settle scan
    // completes the reported transition.
    let promoted = pair.standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    pair.active.advance(1).unwrap();
    pair.standby.advance(1).unwrap();
    pair.active.advance(1).unwrap();
    let report = pair.active.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        pair.active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing preemption must journal on the demoted peer"
    );

    // The defect's wedge, now closed: a demoted unkeyed peer stayed
    // `unsynchronized` forever — no configured source and no provable
    // hint. Under the fix the standing claim's declared monitor
    // resolves as the tracking source: the adoption journals and the
    // peer reconverges `tracking` within a few scans.
    let mut rejoined = false;
    for _ in 0..REJOIN_TICKS {
        pair.standby.advance(1).unwrap();
        pair.active.advance(1).unwrap();
        if matches!(
            pair.active.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            rejoined = true;
            break;
        }
    }
    assert!(
        rejoined,
        "the demoted peer must re-join tracking — the defect left it \
         unsynchronized forever: {:?}",
        pair.active.role().unwrap().sync
    );
    assert!(
        pair.active
            .journal(0)
            .unwrap()
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::TrackingSourceAdopted { .. })),
        "the successor's adoption must journal on the demoted peer"
    );

    // Redundancy restored means the demoted peer is promotable again:
    // promote it back and the second demoted peer re-joins the same
    // way — the claim it just took declares its monitor, so the now-
    // fenced peer resolves it identically. A `409 not_converged` here
    // is the finding's stranded-peer signature.
    pair.active.promote().unwrap();
    pair.standby.advance(1).unwrap();
    pair.active.advance(1).unwrap();
    pair.standby.advance(1).unwrap();
    assert_eq!(pair.active.role().unwrap().role, Role::Active);
    let mut rejoined = false;
    for _ in 0..REJOIN_TICKS {
        pair.active.advance(1).unwrap();
        pair.standby.advance(1).unwrap();
        if matches!(
            pair.standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            rejoined = true;
            break;
        }
    }
    assert!(
        rejoined,
        "the second demoted peer must re-join tracking the same way: {:?}",
        pair.standby.role().unwrap().sync
    );
}

/// The pinned `--owner-token` values the hand-off legs assert on —
/// the field's own fencing verdicts name the standing claim's owner,
/// so fixed tokens let the script tell "the claim still names the
/// demoted peer" from "the claim transferred to the fencing-armed
/// ex-owner".
const OWNER_A: u64 = 0xA0_10_00_01;
const OWNER_B: u64 = 0xB0_20_00_02;
/// The driven ticks the voluntary hand-off gets to complete: the
/// fencing-armed peer's bound reclaim probes every standby scan, so
/// the transfer lands inside a handful — the bound only guards a
/// wedge.
const HANDOFF_TICKS: u64 = 8;

/// The QA finding
/// `demote-orphan-ensure-rearms-claim-under-standby` (#1270): on the
/// unkeyed pair, a routine `POST /demote` on the freshly promoted
/// owner released its write claim — and the demoted run's own
/// orphan-cycle ensure re-armed that claim under its own token inside
/// about one scan. The field then answered `fenced` under a member
/// reporting `standby`: the superseded peer's bound fencing-loss
/// reclaim — every conditional path back, in fact — refused forever,
/// and no journal record named who re-took the claim.
///
/// The contract the finding demands: a voluntary demotion's release
/// is a hand-back the demoted run must not undo — its orphan-cycle
/// ensure stays out of the arbitration it yielded, so the released
/// field stands unclaimed or transfers to the fencing-loss-armed
/// peer's reclaim inside a bounded window, and any orphan-cycle
/// ensure that does land a claim journals the re-arm it performed.
/// Here the released claim is the yielded hand-off the
/// fencing-armed ex-owner's bound reclaim takes: the pair closes on
/// exactly one active — the *other* member — and the demoted run
/// reconverges `tracking`, its journal showing the orphan transition
/// but never a re-arm under its own token.
#[test]
fn a_voluntary_demote_hands_the_claim_to_the_fencing_armed_peer() {
    let plant = spawn_plant(Path::new(STATION), Path::new(DYNAMICS));
    let remote = ["--remote".to_string(), plant.addr.to_string()];
    let mut active_args = remote.to_vec();
    active_args.extend(["--owner-token".to_string(), OWNER_A.to_string()]);
    let active_process = spawn_unkeyed(STATION, &active_args, DT);
    let mut standby_args = remote.to_vec();
    standby_args.extend([
        "--standby".to_string(),
        active_process.addr.to_string(),
        "--owner-token".to_string(),
        OWNER_B.to_string(),
    ]);
    let standby_process = spawn_unkeyed(STATION, &standby_args, DT);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    // The field-side observer: `probe_writer` answers the standing
    // claim's verdict and the verdict's owner token lands in
    // `fenced_by` — the arbitration's own word for who holds the
    // field, exactly as the QA leg's probe read it.
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // The reproduction's healthy precondition: b converges `tracking`
    // on the launched a.
    let mut converged = false;
    for _ in 0..CONVERGE_BOUND {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            converged = true;
            break;
        }
    }
    assert!(converged, "the standby never converged on the active");

    // `POST /promote` on b: its unconditional claim preempts a's; a's
    // first fenced write demotes it in place — the fencing-loss mark
    // that arms a's bound reclaim — and a settles `standby`, tracking
    // the claim's declared monitor.
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    active.advance(1).unwrap();
    standby.advance(1).unwrap();
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(
        field.probe_writer().unwrap(),
        FieldClaim::Held,
        "the promoted peer must hold the claim before the demote"
    );
    assert_eq!(
        field.fenced_by(),
        Some(OWNER_B),
        "the promoted claim must stand under b's token"
    );

    // The routine maintenance action the defect self-pinned on.
    assert_eq!(standby.demote().unwrap().role, Role::Demoting);

    // The hand-off window: the released claim may still *name* b's
    // token — yielded, holderless — while the fencing-armed ex-owner's
    // reclaim converges on it; what the defect produced was the claim
    // re-armed under b's token by b's own ensure, which these ticks
    // must never let stand. The window ends on the first probe that
    // reads unclaimed or names a — and it must end inside the bound.
    let mut handed = None;
    for tick in 1..=HANDOFF_TICKS {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        match field.probe_writer().unwrap() {
            FieldClaim::Unclaimed => {
                handed = Some(None);
                break;
            }
            FieldClaim::Held => match field.fenced_by() {
                Some(OWNER_B) => {}
                owner => {
                    handed = Some(owner);
                    break;
                }
            },
        }
        let b = standby.role().unwrap();
        assert_eq!(
            b.role,
            Role::Standby,
            "tick {tick}: the demoted peer must stay standby: {b:?}"
        );
    }
    assert_eq!(
        handed,
        Some(Some(OWNER_A)),
        "the released claim must transfer to the fencing-loss-armed \
         peer inside {HANDOFF_TICKS} ticks — never re-armed under the \
         demoted peer's token"
    );

    // The reclaim's own resolution: a walks `promoting` → `active` on
    // the claim it re-took and the pair converges on exactly one
    // field owner — b reconverging `tracking` on the successor's
    // declared monitor.
    for _ in 0..HANDOFF_TICKS {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
        if active.role().unwrap().role == Role::Active
            && matches!(
                standby.role().unwrap().sync,
                Some(StandbySync::Tracking { .. })
            )
        {
            break;
        }
    }
    assert_eq!(active.role().unwrap().role, Role::Active);
    let b = standby.role().unwrap();
    assert_eq!(b.role, Role::Standby, "{b:?}");
    assert!(
        matches!(b.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must reconverge on the reclaiming owner: {b:?}"
    );

    // The durable half: the demoted run's orphan transition journals —
    // but no `field_claim_rearmed` may ever land on it, because its
    // own orphan-cycle ensure never re-armed the claim it yielded.
    let journal = standby.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldOrphaned { .. })),
        "the demoted peer's orphan transition must journal: {journal:?}"
    );
    assert!(
        !journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimRearmed { .. })),
        "a voluntary demotion must never journal a re-arm under its \
         own token: {journal:?}"
    );
    // And the pair's *other* side of the defect stays closed: a's
    // reclaim landed inside the same orphan window it refused before —
    // the journaled claimant record attributes the standing claim it
    // probed while b's yielded token still stood.
    let a_journal = active.journal(0).unwrap();
    assert!(
        a_journal
            .iter()
            .any(|entry| matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the fencing demotion must journal on the superseded owner: {a_journal:?}"
    );
}

/// The scenario's wall-clock bound: a stranded demoted peer stays
/// `unsynchronized` forever — the finding's evidence logged 15 s and
/// counting — so any grace this side of forever distinguishes
/// reconvergence from the defect. Thirty seconds is several hundred
/// 50 ms scan cycles of slack.
const SCENARIO_TIMEOUT: Duration = Duration::from_secs(180);

/// The in-namespace driver: plumbs the topology the rig's docker
/// bridge provides — the shared `dcs-plant-server` on the supervising
/// namespace's bound address, each controller in a private namespace
/// wired point-to-point through it — and walks the reproduction over
/// HTTP, exiting nonzero with the last report on any violation. Paths
/// arrive as argv — the controller and plant binaries, the model and
/// dynamics fixtures, and the scratch directory the logs land in.
const DRIVER: &str = r#"
import json
import socket
import subprocess
import sys
import time
import urllib.request

CTL, PLANT, MODEL, DYNAMICS, SCRATCH = sys.argv[1:6]

# Each peer owns a point-to-point /30 with this supervising namespace —
# the docker bridge's job in the shipped deployment. Peer A's monitor
# listens on 10.10.0.2:8080 from the outside; peer B's on 10.10.1.2:8081.
# The shared plant binds the supervising namespace's wildcard port —
# each peer reaches the same listener through its own gateway address,
# exactly like the rig's bridge-local dcs-plant-server.
A = "http://10.10.0.2:8080"
B = "http://10.10.1.2:8081"
PLANT_A = "10.10.0.1:9001"
PLANT_B = "10.10.1.1:9001"
GRACE = 30.0


def sh(*args):
    subprocess.run(args, check=True)


sh("ip", "link", "set", "lo", "up")
with open("/proc/sys/net/ipv4/ip_forward", "w") as handle:
    handle.write("1")

logs = []
holders = []
peers = []

# The field the pair arbitrates on — bound before the peers wire so
# its claim surface answers on both gateway addresses.
log = open(SCRATCH + "/plant.log", "w")
logs.append(log)
plant = subprocess.Popen(
    [PLANT, MODEL, "--dynamics", DYNAMICS, "--listen", "0.0.0.0:9001"],
    stdout=log, stderr=subprocess.STDOUT)


def hold_ns():
    # A placeholder holding a private network namespace: the peer's
    # link must be plumbed before the controller starts — its --remote
    # connect is fatal while the field is unreachable.
    proc = subprocess.Popen(["unshare", "-n", "--", "sleep", "infinity"])
    holders.append(proc)
    return proc


def wire(holder, link, ctrl_ip, peer_ip):
    sh("ip", "link", "add", "v" + link, "type", "veth",
       "peer", "name", "vp" + link, "netns", str(holder.pid))
    sh("ip", "addr", "add", ctrl_ip + "/30", "dev", "v" + link)
    sh("ip", "link", "set", "v" + link, "up")
    for cmd in (
            ("ip", "link", "set", "lo", "up"),
            ("ip", "addr", "add", peer_ip + "/30", "dev", "vp" + link),
            ("ip", "link", "set", "vp" + link, "up"),
            ("ip", "route", "add", "default", "via", ctrl_ip)):
        sh("nsenter", "-t", str(holder.pid), "-n", *cmd)


def spawn_peer(tag, holder, *extra):
    log = open(SCRATCH + "/" + tag + ".log", "w")
    logs.append(log)
    proc = subprocess.Popen(
        ["nsenter", "-t", str(holder.pid), "-n", "--",
         CTL, MODEL, "--scan-ms", "50", *extra],
        stdout=log, stderr=subprocess.STDOUT)
    peers.append(proc)
    return proc


def get(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read())


def post(url):
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read())


def tracking(report):
    sync = report.get("sync")
    return isinstance(sync, dict) and "tracking" in sync


def await_tracking(url, who):
    deadline = time.monotonic() + GRACE
    report = None
    while time.monotonic() < deadline:
        try:
            report = get(url + "/role")
        except Exception:
            report = None
        if report is not None:
            detail = json.dumps(report.get("sync"))
            if "0.0.0.0" in detail or "[::]" in detail:
                raise SystemExit(
                    who + " tracks an unroutable source: " + detail)
            if tracking(report):
                return report
        time.sleep(0.25)
    raise SystemExit(who + " never reached tracking: " + repr(report))


def claim_monitor(expect):
    # The standing claim's declared monitor as a claim-state verdict
    # reports it — the endpoint the field's arbitration hands every
    # peer the claim supersedes. A fresh attachment's probe meets the
    # fenced answer carrying it.
    deadline = time.monotonic() + GRACE
    seen = None
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection(("10.10.0.1", 9001), 3)
            try:
                sock.sendall(b'{"op":"probe_writer"}\n')
                data = b""
                while b"\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()
            error = (json.loads(data).get("error") or {})
            seen = error.get("monitor")
            if error.get("kind") == "fenced" and seen == expect:
                return seen
        except Exception:
            pass
        time.sleep(0.25)
    raise SystemExit("the claim monitor never named " + repr(expect)
                     + " (last verdict: " + repr(seen) + ")")


def journal_payloads(url, kind):
    return [entry["event"][kind]
            for entry in get(url + "/journal?since=0")
            if kind in entry.get("event", {})]


a_ns = hold_ns()
b_ns = hold_ns()
time.sleep(0.5)
wire(a_ns, "a", "10.10.0.1", "10.10.0.2")
wire(b_ns, "b", "10.10.1.1", "10.10.1.2")
try:
    # The reproduction's launch shape: both controllers wildcard-bound —
    # every documented container launch — on the shared field, and the
    # pair unkeyed: no --pair-token, so no announced ?peer= hint can
    # ever prove an endpoint.
    spawn_peer("a", a_ns, "--listen", "0.0.0.0:8080",
               "--remote", PLANT_A)
    spawn_peer("b", b_ns, "--listen", "0.0.0.0:8081",
               "--remote", PLANT_B, "--standby", "10.10.0.2:8080")

    print("standby converged:", await_tracking(B, "the standby"),
          flush=True)

    # The reproduction: POST /promote on the converged standby while the
    # active still owns the field — no demote boundary ever runs on the
    # ex-owner, so the recovery is the involuntary fenced path's.
    print("promote b:", post(B + "/promote"), flush=True)

    # The verdict the ex-owner's fencing rode on must name the
    # successor's dialable monitor — the defect stored the wildcard
    # bind verbatim, which a foreign namespace dials as its own
    # loopback.
    monitor = claim_monitor("10.10.1.2:8081")
    print("claim monitor:", monitor, flush=True)

    # The defect's wedge: the ex-owner's next paced write is fenced and
    # demotes it in place, and its paced tracking cycle must resolve
    # the claim-declared monitor — on the failed fix this dial hit the
    # peer's own loopback and it parked standby/unsynchronized for good.
    report = await_tracking(A, "the demoted peer")
    if report.get("role") != "standby":
        raise SystemExit("the demoted peer is not standby: "
                         + repr(report))
    print("demoted peer reconverged:", report, flush=True)

    if not journal_payloads(A, "field_claim_lost"):
        raise SystemExit("the fencing loss never journaled")
    adopted = [payload.get("source")
               for payload in journal_payloads(A, "tracking_source_adopted")]
    if "10.10.1.2:8081" not in adopted:
        raise SystemExit("no adoption of the successor's routable "
                         "monitor: " + repr(adopted))

    # Redundancy restored means the reconverged ex-owner is promotable —
    # the finding's 409 not_converged — and the peer the return switch
    # fences re-joins on its claim-declared monitor the same way.
    print("promote a:", post(A + "/promote"), flush=True)
    monitor = claim_monitor("10.10.0.2:8080")
    report = await_tracking(B, "the twice-demoted peer")
    if report.get("role") != "standby":
        raise SystemExit("the twice-demoted peer is not standby: "
                         + repr(report))
    print("restored:", report, flush=True)
    print("DEMOTED-REJOIN-OK", flush=True)
finally:
    for proc in peers + holders + [plant]:
        proc.kill()
    for proc in peers + holders + [plant]:
        proc.wait()
    for log in logs:
        log.close()
"#;

/// Whether `args` on `tool` runs at all — the capability probe for each
/// piece of the namespace rig (`unshare`/`nsenter`/`ip`/`python3`),
/// answering the missing piece's name for the skip message.
fn runnable(tool: &str, args: &[&str]) -> bool {
    Command::new(tool)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

/// The capability the rig needs that this environment lacks, if any.
fn missing_capability() -> Option<String> {
    for (tool, args) in [
        ("python3", ["--version"].as_slice()),
        ("ip", ["-Version"].as_slice()),
        ("nsenter", ["--version"].as_slice()),
    ] {
        if !runnable(tool, args) {
            return Some(format!("`{tool}` is not runnable"));
        }
    }
    if !runnable("unshare", &["-Urn", "true"]) {
        return Some("unprivileged user+network namespaces are refused".to_string());
    }
    None
}

/// The finding's reproduction on real network namespaces — the
/// topology the failed verification ran, which no same-host rig can
/// stand in for: each controller wildcard-bound in a private
/// namespace, the shared `dcs-plant-server` bridged through the
/// supervising one, the pair unkeyed and `--scan-ms` paced.
/// `POST /promote` on the converged standby fences the field owner;
/// the demoted peer must resolve the claim-declared monitor and reach
/// `tracking` inside the grace — where a verdict naming the wildcard
/// verbatim, the failed fix's output, dials the demoted peer's own
/// netns and strands it `unsynchronized` forever. The return switch
/// then proves redundancy held in both directions.
///
/// The test skips — reporting the missing capability — where
/// unprivileged user namespaces are unavailable (`unshare -Urn`
/// refused, or `ip`/`nsenter`/`python3` absent); the driven cover
/// above keeps the contract asserted there. The QA lane re-verifies
/// the merged fix on the rig.
#[test]
fn a_demoted_peer_rejoins_on_the_claim_monitor_across_namespaces() {
    if let Some(missing) = missing_capability() {
        eprintln!("skipping the network-namespace pair: {missing}");
        return;
    }

    let dir = std::env::temp_dir().join(format!("dcs-demoted-rejoin-ns-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let driver = dir.join("driver.py");
    std::fs::write(&driver, DRIVER).unwrap();
    let output_log = dir.join("driver.log");

    // The whole scenario lives inside one unprivileged user+network
    // namespace: the driver plumbs the veth pairs and drives HTTP from
    // the supervising namespace — the only side of the boundary the
    // peers' addresses are reachable from.
    let log = std::fs::File::create(&output_log).unwrap();
    let mut child = Command::new("unshare")
        .arg("-Urn")
        .arg("python3")
        .arg(&driver)
        .arg(CONTROLLER)
        .arg(workspace_binary("dcs-plant-server"))
        .arg(STATION)
        .arg(DYNAMICS)
        .arg(&dir)
        .stdin(Stdio::null())
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .expect("cannot spawn unshare");

    let deadline = Instant::now() + SCENARIO_TIMEOUT;
    let status = loop {
        match child.try_wait().unwrap() {
            Some(status) => break status,
            None if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            None => {
                let _ = child.kill();
                let _ = child.wait();
                let log = std::fs::read_to_string(&output_log).unwrap_or_default();
                panic!("the namespace pair scenario timed out; driver log:\n{log}");
            }
        }
    };
    let log = std::fs::read_to_string(&output_log).unwrap_or_default();
    assert!(
        status.success() && log.contains("DEMOTED-REJOIN-OK"),
        "the namespace pair scenario failed ({status}); driver log:\n{log}"
    );
}
