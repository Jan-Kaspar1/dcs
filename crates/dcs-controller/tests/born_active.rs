//! The born-active startup-failure contract — architecture decision 103
//! (#985, implemented by #1017): every startup-failure class a launched
//! active's deferred startup claim can meet settles to the recorded
//! disposition rather than wedging unreported or dying silently. The two
//! QA findings the contract answers replay here:
//!
//! - `launched-active-boot-requires-reachable-plant`: an unreachable
//!   field at boot is no longer a process exit inside driver assembly —
//!   the run stands pending behind the served standby surface (role
//!   `standby`, sync `unsynchronized`, gate closed, commands refused),
//!   and the conditional startup grant retries on each answered field
//!   contact until a verdict lands.
//! - `startup-claim-refusal-leaves-pair-unpaired`: a live incumbent's
//!   refusal settles the launch onto the declared pair's standby —
//!   tracking the `--peer` it named — or exits the process where no
//!   pair was declared.
//!
//! The rig is the failover harness's shape: a `dcs-plant-server`
//! process owns the shared `tank_loop` plant and the controllers load
//! the same model re-pointed at `sim-tcp`, `--driven` so every scan —
//! and therefore every claim probe and deferred ask — happens inside a
//! `POST /scan` request.

use dcs_core::{
    Command, CommandError, CommandOutcome, FieldClaim, JournalEvent, PointId, Role, StandbySync,
    Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::{ClaimGrant, RemoteDriver};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{
    CONTROLLER, SimTcp, controller_model, listening_on, spawn, spawn_controller, spawn_plant,
    workspace_binary,
};

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
/// The born-active's pinned field-ownership token.
const LAUNCHED: u64 = 94;

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-born-active-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A port nothing listens on: bound to learn the address, dropped so
/// the plant server can take it later — the unreachable-field endpoint
/// the pending run retries toward.
fn unclaimed_addr() -> std::net::SocketAddr {
    let held = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = held.local_addr().unwrap();
    drop(held);
    addr
}

/// A `dcs-plant-server` process bound at `addr` — the late-arriving
/// field the pending launch's deferred claim retries toward.
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

/// A write command any run can be asked: the valve output — refused or
/// applied, the receipt's outcome is the served evidence.
fn write_valve() -> Command {
    Command::WriteValue {
        point: PointId(20),
        kind: ValueKind::Float,
        value: Value::Float(0.5),
    }
}

/// Asserts `client` answers `POST /command` with the not-active
/// rejection the pending/refused surface owes: a receipt exists — the
/// served evidence — and its reason names the run's standby role.
fn assert_command_refused(client: &MonitorClient) {
    let receipt = client.command(&write_valve()).unwrap();
    let CommandOutcome::Rejected {
        reason: CommandError::NotActive { role, .. },
    } = receipt.outcome
    else {
        panic!("a pending or rejoined standby must refuse commands: {receipt:?}");
    };
    assert_eq!(role, Role::Standby);
}

/// The launch's stand-down is the journaled evidence: one `RoleChanged`
/// entry `active` → `standby` under the field-arbitration origin — the
/// refusal or inconclusive ask the run stood down on.
fn assert_stood_down(client: &MonitorClient) {
    let journal = client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::RoleChanged {
                from: Role::Active,
                to: Role::Standby,
                origin: Some(dcs_core::SwitchOrigin::Fenced),
                ..
            }
        )),
        "the stand-down transition must journal: {journal:?}"
    );
}

/// The unreachable-field-at-boot class: the launched active neither
/// exits nor wedges silent — it serves the monitor pending behind the
/// honest standby surface, refuses commands and issues no writes while
/// the field cannot be asked, and the first answering contact completes
/// the deferred startup: the grant lands, the gate lifts, and the role
/// settles `promoting` → `active` on the next scan.
#[test]
fn an_unreachable_field_stands_the_launch_pending_until_contact_answers() {
    let dir = scratch("unreachable");
    let plant_addr = unclaimed_addr();
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;

    // The finding's reproduction: the model-declared `sim-tcp` field
    // answers nothing at boot — assembly used to die here.
    let launched = spawn_controller(
        &model,
        &["--owner-token".to_string(), LAUNCHED.to_string()],
        DT,
    );
    let client = MonitorClient::new(launched.addr);

    // The pending surface: served, honest, and inert — standby,
    // unsynchronized, no observed claim, commands refused, scans
    // quiesced.
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    assert_command_refused(&client);
    assert_stood_down(&client);

    // Answering nothing still asks nothing: driven scans issue probes
    // the field cannot answer, and the run stays pending — no claim
    // verdict, no role move.
    client.advance(2).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    client.health().unwrap();

    // The field arrives: the first scan to reach it re-issues the
    // deferred conditional grant, which takes the claim — `promoting`
    // reports the granted transition mid-flight, the next scan settles
    // `active` under the lifted gate.
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );
    // The remote kind's re-attach cadence paces the retry — the
    // recorded one-contact-per-interval bound — so the wait is the
    // contract's own cadence, not a grace.
    std::thread::sleep(
        dcs_sim_net::RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200),
    );
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert!(
        matches!(report.role, Role::Promoting | Role::Active),
        "the answered grant must move the run toward active: {report:?}"
    );
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    // The claim the deferred grant took is this run's: a probe joining
    // under its token meets the shared hold — proof the field never
    // saw a write or claim before the verdict.
    let probe = RemoteDriver::connect(plant_addr).unwrap();
    assert_eq!(probe.ensure_writer(LAUNCHED).unwrap(), ClaimGrant::Shared);
    probe.release_writer().unwrap();

    // The completed startup is journaled too: the deferred grant's
    // landing walks `standby` → `promoting` → `active` under the
    // reclaim origin — the conditional ask finally answered.
    let journal = client.journal(0).unwrap();
    for (from, to) in [
        (Role::Standby, Role::Promoting),
        (Role::Promoting, Role::Active),
    ] {
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::RoleChanged {
                    from: f,
                    to: t,
                    ..
                } if *f == from && *t == to
            )),
            "the deferred grant's {from:?} -> {to:?} must journal: {journal:?}"
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

