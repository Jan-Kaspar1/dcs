//! Rolling a revised plant model into production through the redundant
//! pair — the rolling model-revision decision wired as a scripted,
//! tick-paced run over the shared simulated plant.
//!
//! A `dcs-plant-server` owns the tank-loop plant; the active controller
//! runs model v1 — the tank loop with the setpoint moved to an
//! image-carried internal point, plus a second held operator value the
//! revision drops — and the standby runs model v2, launched with
//! `--revised` so its fingerprint-different checkpoints cross the model
//! boundary through the documented carryover rule instead of degrading
//! on the mismatch. v2 drops the removed operator point, declares a new
//! one that must initialize fresh, and re-ids the PID component — its
//! checkpointed `"pid:2"` state names a component the revision does not
//! register — while keeping the field points, the control shape, and the
//! parameters identical so post-promotion outputs are deterministic.
//!
//! The scenario: the standby's first pull reinitializes it — `GET /role`
//! reports the named state carrying the carryover report of what
//! transferred, what initialized, and what was named dropped — the
//! documented `POST /demote`-then-`POST /promote` order moves the field
//! writer to the revised model at a scan boundary with the demoted peer
//! quiesced, and the scripted run repeats identically. A revision whose
//! changes break the carryover rule is rejected before promotion with a
//! named error and the old active keeps the field.

use dcs_core::{
    CarryoverReport, Command, DroppedElement, IoDriver, JournalEvent, PointId, Role, StandbySync,
    SwitchError, TelemetrySnapshot, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The shared plant's model — the dcs-plant tank loop.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
/// The plant-side physics: the raw level lags the valve with τ = 2 s.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
/// The model document the controller-side models revise from.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the pair runs before the standby's first crossing is inspected.
const N: u64 = 20;
/// Ticks of promoted-peer output the run records.
const M: u64 = 40;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// v1's second held operator value — the revision drops it.
const DROPPED_KNOB: PointId = PointId(31);
/// The revision's new held operator value — initialized, never carried.
const NEW_KNOB: PointId = PointId(32);
/// The operator setpoint value the run carries across the boundary.
const OPERATING_POINT: f64 = 33.5;

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

/// Writes `document` — a model JSON — under `dir` and loads it once so
/// the caller can compare fingerprints.
fn write_model(dir: &Path, name: &str, document: &serde_json::Value) -> (PathBuf, PlantModel) {
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(document).unwrap()).unwrap();
    let model = PlantModel::load(&serde_json::to_string(document).unwrap()).unwrap();
    (path, model)
}

/// The base controller-side document: the tank loop with its devices
/// re-pointed at `sim-tcp` carrying the plant's address, the setpoint
/// point 11 moved image-side — an internal writable `In` point, the
/// operator value the carryover rule is about — and a second held
/// operator value at point 31.
fn base_model(plant: SocketAddr) -> serde_json::Value {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        device["kind"] = "sim-tcp".into();
        device["parameters"] = serde_json::json!({ "address": plant.to_string() });
    }
    let points = document["io_points"].as_array_mut().unwrap();
    let setpoint = points
        .iter_mut()
        .find(|point| point["id"] == SETPOINT.0)
        .unwrap();
    setpoint.as_object_mut().unwrap().remove("channel");
    setpoint["initial"] = serde_json::json!({ "float": 25.0 });
    points.push(serde_json::json!({
        "id": DROPPED_KNOB.0,
        "direction": "in",
        "value_type": "float",
        "writable": true,
        "initial": { "float": 0.0 }
    }));
    document
}

/// Model v1, as written under `dir`: the base document unchanged.
fn v1_model(dir: &Path, name: &str, plant: SocketAddr) -> (PathBuf, PlantModel) {
    write_model(dir, name, &base_model(plant))
}

