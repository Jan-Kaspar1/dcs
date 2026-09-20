//! Rolling a revised plant model on a lone controller through the
//! `--revised --state-file` resume — the scheduled-outage half of the
//! rolling model-revision decision, scripted against the binary itself.
//!
//! Where the pair roll (`model_revision.rs`) crosses the model boundary
//! on a tracking standby's pulled checkpoints without interrupting the
//! process, a lone controller has no peer to carry the run: its roll is
//! the restart the `--state-file` already covers, and the arm extends
//! to that seam. The scripted runs below drive a `--driven` controller
//! on model v1 — commands landing the held operator value, the force,
//! and the output image the carryover rule is about — kill it, and
//! restart on the derivation's revised document armed `--revised`: the
//! persisted checkpoint crosses the boundary through
//! `Executor::reinitialize`, resuming at the checkpointed tick with the
//! carried set and the crossing's report printed and journaled. A
//! rule-breaking revision — the retyped carried point
//! `qa_lane/revision-incompatible.json` models — fails startup naming
//! the `CarryoverError` and leaves the file untouched, and the same
//! restart unarmed still refuses the fingerprint.

use dcs_core::{
    CarriedPoint, Command, CommandOutcome, DroppedElement, ForcedPoint, JournalEvent, PointId,
    Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{CONTROLLER, Spawned, image_value, kill, listening_on, spawn_logged, write_model};

/// The shared tank loop: local simulated I/O, a PID integrating a
/// setpoint error against a looped-back level — run state the
/// checkpoint must carry for the roll to continue the interrupted run.
const TANK_LOOP_SOURCE: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the run executes before the restart.
const N: u64 = 10;
/// v1's field setpoint — writable, forced across the boundary.
const SETPOINT: PointId = PointId(10);
const VALVE: PointId = PointId(12);
/// The held operator value both models serve — the carried internal
/// point the breaking revision retypes.
const HELD: PointId = PointId(30);
/// The revision's added held operator value — initialized, never
/// carried. Its id sits below the mounted model's maximum declared id
/// so assembly's synthesized-link allocation base does not shift and
/// no unrelated element is renamed across the boundary — the
/// `qa_lane/revision.json` recipe's own rule.
const NEW_KNOB: PointId = PointId(13);
const OPERATING_POINT: f64 = 33.5;
const FORCED_LEVEL: f64 = 42.0;

fn run(args: &[&str]) -> std::process::Output {
    Process::new(CONTROLLER).args(args).output().unwrap()
}

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-lone-revision-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The checkpoint `path` currently holds.
fn persisted(path: &Path) -> Checkpoint {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

/// Model v1: the tank loop plus a held operator value at point 30 —
/// the writable internal `In` point the carryover rule is about.
fn base_model() -> serde_json::Value {
    let mut document: serde_json::Value = serde_json::from_str(TANK_LOOP_SOURCE).unwrap();
    document["io_points"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "id": HELD.0,
            "direction": "in",
            "value_type": "float",
            "writable": true,
            "initial": { "float": 25.0 }
        }));
    document
}

/// The checked-in revision derivation applied to the mounted document:
/// additive only — one new writable internal point plus its monitoring
/// signal, the same shape the lane's `qa_lane/revision.json` recipe
/// applies — shifting the fingerprint without touching field wiring or
/// the carried set.
fn revise(document: &mut serde_json::Value) {
    document["io_points"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "id": NEW_KNOB.0,
            "direction": "in",
            "value_type": "bool",
            "writable": true,
            "initial": { "bool": false }
        }));
    document["signals"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "id": 1013,
            "name": "lone-revision-marker",
            "source": NEW_KNOB.0,
            "unit": "",
            "description": "lone-roll revision marker — added by the derivation",
            "group": "ops"
        }));
}

/// The rule-breaking revision: the checked-in derivation plus a retype
/// of the carried point — `qa_lane/revision-incompatible.json`'s
/// shape: the revision keeps point 30's declared identity but changes
/// its kind, exactly the `CarryoverError::InternalKindMismatch` the
/// carryover rule refuses. Point 30 wires nothing, so the document
/// still validates — the crossing, not the load, is what fails.
fn broken_model() -> serde_json::Value {
    let mut document = base_model();
    revise(&mut document);
    let held = document["io_points"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|point| point["id"] == HELD.0)
        .unwrap();
    held["value_type"] = "int".into();
    held["initial"] = serde_json::json!({ "int": 0 });
    document
}

fn v1_model(dir: &Path) -> (PathBuf, PlantModel) {
    write_model(dir, "v1.json", &base_model())
}

fn v2_model(dir: &Path) -> (PathBuf, PlantModel) {
    let mut document = base_model();
    revise(&mut document);
    write_model(dir, "v2.json", &document)
}

