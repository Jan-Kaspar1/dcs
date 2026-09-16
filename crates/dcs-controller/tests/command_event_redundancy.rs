//! Declared-command and emitted-event semantics across the redundant
//! pair — issue #381's scripted proving run.
//!
//! The harness is `hot_swap`'s driven pair — two `dcs-controller
//! --driven` peers over the shared simulated plant plus an
//! uninterrupted reference controller on its own plant — with the
//! controller-side model extended by a `sequencer` component wired to
//! internal points: the proving kind's declared commands (`advance`,
//! `reset`) and emitted events (`step_completed`) ride the same command
//! path and journal as everything else, without touching field I/O.
//!
//! The pinned semantics under test:
//!
//! - a tracking standby emits and journals the same declared events as
//!   the active — identical per-peer streams, so a promoted peer's
//!   journal is indistinguishable from an uninterrupted run's;
//! - a declared command invoked against a peer not reporting settled
//!   `active` — standby, or mid-transition — answers the named
//!   `NotActive` rejection receipt like every other command;
//! - an invocation admitted but unsettled at promotion is carried by
//!   the promote boundary's final checkpoint pull and settles exactly
//!   once on the new active: one receipt, one journaled outcome;
//! - the emitted-event sequence continues monotonically after
//!   promotion — the new active's served stream equals the reference
//!   run's for the stated tick count.

use dcs_core::{
    Command, CommandError, CommandOutcome, EmittedEvent, IoDriver, JournalEvent, PointId, Role,
    TelemetrySnapshot, Tick, Value,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::collections::BTreeMap;
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
/// The controller-side model source — the shared tank-loop model
/// [`controller_model`] re-points at `sim-tcp` and extends with the
/// proving sequencer.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the pair runs — the standby tracking gate-closed — before the
/// switchover.
const N: u64 = 6;
/// Ticks after the switch the run is compared against the reference.
const M: u64 = 10;
const VALVE: PointId = PointId(20);
/// The sequencer's reported step index — an internal `Out` point.
const STEP: PointId = PointId(33);

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
/// `sim-tcp`, extended by the proving `sequencer` — a four-step table
/// of two-tick steps whose `run`/`reset` inputs and `out`/`step`/`done`
/// outputs are internal points, so the component's declared commands
/// and emitted events exercise the pair without field I/O.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        device["kind"] = "sim-tcp".into();
        device["parameters"] = serde_json::json!({ "address": plant.to_string() });
    }
    document["io_points"].as_array_mut().unwrap().extend([
        // `run` held true steps the table; `reset` held false lets the
        // declared `reset` command own the restart.
        serde_json::json!({"id": 30, "direction": "in", "value_type": "bool", "initial": {"bool": true}}),
        serde_json::json!({"id": 31, "direction": "in", "value_type": "bool", "initial": {"bool": false}}),
        serde_json::json!({"id": 32, "direction": "out", "value_type": "float", "initial": {"float": 0.0}}),
        serde_json::json!({"id": 33, "direction": "out", "value_type": "int", "initial": {"int": 0}}),
        serde_json::json!({"id": 34, "direction": "out", "value_type": "bool", "initial": {"bool": false}}),
    ]);
    document["components"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({
            "id": 3,
            "kind": "sequencer",
            "parameters": {
                "step_count": {"int": 4},
                "step_1_ticks": {"int": 2}, "step_1_out": {"float": 10.0},
                "step_2_ticks": {"int": 2}, "step_2_out": {"float": 20.0},
                "step_3_ticks": {"int": 2}, "step_3_out": {"float": 30.0},
                "step_4_ticks": {"int": 2}, "step_4_out": {"float": 40.0}
            },
            "ports": {
                "run": {"direction": "in", "value_type": "bool"},
                "reset": {"direction": "in", "value_type": "bool"},
                "out": {"direction": "out", "value_type": "float"},
                "step": {"direction": "out", "value_type": "int"},
                "done": {"direction": "out", "value_type": "bool"}
            }
        }));
    document["connections"].as_array_mut().unwrap().extend([
        serde_json::json!({"from": {"point": 30}, "to": {"port": {"component": 3, "name": "run"}}}),
        serde_json::json!({"from": {"point": 31}, "to": {"port": {"component": 3, "name": "reset"}}}),
        serde_json::json!({"from": {"port": {"component": 3, "name": "out"}}, "to": {"point": 32}}),
        serde_json::json!({"from": {"port": {"component": 3, "name": "step"}}, "to": {"point": 33}}),
        serde_json::json!({"from": {"port": {"component": 3, "name": "done"}}, "to": {"point": 34}}),
    ]);
    document["signals"].as_array_mut().unwrap().extend([
        serde_json::json!({"id": 103, "name": "sequence-out", "source": 32, "unit": "%"}),
        serde_json::json!({"id": 104, "name": "sequence-step", "source": 33}),
        serde_json::json!({"id": 105, "name": "sequence-done", "source": 34}),
    ]);
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// The `sequencer` declared-command invocation — `advance` takes the
/// optional `count` argument, `reset` takes none.
fn invoke(command: &str, count: Option<i64>) -> Command {
    Command::Invoke {
        component: "sequencer:3".to_string(),
        command: command.to_string(),
        arguments: count
            .map(|count| BTreeMap::from([("count".to_string(), Value::Int(count))]))
            .unwrap_or_default(),
    }
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

/// The journal's emitted-event stream — `(tick, event)` per
/// `EventEmitted` entry: the comparable shape the identical-streams
/// assertions read.
fn emitted(client: &MonitorClient) -> Vec<(u64, EmittedEvent)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::EventEmitted { event } => Some((entry.tick.0, event.clone())),
            _ => None,
        })
        .collect()
}

