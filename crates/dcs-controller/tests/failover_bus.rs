//! End-to-end automatic failover over the register-mapped fieldbus —
//! `failover.rs`'s scripted two-peer rig carried onto the `sim-bus`
//! register protocol. The driven pair, the checkpoint-pull heartbeat,
//! and the missed-transfer budget are unchanged; what changes is the
//! shared field: two `dcs-sim-bus-device` processes serve the tank-loop
//! model's channels as numbered registers, so detection, the
//! convergence-gated self-promotion at the scan boundary, and the
//! single-writer fencing verdict all run over the register bank —
//! identical control and lifecycle semantics on a second field
//! interface.
//!
//! Where the plant server owns simulated dynamics, the register bank
//! holds values only: the orphaned field during the detection window is
//! simply the last-written registers, and the field owner's `step`
//! advances only the bank's stamping clock. Bumpless continuation
//! therefore means the promoted peer's register writes continue the
//! reference run's — value and stamped tick alike. The reference is the
//! same bus model assembled in-process against its own pair of device
//! servers, its `WriteGate` replaying the outage window exactly as
//! `failover.rs`'s does: closed and unstepped while the standby counts
//! misses, lifted and stepping again on the promotion tick.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_controller::registry;
use dcs_core::{
    IoDriver, IoError, JournalEvent, LinkState, PointId, Role, Sample, StandbySync,
    TelemetrySnapshot, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::{Executor, WriteGate};
use dcs_sim_bus::{BusDriver, LinkError, PointRegister};
use std::collections::BTreeMap;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

mod support;

use support::{
<<<<<<< HEAD
    Spawned, image_value, kill, pump, serving_device, spawn, spawn_controller, workspace_binary,
=======
    CONTROLLER, PAIR_TOKEN, Spawned, image_value, pump, serving_device, spawn, spawn_controller,
    workspace_binary,
>>>>>>> origin/main
};

/// The model the rig runs — the shared tank-loop document whose
/// devices [`bus_model`] re-points at `sim-bus`: level raw (10) and
/// setpoint (11) in, valve command (20) out, an analog-input scaling
/// and a PID parameterized for dt 0.1.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt. The
/// bank's step ignores it (the register protocol advances a stamping
/// clock, not dynamics); the controllers take it for parity with the
/// paced and `sim-tcp` rigs.
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
/// The model devices the rig serves: device 1 carries the input
/// channels, device 2 the valve command — one `dcs-sim-bus-device`
/// process per device, one register bank each.
const AI_DEVICE: u64 = 1;
const AO_DEVICE: u64 = 2;
/// The channel→register map [`bus_registers`] declares and every device
/// server builds its bank from.
const LEVEL_REGISTER: u16 = 4;
const SETPOINT_REGISTER: u16 = 5;
const VALVE_REGISTER: u16 = 6;
/// The observer attachment's point→register map on each bank — the
/// same mapping the controllers' `sim-bus` backends resolve.
const AI_POINTS: &[PointRegister] = &[
    PointRegister {
        point: LEVEL,
        register: LEVEL_REGISTER,
        kind: ValueKind::Float,
    },
    PointRegister {
        point: SETPOINT,
        register: SETPOINT_REGISTER,
        kind: ValueKind::Float,
    },
];
const AO_POINTS: &[PointRegister] = &[PointRegister {
    point: VALVE,
    register: VALVE_REGISTER,
    kind: ValueKind::Float,
}];

/// A `dcs-sim-bus-device` process serving `model`'s declared `device`
/// on an ephemeral port: it announces `serving device <id> on <addr>
/// (declared <listen>)` once bound.
fn spawn_device(model: &Path, device: u64) -> Spawned {
    spawn_device_at(model, device, "127.0.0.1:0".to_string())
}

/// [`spawn_device`] bound at `listen` — the restarted device process a
/// restart scenario re-serves the carried address with.
fn spawn_device_at(model: &Path, device: u64, listen: String) -> Spawned {
    spawn(
        &workspace_binary("dcs-sim-bus-device"),
        &[
            model.to_str().unwrap().to_string(),
            "--device".to_string(),
            device.to_string(),
            "--listen".to_string(),
            listen,
        ],
        serving_device(device),
    )
}

/// One `dcs-sim-bus-device` process per model device — the shared field
/// a redundant pair attaches to. The returned map feeds the
/// controller-side model's `address` parameters.
fn spawn_devices(serving_model: &Path) -> BTreeMap<u64, Spawned> {
    [AI_DEVICE, AO_DEVICE]
        .iter()
        .map(|&device| (device, spawn_device(serving_model, device)))
        .collect()
}

/// The register map the re-pointed model declares — every channel of
/// the named device at its register. Both ends read this map: the
/// device servers build their banks from it and the controllers'
/// `sim-bus` backends probe it.
fn bus_registers(device: u64) -> serde_json::Value {
    match device {
        AI_DEVICE => serde_json::json!({
            "lt101_raw": LEVEL_REGISTER,
            "lic101_sp": SETPOINT_REGISTER,
        }),
        AO_DEVICE => serde_json::json!({ "lv101_cmd": VALVE_REGISTER }),
        other => panic!("the tank-loop model declares no device {other}"),
    }
}

/// Writes the model whose field I/O lives on the bus: the tank-loop
/// document with every device re-pointed at `sim-bus` — the same shape
/// `failover.rs`'s `controller_model` gives `sim-tcp` — each device's
/// parameters carrying the address `address_of` reports plus the
/// declared channel→register map.
fn bus_model(dir: &Path, name: &str, address_of: impl Fn(u64) -> String) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        let id = device["id"].as_u64().unwrap();
        device["kind"] = "sim-bus".into();
        device["parameters"] = serde_json::json!({
            "address": address_of(id),
            "registers": bus_registers(id),
        });
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// A raw `BusDriver` attachment to a device server — the rig's window
/// on the register bank and its pre-claim field writer.
fn attach(addr: SocketAddr, points: &[PointRegister]) -> BusDriver {
    BusDriver::connect(addr, points).unwrap()
}

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

/// The in-process reference run: the same bus model assembled through
/// the standard registry against its own pair of device servers, behind
/// a `WriteGate` the script closes and reopens to replay the orphaned
/// field's outage window — quiesced writes and unstepped banks while
/// the standby detects the loss, gate lifted and stepping resumed on
/// the tick the standby promotes.
struct Reference<'d> {
    executor: Executor<'d>,
    gate: &'d WriteGate<'d>,
    field: &'d FanoutDriver,
}

