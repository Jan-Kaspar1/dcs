//! The QA finding `remote-born-active-claims-foreign-model-field`: a
//! `--remote` launch's field attachment is the same claim surface the
//! `sim-tcp` device backend arbitrates through, but the born-active
//! shape built a bare `RemoteDriver` — the declared-point
//! correspondence probe lived only inside the `sim-tcp` spec-builder,
//! so a miswired endpoint (the wrong plant container, stale DNS, a
//! copy-paste deployment) let a launched active seize write-ownership
//! of a foreign plant and drive it, the mismatch surfacing only as
//! per-point quality faults — a wrong-plant actuation path.
//!
//! The contract the finding demands is the `sim-tcp` rule applied to
//! the `--remote` attachment: every channel-bound `io_point` the model
//! declares must answer on the plant server with the declared value
//! kind before the field's write-ownership claim may land. A plant
//! that answers serving a different model fails the launch outright —
//! the same assembly verdict the `sim-tcp` factory returns — while a
//! plant that cannot answer at all leaves the probes outstanding on
//! the attachment, re-run ahead of every deferred startup-claim ask:
//! a field that thaws serving a foreign model keeps the pending run
//! waiting, the claim never landing.
//!
//! The rig mirrors the QA reproduction: a `dcs-plant-server` serving
//! the dosing skid — whose point set shares pump_station's low ids but
//! declares nothing at 120 — against a `dcs-controller --remote` born
//! active loading the pump station model.

use dcs_core::{FieldClaim, Role, StandbySync};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::net::TcpListener;
use std::path::Path;
use std::process::Command as Process;

mod support;

use support::{CONTROLLER, listening_on, spawn, spawn_controller, spawn_plant, workspace_binary};

/// The controller-side model — the QA rig's own pump station. Its
/// channel-bound point 120 is the id the foreign field does not serve,
/// the reproduction's `unknown_point` evidence.
const STATION: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/pump_station.json"
);
/// The foreign field's model — the dosing skid: a different plant
/// answering honestly for the points its own model serves.
const FOREIGN_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid.json"
);
/// The foreign field's physics.
const FOREIGN_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid_dynamics.json"
);
const DT: &str = "0.1";

/// A port nothing listens on: bound to learn the address, dropped so
/// the foreign plant can take it later — the unreachable endpoint the
/// pending launch's deferred probes retry toward.
fn unclaimed_addr() -> std::net::SocketAddr {
    let held = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = held.local_addr().unwrap();
    drop(held);
    addr
}

/// A `dcs-plant-server` bound at `addr` — the late-arriving foreign
/// field the deferred probes meet on the thaw.
fn spawn_plant_at(model: &Path, dynamics: &Path, addr: std::net::SocketAddr) -> support::Spawned {
    spawn(
        &workspace_binary("dcs-plant-server"),
        &[
            model.to_str().unwrap().to_string(),
            "--dynamics".to_string(),
            dynamics.to_str().unwrap().to_string(),
            "--listen".to_string(),
            addr.to_string(),
        ],
        listening_on,
    )
}

/// The eager leg — the QA reproduction itself: the foreign field is
/// already serving when the launch attaches, so the declared-point
/// probe answers inside startup and the miswired `--remote` is an
/// assembly failure, not a running owner. The field's claim must
/// never have landed: a claim outlives its dead holder by design, so
/// a field still reporting `unclaimed` after the launch died is the
/// proof the wrong-plant actuation path stayed closed.
#[test]
fn a_remote_launch_against_a_foreign_field_fails_before_the_claim() {
    let plant = spawn_plant(Path::new(FOREIGN_MODEL), Path::new(FOREIGN_DYNAMICS));

    let output = Process::new(CONTROLLER)
        .args([
            STATION.to_string(),
            "--remote".to_string(),
            plant.addr.to_string(),
            "--ticks".to_string(),
            "3".to_string(),
        ])
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "the launch against the foreign field ran"
    );
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(
        stderr.contains("does not serve io point") || stderr.contains("serves io point"),
        "the launch failure must name the correspondence verdict: {stderr}"
    );

    // The claim never landed: the foreign field's arbitration still
    // reports unclaimed — the launched run died before its startup
    // ask, so no dead token stands fencing the field's rightful owner.
    let probe = RemoteDriver::connect(plant.addr).unwrap();
    assert_eq!(
        probe.probe_writer().unwrap(),
        FieldClaim::Unclaimed,
        "the foreign field must never carry the launch's claim"
    );
}

/// The deferred leg — the reproduction's second surface: the launch's
/// field answers nothing at boot, so the correspondence probes stand
/// outstanding on the attachment and the run waits pending; when the
/// endpoint thaws serving the *foreign* model, the re-run probe refuses
/// ahead of every grant ask — the pending seat is never granted, the
/// field's claim never lands, and the run never reports `active`.
#[test]
fn a_remote_launch_pending_on_silence_never_claims_a_foreign_field() {
    let addr = unclaimed_addr();

    // Boot against the silent endpoint: the transport half defers, so
    // the run stands pending — the honest standby surface, the
    // deferred startup claim carrying its outstanding probes.
    let launched = spawn_controller(
        Path::new(STATION),
        &["--remote".to_string(), addr.to_string()],
        DT,
    );
    let client = MonitorClient::new(launched.addr);
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

    // The field arrives serving the wrong model: every answered
    // contact re-runs the outstanding probes ahead of the grant ask,
    // and the dosing skid answers point 120 unknown — the ask never
    // runs, the run stays pending through every driven scan.
    let _plant = spawn_plant_at(Path::new(FOREIGN_MODEL), Path::new(FOREIGN_DYNAMICS), addr);
    // The remote kind's re-attach cadence paces the retry — the
    // recorded one-contact-per-interval bound — so the wait is the
    // contract's own cadence, not a grace.
    std::thread::sleep(RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200));
    for tick in 0..4 {
        client.advance(1).unwrap();
        let report = client.role().unwrap();
        assert!(
            !matches!(report.role, Role::Active | Role::Promoting),
            "tick {tick}: the foreign field granted the pending claim: {report:?}"
        );
        assert_ne!(
            report.field_claim,
            Some(FieldClaim::Held),
            "tick {tick}: the foreign field's claim reported held: {report:?}"
        );
    }

    // The claim never landed on the field either: probing the plant
    // directly still reports unclaimed — the pending run's grant asks
    // were all refused ahead of the wire.
    let probe = RemoteDriver::connect(addr).unwrap();
    assert_eq!(
        probe.probe_writer().unwrap(),
        FieldClaim::Unclaimed,
        "the foreign field must never carry the pending launch's claim"
    );
}
