//! End-to-end automatic failover over the shared simulated plant — the
//! issue's acceptance rig: active loss detected through the checkpoint-pull
//! heartbeat, the converged standby self-promoting at the scan boundary,
//! and the plant server's write-ownership claim fencing the old peer.
//!
//! The rig reuses the hot-swap harness's shape: a `dcs-plant-server`
//! process owns the shared `tank_loop` plant and two `dcs-controller`
//! processes load the same model re-pointed at `sim-tcp`, both running
//! `--driven` so every scan happens inside a `POST /scan` request — the
//! standby's request carries its checkpoint pull, the heartbeat the
//! failover decision counts. `--auto-promote 3` arms the standby: three
//! consecutive failed pulls — the stated bound — self-promote a peer whose
//! convergence still stands.
//!
//! Two loss modes are exercised: killing the active's process, and
//! partitioning the heartbeat path through a TCP relay that drops the
//! standby's checkpoint connections while the active keeps running. The
//! relay is also the transient-fault injector: one dropped pull must not
//! promote, and the miss count resets on the next successful pull.
//!
//! Bumpless continuation is proven against an in-process reference run —
//! the same model assembled on a `WriteGate`-gated `RemoteDriver` over a
//! second plant server, stepping every tick except the outage window,
//! where its gate closes and its plant holds still exactly like the
//! orphaned pair field does. The promoted standby's field trace must equal
//! the reference's, tick for tick, across two identical scripted runs.

use dcs_assembly::assemble;
use dcs_controller::registry;
use dcs_core::{
    IoDriver, IoError, JournalEvent, PointId, Role, StandbySync, TelemetrySnapshot, Value,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::{Executor, WriteGate};
use dcs_sim_net::{RemoteDriver, RemoteError};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};

mod support;

use support::{SimTcp, controller_model, image_value, kill, pump, spawn_controller, spawn_plant};

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
const DT_F64: f64 = 0.1;
/// The consecutive-miss budget every failover scenario runs under —
/// the stated bound: the standby promotes at the third lost pull's
/// scan boundary.
const BUDGET: u32 = 3;
/// Ticks the pair runs converged before the loss lands.
const N: u64 = 10;
/// Post-failover ticks whose field trace must equal the reference's.
const M: u64 = 10;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);

/// A controllable network path for the checkpoint-pull heartbeat: while
/// `partitioned` is clear the relay forwards each connection to the
/// active's monitor; while set it accepts and immediately drops them —
/// the refused/EOF failure a partitioned or dead peer produces. The flag
/// is only ever flipped between scripted ticks, so a pull's verdict is
/// never racy.
struct Relay {
    addr: SocketAddr,
    partitioned: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<()>>,
}

impl Relay {
    /// A relay forwarding to `upstream` — the active's monitor address
    /// the standby's `--standby` is pointed at.
    fn forwarding(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let partitioned = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let accept = {
            let partitioned = Arc::clone(&partitioned);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let Ok(stream) = stream else { continue };
                    if partitioned.load(Ordering::Relaxed) {
                        // Dropped on the floor: the pull sees a refused
                        // or immediately closed connection — fast, never
                        // a hang.
                        drop(stream);
                    } else {
                        thread::spawn(move || pump(stream, upstream));
                    }
                }
            })
        };
        Self {
            addr,
            partitioned,
            stop,
            accept: Some(accept),
        }
    }

    /// Drops or restores the heartbeat path mid-run.
    fn partition(&self, cut: bool) {
        self.partitioned.store(cut, Ordering::Relaxed);
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        // Wake the blocking accept so the loop observes the flag.
        let _ = TcpStream::connect(self.addr);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

/// The in-process reference run: the same model on a `RemoteDriver` over
/// its own plant server, behind a `WriteGate` the script closes and
/// reopens to replay the orphaned field's outage window — quiesced writes
/// and no steps while the standby detects the loss, gate lifted and
/// stepping resumed on the tick the standby promotes.
struct Reference<'d> {
    executor: Executor<'d>,
    gate: &'d WriteGate<'d>,
    plant: RemoteDriver,
}

impl Reference<'_> {
    /// One tick in the field-owning half of the run: scan, write, step.
    ///
    /// The pair's `FanoutDriver` runs every backend's step hook per scan,
    /// so a model with two `sim-tcp` devices steps the shared plant
    /// twice — the reference replays that cadence exactly.
    fn owned_tick(&mut self) -> TelemetrySnapshot {
        self.executor.scan().unwrap();
        for _ in 0..2 {
            self.plant.step(DT_F64).unwrap();
        }
        self.executor.snapshot()
    }

    /// One tick inside the outage window: the gate quiesces the write and
    /// the plant's clock holds — the field the orphaned pair plant keeps.
    fn orphaned_tick(&mut self) -> TelemetrySnapshot {
        self.gate.close();
        self.executor.scan().unwrap();
        self.gate.open();
        self.executor.snapshot()
    }
}

