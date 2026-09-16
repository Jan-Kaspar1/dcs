//! End-to-end two-controller hot swap over the shared simulated plant —
//! the milestone's done-when wired as one scripted, tick-paced run.
//!
//! A `dcs-plant-server` process owns the shared simulated plant — the
//! `tank_loop` fixture with its first-order-lag dynamics — and two
//! `dcs-controller` processes load the same model, its devices re-pointed
//! at the `sim-tcp` kind carrying the plant's address so the run
//! exercises the driver registry's remote-sim path. Both controllers
//! run `--driven`: scans happen only when `POST /scan` requests them,
//! each requested scan carrying the tracking standby's checkpoint pull
//! and the field-owning peer's plant step inside the request's boundary,
//! so the run is deterministic and never wall-clock paced.
//!
//! The scenario: the standby converges on the active's checkpoints while
//! its write gate quiesces every field write; the documented switchover —
//! `POST /demote` on the active, then `POST /promote` on the converged
//! standby — moves field-write ownership at the scan boundary between
//! two ticks; the promoted controller's output sequence then equals the
//! uninterrupted single-controller reference run's — a third driven
//! controller against a second plant server — while the demoted peer
//! keeps scanning quiesced. `GET /role` reports each peer's stage, the
//! journals carry the transitions, and the whole scripted run repeats
//! identically.

use dcs_core::{
    Command, CommandError, CommandOutcome, IoDriver, JournalEvent, PointId, Quality, QualityReason,
    Role, StandbySync, SwitchError, TelemetrySnapshot, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

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
/// Ticks the pair runs — the standby tracking gate-closed — before the
/// switchover.
const N: u64 = 20;
/// Ticks after the switch whose outputs must equal the reference run's.
const M: u64 = 40;
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

/// One scripted run of the full hot-swap scenario. Returns the valve
/// command and raw level the shared plant carried after each tick — the
/// field's output sequence two runs must reproduce exactly.
fn run_swap(tag: &str) -> Vec<(Value, Value)> {
    let dir = std::env::temp_dir().join(format!("dcs-hot-swap-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants: the pair's and the reference run's — identical
    // model and dynamics, identical request sequences, identical runs.
    let pair_plant = spawn_plant();
    let reference_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let reference_model = controller_model(&dir, "reference.json", reference_plant.addr);

    // The pair: the active first — the standby's --standby names its
    // monitoring address — then the standby, then the reference run on
    // its own plant.
    let active_process = spawn_controller(&pair_model, &[]);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
    );
    let reference_process = spawn_controller(&reference_model, &[]);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let reference = MonitorClient::new(reference_process.addr);

    // Observers on both shared plants — the field the scenario asserts
    // on. The setpoint lands once, as the run's operating point.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    let reference_field = RemoteDriver::connect(reference_plant.addr).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    reference_field.write(SETPOINT, Value::Float(50.0)).unwrap();

    // Roles are visible on both monitor surfaces before any transfer:
    // the active, and an unsynchronized standby.
    assert_eq!(active.role().unwrap().role, Role::Active);
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

    // Promoting the unconverged standby is the named refusal, not a
    // take-over — and a standby's commands are refused at the role
    // boundary rather than phantom-applied.
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NotConverged {
            sync: StandbySync::Unsynchronized
        }
    );
    let receipt = standby
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(42.0),
        })
        .unwrap();
    assert!(
        matches!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotActive {
                    role: Role::Standby,
                    ..
                }
            }
        ),
        "{receipt:?}"
    );

    // Phase 1: N ticks. Each tick the standby pulls the active's
    // checkpoint, applies it, and scans quiesced; the active scans and
    // steps the shared plant; the reference does the same on its plant.
    // The standby's snapshots equal the active's tick for tick — it is
    // tracking — and the field carries only the field owner's writes.
    let mut trace = Vec::new();
    for tick in 1..=N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
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

    // The documented switchover at the scan boundary between ticks N
    // and N+1: demote the active first — its gate closes with the
    // request — then promote the converged standby, whose gate lifts at
    // the same boundary.
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);

    // The demoted peer's next scan is already quiesced: the run still
    // computes the outputs — its image advances — but nothing reaches
    // the field. (The process is still moving at tick N+1, so the image
    // provably differs from the held value: the gate suppressed a real
    // write.)
    let held = field.read(VALVE).unwrap().value;
    let quiesced = active.advance(1).unwrap();
    assert_eq!(field.read(VALVE).unwrap().value, held);
    assert_ne!(image_value(&quiesced, VALVE), held);

    // The promoted peer's first scan continues the checkpointed run:
    // same tick, same outputs — and now the field carries its write.
    let continued = standby.advance(1).unwrap();
    assert_eq!(continued, quiesced);
    assert_eq!(continued, reference.advance(1).unwrap());
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&continued, VALVE));
    trace.push((carried, field.read(LEVEL).unwrap().value));

    // Phase 2: M-1 more ticks — the demoted peer scans quiesced (still
    // computing the uninterrupted run), the promoted peer writes and
    // steps the plant, and the reference run confirms the takeover is
    // bumpless: identical outputs, tick for tick.
    for tick in (N + 2)..=(N + M) {
        let quiesced = active.advance(1).unwrap();
        let continued = standby.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
        assert_eq!(continued, quiesced, "tick {tick}");
        assert_eq!(continued, alone, "tick {tick}");
        let carried = field.read(VALVE).unwrap().value;
        assert_eq!(carried, image_value(&continued, VALVE), "tick {tick}");
        trace.push((carried, field.read(LEVEL).unwrap().value));
    }

    // Roles settled and reported on both monitor surfaces; a repeated
    // promotion is the named refusal too.
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.sync, None);
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::AlreadyActive
    );

    // The demoted peer stays quiesced — its commands are refused at the
    // role boundary.
    let receipt = active
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(1.0),
        })
        .unwrap();
    assert!(matches!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotActive {
                role: Role::Standby,
                ..
            }
        }
    ));

    // The journals carry both halves of the switch.
    let role_changes = |client: &MonitorClient| -> Vec<(Role, Role)> {
        client
            .journal(0)
            .unwrap()
            .iter()
            .filter_map(|entry| match entry.event {
                JournalEvent::RoleChanged { from, to, .. } => Some((from, to)),
                _ => None,
            })
            .collect()
    };
    assert_eq!(
        role_changes(&standby),
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active)
        ]
    );
    assert_eq!(
        role_changes(&active),
        vec![
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby)
        ]
    );

    let _ = std::fs::remove_dir_all(&dir);
    trace
}

