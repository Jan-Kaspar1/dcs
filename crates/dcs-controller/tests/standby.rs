//! Standby synchronization: a second `dcs-controller` instance assembled
//! from the same plant model pulls checkpoints over the monitoring
//! transport (`GET /checkpoint`, per the peer-transport decision), applies
//! them to its running executor, and continues the run identically to the
//! uninterrupted active — the issue's acceptance criterion.
//!
//! Both field-observation modes are covered: a standby-local
//! `SimDriver`, whose private plant each checkpoint's driver section
//! resynchronizes, and a shared plant observed through `RemoteDriver`s,
//! where the standby tracks the real field behind a `WriteGate` that
//! keeps it output-quiescent until promotion.

use dcs_assembly::{assemble, sim_channel_map, sim_driver};
use dcs_controller::registry;
use dcs_core::{IoDriver, PointId, StandbySync, Tick, Value};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{ApplyError, Peer, RestoreError, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::thread;
use std::time::Duration;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");
const TANK_LOOP_PATH: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");

/// The process time each simulated plant advances per scan — the
/// fixture's PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the active runs before the first transfer.
const N: u64 = 30;
/// Ticks of continuation compared against the uninterrupted run.
const M: u64 = 20;

fn tank_loop() -> PlantModel {
    PlantModel::load(TANK_LOOP).unwrap()
}

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// thread exit instead of hanging the scope's join — the same pattern
/// `dcs-sim-net`'s tests use.
trait Stoppable {
    fn stop(&self);
}

impl Stoppable for PlantServer {
    fn stop(&self) {
        self.shutdown();
    }
}

impl Stoppable for Monitor<'_> {
    fn stop(&self) {
        self.shutdown();
    }
}

struct ShutdownOnDrop<'s, T: Stoppable>(&'s T);

impl<T: Stoppable> Drop for ShutdownOnDrop<'_, T> {
    fn drop(&mut self) {
        self.0.stop();
    }
}

/// Acceptance test: an active serving `GET /checkpoint` runs N ticks, the
/// standby applies the transferred checkpoint, and the standby's
/// subsequent snapshots equal the uninterrupted run's — with a second
/// mid-continuation transfer showing repeated transfers keep the standby
/// converged. Both sides run standby-local `SimDriver`s, so the
/// checkpoint's driver section carries the simulated field state.
#[test]
fn standby_with_local_sim_continues_the_actives_run_identically() {
    let model = tank_loop();
    let registry = registry();

    // The active: an executor over a private simulated plant, behind the
    // monitor whose checkpoint endpoint the standby pulls from.
    let active_plant = sim_driver(&model).unwrap();
    let active = assemble(&model, &registry, &active_plant).unwrap();
    let monitor = Monitor::bind(("127.0.0.1", 0), active, model.signal_index()).unwrap();
    let active_client = MonitorClient::new(monitor.local_addr());

    // The standby: the same model and registry assemble an equivalent
    // executor over its own private plant — a gate-less tracking peer.
    let standby_plant = sim_driver(&model).unwrap();
    let mut standby = Peer::standby(assemble(&model, &registry, &standby_plant).unwrap(), None);
    assert_eq!(standby.sync_state(), &StandbySync::Unsynchronized);

    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let _monitor = ShutdownOnDrop(&monitor);

        // Free-running before the first transfer leaves the standby
        // diverged — the checkpoint is what aligns it.
        for _ in 0..3 {
            standby.scan();
            standby_plant.step(DT);
        }

        // The active runs N ticks; each scan advances its plant once.
        for _ in 0..N {
            active_client.advance(1).unwrap();
            active_plant.step(DT);
        }

        // The checkpoint transfers over the monitoring transport; the
        // standby aligns at the checkpointed tick.
        let checkpoint = active_client.checkpoint().unwrap();
        assert_eq!(checkpoint.tick, Tick(N));
        assert!(
            checkpoint.driver.is_some(),
            "a local SimDriver captures the field state the standby restores"
        );
        standby.apply(&checkpoint).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(N) }
        );
        assert_eq!(standby.aligned_tick(), Some(Tick(N)));

        // The continuation: each tick the active scans and the standby
        // scans against its resynchronized private plant, then both
        // plants step — every standby snapshot equals the reference's.
        for tick in (N + 1)..=(N + M) {
            if tick == N + 10 {
                // A mid-continuation transfer reconverges without drift.
                standby.apply(&active_client.checkpoint().unwrap()).unwrap();
                assert_eq!(
                    standby.sync_state(),
                    &StandbySync::Tracking {
                        aligned: Tick(N + 9)
                    }
                );
            }
            let reference = active_client.advance(1).unwrap();
            standby.scan();
            active_plant.step(DT);
            standby_plant.step(DT);
            assert_eq!(standby.snapshot(), reference, "tick {tick}");
        }
        assert_eq!(standby.tick(), Tick(N + M));
    });
}