/// Applies the v2 revision to `document`: point 31 is gone, a new held
/// operator value arrives at point 32, and the PID component is re-idd
/// 2 → 3 — the checkpoint's `"pid:2"` state entry names a component the
/// revision does not register.
fn revise(document: &mut serde_json::Value) {
    let points = document["io_points"].as_array_mut().unwrap();
    points.retain(|point| point["id"] != DROPPED_KNOB.0);
    points.push(serde_json::json!({
        "id": NEW_KNOB.0,
        "direction": "in",
        "value_type": "float",
        "writable": true,
        "initial": { "float": 5.0 }
    }));
    let pid = document["components"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|component| component["kind"] == "pid")
        .unwrap();
    pid["id"] = 3.into();
    for connection in document["connections"].as_array_mut().unwrap() {
        for end in ["from", "to"] {
            if let Some(component) = connection[end]["port"]["component"].as_u64()
                && component == 2
            {
                connection[end]["port"]["component"] = 3.into();
            }
        }
    }
}

/// Model v2, as written under `dir`: the base document revised.
fn v2_model(dir: &Path, name: &str, plant: SocketAddr) -> (PathBuf, PlantModel) {
    let mut document = base_model(plant);
    revise(&mut document);
    write_model(dir, name, &document)
}

/// The breaking revision: v2 with the setpoint point retyped `Float` →
/// `Int` — a model that validates and assembles (the PID's `sp` port
/// binds a new `Float` point 14) but whose carried value cannot cross.
fn broken_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document = base_model(plant);
    revise(&mut document);
    let points = document["io_points"].as_array_mut().unwrap();
    let setpoint = points
        .iter_mut()
        .find(|point| point["id"] == SETPOINT.0)
        .unwrap();
    setpoint["value_type"] = "int".into();
    setpoint["initial"] = serde_json::json!({ "int": 50 });
    points.push(serde_json::json!({
        "id": 14,
        "direction": "in",
        "value_type": "float",
        "initial": { "float": 25.0 }
    }));
    for connection in document["connections"].as_array_mut().unwrap() {
        if connection["to"]["port"]["name"] == "sp" {
            connection["from"]["point"] = 14.into();
        }
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    // The broken revision is still a valid model — it must fail at the
    // carryover rule, not at load.
    PlantModel::load(&serde_json::to_string(&document).unwrap()).unwrap();
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

/// The report the peer's sync state carries, or a panic naming what it
/// actually reported.
fn reinit_report(standby: &MonitorClient) -> CarryoverReport {
    let report = standby.role().unwrap();
    match report.sync {
        Some(StandbySync::Reinitialized { report }) => *report,
        other => panic!("expected a reinitialized standby, found {other:?}"),
    }
}

/// One scripted run of the full revision roll: v1 active, v2
/// `--revised` standby, the documented demote-then-promote switchover,
/// and M ticks of the promoted peer's field writes. Returns the run's
/// auditable record — the carryover report, the role transitions, the
/// crossing's journal entries, and the field's per-tick trace — as JSON
/// two runs must reproduce exactly.
fn run_roll(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-revision-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant();
    let (v1_path, v1) = v1_model(&dir, "v1.json", plant.addr);
    let (v2_path, v2) = v2_model(&dir, "v2.json", plant.addr);
    assert_ne!(
        v1.fingerprint(),
        v2.fingerprint(),
        "the revision must fingerprint differently by design"
    );

    let active_process = spawn_controller(&v1_path, &[]);
    let standby_process = spawn_controller(
        &v2_path,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--revised".to_string(),
        ],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // The run's operator inputs land by command — point 11 is the
    // controllers' internal held setpoint — then N ticks of pair
    // operation carry the crossings every pull.
    for (point, value) in [
        (SETPOINT, Value::Float(OPERATING_POINT)),
        (DROPPED_KNOB, Value::Float(7.0)),
    ] {
        let receipt = active
            .command(&Command::WriteValue {
                point,
                kind: ValueKind::Float,
                value,
            })
            .unwrap();
        assert!(
            matches!(
                receipt.outcome,
                dcs_core::CommandOutcome::Applied { .. }
                    | dcs_core::CommandOutcome::Accepted { .. }
            ),
            "{receipt:?}"
        );
    }
    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }

    // The named state and its audit record: the checkpoint crossed the
    // model boundary — fingerprints name both sides — the held operator
    // value carried, the output image carried, the revision's new point
    // initialized, and every element without a continuation is named.
    let report = reinit_report(&standby);
    assert_eq!(report.from, Some(v1.fingerprint()));
    assert_eq!(report.to, Some(v2.fingerprint()));
    assert_eq!(
        report.carried,
        vec![dcs_core::CarriedPoint {
            point: SETPOINT,
            value: Value::Float(OPERATING_POINT),
        }]
    );
    assert_eq!(
        report.carried_outputs.len(),
        1,
        "the valve image sample carries: {report:?}"
    );
    assert_eq!(report.carried_outputs[0].point, VALVE);
    // Everything without a continuation across the boundary is named:
    // the removed operator point, the `analog-input:1.out → pid.pv`
    // wire's synthesized link points — assembly allocates their ids
    // above the declared set, so v1's 32/33 are not v2's 33/34 and an
    // allocated id is not a declared identity the carryover rule can
    // match — and the re-idd PID's checkpointed state.
    assert_eq!(
        report.dropped,
        vec![
            DroppedElement::InternalPoint {
                point: DROPPED_KNOB
            },
            DroppedElement::InternalPoint { point: PointId(33) },
            DroppedElement::OutputPoint { point: PointId(32) },
            DroppedElement::Component {
                name: "pid:2".to_string()
            },
        ]
    );
    assert_eq!(
        report.reinitialized,
        vec!["analog-input:1".to_string(), "pid:3".to_string()]
    );
    assert_eq!(report.initialized, vec![NEW_KNOB]);
    // The carried value stands in the revised run's image and the new
    // point stands at its declared initial.
    let snapshot = standby.snapshot().unwrap();
    assert_eq!(
        image_value(&snapshot, SETPOINT),
        Value::Float(OPERATING_POINT)
    );
    assert_eq!(image_value(&snapshot, NEW_KNOB), Value::Float(5.0));

    // The crossing is journaled once — the transition, not every pull.
    let reinit_entries = standby
        .journal(0)
        .unwrap()
        .iter()
        .filter(|entry| matches!(entry.event, JournalEvent::Reinitialized { .. }))
        .count();
    assert_eq!(reinit_entries, 1, "one crossing journals once");

    // The documented switchover at the scan boundary: demote the old
    // active — its gate closes with the request — then promote the
    // reinitialized peer. Exactly one writer throughout: the field holds
    // the old run's last write between the two requests, and the demoted
    // peer's scans never reach it.
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let held = field.read(VALVE).unwrap().value;
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    let _quiesced = active.advance(1).unwrap();
    assert_eq!(field.read(VALVE).unwrap().value, held);

    // M ticks of the promoted peer writing the field — each tick the
    // field carries exactly its image's staged output — while the old
    // peer scans quiesced.
    let mut trace = Vec::new();
    for _ in 0..M {
        let continued = standby.advance(1).unwrap();
        let carried = field.read(VALVE).unwrap().value;
        assert_eq!(carried, image_value(&continued, VALVE));
        trace.push((carried, field.read(LEVEL).unwrap().value));
        let before = field.read(VALVE).unwrap().value;
        active.advance(1).unwrap();
        assert_eq!(
            field.read(VALVE).unwrap().value,
            before,
            "the demoted peer never writes the field"
        );
    }

    // Roles settled on both surfaces: the revised model owns the field;
    // the old peer is a quiesced standby — it was never a tracking peer
    // of the new active (it started without `--standby`), so it reports
    // unsynchronized rather than converging onto foreign checkpoints.
    let role = standby.role().unwrap();
    assert_eq!(role.role, Role::Active);
    assert_eq!(role.sync, None);
    let role = active.role().unwrap();
    assert_eq!(role.role, Role::Standby);
    assert_eq!(role.sync, Some(StandbySync::Unsynchronized));
    // The carried operator value is still the revised run's setpoint.
    let snapshot = standby.snapshot().unwrap();
    assert_eq!(
        image_value(&snapshot, SETPOINT),
        Value::Float(OPERATING_POINT)
    );

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

    // The digest's fingerprints are masked: each run's plant listens on
    // an ephemeral port whose address is part of the fingerprinted
    // document, so the hash values legitimately differ run to run — the
    // assertions above already pinned them to the loaded models.
    let mut masked_report = serde_json::to_value(&report).unwrap();
    masked_report["from"] = "v1".into();
    masked_report["to"] = "v2".into();

    let digest = serde_json::json!({
        "trace": trace
            .iter()
            .map(|(valve, level)| [valve, level])
            .collect::<Vec<_>>(),
        "report": masked_report,
        "transitions": [role_changes(&active), role_changes(&standby)],
        "final": [
            active.snapshot().unwrap(),
            standby.snapshot().unwrap(),
            field.read(VALVE).unwrap(),
            field.read(LEVEL).unwrap(),
        ],
    });
    let _ = std::fs::remove_dir_all(&dir);
    digest
}

