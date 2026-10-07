//! Scripted operations verification over two driven controller pairs: durable
//! journal replay across a standby restart, field continuity, and actor
//! attribution on commands and settlements. Repeated runs compare the
//! authoritative field traces and durable records.

use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent, PointId, Role,
    StandbySync, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};

mod support;

use support::{
    SimTcp, canonicalize_origins, image_value, kill, settled_receipts, sim_tcp_document,
    spawn_controller, spawn_controller_logged, spawn_plant, write_model,
};

/// The shared plant's model — the dcs-plant tank loop: level raw (10)
/// and setpoint (11) in, valve command (20) out.
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
/// Ticks both pairs run before pair A's standby restarts mid-run.
const PRE: u64 = 6;
/// Ticks both pairs run after the restart — the post-restart entries
/// the seq-continuity assertions read.
const POST: u64 = 4;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// The declared actor identity the attribution leg submits under — the
/// page's `?operator=` equivalent.
const OPERATOR: &str = "ops-lead";
/// The operator values the attributed and unattributed commands land.
const MOVED_SETPOINT: f64 = 55.0;
const RESTED_SETPOINT: f64 = 61.0;

/// Writes the controller-side model for a plant server at `plant`: the
/// shared tank-loop model re-pointed at `sim-tcp` per [`sim_tcp_document`]
/// — the remote-sim path through the assembly driver registry — and the
/// setpoint point 11 moved image-side: an internal writable `In` point,
/// the operator value the attributed commands target.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document = sim_tcp_document(MODEL_SOURCE, plant, SimTcp::PerDevice);
    let setpoint = document["io_points"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|point| point["id"] == SETPOINT.0)
        .unwrap();
    setpoint.as_object_mut().unwrap().remove("channel");
    setpoint["initial"] = serde_json::json!({ "float": 25.0 });
    write_model(dir, name, &document).0
}

/// Asserts the field carries exactly `owner`'s last write and records
/// the `(valve, level)` pair the run's trace must reproduce.
fn field_carry(field: &RemoteDriver, owner: &TelemetrySnapshot, trace: &mut Vec<(Value, Value)>) {
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(
        carried,
        image_value(owner, VALVE),
        "the field must carry only the field owner's writes"
    );
    trace.push((carried, field.read(LEVEL).unwrap().value));
}

/// One scripted tick on a pair: the tracking peer scans first — its
/// checkpoint pull lands inside its `POST /scan` — then the field
/// owner scans and steps the shared plant. Returns the owner's
/// snapshot; the pair must tick together.
fn tick_pair(standby: &MonitorClient, active: &MonitorClient) -> TelemetrySnapshot {
    let tracked = standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    assert_eq!(tracked.tick, owner.tick, "the pair must tick together");
    owner
}

/// The journal file's raw records in file order, decoded as JSON —
/// `{"run_boundary": …}` markers and `{"entry": …}` records alike.
fn file_records(path: &Path) -> Vec<serde_json::Value> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// The file's `entry` records, in file order, decoded back into the
/// contract's `JournalEntry` — marker lines are skipped.
fn file_entries(path: &Path) -> Vec<JournalEntry> {
    file_records(path)
        .iter()
        .filter_map(|record| {
            record
                .get("entry")
                .map(|entry| serde_json::from_value(entry.clone()).unwrap())
        })
        .collect()
}

/// The file's `run_boundary` marker records as `(run, tick)` pairs.
fn file_boundaries(path: &Path) -> Vec<(u64, u64)> {
    file_records(path)
        .iter()
        .filter_map(|record| {
            record.get("run_boundary").map(|marker| {
                (
                    marker["run"].as_u64().unwrap(),
                    marker["tick"].as_u64().unwrap(),
                )
            })
        })
        .collect()
}

