//! The QA finding `demoted-peer-tracks-announced-wildcard-addr`
//! exercised on real network namespaces — the reproduction's own
//! topology, which no in-process or same-host rig can stand in for.
//!
//! A tracking standby's checkpoint pulls announce its monitor address
//! as `GET /checkpoint?peer=<addr>`, and the paced loop announces
//! `local_addr()` verbatim — the *bound* socket address. Under the
//! shipped deployment shape (`--listen 0.0.0.0:<port>`, declared by the
//! reference plant's own `deploy/manifest.json`) that bind address is
//! the wildcard: routable everywhere the puller is, but meaningless as
//! a *target* — `0.0.0.0` dials the connecting host's own stack. The
//! serving monitor resolves the wildcard to the pull connection's
//! proven source address before recording it as its demotion tracking
//! source, so a demoted launched active follows a routable address
//! back to its successor.
//!
//! Same-host tests cannot see the defect: every namespace-local dial
//! of `0.0.0.0:<port>` resolves to loopback, where the wildcard-bound
//! peer answers — the recorded source being wrong only shows up when
//! the peers cannot share a stack. This test puts each controller in
//! its own network namespace — `unshare -Urn` for the supervising
//! namespace, one child `unshare -n` per peer, a point-to-point `veth`
//! pair routed between them through the supervising namespace the way
//! the deployment's docker bridge routes between containers — so a
//! recorded `0.0.0.0` tracking source would dial the demoted peer's
//! own netns and refuse forever, exactly the reported failure.
//!
//! The scenario is the issue's — on the keyed pair a real redundant
//! deployment declares, since an announced-only demotion now verifies
//! against the keyed `line_proof` and refuses unkeyed: the standby
//! converges on `--standby <active>:<port>` while the launched active
//! was never told a peer; the documented `POST /demote` then
//! `POST /promote` switch moves the field; the demoted peer must reach `tracking` on
//! its announced successor inside the convergence grace — and the same
//! switch back must restore the launch arrangement, proving the peer
//! stayed re-promotable. Every degraded `sync` report the run sees is
//! additionally checked for a `0.0.0.0` or `[::]` target, so a
//! wildcard tracking source fails loudly even before the grace lapses.
//!
//! The test skips — reporting the missing capability — where
//! unprivileged user namespaces are unavailable (`unshare -Urn`
//! refused, or `ip`/`nsenter`/`python3` absent); the in-process cover
//! in `dcs-monitor`'s tracking tests keeps the contract asserted
//! there. The QA lane re-verifies the merged fix on the rig.

use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

mod support;

use support::CONTROLLER;

/// The model both controllers load — local `sim` devices, so each
/// peer's field is its private simulated copy; the announce contract
/// under test lives entirely on the monitor's checkpoint path.
const MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-monitor/fixtures/monitor.json"
);

/// The scenario's wall-clock bound: a broken announced source degrades
/// the demoted peer forever, so any grace this side of forever
/// distinguishes reconvergence from the defect — thirty seconds is
/// several hundred 50 ms scan cycles of slack.
const SCENARIO_TIMEOUT: Duration = Duration::from_secs(180);

/// The in-namespace driver: plumbs the topology, spawns the paced
/// pair, and walks the converge → switch → restore sequence over HTTP,
/// exiting nonzero with the last report on any violation. Paths arrive
/// as argv — the controller binary, the model file, and the scratch
/// directory the peer logs land in.
const DRIVER: &str = r#"
import json
import subprocess
import sys
import time
import urllib.request

CTL, MODEL, SCRATCH = sys.argv[1], sys.argv[2], sys.argv[3]

# Each peer owns a point-to-point /30 with this supervising namespace —
# the docker bridge's job in the shipped deployment. Peer A's monitor
# listens on 10.10.0.2:8080 from the outside; peer B's on 10.10.1.2:8081.
A = "http://10.10.0.2:8080"
B = "http://10.10.1.2:8081"
GRACE = 30.0


def sh(*args):
    subprocess.run(args, check=True)


