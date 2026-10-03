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
use dcs_core::{
    IoDriver, PointId, Quality, QualityReason, StandbySync, TelemetrySnapshot, Tick, Value,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{ApplyError, Peer, RestoreError, TrackReport, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::thread;
use std::time::Duration;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");
const TANK_LOOP_PATH: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-assembly/fixtures/tank_loop.json"
);
/// The QA rig's own station model — the model `qa_lane`'s scratch field
/// serves and both rig seats load. Its `net-flow` input (point 13) is
/// the rig's budgeted channel: `stale_after_ticks: 5` declared in the
/// fixture, so the reproduction needs no model edit to stage.
const STATION: &str = include_str!("../../dcs-demo/fixtures/pump_station.json");
const BINARY: &str = env!("CARGO_BIN_EXE_dcs-controller");

/// The process time each simulated plant advances per scan — the
/// fixture's PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the active runs before the first transfer.
const N: u64 = 30;
/// Ticks of continuation compared against the uninterrupted run.
const M: u64 = 20;
/// The rig's cadence asymmetry, `owner --scan-ms 100` against
/// `fast --scan-ms 10`: the reader scans this many times for every
/// field step its owner takes.
const READER_PER_OWNER: u64 = 10;
/// How many of the owner's field steps the pair runs — enough cycles
/// that a per-step flap would show on every one of them.
const OWNER_SCANS: u64 = 40;
/// The freshness verdict the reader must not present on a stepping
/// field.
const STALE: Quality = Quality::Uncertain(QualityReason::Stale);
/// The station's budgeted `net-flow` input — the rig's point 13,
/// declared with `stale_after_ticks: 5`.
const NET_FLOW: PointId = PointId(13);

fn tank_loop() -> PlantModel {
    PlantModel::load(TANK_LOOP).unwrap()
}

/// The rig's station model, as the two rig seats load it.
fn station() -> PlantModel {
    PlantModel::load(STATION).unwrap()
}

/// The quality a snapshot's image reports for the station's budgeted
/// `net-flow` input.
fn net_flow_quality(snapshot: &TelemetrySnapshot) -> Quality {
    snapshot
        .points
        .iter()
        .find(|point| point.point == NET_FLOW)
        .and_then(|point| point.sample)
        .unwrap_or_else(|| panic!("no sample for {NET_FLOW:?}"))
        .quality
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

/// The QA finding `stale-after-ticks-flap-confirmed-on-revision`
/// (#1453), at the rig's own shape: a *tracking standby* whose scan
/// cadence beats the field owner's step cadence. The owner's seat runs
/// `--scan-ms 100` against the standby's `--scan-ms 10`, both attached
/// through `--remote` to one shared plant, so the owner's scan steps
/// the field once per ten standby scans and every plant step is the only
/// thing that re-stamps the budgeted `net-flow` channel — point 13, with
/// its `stale_after_ticks: 5` — the rig's own station model declares.
/// Nine of every ten standby reads therefore serve the byte-identical
/// held report, and that five-tick budget must not read the pace
/// asymmetry as staleness: the image verdict the standby's journal
/// records must not oscillate on a field the owner keeps stepping.
///
/// The reader tracks the owner throughout — one checkpoint pull per
/// standby scan, the `--standby` wiring's own cadence — so this is the
/// real tracking standby over the real station model rather than an
/// isolated reader over a canned driver, and both seats' verdicts are
/// audited: the owner's own image never left `Good` while it scanned at
/// the field's pace, so a flap could only ever have been the faster
/// observer's.
#[test]
fn a_tracking_standby_scanning_faster_than_the_field_owner_never_flaps_stale() {
    let model = station();
    let registry = registry();
    let plant = PlantServer::bind(
        ("127.0.0.1", 0),
        SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
    )
    .unwrap();
    let plant_addr = plant.local_addr().unwrap();

    // The owner seat: the field claim and the only attachment that
    // steps the shared plant, served over the monitor the standby pulls.
    let owner_driver = RemoteDriver::connect(plant_addr).unwrap();
    let owner = assemble(&model, &registry, &owner_driver).unwrap();
    let monitor = Monitor::bind(("127.0.0.1", 0), owner, model.signal_index()).unwrap();
    let owner_client = MonitorClient::new(monitor.local_addr());

    // The reader seat: the same model on its own attachment to the same
    // plant, behind the closed gate a standby's writes are quiesced at.
    let reader_driver = RemoteDriver::connect(plant_addr).unwrap();
    let gate = WriteGate::closed(&reader_driver);
    let mut reader = Peer::standby(assemble(&model, &registry, &gate).unwrap(), Some(&gate));

    thread::scope(|scope| {
        scope.spawn(|| plant.serve());
        let _plant = ShutdownOnDrop(&plant);
        owner_driver.claim_writer(1).unwrap();
        scope.spawn(|| monitor.serve());
        let _monitor = ShutdownOnDrop(&monitor);

        // The rig's staged pair: the owner's scan paced at the field's
        // own `--scan-ms 100`, the standby's at `--scan-ms 10`, with
        // the field stepped only by its owner. Reader scans are numbered
        // across the whole run so a verdict can be placed against the
        // owner's step cadence it straddles.
        let mut reader_stale = Vec::new();
        let mut owner_stale = Vec::new();
        for owner_scan in 1..=OWNER_SCANS {
            let owner_snapshot = owner_client.advance(1).unwrap();
            if net_flow_quality(&owner_snapshot) == STALE {
                owner_stale.push(owner_scan);
            }
            // Only the owner steps the field: one plant step per ten
            // standby scans is the whole asymmetry.
            owner_driver.step(DT).unwrap();
            for standby_scan in 1..=READER_PER_OWNER {
                let report = reader
                    .track_once(|| owner_client.checkpoint().map_err(|error| error.to_string()));
                assert!(
                    matches!(report, TrackReport::Applied(_)),
                    "the standby stopped tracking its owner at owner scan \
                     {owner_scan}: {report:?}"
                );
                reader.scan();
                if net_flow_quality(&reader.snapshot()) == STALE {
                    reader_stale.push((owner_scan - 1) * READER_PER_OWNER + standby_scan);
                }
            }
        }

        // The field owner's own verdict never moved: it reads the field
        // once per its own step, so the report it sees is the report the
        // owner's own step just re-stamped. Whatever the standby saw,
        // this is the half of the pair that names the field healthy.
        assert!(
            owner_stale.is_empty(),
            "the owner paced one step per scan read its own field stale: \
             {owner_stale:?}"
        );

        // The premise the finding is about, measured rather than
        // assumed: the standby's run clock really did outpace the
        // owner's field steps by the staged cadence ratio, so the run
        // above is the asymmetric reader and not a symmetric pair that
        // happens to pass.
        let owner_tick = owner_client.snapshot().unwrap().tick.0;
        assert!(
            reader.tick().0 >= owner_tick * READER_PER_OWNER,
            "the standby ran {} scans against the owner's {owner_tick} field \
             steps — the cadence asymmetry never staged",
            reader.tick().0
        );

        // The cold start and nothing after it. The standby has
        // demonstrated no pace until it has watched the field publish
        // twice, so the declared five-tick budget judges the first
        // observation alone and the reads past it present stale; from
        // the owner's own step period on, a report the owner has not
        // re-stamped yet sits inside that demonstrated arrival period
        // and the standby presents no stale verdict at all. Every stale
        // verdict must therefore fall inside the first owner step's ten
        // reads — the unfixed reader flapped on each of the forty steps.
        assert!(
            reader_stale.iter().all(|&scan| scan <= READER_PER_OWNER),
            "the standby presented {} stale verdicts on a field the owner \
             kept stepping, the first past its cold start at scan {:?} — \
             the reader's scan cadence, not the field's freshness, decided \
             the verdict",
            reader_stale.len(),
            reader_stale.iter().find(|&&scan| scan > READER_PER_OWNER),
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