impl Reference<'_> {
    /// One tick in the field-owning half of the run: scan, write, step —
    /// each bank's clock advances once, the cadence the pair's field
    /// owner keeps through `FanoutDriver::step`'s per-backend hooks.
    fn owned_tick(&mut self) -> TelemetrySnapshot {
        self.executor.scan();
        self.field.step(DT_F64).unwrap();
        self.executor.snapshot()
    }

    /// One tick inside the outage window: the gate quiesces the write
    /// and the banks' clocks hold — the field the orphaned pair's
    /// registers keep.
    fn orphaned_tick(&mut self) -> TelemetrySnapshot {
        self.gate.close();
        self.executor.scan();
        self.gate.open();
        self.executor.snapshot()
    }
}

/// One scripted failover run over the bus: converge the pair, kill the
/// active, and let the armed standby detect the loss, take both banks'
/// write claim, and self-promote. Returns the field's (valve, level)
/// register trace — samples with their stamping ticks, the sequence a
/// repeated run must reproduce — plus the tick the promotion landed at.
fn run_failover(tag: &str) -> (Vec<(Sample, Sample)>, u64) {
    let dir = std::env::temp_dir().join(format!("dcs-failover-bus-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // The field: two device-server processes per side — the pair's
    // shared banks and the reference run's own — all serving the same
    // declared register map. The serving model's addresses are
    // placeholders; each server binds an ephemeral port through
    // --listen and reports it, then the controller-side models carry
    // the bound addresses.
    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let pair_devices = spawn_devices(&serving);
    let reference_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });
    let reference_model = bus_model(&dir, "reference.json", |device| {
        reference_devices[&device].addr.to_string()
    });

    // Observers on the banks — the field the run asserts on; the
    // setpoint lands before the controllers spawn: the launched
    // active's startup claim fences these attachments, so every later
    // access is a read. `reference_ao` is the register the promoted
    // peer's writes must equal, stamped tick for stamped tick.
    let pair_ai = attach(pair_devices[&AI_DEVICE].addr, AI_POINTS);
    let pair_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    let reference_ao = attach(reference_devices[&AO_DEVICE].addr, AO_POINTS);
    pair_ai.write(SETPOINT, Value::Float(50.0)).unwrap();

    // The active serves checkpoints; the standby pulls one per requested
    // scan — the heartbeat — with the failover budget armed. Arming is
    // honest here because every field-facing device arbitrates a single
    // writer through its device server's claim.
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

    // The reference: the same model in-process against its own device
    // servers — its gate starts open, the field owner's posture, and
    // its banks are never claimed.
    let model = PlantModel::load(&std::fs::read_to_string(&reference_model).unwrap()).unwrap();
    let reference_field = resolve_drivers(&model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap();
    let reference_gate = WriteGate::closed_covering(&reference_field, |point| {
        reference_field.is_field_point(point)
    });
    reference_gate.open();
    let mut reference = Reference {
        executor: assemble(&model, &registry(), &reference_gate).unwrap(),
        gate: &reference_gate,
        field: &reference_field,
    };
    reference_field.write(SETPOINT, Value::Float(50.0)).unwrap();

    // Phase 1: N converged ticks — the standby tracks the active's
    // checkpoints; the reference reproduces both snapshots and the
    // register writes they land.
    let mut trace = Vec::new();
    for tick in 1..=N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        let alone = reference.owned_tick();
        assert_eq!(tracked, owner, "tick {tick}");
        assert_eq!(owner, alone, "tick {tick}");
        let carried = pair_ao.read(VALVE).unwrap();
        assert_eq!(carried.value, image_value(&owner, VALVE), "tick {tick}");
        assert_eq!(carried, reference_ao.read(VALVE).unwrap(), "tick {tick}");
        trace.push((carried, pair_ai.read(LEVEL).unwrap()));
    }
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby never converged: {report:?}"
    );

    // The loss: the active's process dies; its checkpoint endpoint
    // refuses connections from the next pull on, and its bus
    // attachments die with it.
    active_process.child.kill().unwrap();
    active_process.child.wait().unwrap();

    // The detection window — ticks N+1 .. N+BUDGET-1: each requested
    // scan's pull fails, the miss count climbs under the budget, and
    // the peer reports `degraded` without promoting. The orphaned banks
    // hold: no writer, no stepper — the reference replays them
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
        trace.push((pair_ao.read(VALVE).unwrap(), pair_ai.read(LEVEL).unwrap()));
    }

    // The budget-th miss's scan boundary — tick N+BUDGET: the pull
    // fails again, the count reaches the budget, and the still-converged
    // standby takes both banks' write claim and lifts its gate inside
    // this same requested scan. The scan then runs gate-open and its
    // step advances the banks — promotion and first field-owning scan
    // land together, settling the reported role to `active`.
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
    let carried = pair_ao.read(VALVE).unwrap();
    assert_eq!(carried.value, image_value(&promoted, VALVE));
    assert_eq!(carried, reference_ao.read(VALVE).unwrap());
    trace.push((carried, pair_ai.read(LEVEL).unwrap()));

    // Phase 2: M post-failover ticks — the promoted peer keeps writing
    // and stepping the banks, tick-identical to the reference run:
    // bumpless output continuation through the takeover, observed at
    // the register bank.
    for tick in (promotion_tick + 1)..=(promotion_tick + M) {
        let owner = standby.advance(1).unwrap();
        let alone = reference.owned_tick();
        assert_eq!(owner, alone, "tick {tick}");
        let carried = pair_ao.read(VALVE).unwrap();
        assert_eq!(carried.value, image_value(&owner, VALVE), "tick {tick}");
        assert_eq!(carried, reference_ao.read(VALVE).unwrap(), "tick {tick}");
        trace.push((carried, pair_ai.read(LEVEL).unwrap()));
    }

    // The journal carries both halves of the self-promotion — the same
    // transitions a manual `POST /promote` records.
    let role_changes: Vec<(Role, Role)> = standby
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to, .. } => Some((from, to)),
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
    let dir =
        std::env::temp_dir().join(format!("dcs-failover-bus-transient-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let pair_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });
    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences these attachments from boot.
    let field_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    attach(pair_devices[&AI_DEVICE].addr, AI_POINTS)
        .write(SETPOINT, Value::Float(50.0))
        .unwrap();
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
    // promotes nothing either; the registers kept carrying only the
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
        assert_eq!(
            field_ao.read(VALVE).unwrap().value,
            image_value(&owner, VALVE)
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_unconverged_standby_reports_its_state_and_never_promotes() {
    let dir = std::env::temp_dir().join(format!(
        "dcs-failover-bus-unconverged-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();

    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let pair_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });

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
    let field_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);

    // Past the budget and beyond: no convergence proof ever stood, so
    // the failover window never opens — the peer reports `degraded`,
    // stays `standby`, and its writes never reach the registers.
    for miss in 1..=(BUDGET as u64 + 2) {
        standby.advance(1).unwrap();
        let report = standby.role().unwrap();
        assert_eq!(report.role, Role::Standby, "miss {miss} promoted");
        assert!(
            matches!(report.sync, Some(StandbySync::Degraded { .. })),
            "miss {miss} should report degraded: {report:?}"
        );
        assert_eq!(
            field_ao.read(VALVE).unwrap().value,
            Value::Float(0.0),
            "an unpromoted standby's writes reached the field"
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_partitioned_active_is_fenced_when_it_returns() {
    let dir = std::env::temp_dir().join(format!("dcs-failover-bus-fencing-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let pair_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });
    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences these attachments from boot.
    let field_ai = attach(pair_devices[&AI_DEVICE].addr, AI_POINTS);
    let field_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    field_ai.write(SETPOINT, Value::Float(50.0)).unwrap();
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
    // active keeps running — scanning, writing, stepping the banks it
    // still believes it owns.
    relay.partition(true);
    for miss in 1..BUDGET {
        standby.advance(1).unwrap();
        // The old owner's claim still stands: its register writes land.
        active.advance(1).unwrap();
        assert_eq!(standby.role().unwrap().role, Role::Standby, "miss {miss}");
    }

    // The budget-th miss promotes the standby: its claim preempts on
    // both device servers, its scan writes, its step advances the
    // banks. From this boundary the old peer is fenced — its next
    // requested scan's exchange is refused and counted as the named
    // `fenced` fault while the scan completes degraded, and the verdict
    // degrades the superseded peer through the demote path rather than
    // ending its process: the gate re-closes and the reported role
    // moves to `demoting`.
    let promoted = standby.advance(1).unwrap();
    assert_eq!(
        standby.role().unwrap().role,
        Role::Active,
        "report: {:?}",
        standby.role().unwrap()
    );
    let carried = field_ao.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));

    // The register bank's own verdict: an attachment that does not hold
    // the claim — what the returning old peer now is — finds its writes
    // answered `IoError::Fenced` and its steps `LinkError::Fenced`,
    // while reads stay open.
    let fenced = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    assert_eq!(
        fenced.write(VALVE, Value::Float(0.0)),
        Err(IoError::Fenced(VALVE))
    );
    assert_eq!(fenced.step(DT_F64), Err(LinkError::Fenced));
    assert_eq!(fenced.read(VALVE).unwrap().value, carried);
    // The claim covered every field-facing device, not only the one the
    // promoted run writes.
    let fenced_ai = attach(pair_devices[&AI_DEVICE].addr, AI_POINTS);
    assert_eq!(
        fenced_ai.write(SETPOINT, Value::Float(0.0)),
        Err(IoError::Fenced(SETPOINT))
    );
    assert_eq!(fenced_ai.step(DT_F64), Err(LinkError::Fenced));

    // The old peer's requested scan is fenced at the bank — counted as
    // the named fault while the scan completes degraded — and the
    // peer survives: the refusal demotes it in place.
    let fenced_scan = active.advance(1).unwrap();
    assert!(
        matches!(
            fenced_scan
                .io_health
                .last_error
                .as_ref()
                .map(|fault| &fault.error),
            Some(IoError::Fenced(_))
        ),
        "the returning peer's exchange must be refused fenced: {:?}",
        fenced_scan.io_health
    );
    // Nothing the fenced scan staged reached the register bank — and
    // the field's verdict demoted the superseded peer in place.
    assert_eq!(field_ao.read(VALVE).unwrap().value, carried);
    let report = active.role().unwrap();
    assert_eq!(
        report.role,
        Role::Demoting,
        "the fenced peer must adopt the demote path, not die: {report:?}"
    );
    assert_eq!(report.tick, fenced_scan.tick);

    // The link heals — the demoted peer's monitor answers again — and
    // its first quiesced scan settles `standby`: the survivable
    // degraded state. Its writes stay behind the re-closed gate —
    // exactly one peer writes the registers after failover. The
    // promoted peer announced itself through its pulls, so the demoted
    // run's first tracking cycle reconverges on its successor.
    relay.partition(false);
    active.advance(1).unwrap();
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must follow its successor and reconverge: {report:?}"
    );

    for tick in 1..=M {
        let owner = standby.advance(1).unwrap();
        let carried = field_ao.read(VALVE).unwrap().value;
        assert_eq!(
            carried,
            image_value(&owner, VALVE),
            "tick {tick}: the field must carry only the promoted peer's writes"
        );
        // The superseded peer keeps scanning and serving — quiesced at
        // the re-closed gate, alive, and never reaching the banks
        // again.
        active.advance(1).unwrap();
        assert_eq!(active.role().unwrap().role, Role::Standby, "tick {tick}");
    }

    let _ = std::fs::remove_dir_all(&dir);
}

<<<<<<< HEAD
#[test]
fn a_restarted_device_recovers_and_the_peer_promotes() {
    // QA `sim-bus-driver-permanent-disconnect-after-device-blip`: a
    // device server dying and rebinding under a live pair used to sever
    // every attachment permanently — the active scanned on dead field
    // I/O and the peer's promote refused `no live connection`. The
    // drivers now re-attach lazily, so the recovered field serves the
    // same pair and the promotion lands.
    let dir = std::env::temp_dir().join(format!("dcs-failover-bus-restart-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let mut pair_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });
=======
/// The QA reproduction: a second controller launched born-active on
/// the live pair's field must not seize the standing claim. The
/// register protocol's conditional grant answers `fenced` for a
/// different owner's live claim — bus claims die with their last
/// holder's link, so a standing claim *is* a live incumbent — and the
/// born-active launch settles that refusal by exiting nonzero: no
/// preemption, no incumbent fencing, no orphaned tracking peer.
#[test]
fn a_born_active_launch_refuses_over_a_live_incumbent_claim() {
    let dir = std::env::temp_dir().join(format!(
        "dcs-failover-bus-born-active-{}",
        std::process::id()
    ));
    std::fs::create_dir_all(&dir).unwrap();

    let serving = bus_model(&dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let pair_devices = spawn_devices(&serving);
    let pair_model = bus_model(&dir, "pair.json", |device| {
        pair_devices[&device].addr.to_string()
    });
    // The rig's window on the banks — this attachment never claims, so
    // once a claim stands its writes fence: the field's own verdict on
    // whether the claim flipped.
>>>>>>> origin/main
    let field_ai = attach(pair_devices[&AI_DEVICE].addr, AI_POINTS);
    let field_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    field_ai.write(SETPOINT, Value::Float(50.0)).unwrap();

<<<<<<< HEAD
    let active_process = spawn_controller(&pair_model, &[], DT);
    let mut standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converged ticks before the loss.
    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    assert!(
        matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged"
    );

    // The outage: both device processes die — listeners, links, and
    // claims with them — while the controllers keep running.
    let addrs: BTreeMap<u64, SocketAddr> = pair_devices
        .iter()
        .map(|(device, spawned)| (*device, spawned.addr))
        .collect();
    for spawned in pair_devices.values_mut() {
        kill(spawned);
    }
    // The observer attachment severs the same way: the field is down.
    assert_eq!(field_ao.read(VALVE), Err(IoError::Disconnected(VALVE)));

    // A scan inside the outage degrades at the field boundary — failed
    // reads and writes counted, the aggregate link disconnected — and
    // completes anyway: field loss is a transient health event, not a
    // run failure, so the role stays `active`.
    let degraded = active.advance(1).unwrap();
    assert!(
        degraded.io_health.failed_reads > 0,
        "{:?}",
        degraded.io_health
    );
    assert!(
        degraded.io_health.failed_writes > 0,
        "{:?}",
        degraded.io_health
    );
    assert_eq!(
        degraded.io_health.driver.as_ref().map(|driver| driver.link),
        Some(LinkState::Disconnected),
        "{:?}",
        degraded.io_health
    );
    assert_eq!(active.role().unwrap().role, Role::Active);
    // The tracking pull never touches the field — the standby stays
    // attached to its source through the outage.
    standby.advance(1).unwrap();

    // The devices come back on the same addresses — the restart.
    for (device, addr) in &addrs {
        pair_devices.insert(
            *device,
            spawn_device_at(&serving, *device, addr.to_string()),
        );
    }

    // Recovery is bounded: the drivers re-attach lazily with one
    // contact attempt per re-attach window, so a handful more requested
    // scans serve the field again — no controller restart. Both sides'
    // aggregate link must report connected: the standby's quiesced
    // scans keep its links exercised, and its claim asks ride them at
    // promote time.
    let deadline = Instant::now() + Duration::from_secs(15);
    let mut scans = 0u64;
    let recovered = loop {
        let standby_snapshot = standby.advance(1).unwrap();
        let snapshot = active.advance(1).unwrap();
        scans += 1;
        let connected = |snapshot: &TelemetrySnapshot| {
            snapshot
                .io_health
                .driver
                .as_ref()
                .is_some_and(|driver| driver.link == LinkState::Connected)
        };
        if connected(&standby_snapshot) && connected(&snapshot) {
            break snapshot;
        }
        assert!(
            Instant::now() < deadline,
            "the controllers' drivers never re-attached"
        );
        thread::sleep(Duration::from_millis(200));
    };
    assert!(
        scans <= 12,
        "recovery took {scans} scans — the re-attach must land within a few windows"
    );
    // The re-armed claim holds: the owner's writes fence a claim-less
    // attachment again, and the bank carried the recovered scan's
    // write.
    assert_eq!(
        field_ao.write(VALVE, Value::Float(0.0)),
        Err(IoError::Fenced(VALVE))
    );
    assert_eq!(
        field_ao.read(VALVE).unwrap().value,
        image_value(&recovered, VALVE)
    );

    // The reproduction's tail — `POST /promote` on the converged peer:
    // pre-fix it refused with `backend device 2 failed to step: no live
    // connection` because the severed link could never return; now the
    // claim rides the re-attached link (and a link no scan traffic had
    // exercised since the outage replays its claim ask once on the
    // fresh attachment).
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        standby.advance(1).unwrap();
        if matches!(
            standby.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ) {
            break;
        }
        assert!(Instant::now() < deadline, "the standby never reconverged");
    }
    let promoted = match standby.promote() {
        Ok(report) => report,
        Err(error) => {
            kill(&mut standby_process);
            panic!(
                "promote failed: {error}\nstandby stderr:\n{}",
                standby_process.stderr_tail()
            );
        }
    };
    assert_eq!(promoted.role, Role::Promoting, "{promoted:?}");
    let owned = standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);
    // The promoted run's first field-owning scan wrote the bank.
=======
    // Controller A launches born-active: its conditional startup grant
    // lands on the unclaimed field and it scans as the field owner.
    let active_process = spawn_controller(&pair_model, &[], DT);
    let active = MonitorClient::new(active_process.addr);
    let owned = active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Active);
>>>>>>> origin/main
    assert_eq!(
        field_ao.read(VALVE).unwrap().value,
        image_value(&owned, VALVE)
    );

<<<<<<< HEAD
    // The takeover is the field's own arbitration: the superseded
    // active's next scan fences its writes and demotes it in place,
    // and the re-closed gate keeps its writes off the registers.
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
        "the superseded peer's write must fence: {:?}",
        fenced.io_health
    );
    assert_eq!(active.role().unwrap().role, Role::Demoting);
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
=======
    // The reproduction's second launch: born-active on the same model,
    // no `--peer` — previously the unconditional claim seized the
    // field out from under A. Now the conditional ask meets A's
    // standing claim, answers fenced, and the pairless launch fails —
    // exiting nonzero and naming the live incumbent, exactly the
    // sim-tcp verdict.
    let output = std::process::Command::new(CONTROLLER)
        .args([
            pair_model.to_str().unwrap(),
            "--pair-token",
            PAIR_TOKEN,
            "--listen",
            "127.0.0.1:0",
            "--driven",
            "--dt",
            DT,
        ])
        .output()
        .unwrap();
    assert!(
        !output.status.success(),
        "the born-active launch over a live claim must exit nonzero: {output:?}"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("a live peer holds the field's write-ownership claim"),
        "the refusal must name the live incumbent's claim: {stderr}"
    );

    // A never felt the attempt: it keeps scanning, stepping, and
    // writing as the active — and the claim stands unflipped: a fresh
    // attachment's conditional ask on a foreign token still refuses on
    // every bank, and its writes still fence.
    for _ in 0..N {
        let owned = active.advance(1).unwrap();
        assert_eq!(active.role().unwrap().role, Role::Active);
        assert_eq!(
            field_ao.read(VALVE).unwrap().value,
            image_value(&owned, VALVE),
            "the field must carry only the incumbent's writes"
        );
    }
    let probe_ai = attach(pair_devices[&AI_DEVICE].addr, AI_POINTS);
    let probe_ao = attach(pair_devices[&AO_DEVICE].addr, AO_POINTS);
    assert_eq!(
        probe_ai.claim_writer_unless_held(0xffff),
        Err(LinkError::Fenced)
    );
    assert_eq!(
        probe_ao.claim_writer_unless_held(0xffff),
        Err(LinkError::Fenced)
    );
    assert_eq!(
        field_ao.write(VALVE, Value::Float(0.0)),
        Err(IoError::Fenced(VALVE))
    );
    // The incumbent's record shows no fencing episode — the refused
    // ask never disturbed the standing claim.
    assert!(
        active
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::FieldClaimLost { .. })),
        "the incumbent must never report a lost claim"
    );
>>>>>>> origin/main

    let _ = std::fs::remove_dir_all(&dir);
}
