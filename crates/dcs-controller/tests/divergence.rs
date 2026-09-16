//! Standby output-divergence detection against the shared plant — the
//! issue's acceptance test.
//!
//! Two `Peer` instances assemble the same model against `RemoteDriver`s
//! on one `PlantServer`, each behind a `WriteGate` and each serving a
//! monitor. The standby scans gate-closed, staging the `Out` writes its
//! scan would issue; each checkpoint transfer whose tick matches the
//! staged image compares it against the standby's own reads of the same
//! points — the field-observing divergence check. A skewed standby — one
//! whose reads of the shared plant are bent — reports the named
//! `diverged` sync state at the first transfer covering the skewed
//! staged tick, refuses `POST /promote` with the named `NotConverged`
//! error carrying that state, keeps its writes quiesced throughout, and
//! resynchronizes through fresh checkpoints once the skew is gone. The
//! transition lands in the journal attributed to the compared tick, and
//! identical scripted runs produce identical divergence reports.

use dcs_assembly::{assemble, sim_channel_map};
use dcs_controller::registry;
use dcs_core::{
    Command, Divergence, IoDriver, IoError, JournalEntry, JournalEvent, PointId, Role, Sample,
    StandbySync, SwitchError, Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_runtime::{Peer, WriteGate};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The process time the shared plant advances per scan — the fixture's
/// PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the active runs before the first checkpoint transfer — long
/// enough for the loop to settle mid-range, where a skew cannot hide
/// behind the output clamp.
const N: u64 = 30;
/// Tracking ticks before the skew lands.
const K: u64 = 6;
/// The fixture's operator setpoint — written once, mid-range, and the
/// read the skew bends: the PID consumes it within the same scan, so a
/// bent setpoint read bends the staged valve output that scan.
const SETPOINT: PointId = PointId(10);
/// The fixture's valve output — the staged field write under test.
const VALVE: PointId = PointId(12);
/// How far the standby's setpoint reads are bent — far beyond the
/// documented `Float` tolerance, far inside the operating range.
const SKEW: f64 = 10.0;

/// `shutdown` on drop, so a panicking test still lets the scoped serve
/// threads exit instead of hanging the scope's join — the same pattern
/// `dcs-sim-net`'s and the promotion tests use.
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

/// A read-biasing driver wrapper: adds `offset` to `Float` reads of
/// `point` while `armed` — the standby's skewed observation of the
/// shared plant, like a miscalibrated channel on its side of the pair.
/// Writes pass through untouched (the gate in front of it quiesces them
/// anyway); every other point's reads pass through unbiased.
struct BiasedDriver<'d> {
    inner: &'d RemoteDriver,
    point: PointId,
    offset: f64,
    armed: AtomicBool,
}

impl IoDriver for BiasedDriver<'_> {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let mut sample = self.inner.read(point)?;
        if point == self.point
            && self.armed.load(Ordering::Relaxed)
            && let Value::Float(value) = sample.value
        {
            sample.value = Value::Float(value + self.offset);
        }
        Ok(sample)
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        self.inner.write(point, value)
    }
}

/// The valve value the field currently carries, read through `driver`.
fn field_valve(driver: &RemoteDriver) -> Value {
    driver.read(VALVE).unwrap().value
}

/// The valve value `client`'s staged output image reports.
fn image_valve(client: &MonitorClient) -> Value {
    client
        .snapshot()
        .unwrap()
        .points
        .iter()
        .find(|point| point.point == VALVE)
        .and_then(|point| point.sample)
        .unwrap()
        .value
}

/// One tick of the tracked pair: the active scans — the field then
/// carries its `Out` write for that tick — the standby scans and stages
/// its own, and the checkpoint transfer of the same tick runs the
/// divergence check on the staged image. The plant then steps once, so
/// both peers' next scans read the same field.
fn tracking_tick(
    active_client: &MonitorClient,
    standby_client: &MonitorClient,
    standby_monitor: &Monitor<'_>,
    active_driver: &RemoteDriver,
) {
    active_client.advance(1).unwrap();
    standby_client.advance(1).unwrap();
    standby_monitor
        .apply_checkpoint(&active_client.checkpoint().unwrap())
        .unwrap();
    active_driver.step(DT).unwrap();
}

