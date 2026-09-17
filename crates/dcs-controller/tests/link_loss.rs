//! Plant-link loss and recovery under the remote field driver — the
//! QA finding's acceptance contract, taken on the survive side: the
//! field-owning controller keeps scanning and its monitor keeps serving
//! degraded telemetry while the shared plant is down, and the plant's
//! return re-serves the same attachment — the link re-attaching and the
//! recorded single-writer claim re-arming — instead of needing a
//! controller restart.
//!
//! The rig is the failover harness's shape: one `dcs-plant-server`
//! process owns the shared `tank_loop` plant and two `dcs-controller`
//! processes load the same model re-pointed at `sim-tcp`, both
//! `--driven` so every scan happens inside a `POST /scan` request. The
//! field owner under test is the *promoted* standby: its `POST
//! /promote` took the plant's writer claim, which is what the outage
//! drops and the recovery must re-arm — probed by a third-party
//! attachment whose `step` the claim fences.
//!
//! The plant's stop/start is a process kill and respawn on the *same*
//! published port — the shape `docker stop`/`docker start` gives the QA
//! lane: a new server lifetime whose claim table came back empty.

use dcs_core::{LinkState, PointId, Quality, Role, StandbySync};
use dcs_monitor::MonitorClient;
use dcs_sim_net::{RemoteDriver, RemoteError};
use std::net::{SocketAddr, TcpListener};
use std::path::Path;
use std::time::{Duration, Instant};

mod support;

use support::{
    SimTcp, controller_model, image_sample, image_value, kill, listening_on, spawn,
    spawn_controller, workspace_binary,
};

/// The shared plant's model — the dcs-plant tank loop: level raw (10)
/// and setpoint (11) in, valve command (20) out, an analog-input scaling
/// and a PID parameterized for dt 0.1.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
/// The plant-side physics: the raw level lags the valve with τ = 2 s.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
/// The controller-side model source — the shared tank-loop model whose
/// devices [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the pair runs converged before the switchover.
const N: u64 = 10;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);

/// A port the respawned plant rebinds: bound, learned, and released —
/// the narrow race against another listener taking it is the same one
/// every ephemeral-bind test accepts.
fn free_addr() -> SocketAddr {
    TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap()
}

/// A `dcs-plant-server` process serving `model` with `dynamics` merged
/// in, bound on the fixed `addr` so a respawn re-serves the address the
/// controllers' models point at.
fn spawn_plant_at(model: &Path, dynamics: &Path, addr: SocketAddr) -> support::Spawned {
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

/// The fencing probe the claim contract is read through: a third-party
/// attachment's `step` is refused while a writer claim stands. `None`
/// while the plant is not serving — one lost poll, not the verdict.
fn fenced_probe(addr: SocketAddr) -> Option<bool> {
    let probe = RemoteDriver::connect(addr).ok()?;
    Some(matches!(probe.step(0.0), Err(RemoteError::Fenced)))
}

#[test]
fn the_active_controller_rides_out_a_plant_restart() {
    let dir = std::env::temp_dir().join(format!("dcs-link-loss-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant_addr = free_addr();
    let mut plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );
    let pair_model =
        controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;

    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // N converged ticks, then the documented switchover: demote the
    // active, promote the tracking standby — the promote takes the
    // plant's single-writer claim, the standing ownership the outage
    // drops and recovery must re-arm.
    for tick in 1..=N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        assert_eq!(
            image_value(&standby.snapshot().unwrap(), SETPOINT),
            image_value(&active.snapshot().unwrap(), SETPOINT),
            "tick {tick}"
        );
    }
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged"
    );
    active.demote().unwrap();
    standby.promote().unwrap();
    active.advance(1).unwrap();
    let promoted = standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(active.role().unwrap().role, Role::Standby);
    // The promoted peer's claim stands at the field: a third-party
    // attachment's mutation is refused.
    assert_eq!(
        fenced_probe(plant_addr),
        Some(true),
        "the promoted owner never claimed the field"
    );
    let healthy_tick = promoted.tick;

    // The outage: the plant process dies — every attachment's link
    // severs and the port frees, exactly as `docker stop` leaves it.
    kill(&mut plant);

    // Through the outage both monitors keep answering: scans complete
    // with the field boundary counting its failures — reads held
    // bad:communication_fault, writes refused at the dead link, the
    // driver's own surface reporting disconnected — and neither role
    // moves: field loss is not a failover signal.
    let mut outage = None;
    for _ in 0..3 {
        let snapshot = standby
            .advance(1)
            .expect("the active's monitor stopped answering on field loss");
        outage = Some(snapshot);
        assert_eq!(standby.role().unwrap().role, Role::Active);
        assert_eq!(
            active.role().unwrap().role,
            Role::Standby,
            "field loss moved the demoted peer's role"
        );
    }
    let outage = outage.unwrap();
    assert!(
        outage.tick > healthy_tick,
        "the active's scans stopped with the plant"
    );
    for point in [LEVEL, SETPOINT] {
        assert!(
            !image_sample(&outage, point).quality.is_good(),
            "field input {point:?} kept reading good through the outage"
        );
    }
    let health = &outage.io_health;
    assert!(health.failed_reads > 0, "no read failures counted");
    assert!(health.failed_writes > 0, "no write failures counted");
    assert_eq!(
        health.driver.as_ref().map(|driver| driver.link),
        Some(LinkState::Disconnected)
    );
    assert!(health.last_error.is_some());
    let outage_reads = health.failed_reads;
    let outage_writes = health.failed_writes;

    // The return: a new plant lifetime on the same published port —
    // its claim table empty, the owner's re-attach owed the re-arm.
    let _restarted = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );

    let deadline = Instant::now() + Duration::from_secs(60);
    let recovered = loop {
        if Instant::now() > deadline {
            panic!("the returned plant was never re-served by the run");
        }
        // The scan's first field access re-attaches and re-arms the
        // recorded claim; the fencing probe reads the result from the
        // plant's side.
        let Ok(snapshot) = standby.advance(1) else {
            std::thread::sleep(Duration::from_millis(200));
            continue;
        };
        let reclaimed = fenced_probe(plant_addr).unwrap_or(false);
        let healthy = [LEVEL, SETPOINT]
            .iter()
            .all(|&point| image_sample(&snapshot, point).quality == Quality::Good);
        let linked = snapshot
            .io_health
            .driver
            .as_ref()
            .is_some_and(|driver| driver.link == LinkState::Connected);
        if reclaimed && healthy && linked && snapshot.tick > outage.tick {
            break snapshot;
        }
        std::thread::sleep(Duration::from_millis(200));
    };

    // The recovered run still carries the outage's record: the failure
    // counters and the last-fault report are history, not state to
    // reset on a good link.
    let health = &recovered.io_health;
    assert!(health.failed_reads >= outage_reads);
    assert!(health.failed_writes >= outage_writes);
    assert!(health.last_error.is_some());
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(active.role().unwrap().role, Role::Standby);
}