/// The durable record covers the served page: `served` was fetched
/// through `GET /journal`, which waits the sink's drain out, so the
/// file holds every served entry in order — while the paced run keeps
/// journaling, the tail a post-flush append may add behind them.
fn assert_file_covers(path: &Path, served: &[JournalEntry]) {
    let file = file_entries(path);
    assert!(
        file.len() >= served.len(),
        "the durable journal {} is shorter than the served record",
        path.display()
    );
    assert_eq!(
        &file[..served.len()],
        served,
        "the durable journal {} must hold the served record in order",
        path.display()
    );
}

/// One scripted run of the M8 operations scenario documented in the
/// module header. Returns the run's auditable record as JSON two runs
/// must reproduce exactly: both pairs' field traces, the journal
/// leg's boundary markers and entry streams, the overview cards in
/// health and in fault, and the attributed and unattributed command
/// receipts with their journaled echoes.
fn run_operations(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-operations-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants, each serving one redundant pair — the
    // plant-wide surface has two real pairs to aggregate.
    let plant_a = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let plant_b = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model_a = controller_model(&dir, "pair-a.json", plant_a.addr);
    let model_b = controller_model(&dir, "pair-b.json", plant_b.addr);

    // Every controller journals to its own durable file — the
    // operations surface's audit trail. Pair A's standby also persists
    // its checkpoint, so its mid-run restart resumes the run.
    let journal_a_active = dir.join("pair-a-active.jsonl");
    let journal_a_standby = dir.join("pair-a-standby.jsonl");
    let journal_b_active = dir.join("pair-b-active.jsonl");
    let journal_b_standby = dir.join("pair-b-standby.jsonl");
    let state_a_standby = dir.join("pair-a-standby-state.json");

    let a_active_process = spawn_controller(
        &model_a,
        &[
            "--journal-file".to_string(),
            journal_a_active.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let a_standby_args = vec![
        "--standby".to_string(),
        a_active_process.addr.to_string(),
        "--journal-file".to_string(),
        journal_a_standby.to_str().unwrap().to_string(),
        "--state-file".to_string(),
        state_a_standby.to_str().unwrap().to_string(),
    ];
    let mut a_standby_process = spawn_controller(&model_a, &a_standby_args, DT);
    let b_active_process = spawn_controller(
        &model_b,
        &[
            "--journal-file".to_string(),
            journal_b_active.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let b_standby_process = spawn_controller(
        &model_b,
        &[
            "--standby".to_string(),
            b_active_process.addr.to_string(),
            "--journal-file".to_string(),
            journal_b_standby.to_str().unwrap().to_string(),
        ],
        DT,
    );

    let a_active = MonitorClient::new(a_active_process.addr);
    let mut a_standby = MonitorClient::new(a_standby_process.addr);
    let b_active = MonitorClient::new(b_active_process.addr);
    let b_standby = MonitorClient::new(b_standby_process.addr);
    let field_a = RemoteDriver::connect(plant_a.addr).unwrap();
    let field_b = RemoteDriver::connect(plant_b.addr).unwrap();
    let mut trace_a = Vec::new();
    let mut trace_b = Vec::new();

    // Both pairs run their scripted ticks in lockstep: each tick the
    // tracking peer pulls the active's checkpoint and scans quiesced,
    // then the field owner scans and steps its plant.
    for _ in 0..PRE {
        let owner = tick_pair(&a_standby, &a_active);
        field_carry(&field_a, &owner, &mut trace_a);
        let owner = tick_pair(&b_standby, &b_active);
        field_carry(&field_b, &owner, &mut trace_b);
    }

    // -- Leg 1: the durable journal across a restart (#163) ------------
    // The standby's pre-restart journal: entries the file already holds
    // verbatim behind the run-1 boundary marker.
    let before_restart = a_standby.journal(0).unwrap();
    assert!(
        !before_restart.is_empty(),
        "the pre-restart run must have journaled entries"
    );
    assert_file_covers(&journal_a_standby, &before_restart);
    assert_eq!(file_boundaries(&journal_a_standby), vec![(1, 0)]);

    // The restart: the process dies; its replacement resumes the run
    // from the state file — the preamble reports the restored tick —
    // and replays the journal file into the served ring.
    kill(&mut a_standby_process);
    let (resumed_process, preamble) = spawn_controller_logged(&model_a, &a_standby_args, DT);
    a_standby_process = resumed_process;
    a_standby = MonitorClient::new(a_standby_process.addr);
    assert!(
        preamble.iter().any(|line| {
            line.contains("resumed from state file") && line.contains(&format!("at tick {PRE}"))
        }),
        "the restart must report the resume: {preamble:?}"
    );

    // `GET /journal` answers the pre-restart entries verbatim —
    // replayed, not re-journaled — behind the restart's served
    // boundary entry, the file's run-2 marker separating the two
    // lifetimes at the restored tick.
    let served = a_standby.journal(0).unwrap();
    assert_eq!(&served[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        served[before_restart.len()],
        JournalEntry {
            seq: before_restart.last().unwrap().seq + 1,
            tick: Tick(PRE),
            event: JournalEvent::RunBoundary { run: 2 },
        },
        "the restart marker must be served at the restored tick: {served:?}"
    );
    assert_eq!(served.len(), before_restart.len() + 1);
    assert_eq!(file_boundaries(&journal_a_standby), vec![(1, 0), (2, PRE)]);

    // The pair cadence resumes: the restarted standby reconverges on
    // the active's checkpoints inside its first requested scan.
    for _ in 0..POST {
        let owner = tick_pair(&a_standby, &a_active);
        field_carry(&field_a, &owner, &mut trace_a);
        let owner = tick_pair(&b_standby, &b_active);
        field_carry(&field_b, &owner, &mut trace_b);
    }

    // The resumed run's entries continue the `seq` numbering, and the
    // file holds the whole record — both lifetimes' entries in order.
    let after_restart = a_standby.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1,
        "the first post-restart entry continues the seq numbering"
    );
    assert_file_covers(&journal_a_standby, &after_restart);

    // The file's record order separates the lifetimes: the run-1
    // marker, the run-1 entries, the run-2 marker at the restored
    // tick, then the resumed run's entries.
    let records = file_records(&journal_a_standby);
    assert!(
        records.len() >= after_restart.len() + 2,
        "the file holds both lifetimes' records — post-flush appends \
         may add behind them: {}",
        records.len()
    );
    assert_eq!(
        records[0],
        serde_json::json!({ "run_boundary": { "run": 1, "tick": 0 } })
    );
    assert_eq!(
        records[before_restart.len() + 1],
        serde_json::json!({ "run_boundary": { "run": 2, "tick": PRE } })
    );
    for (index, entry) in after_restart.iter().enumerate() {
        let record = &records[index + 1 + usize::from(index >= before_restart.len())];
        assert_eq!(
            record["entry"],
            serde_json::to_value(entry).unwrap(),
            "file record {} must hold the served entry verbatim",
            index + 1
        );
    }

    // The restarted standby is tracking again — the mid-run restart
    // cost the pair nothing the checkpoint stream did not earn back.
    let restarted_report = a_standby.role().unwrap();
    assert_eq!(restarted_report.role, Role::Standby);
    assert_eq!(
        restarted_report.sync,
        Some(StandbySync::Tracking {
            aligned: Tick(PRE + POST - 1)
        }),
        "the resumed standby must reconverge on the active's checkpoints"
    );

    // Pair A's active journaled the uninterrupted run to its own file;
    // pair B's controllers hold their run-1 records too.
    assert_eq!(file_boundaries(&journal_a_active), vec![(1, 0)]);
    let a_active_served = a_active.journal(0).unwrap();
    assert_file_covers(&journal_a_active, &a_active_served);
    for path in [&journal_b_active, &journal_b_standby] {
        assert_eq!(file_boundaries(path), vec![(1, 0)]);
        assert!(!file_entries(path).is_empty());
    }

    // The surviving pair continues after the second pair stops.
    let mut b_active_process = b_active_process;
    let mut b_standby_process = b_standby_process;
    kill(&mut b_active_process);
    kill(&mut b_standby_process);
    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);

    // Actor attribution on the command path.
    // A command submitted with a declared actor — the attributed
    // envelope the page sends under ?operator= — settles into a
    // receipt carrying it.
    let attributed = Command::WriteValue {
        point: SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(MOVED_SETPOINT),
    };
    let receipt = a_active.command_as(&attributed, Some(OPERATOR)).unwrap();
    let apply_tick = match receipt.outcome {
        CommandOutcome::Accepted { apply_tick } => apply_tick,
        other => panic!("the attributed command must be accepted: {other:?}"),
    };
    assert_eq!(receipt.actor.as_deref(), Some(OPERATOR));

    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);
    assert_eq!(
        image_value(&owner, SETPOINT),
        Value::Float(MOVED_SETPOINT),
        "the attributed command applied at its scan boundary"
    );

    // The journaled CommandSettled echoes the settled receipt —
    // attribution included — at the apply tick.
    let settled = settled_receipts(&a_active.journal(0).unwrap());
    assert_eq!(
        settled.last().unwrap(),
        &CommandReceipt {
            command: attributed.clone(),
            outcome: CommandOutcome::Applied { tick: apply_tick },
            actor: Some(OPERATOR.to_string()),
            reason: None,
            // The journaled settle keeps the issued receipt's minted
            // submission identity (#775).
            submission: receipt.submission,
        },
        "the journaled CommandSettled must carry the declared actor"
    );

    // An unattributed command — the bare `Command` body a
    // pre-attribution client sends — journals as before: settled with
    // no actor, never a rejection.
    let bare = Command::WriteValue {
        point: SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(RESTED_SETPOINT),
    };
    let bare_receipt = a_active.command(&bare).unwrap();
    assert_eq!(bare_receipt.actor, None);
    assert!(matches!(
        bare_receipt.outcome,
        CommandOutcome::Accepted { .. }
    ));
    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);
    let settled = settled_receipts(&a_active.journal(0).unwrap());
    let unattributed = settled.last().unwrap();
    assert_eq!(unattributed.command, bare);
    assert_eq!(unattributed.actor, None);
    assert!(matches!(
        unattributed.outcome,
        CommandOutcome::Applied { .. }
    ));

    // The durable file carries the same record the endpoint serves —
    // the attributed entry included.
    let served_journal = a_active.journal(0).unwrap();
    assert_file_covers(&journal_a_active, &served_journal);

    let mut digest = serde_json::json!({
        "field_trace": {
            "pair_a": trace_a.iter().map(|(valve, level)| [valve, level]).collect::<Vec<_>>(),
            "pair_b": trace_b.iter().map(|(valve, level)| [valve, level]).collect::<Vec<_>>(),
        },
        "journal": {
            "pre_restart": before_restart,
            "post_restart": after_restart,
            "boundaries": file_boundaries(&journal_a_standby),
            "restarted_sync": restarted_report.sync,
        },
        "actor": {
            "attributed_receipt": receipt,
            "unattributed_receipt": bare_receipt,
            "settled": settled_receipts(&served_journal),
        },
    });
    canonicalize_origins(&mut digest, &mut Vec::new());

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

#[test]
fn the_m8_operations_criteria_walk_end_to_end_in_one_scripted_run() {
    run_operations("once");
}

#[test]
fn identical_operations_runs_reproduce_the_identical_digest() {
    let first = run_operations("a");
    let second = run_operations("b");
    if first != second {
        let dir =
            std::env::temp_dir().join(format!("dcs-operations-digest-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("first.json"),
            serde_json::to_string_pretty(&first).unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.join("second.json"),
            serde_json::to_string_pretty(&second).unwrap(),
        )
        .unwrap();
        panic!(
            "identical scripted runs produced different digests — see {}",
            dir.display()
        );
    }
}
