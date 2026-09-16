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
use dcs_core::{IoDriver, JournalEvent, PointId, Role, StandbySync, TelemetrySnapshot, Value};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::{Executor, WriteGate};
use dcs_sim_net::RemoteDriver;
use std::io::{BufRead, BufReader};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
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

/// The `dcs-plant-server` binary — a sibling of the controller binary
/// under test in the workspace target dir; workspace builds produce it.
fn plant_server() -> PathBuf {
    let binary = Path::new(CONTROLLER)
        .parent()
        .unwrap()
        .join(format!("dcs-plant-server{}", std::env::consts::EXE_SUFFIX));
    assert!(
        binary.is_file(),
        "{} not found — build the workspace first",
        binary.display()
    );
    binary
}

/// A spawned process: its bound address learned from the `listening on`
/// stderr line, stderr held open so a later diagnostic write never meets
/// a closed pipe, and a kill on drop so a panicking test leaves no stray
/// processes behind.
struct Spawned {
    child: Child,
    addr: SocketAddr,
    _stderr: BufReader<ChildStderr>,
}

impl Drop for Spawned {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawns `binary`, reads its `listening on <addr>` line, and returns
/// the running process.
fn spawn(binary: &Path, args: &[String]) -> Spawned {
    let mut child = Process::new(binary)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("cannot spawn {}: {error}", binary.display()));
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut line = String::new();
    if stderr.read_line(&mut line).unwrap() == 0 {
        panic!("{} exited before reporting its address", binary.display());
    }
    let addr = line
        .trim()
        .strip_prefix("listening on ")
        .unwrap_or_else(|| {
            panic!(
                "expected a `listening on` line from {}, found {line:?}",
                binary.display()
            )
        })
        .parse()
        .unwrap();
    Spawned {
        child,
        addr,
        _stderr: stderr,
    }
}

/// A plant-server process serving the shared tank-loop plant on an
/// ephemeral port.
fn spawn_plant() -> Spawned {
    spawn(
        &plant_server(),
        &[
            PLANT_MODEL.to_string(),
            "--dynamics".to_string(),
            PLANT_DYNAMICS.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ],
    )
}

/// A `--driven` controller process on `model`: the monitor serves on an
/// ephemeral port and scans run only when `POST /scan` requests them.
fn spawn_controller(model: &Path, extra: &[String]) -> Spawned {
    let mut args = vec![model.to_str().unwrap().to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", DT] {
        args.push(arg.to_string());
    }
    spawn(Path::new(CONTROLLER), &args)
}

/// Writes the controller-side model for a plant server at `plant`: the
/// shared tank-loop model with every device's kind re-pointed at
/// `sim-tcp` and `parameters.address` set — the remote-sim path through
/// the assembly driver registry.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        device["kind"] = "sim-tcp".into();
        device["parameters"] = serde_json::json!({ "address": plant.to_string() });
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// The value `snapshot`'s image reports for `point`.
fn image_value(snapshot: &TelemetrySnapshot, point: PointId) -> Value {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap()
        .value
}

/// Pumps one accepted connection against the real monitor: two
/// copy loops, one per direction, each ending by half-closing the
/// other side so the request/response pair completes and the sockets
/// close cleanly.
fn pump(client: TcpStream, upstream: SocketAddr) {
    let Ok(server) = TcpStream::connect(upstream) else {
        return;
    };
    let Ok(client_reader) = client.try_clone() else {
        return;
    };
    let Ok(server_reader) = server.try_clone() else {
        return;
    };
    let writer = thread::spawn(move || {
        let mut from = client_reader;
        let mut to = server;
        let _ = std::io::copy(&mut from, &mut to);
        let _ = to.shutdown(Shutdown::Write);
    });
    let mut from = server_reader;
    let mut to = client;
    let _ = std::io::copy(&mut from, &mut to);
    let _ = to.shutdown(Shutdown::Write);
    let _ = writer.join();
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
    let pair_plant = spawn_plant();
    let reference_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

    // The active serves checkpoints; the standby pulls one per requested
    // scan — the heartbeat — with the failover budget armed.
    let mut active_process = spawn_controller(&pair_model, &[]);
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // The reference: the same model in-process against its own plant —
    // its gate starts open, the field owner's posture.
    let model = PlantModel::load(MODEL_SOURCE).unwrap();
    let reference_driver = RemoteDriver::connect(reference_plant.addr).unwrap();
    let reference_gate = WriteGate::closed(&reference_driver);
    reference_gate.open();
    let mut reference = Reference {
        executor: assemble(&model, &registry(), &reference_gate).unwrap(),
        gate: &reference_gate,
        plant: RemoteDriver::connect(reference_plant.addr).unwrap(),
    };

    // Observers on both plants — the field the run asserts on; the
    // setpoint lands once, before any claim exists.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
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
    let dir = std::env::temp_dir().join(format!("dcs-failover-transient-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let active_process = spawn_controller(&pair_model, &[]);
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
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();

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

    let pair_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

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

    let pair_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let active_process = spawn_controller(&pair_model, &[]);
    let relay = Relay::forwarding(active_process.addr);
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();

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
    // fenced — its next requested scan's field write is refused with the
    // named `fenced` error, and the field carries only the new owner's
    // output.
    let promoted = standby.advance(1).unwrap();
    assert_eq!(
        standby.role().unwrap().role,
        Role::Active,
        "report: {:?}",
        standby.role().unwrap()
    );
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));

    let error = active.advance(1).unwrap_err();
    assert!(
        error.to_string().contains("fenced"),
        "the returning peer's write must fail fenced: {error}"
    );
    // Nothing the fenced scan staged reached the field.
    assert_eq!(field.read(VALVE).unwrap().value, carried);

    // The link heals — the old peer's monitor answers again and still
    // reports `active`: fencing is the field's verdict, not a role the
    // fenced peer adopted. Its writes stay refused — exactly one peer
    // writes the field after failover.
    relay.partition(false);
    assert_eq!(active.role().unwrap().role, Role::Active);
    let error = active.advance(1).unwrap_err();
    assert!(
        error.to_string().contains("fenced"),
        "a healed but superseded peer stays fenced: {error}"
    );

    for tick in 1..=M {
        let owner = standby.advance(1).unwrap();
        let carried = field.read(VALVE).unwrap().value;
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