/// The shared-field mode: active and standby attach `RemoteDriver`s to
/// one `PlantServer`. The standby observes the real field — its peer's
/// checkpoint carries no driver section — and a `WriteGate` keeps it
/// output-quiescent: its scans compute outputs but never write the
/// shared plant.
#[test]
fn standby_sharing_the_field_tracks_through_a_write_gate() {
    let model = tank_loop();
    let registry = registry();
    let plant = PlantServer::bind(
        ("127.0.0.1", 0),
        SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
    )
    .unwrap();
    let plant_addr = plant.local_addr().unwrap();

    // The active attaches to the shared plant and serves the monitor.
    let active_driver = RemoteDriver::connect(plant_addr).unwrap();
    let active = assemble(&model, &registry, &active_driver).unwrap();
    let monitor = Monitor::bind(("127.0.0.1", 0), active, model.signal_index()).unwrap();
    let active_client = MonitorClient::new(monitor.local_addr());

    // The standby attaches to the same plant behind a closed gate.
    let standby_driver = RemoteDriver::connect(plant_addr).unwrap();
    let gate = WriteGate::closed(&standby_driver);
    let mut standby = Peer::standby(assemble(&model, &registry, &gate).unwrap(), Some(&gate));

    thread::scope(|scope| {
        scope.spawn(|| plant.serve());
        let _plant = ShutdownOnDrop(&plant);
        // The field fails closed while unclaimed: the active's
        // mutations ride this attachment's claim — taken once the
        // plant is serving, before the first scan.
        active_driver.claim_writer(1).unwrap();
        scope.spawn(|| monitor.serve());
        let _monitor = ShutdownOnDrop(&monitor);

        for _ in 0..N {
            active_client.advance(1).unwrap();
            active_driver.step(DT).unwrap();
        }

        // A field-observing driver holds no transferable state: the
        // field itself is the state.
        let checkpoint = active_client.checkpoint().unwrap();
        assert!(checkpoint.driver.is_none());
        standby.apply(&checkpoint).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(N) }
        );
        assert_eq!(standby.aligned_tick(), Some(Tick(N)));

        // Each continuation tick the standby scans between the active's
        // scan and the plant step, reading the same field values the
        // active read — the snapshots are identical.
        let mut last_reference = None;
        for tick in (N + 1)..=(N + M) {
            let reference = active_client.advance(1).unwrap();
            standby.scan();
            active_driver.step(DT).unwrap();
            assert_eq!(standby.snapshot(), reference, "tick {tick}");
            last_reference = Some(reference);
        }

        // Output-quiescent: a write through the standby's driver is
        // accepted but never reaches the field — the field still holds
        // exactly the active's last write.
        let active_valve = last_reference
            .unwrap()
            .points
            .iter()
            .find(|point| point.point == PointId(12))
            .and_then(|point| point.sample)
            .unwrap()
            .value;
        gate.write(PointId(12), Value::Float(-42.0)).unwrap();
        assert_eq!(
            standby_driver.read(PointId(12)).unwrap().value,
            active_valve
        );
    });
}

/// Transfer failures and mismatched checkpoints leave the standby in a
/// named, recoverable state: a checkpoint from a different component set
/// is rejected naming the element, a fetch against a dead peer degrades
/// the standby, and the next good transfer reconverges it.
#[test]
fn failed_and_mismatched_transfers_leave_a_recoverable_degraded_state() {
    let model = tank_loop();
    let registry = registry();

    let active_plant = sim_driver(&model).unwrap();
    let active = assemble(&model, &registry, &active_plant).unwrap();
    let monitor = Monitor::bind(("127.0.0.1", 0), active, model.signal_index()).unwrap();
    let active_client = MonitorClient::new(monitor.local_addr());

    let standby_plant = sim_driver(&model).unwrap();
    let mut standby = Peer::standby(assemble(&model, &registry, &standby_plant).unwrap(), None);

    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let _monitor = ShutdownOnDrop(&monitor);

        for _ in 0..5 {
            active_client.advance(1).unwrap();
            active_plant.step(DT);
        }
        standby.apply(&active_client.checkpoint().unwrap()).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(5) }
        );
        assert_eq!(standby.aligned_tick(), Some(Tick(5)));

        // A checkpoint from a different component set is rejected with
        // the named element, and the standby degrades — but keeps its
        // last-good alignment.
        let mut foreign = active_client.checkpoint().unwrap();
        let renamed = foreign.components.remove("pid:2").unwrap();
        foreign.components.insert("pid:9".to_string(), renamed);
        let error = standby.apply(&foreign).unwrap_err();
        assert_eq!(
            error,
            ApplyError::Restore(RestoreError::UnknownComponent {
                component: "pid:9".to_string()
            })
        );
        assert!(
            matches!(standby.sync_state(), StandbySync::Degraded { detail } if detail.contains("pid:9")),
            "state {:?} names the mismatched element",
            standby.sync_state()
        );
        assert_eq!(standby.aligned_tick(), Some(Tick(5)));
        assert_eq!(standby.tick(), Tick(5));

        // A missing entry — a smaller component set — names it too.
        let mut foreign = active_client.checkpoint().unwrap();
        foreign.components.remove("analog-input:1").unwrap();
        let error = standby.apply(&foreign).unwrap_err();
        assert_eq!(
            error,
            ApplyError::Restore(RestoreError::MissingComponent {
                component: "analog-input:1".to_string()
            })
        );

        // A transfer that produces no checkpoint at all — an
        // unreachable active — degrades the standby the same named way.
        let dead_client = MonitorClient::new("127.0.0.1:1".parse().unwrap());
        let error = dead_client.checkpoint().unwrap_err();
        standby.note_transfer_failed(&error);
        assert!(
            matches!(standby.sync_state(), StandbySync::Degraded { .. }),
            "{:?}",
            standby.sync_state()
        );

        // The standby keeps scanning its last-known state meanwhile, and
        // the next good transfer reconverges it.
        standby.scan();
        standby_plant.step(DT);
        for _ in 0..7 {
            active_client.advance(1).unwrap();
            active_plant.step(DT);
        }
        standby.apply(&active_client.checkpoint().unwrap()).unwrap();
        assert_eq!(
            standby.sync_state(),
            &StandbySync::Tracking { aligned: Tick(12) }
        );
        assert_eq!(standby.aligned_tick(), Some(Tick(12)));

        // And the reconverged run again continues identically.
        for tick in 13..=20 {
            let reference = active_client.advance(1).unwrap();
            standby.scan();
            active_plant.step(DT);
            standby_plant.step(DT);
            assert_eq!(standby.snapshot(), reference, "tick {tick}");
        }
    });
}

