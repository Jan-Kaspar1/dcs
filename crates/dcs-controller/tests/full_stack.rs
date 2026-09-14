//! The full-stack demonstration: the shared showcase plant served by
//! `dcs-plant-server`, a redundant `dcs-controller` pair attached over
//! `sim-tcp` remote devices, and the monitoring surface exercised
//! through [`PairClient`] — the vision's demonstration path run as one
//! scripted, tick-paced integration scenario.
//!
//! ## Topology
//!
//! Two `dcs-plant-server` processes each serve the showcase model —
//! `dcs-demo`'s `showcase.json` — merged with its dynamics document
//! `showcase_dynamics.json`: the tank's first-order lag from the valve's
//! raw command to the level's raw input, the same element
//! `dcs_demo::showcase::driver` adds in-process. One plant is the pair's
//! shared field; the other paces an uninterrupted single-controller
//! reference run whose outputs the pair's must equal.
//!
//! The controllers load the same model with its devices re-pointed at
//! the `sim-tcp` kind: every declared channel merges onto one remote
//! device carrying the plant's address, so each controller's fan-out is
//! a single [`RemoteDriver`] backend stepping the shared plant once per
//! scan — the pacing the dynamics document and the pid's `dt` are tuned
//! for. The model's field loopbacks (pump-run feedback, valve position)
//! resolve to fan-out routes the field owner applies at each step —
//! through the write gate, so a tracking peer's copies stay quiesced —
//! while the plant's own channel-map loopbacks keep the wires true for
//! every attached client.
//!
//! ## Tick pacing
//!
//! All three controllers run `--driven`: a scan happens only when
//! `POST /scan` requests it, each request carrying the tracking
//! standby's checkpoint pull and the field owner's plant step inside the
//! request boundary — the #64 convention; nothing is wall-clock paced.
//! The world is perturbed through the shipped plant tooling: the run
//! drives `dcs-plant-ctl` invocations to list the served points and to
//! inject and clear the fault on both plants.
//!
//! ## The scripted sequence
//!
//! 1. **Roles and the pair view.** `GET /role` reports `active` and an
//!    unsynchronized `standby`; the pair view selects the active as its
//!    source and serves the model's signal index. An early
//!    `POST /promote` is refused `not_converged`.
//! 2. **Settle.** `SETTLE_SCANS` scripted ticks — each one the standby
//!    pulling the active's checkpoint and scanning gate-closed, the
//!    active scanning and stepping the plant, the reference doing the
//!    same on its plant. Every tick the three snapshots are equal and
//!    the field carries the owner's write. The loop regulates at the
//!    declared 60% with the start-delay trip already counted.
//! 3. **Setpoint command.** `PairClient::command` writes 40% to the
//!    writable setpoint point on the settled active — accepted for the
//!    next scan boundary. The tracking peer sits the apply tick out —
//!    queued commands are not checkpoint-carried, the documented
//!    boundary — and picks the applied value up in its next pull.
//! 4. **Regulation at the moved setpoint.** The rest of the moved phase
//!    in triple agreement, re-settling at 40%.
//! 5. **Injected fault.** `dcs-plant-ctl fault 30 bad:communication_fault`
//!    on both plants: the conditioned run status goes `Bad`, the
//!    interlock trips on the bad permissive and drives the valve to its
//!    safe value, and the level drains past the low alarm — horn
//!    asserted, motor fault flagged, the trip counter latching its
//!    preset — the documented behavior of `dcs-demo`'s showcase run,
//!    observed here through monitor payloads. The fault is visible in
//!    the snapshot and journaled as a quality transition on both peers.
//!    (The fault is injected as a quality fault, not `disconnected`: the
//!    field owner's own loopback route writes the feedback point every
//!    step, and a disconnect would fail that write and the scan request
//!    itself. A quality fault degrades the signal without breaking the
//!    exchange — and produces the same `Bad(CommunicationFault)` the
//!    executor marks a failed read with.)
//! 6. **Promotion mid-fault.** `POST /demote` on the active, then
//!    `POST /promote` on the converged standby: the field writer moves
//!    at the scan boundary, `GET /role` reports the transition states on
//!    both peers, the pair view follows the promoting peer, and the
//!    promoted peer's outputs keep matching the uninterrupted reference
//!    — bumpless continuation through the faulted run, the demoted peer
//!    still agreeing scan for scan behind its closed gate.
//! 7. **Recovery.** The tooling clears the fault; the interlock
//!    auto-resets, the level recovers to the moved setpoint, and the
//!    alarm and motor fault clear.
//! 8. **Post-switch command.** The trip-counter reset lands on the new
//!    active through the pair view — assert, then release to rearm —
//!    clearing the latched count.
//!
//! The whole run executes twice; the per-tick field trace, the stage
//! snapshots, and the journals must be identical — the determinism the
//! acceptance criteria require.