/// One scripted failover run: converge the pair, kill the active, and let
/// the armed standby detect the loss and self-promote. Returns the
/// field's (valve, level) trace — the sequence a repeated run must
/// reproduce — plus the tick the promotion landed at.
fn run_failover(tag: &str) -> (Vec<(Value, Value)>, u64) {
    let dir = std::env::temp_dir().join(format!("dcs-failover-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // The pair's shared plant and the reference run's own — identical
    // model, identical dynamics.
    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let reference_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    // The field observer the run asserts on — and its setpoint lands
    // before the controllers spawn: the launched active's startup claim
    // fences this attachment, so every later access is a read.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();

    // The active serves checkpoints; the standby pulls one per requested
    // scan — the heartbeat — with the failover budget armed.
    let mut active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // The reference: the same model in-process against its own plant —
    // its gate starts open, the field owner's posture. Its plant is
    // never claimed — the in-process run carries no claim hook.
    let model = PlantModel::load(MODEL_SOURCE).unwrap();
    let reference_driver = RemoteDriver::connect(reference_plant.addr).unwrap();
    let reference_gate = WriteGate::closed(&reference_driver);
    reference_gate.open();
    let mut reference = Reference {
        executor: assemble(&model, &registry(), &reference_gate).unwrap(),
        gate: &reference_gate,
        plant: RemoteDriver::connect(reference_plant.addr).unwrap(),
    };
    reference.plant.write(SETPOINT, Value::Float(50.0)).unwrap();

    // Phase 1: N converged ticks — the standby tracks the active's
    // checkpoints; the reference reproduces both snapshots and the
    // field they write.
    let mut trace = Vec::new();
    for tick in 1..=N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        let alone = reference.owned_tick();
        assert_eq!(tracked, owner, "tick {tick}");
        assert_eq!(owner, alone, "tick {tick}");
        let carried = field.read(VALVE).unwrap().value;
        assert_eq!(carried, image_value(&owner, VALVE), "tick {tick}");
        trace.push((carried, field.read(LEVEL).unwrap().value));
    }
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby never converged: {report:?}"
    );

    // The loss: the active's process dies; its checkpoint endpoint
    // refuses connections from the next pull on.
    active_process.child.kill().unwrap();
    active_process.child.wait().unwrap();

    // The detection window — ticks N+1 .. N+BUDGET-1: each requested
    // scan's pull fails, the miss count climbs under the budget, and
    // the peer reports `degraded` without promoting. The orphaned field
    // holds: no writer, no stepper — the reference replays it
    // gate-closed and unstepped.
    for tick in (N + 1)..(N + BUDGET as u64) {
        let tracked = standby.advance(1).unwrap();
        let alone = reference.orphaned_tick();
        assert_eq!(tracked, alone, "tick {tick}");
        let report = standby.role().unwrap();
        assert_eq!(report.role, Role::Standby, "miss {tick} promoted early");
        assert!(
            matches!(report.sync, Some(StandbySync::Degraded { .. })),
            "miss {tick} should report degraded: {report:?}"
        );
        trace.push((
            field.read(VALVE).unwrap().value,
            field.read(LEVEL).unwrap().value,
        ));
    }

    // The budget-th miss's scan boundary — tick N+BUDGET: the pull
    // fails again, the count reaches the budget, and the still-converged
    // standby takes the field claim and lifts its gate inside this same
    // requested scan. The scan then runs gate-open and its step drives
    // the plant — promotion and first field-owning scan land together,
    // settling the reported role to `active`.
    let promotion_tick = N + BUDGET as u64;
    let promoted = standby.advance(1).unwrap();
    let alone = reference.owned_tick();
    assert_eq!(promoted, alone, "tick {promotion_tick}");
    let report = standby.role().unwrap();
    assert_eq!(
        report.role,
        Role::Active,
        "self-promotion missed its boundary: {report:?}"
    );
    assert_eq!(report.tick.0, promotion_tick);
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));
    trace.push((carried, field.read(LEVEL).unwrap().value));

    // Phase 2: M post-failover ticks — the promoted peer keeps writing
    // and stepping the plant, tick-identical to the reference run:
    // bumpless output continuation through the takeover.
    for tick in (promotion_tick + 1)..=(promotion_tick + M) {
        let owner = standby.advance(1).unwrap();
        let alone = reference.owned_tick();
        assert_eq!(owner, alone, "tick {tick}");
        let carried = field.read(VALVE).unwrap().value;
        assert_eq!(carried, image_value(&owner, VALVE), "tick {tick}");
        trace.push((carried, field.read(LEVEL).unwrap().value));
    }

    // The journal carries both halves of the self-promotion — the same
    // transitions a manual `POST /promote` records.
    let role_changes: Vec<(Role, Role)> = standby
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to } => Some((from, to)),
            _ => None,
        })
        .collect();
    assert_eq!(
        role_changes,
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active)
        ]
    );

    let _ = std::fs::remove_dir_all(&dir);
    (trace, promotion_tick)
}