/// What one scripted divergence run observed — the acceptance criteria
/// in comparable form.
struct Outcome {
    /// The standby's `sync` payload while diverged.
    diverged_sync: StandbySync,
    /// The journal's `divergence_detected` entries, in order.
    journal: Vec<JournalEntry>,
    /// The named error `POST /promote` answered while diverged.
    promote_error: SwitchError,
    /// The field's valve while diverged — proof the staged write stayed
    /// quiesced.
    field_valve: Value,
    /// The standby's own image of the valve before the resyncing apply
    /// overwrote it — what it would have written.
    staged_valve: Value,
    /// The standby's `sync` payload after the resync cleared the skew.
    resynced_sync: StandbySync,
    /// The role `POST /promote` reported once resynchronized.
    promoted_role: Role,
}

/// Runs the scripted scenario against a fresh plant and pair: converge,
/// track `K` ticks clean, arm the skew, and observe.
fn run_scenario() -> Outcome {
    let model = PlantModel::load(TANK_LOOP).unwrap();
    let registry = registry();
    let plant = std::sync::Arc::new(
        PlantServer::bind(
            ("127.0.0.1", 0),
            SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
        )
        .unwrap(),
    );
    let _plant = ShutdownOnDrop(&*plant);
    let plant_addr = plant.local_addr().unwrap();

    // The plant serves before the peers construct: a launched active's
    // startup claim needs the server answering, and a bound-but-unserved
    // listener lets a connect through while the claim request waits for
    // nobody.
    let serving = thread::spawn({
        let plant = std::sync::Arc::clone(&plant);
        move || plant.serve()
    });

    let active_driver = RemoteDriver::connect(plant_addr).unwrap();
    let active_gate = WriteGate::closed(&active_driver);
    let mut active = Peer::active(
        assemble(&model, &registry, &active_gate).unwrap(),
        Some(&active_gate),
    )
    .with_field_claim(|| {
        active_driver
            .claim_writer(1)
            .map_err(|error| error.to_string())
    });
    active.activate().unwrap();
    let active_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), active, model.signal_index()).unwrap();
    let active_client = MonitorClient::new(active_monitor.local_addr());

    // The standby observes the shared plant through the bias — armed and
    // relieved under test control — behind its closed gate.
    let standby_driver = RemoteDriver::connect(plant_addr).unwrap();
    let biased = BiasedDriver {
        inner: &standby_driver,
        point: SETPOINT,
        offset: SKEW,
        armed: AtomicBool::new(false),
    };
    let standby_gate = WriteGate::closed(&biased);
    let standby = Peer::standby(
        assemble(&model, &registry, &standby_gate).unwrap(),
        Some(&standby_gate),
    )
    .with_field_claim(|| {
        standby_driver
            .claim_writer(2)
            .map_err(|error| error.to_string())
    });
    let standby_monitor =
        Monitor::bind_peer(("127.0.0.1", 0), standby, model.signal_index()).unwrap();
    let standby_client = MonitorClient::new(standby_monitor.local_addr());

    let outcome = thread::scope(|scope| {
        scope.spawn(|| active_monitor.serve());
        let _active_monitor = ShutdownOnDrop(&active_monitor);
        scope.spawn(|| standby_monitor.serve());
        let _standby_monitor = ShutdownOnDrop(&standby_monitor);

        // A mid-range setpoint: the valve settles away from both clamps,
        // where the skew must bend the staged output visibly.
        active_client
            .command(&Command::WriteValue {
                point: SETPOINT,
                kind: ValueKind::Float,
                value: Value::Float(50.0),
            })
            .unwrap();
        for _ in 0..N {
            active_client.advance(1).unwrap();
            active_driver.step(DT).unwrap();
        }

        // Converge and track K ticks: the healthy standby stays
        // `tracking` — the divergence check never fires on an unskewed
        // peer.
        standby_monitor
            .apply_checkpoint(&active_client.checkpoint().unwrap())
            .unwrap();
        for _ in 0..K {
            tracking_tick(
                &active_client,
                &standby_client,
                &standby_monitor,
                &active_driver,
            );
            let report = standby_client.role().unwrap();
            assert!(
                matches!(report.sync, Some(StandbySync::Tracking { .. })),
                "healthy standby must stay tracking, got {report:?}"
            );
        }

        // Arm the skew: the standby's staged image for this scan bends
        // away from the field's value. The stated detection bound is one
        // transfer — the checkpoint of the staged tick, applied at the
        // same cycle's end.
        biased.armed.store(true, Ordering::Relaxed);
        let diverged_tick = Tick(N + K + 1);
        active_client.advance(1).unwrap();
        standby_client.advance(1).unwrap();
        // The staged image before the apply overwrites it — the standby
        // would have written this.
        let staged_valve = image_valve(&standby_client);
        standby_monitor
            .apply_checkpoint(&active_client.checkpoint().unwrap())
            .unwrap();
        active_driver.step(DT).unwrap();

        let report = standby_client.role().unwrap();
        assert!(
            matches!(report.sync, Some(StandbySync::Diverged { .. })),
            "skewed standby must report diverged, got {report:?}"
        );
        let diverged_sync = report.sync.clone().unwrap();
        assert_eq!(report.tick, diverged_tick);

        // The named promotion refusal carries the named state, and the
        // field's valve still carries the active's write — the standby's
        // staged output stayed quiesced.
        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        let promote_error = serde_json::from_str::<SwitchError>(&body).unwrap();
        assert_eq!(
            promote_error,
            SwitchError::NotConverged {
                sync: diverged_sync.clone()
            }
        );
        assert!(!standby_gate.is_open());
        let field_valve = field_valve(&standby_driver);
        assert_ne!(
            staged_valve, field_valve,
            "the skew must bend the staged output away from the field's"
        );

        // The transition is journaled once, attributed to the tick the
        // compared staged image belonged to.
        let journal: Vec<JournalEntry> = standby_client
            .journal(0)
            .unwrap()
            .into_iter()
            .filter(|entry| matches!(entry.event, JournalEvent::DivergenceDetected { .. }))
            .collect();
        assert_eq!(journal.len(), 1, "the transition is journaled once");
        assert_eq!(journal[0].tick, diverged_tick);

        // Relieved of the skew, the next transfers resynchronize: the
        // first clean same-tick comparison returns the peer to
        // `tracking` — and promotion is accepted again.
        biased.armed.store(false, Ordering::Relaxed);
        for _ in 0..2 {
            tracking_tick(
                &active_client,
                &standby_client,
                &standby_monitor,
                &active_driver,
            );
        }
        let resynced = standby_client.role().unwrap();
        assert!(
            matches!(resynced.sync, Some(StandbySync::Tracking { .. })),
            "a fresh checkpoint must resync a diverged standby, got {resynced:?}"
        );
        let resynced_sync = resynced.sync.unwrap();

        let promoted = standby_client.promote().unwrap();
        assert_eq!(promoted.role, Role::Promoting);
        assert!(standby_gate.is_open());

        Outcome {
            diverged_sync,
            journal,
            promote_error,
            field_valve,
            staged_valve,
            resynced_sync,
            promoted_role: promoted.role,
        }
    });
    drop(_plant);
    serving.join().unwrap();
    outcome
}