/// The refused-startup-claim class with a declared pair: the live
/// incumbent's claim refuses the restart's conditional grant, and the
/// run rejoins as the pair's standby — tracking the `--peer` it
/// declared — rather than standing unpaired or preempting the
/// incumbent.
#[test]
fn a_refused_startup_claim_rejoins_the_declared_pair() {
    let dir = scratch("refused-rejoin");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    // The live incumbent: launched active pinned to its token, the
    // field's standing controller claim under it.
    let incumbent = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let incumbent_client = MonitorClient::new(incumbent.addr);
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    // The stale restart: same field, fresh token, `--peer` naming the
    // pair it rejoins when the field refuses it.
    let restart = spawn_controller(
        &model,
        &[
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--peer".to_string(),
            incumbent.addr.to_string(),
        ],
        DT,
    );
    let client = MonitorClient::new(restart.addr);

    // The verdict already settled at activation: standby behind the
    // served surface, claim observed held, commands refused.
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, Some(FieldClaim::Held));
    assert_command_refused(&client);
    assert_stood_down(&client);

    // The refusal's observed claimant journals the incumbent's token —
    // the attribution the field's arbitration answered with.
    let journal = client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::FieldClaimObserved { claimant, .. } if *claimant == INCUMBENT
        )),
        "the incumbent's refusal must attribute: {journal:?}"
    );

    // Rejoined, not unpaired: driven scans pull the declared peer's
    // checkpoints and the standby converges.
    client.advance(2).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(
            report.sync,
            Some(StandbySync::Tracking { .. } | StandbySync::Reinitialized { .. })
        ),
        "the declared pair must track, not stand unpaired: {report:?}"
    );

    // The incumbent was never preempted: its claim still names its own
    // token and its run keeps owning the field.
    let probe = RemoteDriver::connect(plant.addr).unwrap();
    assert_eq!(
        probe.ensure_writer(INCUMBENT).unwrap(),
        ClaimGrant::Shared,
        "the incumbent's claim was preempted"
    );
    probe.release_writer().unwrap();
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    let _ = std::fs::remove_dir_all(&dir);
}

/// The refused-startup-claim class without a declared pair: the record
/// chooses process exit — the run has no pair to rejoin and must not
/// stand an unpaired active — with the refusal named on the way out.
/// Both monitored launch shapes take the same disposition.
#[test]
fn a_refused_startup_claim_undeclared_exits() {
    let dir = scratch("refused-exit");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    let incumbent = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let incumbent_client = MonitorClient::new(incumbent.addr);
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    for mode in [vec!["--driven", "--dt", DT], vec!["--scan-ms", "50"]] {
        let mut args = vec![
            model.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ];
        args.extend(mode.into_iter().map(String::from));
        let output = Process::new(CONTROLLER).args(&args).output().unwrap();
        assert!(!output.status.success(), "the unpaired refusal ran");
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains("no --peer was declared, so there is no pair to rejoin"),
            "{stderr}"
        );
        // The incumbent's claim stands untouched through each exit —
        // a preempted claim would fence its next write.
        let probe = RemoteDriver::connect(plant.addr).unwrap();
        assert_eq!(probe.ensure_writer(INCUMBENT).unwrap(), ClaimGrant::Shared,);
        probe.release_writer().unwrap();
        incumbent_client.advance(1).unwrap();
        assert_eq!(incumbent_client.role().unwrap().role, Role::Active);
    }

    let _ = std::fs::remove_dir_all(&dir);
}
