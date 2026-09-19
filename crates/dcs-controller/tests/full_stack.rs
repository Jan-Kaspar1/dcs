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
//! `showcase_dynamics.json`: the tank's second-order lag from the
//! valve's raw command to the level's raw input, the first-order lags
//! on the two redundant transmitter legs and the flow channel — the
//! same elements `dcs_demo::showcase::driver` adds in-process. One
//! plant is the pair's shared field; the other paces an uninterrupted
//! single-controller reference run whose outputs the pair's must equal.
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
//! 5. **The batch program's declared surface.** `B-101` — the model's
//!    `sequencer` — is the checked-in composition's kind carrying the
//!    declared contract: the served `/schema` lists its `advance` and
//!    `reset` commands and its `step_completed` event under the
//!    `Declared` provenance beside the adapted entries. The operator
//!    starts the table through the writable `run` point — the declared
//!    commands do not alias the held inputs, so no bespoke point is
//!    added — the first step's `step_completed` emission journals at
//!    the producing tick on every controller and in both peers' durable
//!    journal files, the `advance`/`reset` invocations settle through
//!    the receipted command path, and the alarm report computes over
//!    the emitted-event record.
//! 6. **Injected fault.** `dcs-plant-ctl fault 30 bad:communication_fault`
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
//! 7. **Promotion mid-fault.** `POST /demote` on the active, then
//!    `POST /promote` on the converged standby: the field writer moves
//!    at the scan boundary, `GET /role` reports the transition states on
//!    both peers, the pair view follows the promoting peer, and the
//!    promoted peer's outputs keep matching the uninterrupted reference
//!    — bumpless continuation through the faulted run, the demoted peer
//!    still agreeing scan for scan behind its closed gate.
//! 8. **Recovery.** The tooling clears the fault; the interlock
//!    auto-resets, the level recovers to the moved setpoint, and the
//!    alarm and motor fault clear.
//! 9. **Post-switch command.** The trip-counter reset lands on the new
//!    active through the pair view — assert, then release to rearm —
//!    clearing the latched count.
//!
//! The whole run executes twice; the per-tick field trace, the stage
//! snapshots, and the journals must be identical — the determinism the
//! acceptance criteria require.

use dcs_core::{
    AdaptedCommand, AdaptedEvent, Command, CommandAvailability, CommandError, CommandOutcome,
    EmittedEvent, EventEmission, EventRetention, EventValue, JournalEntry, JournalEvent, PointId,
    Quality, QualityReason, Role, RoleReport, Sample, StandbySync, SwitchError, TelemetrySnapshot,
    Tick, Value, ValueKind,
};
use dcs_demo::showcase::{
    self, BATCH_STEP1_TICKS, FAULT_SCANS, INITIAL_SETPOINT, MOVED_SCANS, MOVED_SETPOINT,
    RECOVERY_SCANS, SETTLE_SCANS, points,
};
use dcs_monitor::{MonitorClient, PairClient, PeerStatus, read_journal_file};
use dcs_sim_net::RemoteDriver;
use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
use std::path::Path;
use std::process::Command as Process;

mod support;

use support::{SimTcp, controller_model, spawn_controller, spawn_plant, workspace_binary};

/// The showcase plant model the plant servers load — the #69 fixture.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/showcase.json"
);
/// The showcase's plant-side dynamics document: the tank's second-order
/// lag from `lv101_raw` (20) to `lt101a_raw` (10), the first-order
/// transmitter lags to `lt101b_raw`/`lt101c_raw` (12/13), and the flow
/// lag from `lv101_fb` (11) to `ft101_raw` (14) — the same elements
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
/// The batch program's instance name — the showcase's `sequencer`
/// component, addressed as `"<kind>:<id>"` by the served schema, the
/// `invoke` command, and the emitted event.
const BATCH_COMPONENT: &str = "sequencer:20";
/// Scans the run lingers after the batch commands settle — the tracking
/// peer re-converging on the invocations' checkpoints.
const BATCH_CLOSE_SCANS: u64 = 3;

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