sh("ip", "link", "set", "lo", "up")
with open("/proc/sys/net/ipv4/ip_forward", "w") as handle:
    handle.write("1")

peers = []
logs = []


def spawn_peer(tag, listen, *extra):
    log = open(SCRATCH + "/" + tag + ".log", "w")
    logs.append(log)
    proc = subprocess.Popen(
        ["unshare", "-n", "--", CTL, MODEL,
         "--scan-ms", "50", "--listen", listen,
         "--pair-token", "dcs-test-pair", *extra],
        stdout=log, stderr=subprocess.STDOUT)
    peers.append(proc)
    return proc


def wire(proc, link, ctrl_ip, peer_ip):
    sh("ip", "link", "add", "v" + link, "type", "veth",
       "peer", "name", "vp" + link, "netns", str(proc.pid))
    sh("ip", "addr", "add", ctrl_ip + "/30", "dev", "v" + link)
    sh("ip", "link", "set", "v" + link, "up")
    for cmd in (
            ("ip", "link", "set", "lo", "up"),
            ("ip", "addr", "add", peer_ip + "/30", "dev", "vp" + link),
            ("ip", "link", "set", "vp" + link, "up"),
            ("ip", "route", "add", "default", "via", ctrl_ip)):
        sh("nsenter", "-t", str(proc.pid), "-n", *cmd)


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
                    who + " tracks an unroutable announced source: "
                    + detail)
            if tracking(report):
                return report
        time.sleep(0.25)
    raise SystemExit(who + " never reached tracking: " + repr(report))


a = spawn_peer("a", "0.0.0.0:8080")
b = spawn_peer("b", "0.0.0.0:8081", "--standby", "10.10.0.2:8080")
try:
    time.sleep(0.5)
    wire(a, "a", "10.10.0.1", "10.10.0.2")
    wire(b, "b", "10.10.1.1", "10.10.1.2")

    # Converge: the standby's per-scan pulls announce its wildcard bind
    # address on the field owner; the owner's recorded tracking source
    # must be the pull connection's proven source, not 0.0.0.0.
    print("standby converged:", await_tracking(B, "the standby"),
          flush=True)

    # The documented switchover: demote the launched active, promote the
    # converged standby. The demoted peer's tracking source is whatever
    # the announce recorded — the wildcard this finding names, or the
    # resolved pull source the contract requires.
    print("demote a:", post(A + "/demote"), flush=True)
    print("promote b:", post(B + "/promote"), flush=True)
    print("demoted peer reconverged:",
          await_tracking(A, "the demoted peer"), flush=True)

    # The same switch back proves the peer stayed re-promotable and
    # restores the launch arrangement.
    print("demote b:", post(B + "/demote"), flush=True)
    print("promote a:", post(A + "/promote"), flush=True)
    print("restored:", await_tracking(B, "the restored standby"),
          flush=True)
    print("WILDCARD-ANNOUNCE-OK", flush=True)
finally:
    for proc in peers:
        proc.kill()
    for proc in peers:
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

/// Two `dcs-controller` processes in separate network namespaces, both
/// `--listen 0.0.0.0:<port>` like the shipped container deployment: the
/// standby converges on the launched active, the documented
/// demote-then-promote switch runs, and the demoted peer must reach
/// `tracking` on its announced successor — an address that stays
/// routable only because the serving monitor resolved the announced
/// wildcard to the pull connection's proven source. On the defective
/// build the demoted peer's pulls dial `0.0.0.0:8081` — its own
/// namespace's stack — and degrade `connection refused` forever.
#[test]
fn a_demoted_launched_active_tracks_its_announced_successor_across_namespaces() {
    if let Some(missing) = missing_capability() {
        eprintln!("skipping the network-namespace pair: {missing}");
        return;
    }

    let dir = std::env::temp_dir().join(format!("dcs-wildcard-announce-{}", std::process::id()));
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
        .arg(MODEL)
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
        status.success() && log.contains("WILDCARD-ANNOUNCE-OK"),
        "the namespace pair scenario failed ({status}); driver log:\n{log}"
    );
}