use dcs_core::{
    Command, CommandError, CommandOutcome, JournalEntry, JournalEvent, PointId, Quality,
    QualityReason, Role, RoleReport, Sample, StandbySync, SwitchError, TelemetrySnapshot, Tick,
    Value, ValueKind,
};
use dcs_demo::showcase::{
    self, FAULT_SCANS, INITIAL_SETPOINT, MOVED_SCANS, MOVED_SETPOINT, RECOVERY_SCANS, SETTLE_SCANS,
    points,
};
use dcs_monitor::{MonitorClient, PairClient, PeerStatus};
use dcs_sim_net::RemoteDriver;
use std::collections::BTreeSet;
use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The showcase plant model the plant servers load — the #69 fixture.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase.json"
);
/// The showcase's plant-side dynamics document: the tank's first-order
/// lag from `lv101_raw` (20) to `lt101_raw` (10), the same element
/// `dcs_demo::showcase::driver` declares in-process.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase_dynamics.json"
);
/// The controller-side model source — the shared showcase model whose
/// devices [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-demo/fixtures/showcase.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the promoted pair runs still-faulted after the switchover,
/// proving continuation against the reference.
const POST_SWITCH_SCANS: u64 = 30;

/// A `dcs-*` binary sibling of the controller binary under test in the
/// workspace target dir; workspace builds produce them.
fn workspace_binary(name: &str) -> PathBuf {
    let binary = Path::new(CONTROLLER)
        .parent()
        .unwrap()
        .join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
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

/// A plant-server process serving the showcase plant — model plus
/// dynamics document — on an ephemeral port.
fn spawn_plant() -> Spawned {
    spawn(
        &workspace_binary("dcs-plant-server"),
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

/// One `dcs-plant-ctl` invocation against `plant` — the plant tooling
/// the scenario perturbs the world through. Returns the server's
/// response payload; a nonzero exit or an unparseable answer fails the
/// run.
fn plant_ctl(plant: SocketAddr, args: &[&str]) -> serde_json::Value {
    let output = Process::new(workspace_binary("dcs-plant-ctl"))
        .arg(plant.to_string())
        .args(args)
        .output()
        .expect("cannot spawn dcs-plant-ctl");
    assert!(
        output.status.success(),
        "dcs-plant-ctl {args:?} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

/// The `{"result":"done"}` acknowledgement the plant server answers a
/// mutation request with.
fn plant_ctl_done(plant: SocketAddr, args: &[&str]) {
    assert_eq!(
        plant_ctl(plant, args),
        serde_json::json!({ "result": "done" }),
        "dcs-plant-ctl {args:?} was not acknowledged"
    );
}

/// Writes the controller-side model for a plant server at `plant`: the
/// shared showcase model with every declared channel merged onto one
/// `sim-tcp` device carrying the plant's address — the remote-sim path
/// through the assembly driver registry. One remote backend steps the
/// plant once per scan, keeping the dynamics document's dt pacing.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    let mut channels = serde_json::Map::new();
    for device in document["devices"].as_array().unwrap() {
        for (channel, declaration) in device["channels"].as_object().unwrap() {
            channels.insert(channel.clone(), declaration.clone());
        }
    }
    document["devices"] = serde_json::json!([{
        "id": 1,
        "kind": "sim-tcp",
        "parameters": { "address": plant.to_string() },
        "channels": channels,
    }]);
    for point in document["io_points"].as_array_mut().unwrap() {
        if let Some(channel) = point.get_mut("channel") {
            channel["device"] = serde_json::json!(1);
        }
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// The sample `snapshot`'s image reports for `point`.
fn image_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    showcase::sample(snapshot, point).expect("the showcase maps every named point")
}

fn float(snapshot: &TelemetrySnapshot, point: PointId) -> f64 {
    let Value::Float(value) = image_sample(snapshot, point).value else {
        panic!("point {} is a Float point", point.0)
    };
    value
}

fn bool_point(snapshot: &TelemetrySnapshot, point: PointId) -> bool {
    let Value::Bool(value) = image_sample(snapshot, point).value else {
        panic!("point {} is a Bool point", point.0)
    };
    value
}

fn int_point(snapshot: &TelemetrySnapshot, point: PointId) -> i64 {
    let Value::Int(value) = image_sample(snapshot, point).value else {
        panic!("point {} is an Int point", point.0)
    };
    value
}

/// No component stepped in error anywhere in the snapshot.
fn assert_clean(snapshot: &TelemetrySnapshot) {
    assert!(
        snapshot
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "every component stepped clean: {:?}",
        snapshot.components
    );
}

/// Whether `journal` carries the documented fault transition: `point`
/// observed `Good` turning `Bad(CommunicationFault)`.
fn journals_fault(journal: &[JournalEntry], point: PointId) -> bool {
    journal.iter().any(|entry| {
        matches!(
            &entry.event,
            JournalEvent::QualityChanged {
                point: p,
                from: Some(Quality::Good),
                to: Quality::Bad(QualityReason::CommunicationFault),
            } if *p == point
        )
    })
}

/// The role sequence `journal` records, in order.
fn journaled_roles(journal: &[JournalEntry]) -> Vec<(Role, Role)> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::RoleChanged { from, to } => Some((*from, *to)),
            _ => None,
        })
        .collect()
}

/// What one tick left on the pair's shared plant — the field the
/// scenario's assertions and the run-to-run comparison observe.
#[derive(Debug, Clone, PartialEq)]
struct FieldRow {
    /// The run tick the observation followed.
    tick: Tick,
    /// Raw level input `lt101_raw`.
    level_raw: Sample,
    /// Raw valve command output `lv101_raw`.
    valve_raw: Sample,
    /// Pump run feedback `p101_run` — `Bad` quality while the fault runs.
    run_feedback: Sample,
    /// Pump starter command `p101_cmd`.
    pump_command: Sample,
    /// Alarm horn `la101_horn`.
    horn: Sample,
}

/// The field state after the field owner's scan of `tick`.
fn observe(field: &RemoteDriver, tick: Tick) -> FieldRow {
    use dcs_core::IoDriver;
    FieldRow {
        tick,
        level_raw: field.read(points::LEVEL_RAW).unwrap(),
        valve_raw: field.read(points::VALVE_RAW).unwrap(),
        run_feedback: field.read(points::PUMP_RUN).unwrap(),
        pump_command: field.read(points::PUMP_COMMAND).unwrap(),
        horn: field.read(points::HORN).unwrap(),
    }
}

/// One scripted tick of the demonstration: the quiesced peer scans first
/// — pulling the field owner's checkpoint while it tracks — then the
/// owner and the uninterrupted reference scan and step their plants. The
/// three snapshots must be equal and the field must carry the owner's
/// write. Returns the field owner's snapshot.
fn tick(
    quiesced: &MonitorClient,
    owner: &MonitorClient,
    reference: &MonitorClient,
    field: &RemoteDriver,
    trace: &mut Vec<FieldRow>,
) -> TelemetrySnapshot {
    let tracking = quiesced.advance(1).unwrap();
    let owner_image = owner.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    let n = owner_image.tick;
    assert_eq!(
        tracking, owner_image,
        "quiesced peer diverged at tick {n:?}"
    );
    assert_eq!(
        owner_image, alone,
        "pair diverged from reference at tick {n:?}"
    );
    let row = observe(field, n);
    assert_eq!(
        row.valve_raw.value,
        image_sample(&owner_image, points::VALVE_RAW).value,
        "the field did not carry the field owner's write at tick {n:?}"
    );
    trace.push(row);
    owner_image
}

/// Runs `scans` scripted ticks, returning the field owner's snapshot at
/// the phase's end.
fn phase(
    quiesced: &MonitorClient,
    owner: &MonitorClient,
    reference: &MonitorClient,
    field: &RemoteDriver,
    trace: &mut Vec<FieldRow>,
    scans: u64,
) -> TelemetrySnapshot {
    let mut image = None;
    for _ in 0..scans {
        image = Some(tick(quiesced, owner, reference, field, trace));
    }
    image.unwrap()
}

/// The pair view's role poll: every peer must be reporting; returns the
/// reports in configured-peer order.
fn poll(pair: &mut PairClient) -> Vec<RoleReport> {
    pair.poll_roles();
    pair.peers()
        .iter()
        .map(|peer| match peer.status() {
            PeerStatus::Reporting(report) => report.clone(),
            status => panic!("peer {} is not reporting: {status:?}", peer.addr()),
        })
        .collect()
}

/// What one scripted run produces — the outcome a repeated run must
/// reproduce identically: the per-tick field trace, every stage's
/// snapshot as the pair view served it, and both field-owning peers'
/// journals.
#[derive(Debug, PartialEq)]
struct Outcome {
    /// The pair plant's observed field state after each owner tick.
    field: Vec<FieldRow>,
    /// Stage snapshots served through the pair view, in stage order:
    /// settled, moved, faulted, post-switch, recovered, reset.
    stages: Vec<TelemetrySnapshot>,
    /// The first field owner's journal, captured after the fault phase.
    owner_journal: Vec<JournalEntry>,
    /// The promoted peer's journal at the run's end.
    promoted_journal: Vec<JournalEntry>,
}

/// One scripted run of the full-stack scenario documented in the module
/// header. Returns the run's [`Outcome`].
fn run_full_stack(tag: &str) -> Outcome {
    let dir = std::env::temp_dir().join(format!("dcs-full-stack-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants: the pair's and the reference run's — identical
    // model and dynamics, identical request sequences, identical runs.
    let pair_plant = spawn_plant();
    let reference_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let reference_model = controller_model(&dir, "reference.json", reference_plant.addr);

    // The plant tooling lists the served field surface — the seven
    // channel-bound points of the showcase model.
    let listed = plant_ctl(pair_plant.addr, &["list"]);
    let served: BTreeSet<u64> = listed["points"]
        .as_array()
        .unwrap()
        .iter()
        .map(|point| point["point"].as_u64().unwrap())
        .collect();
    assert_eq!(
        served,
        [10, 11, 20, 30, 31, 40, 41].into_iter().collect(),
        "the plant serves the showcase's channel-bound points"
    );

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

    // An observer client on the pair's plant — the field's own view of
    // what each tick carried.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();

    // The pair view over both monitor surfaces.
    let mut pair = PairClient::new([active_process.addr, standby_process.addr]);

    // --- Stage: roles and the pair view before the first tick ---
    assert_eq!(active.role().unwrap().role, Role::Active);
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(reference.role().unwrap().role, Role::Active);

    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Active);
    assert_eq!(reports[1].role, Role::Standby);
    assert_eq!(pair.source(), Some(active_process.addr));

    // The pair view serves the model's monitoring surface: the setpoint
    // point resolves to its signal with its declared command surface.
    let signals = pair.signals().unwrap();
    let setpoint = signals.get(points::LEVEL_SETPOINT).unwrap();
    assert_eq!(setpoint.name, "level-setpoint");
    assert!(setpoint.writable);

    // Promoting the unconverged standby is the named refusal, not a
    // take-over.
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NotConverged {
            sync: StandbySync::Unsynchronized
        }
    );
    // And a standby refuses commands at the role boundary — the pair
    // view's never-send-to-standby rule rests on it.
    let receipt = standby
        .command(&Command::WriteValue {
            point: points::LEVEL_SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(MOVED_SETPOINT),
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

    let mut trace = Vec::new();
    let mut stages = Vec::new();

    // --- Stage: settle at the declared setpoint ---
    let settled = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        SETTLE_SCANS,
    );
    // The pair view reads the field owner's image.
    let view = pair.snapshot().unwrap();
    assert_eq!(view, settled);
    stages.push(view.clone());
    assert!(
        (float(&view, points::LEVEL_PERCENT) - INITIAL_SETPOINT).abs() < 0.5,
        "the loop settled at the declared setpoint"
    );
    assert!(
        (float(&view, points::VALVE_RAW) - 13.6).abs() < 0.1,
        "the valve sits at the settled command"
    );
    assert!(!bool_point(&view, points::INTERLOCK_TRIPPED));
    assert!(!bool_point(&view, points::LEVEL_ALARM));
    assert!(!bool_point(&view, points::PUMP_FAULT));
    assert_eq!(int_point(&view, points::TRIP_COUNT), 1);
    assert!(!bool_point(&view, points::TRIPS_DONE));
    assert_clean(&view);
    // The standby converged on the active's checkpoints: it reports
    // tracking, aligned one pull behind the owner's tick.
    let report = standby.role().unwrap();
    assert_eq!(
        report.sync,
        Some(StandbySync::Tracking {
            aligned: Tick(SETTLE_SCANS - 1)
        }),
        "the standby is tracking the field owner: {report:?}"
    );

    // --- Stage: the operator's setpoint command through the pair view ---
    let apply_tick = Tick(SETTLE_SCANS + 1);
    let receipt = pair
        .command(&Command::WriteValue {
            point: points::LEVEL_SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(MOVED_SETPOINT),
        })
        .unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted { apply_tick },
        "the pair view routed the command to the settled active"
    );
    assert_eq!(
        reference
            .command(&Command::WriteValue {
                point: points::LEVEL_SETPOINT,
                kind: ValueKind::Float,
                value: Value::Float(MOVED_SETPOINT),
            })
            .unwrap()
            .outcome,
        CommandOutcome::Accepted { apply_tick }
    );

    // The apply tick: the owner and the reference scan; the tracking
    // peer sits it out — a queued command is not checkpoint-carried —
    // and picks the applied setpoint up in its next pull.
    let owner_image = active.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    assert_eq!(owner_image, alone);
    assert_eq!(owner_image.tick, apply_tick);
    assert_eq!(
        image_sample(&owner_image, points::LEVEL_SETPOINT).value,
        Value::Float(MOVED_SETPOINT)
    );
    let row = observe(&field, apply_tick);
    assert_eq!(
        row.valve_raw.value,
        image_sample(&owner_image, points::VALVE_RAW).value
    );
    trace.push(row);

    // --- Stage: regulation at the moved setpoint ---
    let moved = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        MOVED_SCANS - 1,
    );
    let view = pair.snapshot().unwrap();
    assert_eq!(view, moved);
    stages.push(view.clone());
    assert_eq!(view.tick, Tick(SETTLE_SCANS + MOVED_SCANS));
    assert_eq!(float(&view, points::LEVEL_SETPOINT), MOVED_SETPOINT);
    assert!(
        (float(&view, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the loop re-settled at the commanded setpoint"
    );
    assert!(!bool_point(&view, points::INTERLOCK_TRIPPED));
    assert!(!bool_point(&view, points::LEVEL_ALARM));
    assert_clean(&view);
    // The command's settlement is journaled on the field owner.
    let journal = active.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt }
            if receipt.outcome == CommandOutcome::Applied { tick: apply_tick }
        )),
        "the applied setpoint command is journaled"
    );

    // --- Stage: the injected fault, through the plant tooling ---
    plant_ctl_done(pair_plant.addr, &["fault", "30", "bad:communication_fault"]);
    plant_ctl_done(
        reference_plant.addr,
        &["fault", "30", "bad:communication_fault"],
    );
    let faulted = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        FAULT_SCANS,
    );
    let view = pair.snapshot().unwrap();
    assert_eq!(view, faulted);
    stages.push(view.clone());
    // The documented fault behavior, through the monitor snapshot: the
    // feedback reads Bad, the interlock trips and drives the valve safe,
    // the level drains through the low alarm — horn asserted, motor
    // fault flagged, the trip counter latching its preset.
    assert_eq!(
        image_sample(&view, points::PUMP_RUN).quality,
        Quality::Bad(QualityReason::CommunicationFault)
    );
    assert_eq!(
        image_sample(&view, points::PUMP_RUNNING).quality,
        Quality::Bad(QualityReason::CommunicationFault),
        "the conditioned run status goes bad with the feedback"
    );
    assert!(bool_point(&view, points::INTERLOCK_TRIPPED));
    assert_eq!(float(&view, points::VALVE_RAW), 4.0);
    assert!(bool_point(&view, points::LEVEL_ALARM));
    assert!(bool_point(&view, points::HORN));
    assert!(bool_point(&view, points::PUMP_FAULT));
    assert_eq!(int_point(&view, points::TRIP_COUNT), 2);
    assert!(bool_point(&view, points::TRIPS_DONE));
    assert!(
        float(&view, points::LEVEL_PERCENT) < 10.0,
        "the level drained through the low alarm limit"
    );
    assert_clean(&view);
    // And in the journal: the quality transition is a recorded event on
    // the field owner — and on the tracking peer, which observed the
    // same transition behind its gate.
    let owner_journal = active.journal(0).unwrap();
    assert!(
        journals_fault(&owner_journal, points::PUMP_RUN),
        "the injected fault is journaled on the field owner"
    );
    assert!(
        journals_fault(&standby.journal(0).unwrap(), points::PUMP_RUN),
        "the tracking peer journaled the same transition"
    );

    // --- Stage: the switchover mid-fault ---
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    // The transition states report on both peers' surfaces.
    assert_eq!(active.role().unwrap().role, Role::Demoting);
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Promoting);
    assert!(matches!(report.sync, Some(StandbySync::Tracking { .. })));
    // The pair view follows the promoting peer while the switch settles.
    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Demoting);
    assert_eq!(reports[1].role, Role::Promoting);
    assert_eq!(pair.source(), Some(standby_process.addr));

    // The promoted peer writes the field from the first post-switch
    // tick; the demoted peer keeps scanning gate-closed and still
    // agrees, and the reference paces them both.
    let continued = phase(
        &active,
        &standby,
        &reference,
        &field,
        &mut trace,
        POST_SWITCH_SCANS,
    );
    // Roles settled on the first post-switch scan: the promoted peer
    // reports active, the demoted peer standby — unsynchronized, its
    // track ended with its field ownership.
    assert_eq!(
        standby.role().unwrap(),
        RoleReport {
            role: Role::Active,
            tick: continued.tick,
            sync: None,
        }
    );
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Standby);
    assert_eq!(reports[1].role, Role::Active);
    assert_eq!(pair.source(), Some(standby_process.addr));
    let view = pair.snapshot().unwrap();
    assert_eq!(view, continued);
    stages.push(view.clone());
    assert!(bool_point(&view, points::INTERLOCK_TRIPPED));
    assert_eq!(float(&view, points::VALVE_RAW), 4.0);
    // The journals carry both peers' sides of the switch.
    assert_eq!(
        journaled_roles(&active.journal(0).unwrap()),
        vec![
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby)
        ]
    );
    assert_eq!(
        journaled_roles(&standby.journal(0).unwrap()),
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active)
        ]
    );

    // --- Stage: recovery once the tooling clears the fault ---
    plant_ctl_done(pair_plant.addr, &["clear-fault", "30"]);
    plant_ctl_done(reference_plant.addr, &["clear-fault", "30"]);
    let recovered = phase(
        &active,
        &standby,
        &reference,
        &field,
        &mut trace,
        RECOVERY_SCANS - 2,
    );
    let view = pair.snapshot().unwrap();
    assert_eq!(view, recovered);
    stages.push(view.clone());
    assert_eq!(image_sample(&view, points::PUMP_RUN).quality, Quality::Good);
    assert!(!bool_point(&view, points::INTERLOCK_TRIPPED));
    assert!(!bool_point(&view, points::LEVEL_ALARM));
    assert!(!bool_point(&view, points::HORN));
    assert!(!bool_point(&view, points::PUMP_FAULT));
    assert!(
        (float(&view, points::LEVEL_PERCENT) - MOVED_SETPOINT).abs() < 0.5,
        "the level recovered to the moved setpoint"
    );
    assert_eq!(int_point(&view, points::TRIP_COUNT), 2);
    assert!(bool_point(&view, points::TRIPS_DONE));
    assert_clean(&view);
    // The pair view's history follows the promoted peer's series across
    // the source change: the retained tail is a continuous run of the
    // shared tick domain ending at the recovered tick.
    let history = pair
        .history(&[points::LEVEL_PERCENT], recovered.tick.0 - 10)
        .unwrap();
    let series = &history[0].samples;
    assert!(!series.is_empty());
    assert_eq!(series.last().unwrap().sample.tick, recovered.tick);
    assert!(
        series
            .windows(2)
            .all(|window| window[1].sample.tick.0 == window[0].sample.tick.0 + 1),
        "the merged series continues the run's tick sequence"
    );

    // --- Stage: the post-switch operator reset through the pair view ---
    // The command lands on the new active: the counter's reset clears
    // the latched count, and releasing it rearms.
    let reset_tick = Tick(recovered.tick.0 + 1);
    let receipt = pair
        .command(&Command::WriteValue {
            point: points::TRIP_COUNT_RESET,
            kind: ValueKind::Bool,
            value: Value::Bool(true),
        })
        .unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: reset_tick
        }
    );
    let image = standby.advance(1).unwrap();
    assert_eq!(image.tick, reset_tick);
    assert_eq!(int_point(&image, points::TRIP_COUNT), 0);
    assert!(!bool_point(&image, points::TRIPS_DONE));
    trace.push(observe(&field, reset_tick));
    let receipt = pair
        .command(&Command::WriteValue {
            point: points::TRIP_COUNT_RESET,
            kind: ValueKind::Bool,
            value: Value::Bool(false),
        })
        .unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
    let image = standby.advance(1).unwrap();
    trace.push(observe(&field, image.tick));
    let view = pair.snapshot().unwrap();
    assert_eq!(view, image);
    stages.push(view);

    // Final role reporting: the pair view's poll sees the settled pair.
    let reports = poll(&mut pair);
    assert_eq!(reports[0].role, Role::Standby);
    assert_eq!(reports[1].role, Role::Active);

    Outcome {
        field: trace,
        stages,
        owner_journal,
        promoted_journal: standby.journal(0).unwrap(),
    }
}

/// The full scripted scenario runs twice; every asserted monitor payload
/// and the per-tick field trace must be identical across runs.
#[test]
fn full_stack_demonstration_repeats_identically() {
    let first = run_full_stack("first");
    let second = run_full_stack("second");
    assert_eq!(first, second, "repeated runs produce identical outcomes");
}

/// The dynamics document the plant servers load declares the same
/// first-order lag `dcs_demo::showcase::driver` adds in-process — the
/// shared plant and the in-process run simulate identical physics.
#[test]
fn showcase_dynamics_document_declares_the_tank_lag() {
    let document: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(PLANT_DYNAMICS).unwrap()).unwrap();
    assert_eq!(
        document,
        serde_json::json!([{
            "first_order_lag": {
                "input": points::VALVE_RAW.0,
                "output": points::LEVEL_RAW.0,
                "time_constant": 2.0,
                "initial": 13.6,
            }
        }])
    );
}