/// A `Command::Invoke` against the batch program — the declared-command
/// submission the bounded, receipted command path carries.
fn invoke(command: &str, arguments: &[(&str, Value)]) -> Command {
    Command::Invoke {
        component: BATCH_COMPONENT.to_string(),
        command: command.to_string(),
        arguments: BTreeMap::from_iter(
            arguments
                .iter()
                .map(|(name, value)| (name.to_string(), *value)),
        ),
    }
}

/// The `step_completed` event the batch program's first step emits on
/// its completing scan — the kind-declared emission the journal and the
/// served recent-events view carry.
fn step_completed() -> JournalEvent {
    JournalEvent::EventEmitted {
        event: EmittedEvent {
            event: "step_completed".to_string(),
            component: BATCH_COMPONENT.to_string(),
            fields: [("step".to_string(), EventValue::Value(Value::Int(1)))]
                .into_iter()
                .collect(),
        },
    }
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
    /// settled, moved, batch-closed, faulted, post-switch, recovered,
    /// reset.
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
    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let reference_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::Merged,
    )
    .0;
    let reference_model = controller_model(
        &dir,
        "reference.json",
        MODEL_SOURCE,
        reference_plant.addr,
        SimTcp::Merged,
    )
    .0;

    // The plant tooling lists the served field surface — the eleven
    // channel-bound points of the extended showcase model: the three
    // redundant level transmitters, the valve position feedback, the
    // flow input, the valve command, the pump run feedback and the
    // high-level switch, and the pump, horn, and beacon outputs.
    let listed = plant_ctl(pair_plant.addr, &["list"]);
    let served: BTreeSet<u64> = listed["points"]
        .as_array()
        .unwrap()
        .iter()
        .map(|point| point["point"].as_u64().unwrap())
        .collect();
    assert_eq!(
        served,
        [10, 11, 12, 13, 14, 20, 30, 31, 40, 41, 42]
            .into_iter()
            .collect(),
        "the plant serves the showcase's channel-bound points"
    );

    // The pair: the active first — the standby's --standby names its
    // monitoring address — then the standby, then the reference run on
    // its own plant. Each controller appends its monitor's journal to a
    // durable file — the batch stage asserts the emitted-event record
    // lands there.
    let active_journal = dir.join("active.journal.jsonl");
    let standby_journal = dir.join("standby.journal.jsonl");
    let reference_journal = dir.join("reference.journal.jsonl");
    for path in [&active_journal, &standby_journal, &reference_journal] {
        let _ = std::fs::remove_file(path);
    }
    let active_process = spawn_controller(
        &pair_model,
        &[
            "--journal-file".to_string(),
            active_journal.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let standby_process = spawn_controller(
        &pair_model,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--journal-file".to_string(),
            standby_journal.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let reference_process = spawn_controller(
        &reference_model,
        &[
            "--journal-file".to_string(),
            reference_journal.to_str().unwrap().to_string(),
        ],
        DT,
    );
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

    // --- Stage: the batch program's declared command and event surface ---
    // `B-101` — the model's `sequencer` — is the checked-in
    // composition's kind carrying the declared contract: the served
    // block-interface schema lists its `advance` and `reset` commands
    // and its `step_completed` event under the declared provenance
    // beside the adapted entries — no bespoke writable point stands in
    // for either.
    let schema = active.schema().unwrap();
    let batch = &schema
        .interfaces
        .iter()
        .find(|entry| entry.name == BATCH_COMPONENT)
        .expect("the served schema covers the batch program")
        .interface;
    for (command, availability) in [
        ("advance", CommandAvailability::KindDeclared),
        ("reset", CommandAvailability::Always),
    ] {
        let spec = batch
            .commands
            .iter()
            .find(|spec| spec.name == command)
            .unwrap_or_else(|| panic!("the schema declares {command}"));
        assert_eq!(spec.adapted, AdaptedCommand::Declared, "{command}");
        assert_eq!(spec.availability, availability, "{command}");
    }
    let declared = batch
        .events
        .iter()
        .find(|spec| spec.name == "step_completed")
        .expect("the schema declares step_completed");
    assert_eq!(declared.adapted, AdaptedEvent::Declared);
    assert_eq!(declared.emission, EventEmission::KindEmitted);
    assert_eq!(declared.retention, EventRetention::Journal);

    // The operator starts the table: `run` is the held level input the
    // model wires to the declared `batch-run` writable point — the
    // declared commands do not alias it, so the start stays the
    // ordinary receipted write. The batch program's output is not
    // selected — `batch-mode` stays low — so the table's walk leaves
    // the regulating loop untouched.
    let batch_run = |running: bool| Command::WriteValue {
        point: points::BATCH_RUN,
        kind: ValueKind::Bool,
        value: Value::Bool(running),
    };
    let run_tick = Tick(moved.tick.0 + 1);
    assert_eq!(
        pair.command(&batch_run(true)).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: run_tick
        },
        "the pair view routed the run request to the settled active"
    );
    assert_eq!(
        reference.command(&batch_run(true)).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: run_tick
        }
    );
    // The apply tick: the owner and the reference scan; the tracking
    // peer sits it out — a queued command is not checkpoint-carried —
    // and picks the held `run` up in its next pull.
    let image = active.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    assert_eq!(image, alone);
    assert_eq!(image.tick, run_tick);
    trace.push(observe(&field, run_tick));

    // The table's first step runs its declared ticks out: the
    // completing scan emits `step_completed` — the kind-emitted event —
    // and every controller's journal records it at the producing tick:
    // the field owner's, the tracking peer's (its own scan emits it
    // from the checkpointed state), and the reference's.
    let event_tick = Tick(run_tick.0 + BATCH_STEP1_TICKS - 1);
    let stepped = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        BATCH_STEP1_TICKS - 1,
    );
    assert_eq!(stepped.tick, event_tick);
    // The completing scan still reports the completed step — the table
    // shows step 2 from the next scan on.
    assert_eq!(int_point(&stepped, points::BATCH_STEP), 1);
    for (peer, journal) in [
        ("the field owner", active.journal(0).unwrap()),
        ("the tracking peer", standby.journal(0).unwrap()),
        ("the reference", reference.journal(0).unwrap()),
    ] {
        let emitted: Vec<&JournalEntry> = journal
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::EventEmitted { .. }))
            .collect();
        assert_eq!(
            emitted.len(),
            1,
            "{peer} journaled the batch program's one emission"
        );
        assert_eq!(emitted[0].tick, event_tick, "{peer}");
        assert_eq!(emitted[0].event, step_completed(), "{peer}");
    }
    // The served recent-events view attributes the emission to the
    // instance, and both declared commands report submittable.
    let resources = active.resources().unwrap();
    let batch_resources = resources
        .components
        .iter()
        .find(|component| component.name == BATCH_COMPONENT)
        .expect("the resource view covers the batch program");
    assert!(
        batch_resources
            .events
            .iter()
            .any(|entry| entry.tick == event_tick && entry.event == step_completed()),
        "the served events tail carries the emission at the producing tick"
    );
    for command in ["advance", "reset"] {
        let state = batch_resources
            .commands
            .iter()
            .find(|state| state.name == command)
            .unwrap_or_else(|| panic!("the resource view serves {command}"));
        assert!(state.available, "{command} is submittable: {state:?}");
    }

    // Dropping `run` parks the table mid-step — the held input's
    // release, again the ordinary writable-point write.
    let hold_tick = Tick(event_tick.0 + 1);
    assert_eq!(
        pair.command(&batch_run(false)).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: hold_tick
        }
    );
    assert_eq!(
        reference.command(&batch_run(false)).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: hold_tick
        }
    );
    let image = active.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    assert_eq!(image, alone);
    assert_eq!(image.tick, hold_tick);
    trace.push(observe(&field, hold_tick));

    // The declared `advance` invocation — typed `count` argument and
    // all — applies at the next boundary: the parked table steps to 4.
    let advance = invoke("advance", &[("count", Value::Int(2))]);
    let advance_tick = Tick(hold_tick.0 + 1);
    assert_eq!(
        pair.command(&advance).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: advance_tick
        }
    );
    assert_eq!(
        reference.command(&advance).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: advance_tick
        }
    );
    let image = active.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    assert_eq!(image, alone);
    assert_eq!(image.tick, advance_tick);
    assert_eq!(
        int_point(&image, points::BATCH_STEP),
        4,
        "the invoked advance moved the parked table"
    );
    trace.push(observe(&field, advance_tick));

    // `reset` — `Always`-available — returns the table to its first
    // step: the one-shot action the held `reset` input is not.
    let restart = invoke("reset", &[]);
    let restart_tick = Tick(advance_tick.0 + 1);
    assert_eq!(
        pair.command(&restart).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: restart_tick
        }
    );
    assert_eq!(
        reference.command(&restart).unwrap().outcome,
        CommandOutcome::Accepted {
            apply_tick: restart_tick
        }
    );
    let image = active.advance(1).unwrap();
    let alone = reference.advance(1).unwrap();
    assert_eq!(image, alone);
    assert_eq!(image.tick, restart_tick);
    assert_eq!(int_point(&image, points::BATCH_STEP), 1);
    trace.push(observe(&field, restart_tick));

    // Both invocations settled applied at their apply ticks — the
    // journaled receipts the durable record carries.
    for (command, tick) in [(&advance, advance_tick), (&restart, restart_tick)] {
        assert!(
            active.journal(0).unwrap().iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.command == *command
                        && receipt.outcome == CommandOutcome::Applied { tick }
            )),
            "the applied {command:?} receipt is journaled at {tick:?}"
        );
    }

    // The tracking peer scans the stage out behind its gate — every
    // controller agrees again — and the durable journal files the
    // pair's monitors appended hold the same emitted-event record.
    let closed = phase(
        &standby,
        &active,
        &reference,
        &field,
        &mut trace,
        BATCH_CLOSE_SCANS,
    );
    let view = pair.snapshot().unwrap();
    assert_eq!(view, closed);
    assert_clean(&view);
    stages.push(view);
    for path in [&active_journal, &standby_journal] {
        let data = read_journal_file(path).unwrap();
        assert!(
            data.entries
                .iter()
                .any(|entry| entry.tick == event_tick && entry.event == step_completed()),
            "the durable journal {} carries the emission",
            path.display()
        );
    }

    // The alarm report computes over the emitted-event journal — the
    // served form and the durable file alike — without failure.
    for args in [
        vec![active_process.addr.to_string()],
        vec![
            active_process.addr.to_string(),
            "--journal-file".to_string(),
            active_journal.to_str().unwrap().to_string(),
        ],
    ] {
        let output = Process::new(workspace_binary("dcs-alarm-report"))
            .args(&args)
            .output()
            .expect("cannot spawn dcs-alarm-report");
        assert!(
            output.status.success(),
            "dcs-alarm-report {args:?} failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let report: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert!(
            report["source"]["journal_entries"].as_u64().unwrap() > 0,
            "the report computed over the emitted-event journal: {report}"
        );
    }

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
    // reports active, the demoted peer standby — and already tracking
    // its successor again through the announced follow-peer source.
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
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must follow its successor and reconverge: {report:?}"
    );
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
/// elements `dcs_demo::showcase::driver` adds in-process — the shared
/// plant and the in-process run simulate identical physics: the
/// underdamped second-order tank, the two first-order transmitter lags,
/// and the flow lag.
#[test]
fn showcase_dynamics_document_declares_the_tank_lag() {
    let document: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(PLANT_DYNAMICS).unwrap()).unwrap();
    assert_eq!(
        document,
        serde_json::json!([
            {
                "second_order_lag": {
                    "input": points::VALVE_RAW.0,
                    "output": points::LEVEL_RAW.0,
                    "time_constant": 1.5,
                    "damping_ratio": 0.5,
                    "initial": 13.6,
                }
            },
            {
                "first_order_lag": {
                    "input": points::LEVEL_RAW.0,
                    "output": points::LEVEL_B_RAW.0,
                    "time_constant": 0.3,
                    "initial": 13.6,
                }
            },
            {
                "first_order_lag": {
                    "input": points::LEVEL_RAW.0,
                    "output": points::LEVEL_C_RAW.0,
                    "time_constant": 0.6,
                    "initial": 13.6,
                }
            },
            {
                "first_order_lag": {
                    "input": points::VALVE_FEEDBACK_RAW.0,
                    "output": points::FLOW_RAW.0,
                    "time_constant": 0.4,
                    "initial": 4.0,
                }
            },
        ])
    );
}