#[test]
fn skewed_standby_diverges_blocks_promotion_and_resyncs() {
    let outcome = run_scenario();

    // The diverged report names the valve point and both sides' values —
    // the standby's staged output bent by the skew, the field's the
    // active's.
    let StandbySync::Diverged { mismatches } = &outcome.diverged_sync else {
        panic!("sync must be diverged");
    };
    assert_eq!(
        mismatches,
        &[Divergence {
            point: VALVE,
            staged: outcome.staged_valve,
            field: outcome.field_valve,
        }]
    );
    let JournalEvent::DivergenceDetected {
        mismatches: journaled,
    } = &outcome.journal[0].event
    else {
        panic!("journal entry must be divergence_detected");
    };
    assert_eq!(journaled, mismatches);
    assert_eq!(
        outcome.resynced_sync,
        StandbySync::Tracking {
            aligned: Tick(N + K + 3),
        }
    );
    assert_eq!(outcome.promoted_role, Role::Promoting);
}

#[test]
fn identical_scripted_runs_report_identical_divergence() {
    let first = run_scenario();
    let second = run_scenario();
    assert_eq!(first.journal, second.journal);
    assert_eq!(first.diverged_sync, second.diverged_sync);
    assert_eq!(first.promote_error, second.promote_error);
    assert_eq!(first.staged_valve, second.staged_valve);
    assert_eq!(first.field_valve, second.field_valve);
    assert_eq!(first.resynced_sync, second.resynced_sync);
}
