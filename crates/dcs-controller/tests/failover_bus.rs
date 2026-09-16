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
    IoDriver, IoError, JournalEvent, PointId, Role, Sample, StandbySync, TelemetrySnapshot, Value,
    ValueKind,
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

mod support;

use support::{
    Spawned, image_value, pump, serving_device, spawn, spawn_controller, workspace_binary,
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
    spawn(
        &workspace_binary("dcs-sim-bus-device"),
        &[
            model.to_str().unwrap().to_string(),
            "--device".to_string(),
            device.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
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
        self.executor.scan().unwrap();
        self.field.step(DT_F64).unwrap();
        self.executor.snapshot()
    }

    /// One tick inside the outage window: the gate quiesces the write
    /// and the banks' clocks hold — the field the orphaned pair's
    /// registers keep.
    fn orphaned_tick(&mut self) -> TelemetrySnapshot {
        self.gate.close();
        self.executor.scan().unwrap();
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
    // requested scan's register write is refused with the named
    // `fenced` error, and the field carries only the new owner's
    // output.
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

    let error = active.advance(1).unwrap_err();
    assert!(
        error.to_string().contains("fenced"),
        "the returning peer's write must fail fenced: {error}"
    );
    // Nothing the fenced scan staged reached the register bank.
    assert_eq!(field_ao.read(VALVE).unwrap().value, carried);

    // The link heals — the old peer's monitor answers again and still
    // reports `active`: fencing is the field's verdict, not a role the
    // fenced peer adopted. Its writes stay refused — exactly one peer
    // writes the registers after failover.
    relay.partition(false);
    assert_eq!(active.role().unwrap().role, Role::Active);
    let error = active.advance(1).unwrap_err();
    assert!(
        error.to_string().contains("fenced"),
        "a healed but superseded peer stays fenced: {error}"
    );

    for tick in 1..=M {
        let owner = standby.advance(1).unwrap();
        let carried = field_ao.read(VALVE).unwrap().value;
        assert_eq!(
            carried,
            image_value(&owner, VALVE),
            "tick {tick}: the field must carry only the promoted peer's writes"
        );
        // And every fresh write attempt by the old peer is refused.
        let error = active.advance(1).unwrap_err();
        assert!(error.to_string().contains("fenced"), "tick {tick}: {error}");
    }

    let _ = std::fs::remove_dir_all(&dir);
}