#[test]
fn hot_swap_over_the_shared_plant_is_deterministic_across_runs() {
    // Two complete scripted runs — spawn, converge, switch, M ticks of
    // comparison — must produce identical field output sequences.
    let first = run_swap("a");
    let second = run_swap("b");
    assert_eq!(first.len() as u64, N + M);
    assert_eq!(first, second);
}

#[test]
fn a_mid_run_plant_restart_surfaces_named_io_errors() {
    let dir = std::env::temp_dir().join(format!("dcs-hot-swap-restart-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let mut plant = spawn_plant();
    let model = controller_model(&dir, "pair.json", plant.addr);
    let active_process = spawn_controller(&model, &[]);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // A few ticks of normal operation, then the plant process dies.
    for _ in 0..3 {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }
    plant.child.kill().unwrap();
    plant.child.wait().unwrap();

    // The field owner's next requested scan surfaces the dead plant as
    // the documented IoError — the output write fails `disconnected`,
    // not a silently divergent run.
    let error = active.advance(1).unwrap_err();
    assert!(error.to_string().contains("disconnected"), "{error}");

    // The standby's checkpoint pull still works — the active's monitor
    // is alive — and its quiesced scan completes with the dead field's
    // reads marked Bad(CommunicationFault): degraded quality, not
    // fabricated data.
    let snapshot = standby.advance(1).unwrap();
    let level = snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == LEVEL)
        .and_then(|telemetry| telemetry.sample)
        .unwrap();
    assert_eq!(
        level.quality,
        Quality::Bad(QualityReason::CommunicationFault),
        "{level:?}"
    );

    // A restarted plant is a fresh process at initial state; the dead
    // remote drivers never reconnect, so both peers keep surfacing the
    // named error rather than resuming against a reset field — the
    // demoted one's reads stay Bad, the owner's write stays refused.
    let restarted = spawn_plant();
    let error = active.advance(1).unwrap_err();
    assert!(error.to_string().contains("disconnected"), "{error}");
    let snapshot = standby.advance(1).unwrap();
    let level = snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == LEVEL)
        .and_then(|telemetry| telemetry.sample)
        .unwrap();
    assert_eq!(
        level.quality,
        Quality::Bad(QualityReason::CommunicationFault),
        "{level:?}"
    );
    let fresh = RemoteDriver::connect(restarted.addr).unwrap();
    assert_eq!(fresh.read(VALVE).unwrap().value, Value::Float(0.0));

    let _ = std::fs::remove_dir_all(&dir);
}