/// The driven-mode spawn on `model` persisting `state` and `journal`,
/// armed `--revised` when `armed`. Returns the process plus the stderr
/// preamble — the resume report lives there.
fn spawn_lone(model: &Path, state: &Path, journal: &Path, armed: bool) -> (Spawned, Vec<String>) {
    let mut args = vec![
        model.to_str().unwrap().to_string(),
        "--listen".to_string(),
        "127.0.0.1:0".to_string(),
        "--driven".to_string(),
        "--dt".to_string(),
        DT.to_string(),
        "--state-file".to_string(),
        state.to_str().unwrap().to_string(),
        "--journal-file".to_string(),
        journal.to_str().unwrap().to_string(),
    ];
    if armed {
        args.push("--revised".to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on)
}

/// Runs model v1 for `N` driven scans with the operator inputs the
/// crossing must carry — the held internal value, the force on the
/// field setpoint — then kills the process, leaving the persisted
/// checkpoint at tick `N`.
fn run_first_half(dir: &Path, state: &Path, journal: &Path) -> (PathBuf, PlantModel) {
    let (v1_path, v1) = v1_model(dir);
    let (mut first, preamble) = spawn_lone(&v1_path, state, journal, false);
    assert!(
        !preamble.iter().any(|line| line.contains("resumed")),
        "the first run starts cold: {preamble:?}"
    );
    let client = MonitorClient::new(first.addr);
    for command in [
        Command::WriteValue {
            point: HELD,
            kind: ValueKind::Float,
            value: Value::Float(OPERATING_POINT),
        },
        Command::ForcePoint {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(FORCED_LEVEL),
        },
    ] {
        let receipt = client.command(&command).unwrap();
        assert!(
            matches!(
                receipt.outcome,
                CommandOutcome::Applied { .. } | CommandOutcome::Accepted { .. }
            ),
            "{receipt:?}"
        );
    }
    client.advance(N).unwrap();
    kill(&mut first);
    let checkpoint = persisted(state);
    assert_eq!(checkpoint.tick, Tick(N));
    (v1_path, v1)
}

#[test]
fn a_revised_lone_controller_resumes_across_the_model_boundary() {
    let dir = scratch("roll");
    let state = dir.join("state.json");
    let journal = dir.join("journal.jsonl");
    let (_v1_path, v1) = run_first_half(&dir, &state, &journal);
    let (v2_path, v2) = v2_model(&dir);
    assert_ne!(
        v1.fingerprint(),
        v2.fingerprint(),
        "the revision must fingerprint differently by design"
    );

    // The armed restart: the persisted checkpoint's foreign
    // fingerprint crosses through `Executor::reinitialize` — the
    // startup report prints the crossing before the monitor binds.
    let (mut second, preamble) = spawn_lone(&v2_path, &state, &journal, true);
    let resume = preamble
        .iter()
        .find(|line| line.contains("resumed from state file"))
        .unwrap_or_else(|| panic!("the armed restart must report the resume: {preamble:?}"));
    assert!(
        resume.contains("across the model boundary") && resume.contains("reinitialized"),
        "the crossing report prints at startup: {resume}"
    );
    let client = MonitorClient::new(second.addr);

    // The crossing's carryover report is the durable record —
    // journaled once behind the restart's run-boundary marker, naming
    // both fingerprints and the checkpointed tick it resumed at.
    let entries = client.journal(0).unwrap();
    let report = entries
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::Reinitialized { report } => Some(report.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("the crossing must journal its report: {entries:?}"));
    assert_eq!(report.from, Some(v1.fingerprint()));
    assert_eq!(report.to, Some(v2.fingerprint()));
    assert_eq!(report.resumed_at, Tick(N));
    assert_eq!(
        report.carried,
        vec![CarriedPoint {
            point: HELD,
            value: Value::Float(OPERATING_POINT),
        }],
        "the held operator value carries by declared identity"
    );
    assert!(
        report
            .carried_outputs
            .iter()
            .any(|carried| carried.point == VALVE),
        "the valve image sample carries: {report:?}"
    );
    assert_eq!(
        report.carried_forces,
        vec![ForcedPoint {
            point: SETPOINT,
            value: Value::Float(FORCED_LEVEL),
        }],
        "the force set carries all-or-nothing"
    );
    // Every element without a continuation is named: the checkpoint's
    // synthesized link points — allocated ids are not declared
    // identity — and the captured driver section, which the revision's
    // own channels re-observe.
    assert!(
        report.dropped.contains(&DroppedElement::DriverState),
        "{report:?}"
    );
    assert!(
        report
            .dropped
            .iter()
            .any(|element| matches!(element, DroppedElement::InternalPoint { .. })),
        "the link-carrier internal points are named dropped: {report:?}"
    );
    assert_eq!(report.initialized, vec![NEW_KNOB]);

    // The durable record on disk: the journal file's run-boundary
    // marker separates the two process lifetimes, then the crossing's
    // entry lands behind it.
    let file = std::fs::read_to_string(&journal).unwrap();
    assert!(file.contains("\"run_boundary\":{\"run\":2"), "{file}");
    assert!(file.contains("reinitialized"), "{file}");

    // The run continues at the checkpointed tick: the next requested
    // scan is N+1, the carried setpoint holds, the force still
    // substitutes the field input, and the revision's added point
    // stands at its declared initial.
    let snapshot = client.advance(1).unwrap();
    assert_eq!(snapshot.tick, Tick(N + 1));
    assert_eq!(image_value(&snapshot, HELD), Value::Float(OPERATING_POINT));
    assert_eq!(image_value(&snapshot, SETPOINT), Value::Float(FORCED_LEVEL));
    assert_eq!(image_value(&snapshot, NEW_KNOB), Value::Bool(false));
    assert_eq!(
        client.checkpoint().unwrap().forces,
        [(SETPOINT, Value::Float(FORCED_LEVEL))]
            .into_iter()
            .collect(),
        "the carried force set persists across the boundary"
    );

    kill(&mut second);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_rule_breaking_revision_fails_startup_and_preserves_the_state_file() {
    let dir = scratch("refused");
    let state = dir.join("state.json");
    let journal = dir.join("journal.jsonl");
    let (_v1_path, _v1) = run_first_half(&dir, &state, &journal);
    let (broken_path, _broken) = write_model(&dir, "broken.json", &broken_model());
    let preserved = std::fs::read(&state).unwrap();

    // The armed restart on the rule-breaking revision: the retyped
    // carried point cannot cross — startup fails nonzero naming the
    // CarryoverError, and the state file survives untouched.
    let refused = run(&[
        broken_path.to_str().unwrap(),
        "--ticks",
        "5",
        "--state-file",
        state.to_str().unwrap(),
        "--revised",
    ]);
    assert!(!refused.status.success());
    let stderr = String::from_utf8(refused.stderr).unwrap();
    assert!(stderr.contains("state file"), "{stderr}");
    assert!(stderr.contains("carryover failed"), "{stderr}");
    assert!(
        stderr.contains("internal point 30") && stderr.contains("retype must rename"),
        "the named CarryoverError identifies the element and reason: {stderr}"
    );
    assert_eq!(
        std::fs::read(&state).unwrap(),
        preserved,
        "a refused crossing leaves the state file untouched"
    );
    assert!(refused.stdout.is_empty(), "no partial run's output");

    // The unarmed restart on the plain revision still refuses the
    // fingerprint — the arm is what opens the crossing.
    let (v2_path, _v2) = v2_model(&dir);
    let unarmed = run(&[
        v2_path.to_str().unwrap(),
        "--ticks",
        "5",
        "--state-file",
        state.to_str().unwrap(),
    ]);
    assert!(!unarmed.status.success());
    let stderr = String::from_utf8(unarmed.stderr).unwrap();
    assert!(stderr.contains("fingerprint"), "{stderr}");
    assert_eq!(
        std::fs::read(&state).unwrap(),
        preserved,
        "a refused resume leaves the state file untouched"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn an_armed_matching_fingerprint_resumes_ordinarily() {
    let dir = scratch("matching");
    let state = dir.join("state.json");
    let journal = dir.join("journal.jsonl");
    let (v1_path, _v1) = run_first_half(&dir, &state, &journal);

    // Armed but same model: the matching fingerprint takes the strict
    // apply path — no crossing, no carryover report — and the run
    // continues exactly like an unarmed resume.
    let (mut second, preamble) = spawn_lone(&v1_path, &state, &journal, true);
    let resume = preamble
        .iter()
        .find(|line| line.contains("resumed from state file"))
        .unwrap_or_else(|| panic!("the armed restart must report the resume: {preamble:?}"));
    assert!(!resume.contains("across the model boundary"), "{resume}");
    let client = MonitorClient::new(second.addr);
    let snapshot = client.advance(1).unwrap();
    assert_eq!(snapshot.tick, Tick(N + 1));
    assert!(
        client
            .journal(0)
            .unwrap()
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::Reinitialized { .. })),
        "no crossing journals on a matching fingerprint"
    );

    kill(&mut second);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn revised_without_a_roll_target_is_a_usage_error() {
    // The arm needs a crossing to arm: a tracking peer's pull path or
    // a lone run's state-file resume. Bare --revised stays rejected.
    const TANK_LOOP: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../dcs-assembly/fixtures/tank_loop.json"
    );
    let output = run(&[TANK_LOOP, "--revised", "--ticks", "5"]);
    assert_eq!(output.status.code(), Some(2));
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--revised requires"), "{stderr}");
}