/// The two-process shape: a real `dcs-controller --listen` active serves
/// `GET /checkpoint` while a real `--standby` process pulls, applies,
/// and scans — and exits having produced its snapshot.
#[test]
fn standby_binary_pulls_checkpoints_from_a_listening_active() {
    use std::process::{Command, Stdio};

    // An ephemeral port the active then binds — the usual test-side
    // probe; the brief window where it is free is inherent to spawning
    // a separate listener process. On a busy host another process can
    // take the freed port before the child binds — the child then
    // exits at startup while whatever foreign listener answered the
    // probe goes away mid-test — so a lost bind retries the probe.
    let (addr, client, mut active) = {
        let mut attempt = 0;
        loop {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = probe.local_addr().unwrap();
            drop(probe);

            let mut active = Command::new(BINARY)
                .args([
                    TANK_LOOP_PATH,
                    "--listen",
                    &addr.to_string(),
                    "--scan-ms",
                    "20",
                ])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();

            // Wait until the active's monitor is up before the standby
            // pulls — `try_wait` catching the bind-lost exit so a
            // foreign answer never counts as the active serving.
            let client = MonitorClient::new(addr);
            let mut serving = false;
            for _ in 0..100 {
                if active.try_wait().unwrap().is_some() {
                    break;
                }
                if client.snapshot().is_ok() {
                    serving = true;
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            if serving {
                // The first answer could still be a foreign listener
                // the child has not yet raced to the port; give a lost
                // bind's exit a moment to land, then require the same
                // child both alive and serving.
                thread::sleep(Duration::from_millis(150));
                serving = active.try_wait().unwrap().is_none() && client.snapshot().is_ok();
            }
            attempt += 1;
            if serving {
                break (addr, client, active);
            }
            let _ = active.kill();
            let _ = active.wait();
            assert!(
                attempt < 3,
                "the active's monitor never came up — its binds kept \
                 losing the ephemeral-port race"
            );
        }
    };

    let mut active_before = None;
    for _ in 0..50 {
        if active.try_wait().unwrap().is_some() {
            break;
        }
        if let Ok(snapshot) = client.snapshot() {
            active_before = Some(snapshot.tick.0);
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    let active_before = active_before.expect("the active's monitor stopped serving");
    let standby = Command::new(BINARY)
        .args([
            TANK_LOOP_PATH,
            "--standby",
            &addr.to_string(),
            "--ticks",
            "5",
            "--scan-ms",
            "20",
        ])
        .output()
        .unwrap();
    let mut active_after = None;
    for _ in 0..50 {
        if active.try_wait().unwrap().is_some() {
            break;
        }
        if let Ok(snapshot) = client.snapshot() {
            active_after = Some(snapshot.tick.0);
            break;
        }
        thread::sleep(Duration::from_millis(20));
    }
    let active_after = active_after.expect("the active's monitor stopped serving");
    let _ = active.kill();
    let _ = active.wait();

    assert!(
        standby.status.success(),
        "standby failed: {}",
        String::from_utf8_lossy(&standby.stderr)
    );
    // Tracking, not local counting: the standby's tick is the last
    // applied checkpoint's tick plus its own continuation scans, so it
    // sits inside the active's progression over the same window.
    let snapshot: serde_json::Value = serde_json::from_slice(&standby.stdout).unwrap();
    let standby_tick = snapshot["tick"].as_u64().unwrap();
    assert!(
        active_before < standby_tick && standby_tick <= active_after + 1,
        "standby tick {standby_tick}, active ran {active_before}..={active_after}"
    );
}