#[test]
fn killing_the_active_self_promotes_the_standby_within_the_budget() {
    let (trace, promotion_tick) = run_failover("kill");
    assert_eq!(trace.len() as u64, N + BUDGET as u64 + M);
    assert_eq!(promotion_tick, N + BUDGET as u64);
}

#[test]
fn identical_failover_runs_reproduce_identical_field_traces() {
    let (first, first_tick) = run_failover("a");
    let (second, second_tick) = run_failover("b");
    assert_eq!(first_tick, second_tick);
    assert_eq!(first, second);
}

#[test]
fn a_transient_missed_pull_neither_promotes_nor_rearms() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-transient-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;
    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences this attachment from boot.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    let active_process = spawn_controller(&pair_model, &[], DT);
    // The standby's heartbeat path runs through the relay the test cuts.
    let relay = Relay::forwarding(active_process.addr);
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converge first.
    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }

    // One dropped pull — a transient fault, not a loss: the miss count
    // climbs to one, under the budget, and the peer reports `degraded`
    // while staying `standby`.
    relay.partition(true);
    standby.advance(1).unwrap();
    active.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Degraded { .. })),
        "{report:?}"
    );

    // The next pull lands: the count resets and tracking resumes — the
    // transient left no failover debt behind.
    relay.partition(false);
    standby.advance(1).unwrap();
    active.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "a successful pull must reset the miss count: {report:?}"
    );

    // A second transient — two misses now, still under the budget —
    // promotes nothing either; the field kept carrying only the
    // active's writes throughout.
    relay.partition(true);
    for _ in 0..(BUDGET - 1) {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        assert_eq!(standby.role().unwrap().role, Role::Standby);
    }
    relay.partition(false);
    for _ in 0..3 {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        assert_eq!(tracked, owner);
        assert_eq!(field.read(VALVE).unwrap().value, image_value(&owner, VALVE));
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_unconverged_standby_reports_its_state_and_never_promotes() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-unconverged-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    // A standby whose tracking target never existed: every pull fails
    // from the start, so the peer never converges — and a process that
    // was never the active's peer holds no promotion rights.
    let dead = TcpListener::bind(("127.0.0.1", 0))
        .unwrap()
        .local_addr()
        .unwrap();
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            dead.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
        DT,
    );
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();

    // Past the budget and beyond: no convergence proof ever stood, so
    // the failover window never opens — the peer reports `degraded`,
    // stays `standby`, and its writes never reach the field.
    for miss in 1..=(BUDGET as u64 + 2) {
        standby.advance(1).unwrap();
        let report = standby.role().unwrap();
        assert_eq!(report.role, Role::Standby, "miss {miss} promoted");
        assert!(
            matches!(report.sync, Some(StandbySync::Degraded { .. })),
            "miss {miss} should report degraded: {report:?}"
        );
        assert_eq!(
            field.read(VALVE).unwrap().value,
            Value::Float(0.0),
            "an unpromoted standby's writes reached the field"
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_partitioned_active_is_fenced_when_it_returns() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-fencing-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;
    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences this attachment from boot.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    let active_process = spawn_controller(&pair_model, &[], DT);
    let relay = Relay::forwarding(active_process.addr);
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }

    // The partition: the heartbeat path drops while the partitioned
    // active keeps running — scanning, writing, stepping the plant it
    // still believes it owns.
    relay.partition(true);
    for miss in 1..BUDGET {
        standby.advance(1).unwrap();
        // Still unclaimed field: the old owner's writes land.
        active.advance(1).unwrap();
        assert_eq!(standby.role().unwrap().role, Role::Standby, "miss {miss}");
    }

    // The budget-th miss promotes the standby: its claim preempts, its
    // scan writes, its step drives. From this boundary the old peer is
    // fenced — its next requested scan's field write is refused and
    // counted as the `fenced` fault while the scan completes degraded,
    // and the verdict degrades the superseded peer through the demote
    // path rather than ending its process: the gate re-closes and the
    // reported role moves to `demoting`.
    let promoted = standby.advance(1).unwrap();
    assert_eq!(
        standby.role().unwrap().role,
        Role::Active,
        "report: {:?}",
        standby.role().unwrap()
    );
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));

    let fenced = active.advance(1).unwrap();
    assert_eq!(
        fenced
            .io_health
            .last_error
            .as_ref()
            .map(|fault| fault.error),
        Some(IoError::Fenced(VALVE)),
        "the returning peer's write must be refused fenced: {:?}",
        fenced.io_health
    );
    // Nothing the fenced scan staged reached the field — and the
    // field's verdict demoted the superseded peer in place.
    assert_eq!(field.read(VALVE).unwrap().value, carried);
    let report = active.role().unwrap();
    assert_eq!(
        report.role,
        Role::Demoting,
        "the fenced peer must adopt the demote path, not die: {report:?}"
    );
    assert_eq!(report.tick, fenced.tick);
    // The journal carries the claim loss beside the transition it drove.
    assert!(
        active.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { point } if point == VALVE
        )),
        "the fenced owner's journal must record the claim loss"
    );

    // The link heals — the demoted peer's monitor answers again — and
    // its first quiesced scan settles the reported role to `standby`:
    // the survivable degraded state. Its writes stay behind the
    // re-closed gate — exactly one peer writes the field after failover.
    relay.partition(false);
    active.advance(1).unwrap();
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

    for tick in 1..=M {
        let owner = standby.advance(1).unwrap();
        let carried = field.read(VALVE).unwrap().value;
        assert_eq!(
            carried,
            image_value(&owner, VALVE),
            "tick {tick}: the field must carry only the promoted peer's writes"
        );
        // The superseded peer keeps scanning and serving — quiesced at
        // the re-closed gate, alive, and never reaching the field
        // again.
        active.advance(1).unwrap();
        assert_eq!(active.role().unwrap().role, Role::Standby, "tick {tick}");
    }

    let _ = std::fs::remove_dir_all(&dir);
}