/// The journaled settlements of one exact command — `(tick, outcome)`
/// per `CommandSettled` entry whose receipt carries `command`, however
/// it settled — the exactly-once count for a carried invocation.
fn settlements_of(client: &MonitorClient, command: &Command) -> Vec<(u64, CommandOutcome)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } if &receipt.command == command => {
                Some((entry.tick.0, receipt.outcome.clone()))
            }
            _ => None,
        })
        .collect()
}

#[test]
fn declared_commands_and_emitted_events_survive_promotion() {
    let dir = std::env::temp_dir().join(format!("dcs-cmd-events-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants: the pair's and the reference run's — identical
    // model and dynamics, identical request sequences, identical runs.
    let pair_plant = spawn_plant();
    let reference_plant = spawn_plant();
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let reference_model = controller_model(&dir, "reference.json", reference_plant.addr);

    let active_process = spawn_controller(&pair_model, &[]);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
    );
    let reference_process = spawn_controller(&reference_model, &[]);
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let reference = MonitorClient::new(reference_process.addr);
    // The observer asserting the field answers to exactly one writer.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();

    // A declared command on a peer not reporting settled active is the
    // named refusal — a component invoke gets the same role-boundary
    // answer a point write does, and it journals on the refusing peer.
    // The probes use `count` values no real invocation shares, so each
    // command's journaled settlements read as its own unambiguous
    // audit trail.
    let receipt = standby.command(&invoke("advance", Some(2))).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotActive {
                point: None,
                role: Role::Standby,
            }
        },
        "{receipt:?}"
    );

    // Phase 1: N tracking ticks — each tick the standby pulls the
    // active's checkpoint, applies it, and scans quiesced, so the peers'
    // emitted records are the identical-streams proof: at the boundary
    // after tick 3 the active (and the reference, at its matching
    // boundary) admits `advance {count: 1}`; the standby's next pull
    // carries the pending entry and all three apply it at tick 4.
    for tick in 1..=N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
        assert_eq!(tracked, owner, "tick {tick}");
        assert_eq!(owner, alone, "tick {tick}");
        if tick == 3 {
            for client in [&active, &reference] {
                let receipt = client.command(&invoke("advance", Some(1))).unwrap();
                assert_eq!(
                    receipt.outcome,
                    CommandOutcome::Accepted {
                        apply_tick: Tick(4)
                    },
                    "{receipt:?}"
                );
            }
        }
    }
    // The adopted receipt log is the pair's one command audit.
    assert_eq!(standby.receipts().unwrap(), active.receipts().unwrap());
    assert_eq!(standby.receipts().unwrap().len(), 1);
    // The pinned standby-emission semantics: the tracking peer's
    // journal already carries the same `step_completed` stream at the
    // same ticks — identical to the field owner's and the reference's.
    assert_eq!(emitted(&standby), emitted(&active));
    assert_eq!(emitted(&standby), emitted(&reference));

    // The unsettled-at-promotion case: `reset` is admitted on the
    // active (and the reference) at tick N and stays `Accepted` through
    // the switchover requests. The standby has not pulled since the
    // admission — the promote boundary's final synchronization is the
    // pending invocation's carrier.
    for client in [&active, &reference] {
        let receipt = client.command(&invoke("reset", None)).unwrap();
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(N + 1)
            },
            "{receipt:?}"
        );
    }
    assert_eq!(standby.receipts().unwrap().len(), 1);

    // The documented switchover: demote, then promote — the promote's
    // final pull adopts the still-pending invocation.
    assert_eq!(active.demote().unwrap().role, Role::Demoting);
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    assert_eq!(standby.receipts().unwrap().len(), 2);
    assert_eq!(
        standby.receipts().unwrap()[1].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(N + 1)
        }
    );

    // Mid-transition the new peer is still not settled-active: an
    // invoke lands the named refusal naming `promoting`.
    let receipt = standby.command(&invoke("advance", Some(3))).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotActive {
                point: None,
                role: Role::Promoting,
            }
        },
        "{receipt:?}"
    );

    // Phase 2: M post-switch ticks. The promoted peer's first scan
    // settles the carried invocation — exactly once — at the same
    // boundary the demoted peer's own copy and the reference's apply
    // theirs. Until the post-switch `advance` goes only to the field
    // owner (submitted after tick N+5, applying at N+6), the demoted
    // peer's quiesced run stays the uninterrupted reference too.
    for tick in (N + 1)..=(N + M) {
        let quiesced = active.advance(1).unwrap();
        let continued = standby.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
        assert_eq!(continued, alone, "tick {tick}");
        if tick <= N + 5 {
            assert_eq!(continued, quiesced, "tick {tick}");
        }
        if tick == N + 5 {
            for client in [&standby, &reference] {
                let receipt = client.command(&invoke("advance", Some(1))).unwrap();
                assert_eq!(
                    receipt.outcome,
                    CommandOutcome::Accepted {
                        apply_tick: Tick(N + 6)
                    },
                    "{receipt:?}"
                );
            }
        }
    }
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(standby.role().unwrap().sync, None);
    assert_eq!(active.role().unwrap().role, Role::Standby);
    // The field carries the promoted peer's write — its sequencer
    // reports the uninterrupted run's step.
    let continued = standby.snapshot().unwrap();
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&continued, VALVE)
    );
    assert_eq!(image_value(&continued, STEP), Value::Int(4));

    // A declared command on the demoted peer is refused at the role
    // boundary like the standby's was.
    let receipt = active.command(&invoke("advance", Some(4))).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Rejected {
            reason: CommandError::NotActive {
                point: None,
                role: Role::Standby,
            }
        },
        "{receipt:?}"
    );

    // The pinned record: the promoted peer's emitted-event stream is
    // the uninterrupted reference run's — every `step_completed` at the
    // same tick with the same payload — its receipt log is identical,
    // and the `reset` carried across the boundary settled exactly once
    // at tick N+1: one receipt, one journaled outcome.
    assert_eq!(emitted(&standby), emitted(&reference));
    assert_eq!(standby.receipts().unwrap(), reference.receipts().unwrap());
    assert_eq!(
        settlements_of(&standby, &invoke("reset", None)),
        settlements_of(&reference, &invoke("reset", None)),
    );
    assert_eq!(
        settlements_of(&standby, &invoke("reset", None)),
        vec![(N + 1, CommandOutcome::Applied { tick: Tick(N + 1) })]
    );
    // The propagated `advance` and the post-switch one each settled
    // once as well — the two identical commands read as two entries at
    // their own apply ticks — and each role-boundary refusal journaled
    // once, at the tick it was refused, on the refusing peer.
    assert_eq!(
        settlements_of(&standby, &invoke("advance", Some(1))),
        vec![
            (4, CommandOutcome::Applied { tick: Tick(4) }),
            (N + 6, CommandOutcome::Applied { tick: Tick(N + 6) }),
        ]
    );
    for (client, probe, role, tick) in [
        (&standby, invoke("advance", Some(2)), Role::Standby, 0),
        (&standby, invoke("advance", Some(3)), Role::Promoting, N),
        (&active, invoke("advance", Some(4)), Role::Standby, N + M),
    ] {
        assert_eq!(
            settlements_of(client, &probe),
            vec![(
                tick,
                CommandOutcome::Rejected {
                    reason: CommandError::NotActive { point: None, role }
                }
            )]
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}
