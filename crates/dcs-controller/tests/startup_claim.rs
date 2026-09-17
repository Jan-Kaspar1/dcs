//! The startup field-claim ordering — the QA finding's acceptance
//! contract (`doomed-startup-claim-fences-incumbent`): a launched
//! active's preemptive write-ownership claim is the run's first
//! irreversible shared-field side effect, so it lands only after
//! every fallible local startup step — journal replay, monitor bind,
//! peer-address resolution — has succeeded. A starter that then fails
//! leaves no claim fencing the field's healthy owner; before the
//! reorder, a corrupt journal file's doomed process preempted the
//! incumbent's claim on its way out, and the claim outlived it by
//! design.
//!
//! The rig is the failover harness's shape: a `dcs-plant-server`
//! process owns the shared `tank_loop` plant and the controllers load
//! the same model re-pointed at `sim-tcp`, `--driven` so every scan
//! happens inside a `POST /scan` request. The incumbent runs pinned
//! to owner token 90; each doomed starter carries token 94 and a
//! startup fault — an unreplayable journal file, an unresolvable
//! `--peer` — whose post-claim abort used to strand the preempt.

use dcs_core::Role;
use dcs_monitor::MonitorClient;
use dcs_sim_net::{ClaimGrant, RemoteDriver};
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{CONTROLLER, SimTcp, controller_model, spawn_controller, spawn_plant};

/// The shared plant's model — the dcs-plant tank loop: level raw (10)
/// and setpoint (11) in, valve command (20) out.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
/// The plant-side physics: the raw level lags the valve.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
/// The controller-side model source — the shared tank-loop model whose
/// devices [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// The incumbent's pinned field-ownership token.
const INCUMBENT: u64 = 90;
/// The doomed starter's field-ownership token — a preempt under it is
/// what the field must never see.
const STARTER: u64 = 94;

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-startup-claim-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Runs the controller to completion with `args` — the doomed starter
/// never reaches its `listening on` announcement, so it is collected
/// rather than spawned.
fn run(args: &[String]) -> std::process::Output {
    Process::new(CONTROLLER).args(args).output().unwrap()
}

/// The incumbent's field claim still names its token: a fresh
/// attachment conditionally joining `INCUMBENT`'s claim is granted —
/// flagged shared, the incumbent's live hold beside it — where a
/// preempted claim would fence the join outright. The probe's own
/// hold is handed back, leaving the incumbent's claim exactly as
/// standing.
fn assert_claim_held_by_incumbent(plant: std::net::SocketAddr) {
    let probe = RemoteDriver::connect(plant).unwrap();
    assert_eq!(
        probe.ensure_writer(INCUMBENT).unwrap(),
        ClaimGrant::Shared,
        "the incumbent's claim was preempted"
    );
    probe.release_writer().unwrap();
}

/// A doomed starter's contract: exits nonzero naming its startup
/// failure — and the preemptive claim line never logged, the direct
/// evidence the claim ran after the failing step rather than before.
fn assert_doomed(args: &[String], failure: &str) -> String {
    let output = run(args);
    assert!(!output.status.success(), "the doomed starter ran");
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains(failure), "{stderr}");
    assert!(
        !stderr.contains("write-ownership claim held"),
        "the claim ran before startup validation failed: {stderr}"
    );
    stderr
}

#[test]
fn a_doomed_startup_never_preempts_the_incumbents_field_claim() {
    let dir = scratch("doomed");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model =
        controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    // The healthy incumbent: a launched active pinned to owner token
    // 90 — its startup claim already holds the shared plant.
    let incumbent = spawn_controller(
        &pair_model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let client = MonitorClient::new(incumbent.addr);
    client.advance(2).unwrap();
    assert_eq!(client.role().unwrap().role, Role::Active);

    // The reproduction's doomed starter: the same field under token 94
    // with a journal file whose first line cannot replay — the monitor
    // bind must fail *before* the preemptive claim runs.
    let journal = dir.join("journal.jsonl");
    std::fs::write(&journal, "GARBAGE\n").unwrap();
    let stderr = assert_doomed(
        &[
            pair_model.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            STARTER.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
            "--driven".to_string(),
            "--journal-file".to_string(),
            journal.to_str().unwrap().to_string(),
        ],
        "cannot bind monitor",
    );
    assert!(stderr.contains(journal.to_str().unwrap()), "{stderr}");

    // The incumbent's claim still stands under its own token, and it
    // keeps owning the field through its next scans — a landed stale
    // claim would have fenced its writes and demoted it.
    assert_claim_held_by_incumbent(plant.addr);
    for _ in 0..3 {
        client.advance(1).unwrap();
        assert_eq!(client.role().unwrap().role, Role::Active);
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn every_post_validation_startup_abort_leaves_the_claim_untouched() {
    let dir = scratch("aborts");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model =
        controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    let incumbent = spawn_controller(
        &pair_model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let client = MonitorClient::new(incumbent.addr);
    client.advance(1).unwrap();
    assert_eq!(client.role().unwrap().role, Role::Active);

    let journal = dir.join("journal.jsonl");
    std::fs::write(&journal, "GARBAGE\n").unwrap();
    let base = || {
        vec![
            pair_model.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            STARTER.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ]
    };

    // The same defect class through the other bind paths and failure
    // kinds: a paced run's journal replay, and a driven run's
    // unresolvable tracking-peer address — each an abort that used to
    // land after the preemptive claim.
    let mut paced_journal = base();
    paced_journal.extend([
        "--scan-ms".to_string(),
        "50".to_string(),
        "--journal-file".to_string(),
        journal.to_str().unwrap().to_string(),
    ]);
    assert_doomed(&paced_journal, "cannot bind monitor");

    let mut bad_peer = base();
    bad_peer.extend([
        "--driven".to_string(),
        "--peer".to_string(),
        "not-an-addr".to_string(),
    ]);
    assert_doomed(&bad_peer, "cannot resolve");

    assert_claim_held_by_incumbent(plant.addr);
    for _ in 0..3 {
        client.advance(1).unwrap();
        assert_eq!(client.role().unwrap().role, Role::Active);
    }

    let _ = std::fs::remove_dir_all(&dir);
}