/// The issue's acceptance test on the sim-net path: the launched active
/// takes the plant's single-writer claim at startup — a third
/// attachment's write and step are refused from boot, before any
/// promotion — and a rogue claim's preemption is journaled on the
/// fenced owner, not only reported as its exit cause. The converged
/// standby's promotion still takes the field back.
#[test]
fn the_launched_active_claims_the_field_and_a_rogue_claim_is_journaled() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-claim-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;
    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // A third attachment: the launched active already holds the claim,
    // so the plant refuses its writes and steps from boot — reads stay
    // open to every attachment.
    let rogue = RemoteDriver::connect(pair_plant.addr).unwrap();
    assert_eq!(
        rogue.write(SETPOINT, Value::Float(50.0)),
        Err(IoError::Fenced(SETPOINT)),
        "a third attachment's write must be fenced once the active owns the field"
    );
    assert_eq!(
        rogue.step(DT_F64),
        Err(RemoteError::Fenced),
        "a third attachment's step must be fenced once the active owns the field"
    );
    assert_eq!(rogue.read(SETPOINT).unwrap().value, Value::Float(0.0));

    // Converge the standby — the designed takeover path the rogue
    // claim must leave standing.
    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged: {:?}",
        standby.role().unwrap()
    );

    // A rogue claim still preempts unconditionally — the plant cannot
    // tell it from a promoted peer's takeover — but the loss is no
    // longer silent *or* fatal: the fenced owner's next scan completes
    // degraded with the `fenced` fault counted, and the verdict walks
    // it down the demote path — gate re-closed, role `demoting` — with
    // the claim loss and the transition journaled.
    rogue.claim_writer(0xdead_beef).unwrap();
    let fenced = active.advance(1).unwrap();
    assert!(
        matches!(
            fenced
                .io_health
                .last_error
                .as_ref()
                .map(|fault| &fault.error),
            Some(IoError::Fenced(_))
        ),
        "the preempted owner's write must be refused fenced: {:?}",
        fenced.io_health
    );
    assert_eq!(
        active.role().unwrap().role,
        Role::Demoting,
        "the preempted owner must demote, not die: {:?}",
        active.role().unwrap()
    );
    assert!(
        active.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { point } if point == VALVE
        )),
        "the fenced owner's journal must record the claim loss"
    );
    // The first quiesced scan settles `standby` — the demoted peer
    // keeps running, serving, and stays off the field.
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);

    // The designed takeover still stands: the converged standby's
    // promotion claims the field back from the rogue, and the pair's
    // writes and steps drive the plant again.
    standby.promote().unwrap();
    let owner = standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(
        rogue.read(VALVE).unwrap().value,
        image_value(&owner, VALVE),
        "the promoted peer's write must reach the field the rogue held"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

/// The fenced-active acceptance test: `POST /promote` on the converged
/// standby *before* the old active is demoted — the misordered
/// switchover a manual operation can produce — claims the field out
/// from under a still-running field owner. The superseded peer's next
/// scan is refused at the field, and the fenced boundary must degrade
/// it through the demote path — gate re-closed, the reported role
/// walking `active` → `demoting` → `standby`, the claim loss journaled
/// — not exit the process. The restart half of the finding: relaunching
/// the superseded process takes the startup claim and runs — it does
/// not crash-loop on the claim the field still holds — and the peer it
/// in turn fences out degrades the same way.
#[test]
fn a_misordered_promotion_degrades_the_superseded_active() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-superseded-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;
    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences this attachment from boot.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    let mut active_process = spawn_controller(&pair_model, &[], DT);
    let mut standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged: {:?}",
        standby.role().unwrap()
    );

    // The misordered operator action: promote while the old active
    // still runs and still owns the field. The claim preempts — the
    // plant cannot distinguish it from a demote-then-promote takeover —
    // and the promoted peer owns the field from its first scan.
    let promoting = standby.promote().unwrap();
    assert_eq!(promoting.role, Role::Promoting);
    let promoted = standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));

    // The superseded peer's next scan is refused at the field — and
    // survives: the request answers, the field staged nothing, and the
    // peer reports `demoting` — the demote path adopted in place. Its
    // monitor keeps answering throughout.
    let fenced_scan = active.advance(1).unwrap();
    assert_eq!(
        field.read(VALVE).unwrap().value,
        carried,
        "the fenced scan staged nothing onto the field"
    );
    let demoting = active.role().unwrap();
    assert_eq!(demoting.role, Role::Demoting, "{demoting:?}");
    assert_eq!(demoting.tick, fenced_scan.tick);
    let quiesced = active.advance(1).unwrap();
    let settled = active.role().unwrap();
    assert_eq!(settled.role, Role::Standby, "{settled:?}");
    assert_eq!(settled.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(quiesced.tick.0, fenced_scan.tick.0 + 1);

    // The journal carries the evidence: the claim loss beside the
    // `active` → `demoting` → `standby` transition it drove.
    let journal = active.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { point } if point == VALVE
        )),
        "the fenced owner's journal must record the claim loss: {journal:?}"
    );
    let role_changes: Vec<(Role, Role)> = journal
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to } => Some((from, to)),
            _ => None,
        })
        .collect();
    assert_eq!(
        role_changes,
        vec![
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby)
        ]
    );

    // The demoted peer keeps scanning and serving — every field tick
    // still carries only the promoted peer's output — and its process
    // never exited.
    for tick in 1..=M {
        let owner = standby.advance(1).unwrap();
        let survived = active.advance(1).unwrap();
        assert_eq!(survived.tick.0, quiesced.tick.0 + tick);
        assert_eq!(
            field.read(VALVE).unwrap().value,
            image_value(&owner, VALVE),
            "the field must carry only the field owner's writes"
        );
    }
    assert!(
        active_process.child.try_wait().unwrap().is_none(),
        "the superseded peer exited"
    );

    // The restart half of the finding: relaunching the superseded
    // process — `docker start` on the dead active in the QA run — must
    // not crash-loop. Its startup claim retakes the field and its scans
    // run as the owner.
    kill(&mut active_process);
    let mut restarted_process = spawn_controller(&pair_model, &[], DT);
    let restarted = MonitorClient::new(restarted_process.addr);
    for _ in 0..3 {
        let owner = restarted.advance(1).unwrap();
        assert_eq!(
            field.read(VALVE).unwrap().value,
            image_value(&owner, VALVE),
            "the restarted peer must own the field it claimed at startup"
        );
    }
    assert_eq!(restarted.role().unwrap().role, Role::Active);
    assert!(
        restarted_process.child.try_wait().unwrap().is_none(),
        "the restarted peer exited"
    );

    // The peer the restart fenced out degrades the same way: its first
    // scan under the lost claim is refused, it demotes, and its
    // tracking source — the killed process's address — is gone, so it
    // settles to the survivable degraded standby and stays up.
    standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Demoting);
    standby.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Degraded { .. })),
        "the twice-superseded peer reports its survivable degraded state: {report:?}"
    );
    assert!(
        standby_process.child.try_wait().unwrap().is_none(),
        "the twice-superseded peer exited"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