#[test]
fn revised_model_rolls_into_production_through_the_pair() {
    run_roll("once");
}

#[test]
fn breaking_revision_is_rejected_before_promotion() {
    let dir = std::env::temp_dir().join(format!("dcs-revision-broken-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant();
    let (v1_path, _v1) = v1_model(&dir, "v1.json", plant.addr);
    let broken_path = broken_model(&dir, "broken.json", plant.addr);

    let active_process = spawn_controller(&v1_path, &[]);
    let standby_process = spawn_controller(
        &broken_path,
        &[
            "--standby".to_string(),
            active_process.addr.to_string(),
            "--revised".to_string(),
        ],
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();

    // The operating point lands on the old model's run, then the broken
    // revision's first pull rejects the crossing: the setpoint's carried
    // `Float` value cannot reinterpret as the revision's `Int`.
    active
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(OPERATING_POINT),
        })
        .unwrap();
    for _ in 0..3 {
        active.advance(1).unwrap();
    }
    let before = field.read(VALVE).unwrap().value;

    standby.advance(1).unwrap();
    let report = standby.role().unwrap();
    let Some(StandbySync::Degraded { detail }) = report.sync else {
        panic!("the breaking revision must report degraded, found {report:?}");
    };
    assert!(
        detail.contains("internal point 11") && detail.contains("int"),
        "the named error must identify the element and the kind: {detail}"
    );

    // Promotion is the named refusal — the sync state is the evidence —
    // and the field never sees the failed revision's writes: the gate
    // stayed closed across the whole attempt.
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    assert_eq!(
        serde_json::from_str::<SwitchError>(&body).unwrap(),
        SwitchError::NotConverged {
            sync: StandbySync::Degraded {
                detail: detail.clone()
            }
        }
    );
    for _ in 0..3 {
        standby.advance(1).unwrap();
        assert_eq!(
            field.read(VALVE).unwrap().value,
            before,
            "the rejected revision never wrote the field"
        );
    }

    // The old active kept the field throughout and still writes it.
    assert_eq!(active.role().unwrap().role, Role::Active);
    let owner = active.advance(1).unwrap();
    assert_eq!(field.read(VALVE).unwrap().value, image_value(&owner, VALVE));

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn revision_roll_repeats_identically() {
    // Two complete scripted runs — spawn, cross, switch, M ticks of the
    // promoted peer's writes — must produce identical records.
    let first = run_roll("a");
    let second = run_roll("b");
    if first != second {
        let dir = std::env::temp_dir();
        for (name, digest) in [("first", &first), ("second", &second)] {
            let path = dir.join(format!("dcs-revision-{name}.json"));
            std::fs::write(&path, serde_json::to_string_pretty(digest).unwrap()).unwrap();
        }
        panic!("runs differ — see dcs-revision-{{first,second}}.json under the temp dir");
    }
}
