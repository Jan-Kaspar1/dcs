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
    Command, CommandError, CommandOutcome, EmittedEvent, IoDriver, JournalEvent, PointId, Quality,
    QualityReason, Role, StandbySync, Tick, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};

mod support;

use support::{
    SimTcp, image_sample, image_value, kill, settled_receipts, sim_tcp_document, spawn_controller,
    spawn_controller_logged, spawn_plant, write_model,
};

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
/// A writable internal `In` point — the held operator value the
/// promote-boundary pending-command reproduction writes and reads.
const HELD: PointId = PointId(40);

/// Writes the controller-side model for a plant server at `plant`: the
/// shared tank-loop model re-pointed at `sim-tcp` per
/// [`sim_tcp_document`], extended by the proving `sequencer` — a
/// four-step table of two-tick steps whose `run`/`reset` inputs and
/// `out`/`step`/`done` outputs are internal points, so the component's
/// declared commands and emitted events exercise the pair without
/// field I/O.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document = sim_tcp_document(MODEL_SOURCE, plant, SimTcp::PerDevice);
    document["io_points"].as_array_mut().unwrap().extend([
        // `run` held true steps the table; `reset` held false lets the
        // declared `reset` command own the restart.
        serde_json::json!({"id": 30, "direction": "in", "value_type": "bool", "initial": {"bool": true}}),
        serde_json::json!({"id": 31, "direction": "in", "value_type": "bool", "initial": {"bool": false}}),
        serde_json::json!({"id": 32, "direction": "out", "value_type": "float", "initial": {"float": 0.0}}),
        serde_json::json!({"id": 33, "direction": "out", "value_type": "int", "initial": {"int": 0}}),
        serde_json::json!({"id": 34, "direction": "out", "value_type": "bool", "initial": {"bool": false}}),
        // A writable internal `In` point — the held operator value the
        // promote-boundary pending-command test commands and observes.
        serde_json::json!({"id": 40, "direction": "in", "value_type": "bool", "initial": {"bool": false}, "writable": true}),
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
    write_model(dir, name, &document).0
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
    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let reference_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);
    let reference_model = controller_model(&dir, "reference.json", reference_plant.addr);

    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let reference_process = spawn_controller(&reference_model, &[], DT);
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
    // active's checkpoint, applies it, and scans quiesced. At the
    // boundary after tick 3 the active (and the reference, at its
    // matching boundary) admits `advance {count: 1}`; the standby's
    // next pull carries the pending entry but — per #689 — its
    // quiesced scan must not settle it: the carried receipt stays
    // `Accepted` on the tracker at tick 4 while the field owners
    // apply it, so the tracker's sequencer lags by the invoke's
    // effect for exactly one tick. The tick-5 pull adopts the
    // applied checkpoint (outcome and advanced component state) and
    // the run converges — one journaled settle per peer, never a
    // phantom `Applied` on the gated image.
    for tick in 1..=N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
        if tick == 4 {
            assert_eq!(tracked.tick, owner.tick, "tick {tick}");
            assert_eq!(owner, alone, "tick {tick}");
            assert!(
                matches!(
                    standby.receipts().unwrap()[0].outcome,
                    CommandOutcome::Accepted { .. }
                ),
                "the quiesced scan carries the adopted invoke (#689)"
            );
            assert_eq!(
                active.receipts().unwrap()[0].outcome,
                CommandOutcome::Applied { tick: Tick(4) }
            );
        } else {
            assert_eq!(tracked, owner, "tick {tick}");
            assert_eq!(owner, alone, "tick {tick}");
        }
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
    // The pinned standby-emission semantics, as amended by #689: the
    // tracking peer's journal carries the same `step_completed` stream
    // at the same ticks — identical to the field owner's and the
    // reference's — except at tick 4, where the carried (not settled)
    // invoke leaves the tracker one step behind for exactly one scan.
    // The tick-5 adoption converges state, so every other tick matches.
    let emitted_except_tick_4 = |client: &MonitorClient| {
        emitted(client)
            .into_iter()
            .filter(|(tick, _)| *tick != 4)
            .collect::<Vec<_>>()
    };
    assert_eq!(
        emitted_except_tick_4(&standby),
        emitted_except_tick_4(&active)
    );
    assert_eq!(
        emitted_except_tick_4(&standby),
        emitted_except_tick_4(&reference)
    );

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
    // boundary the reference applies theirs. The demoted peer's own
    // copy stays carried, not settled (#689): its quiesced scan must
    // not mint an `Applied` on the fenced image, so at tick N+1 its
    // sequencer has not taken the reset while the promoted run has —
    // a one-tick lag the next tracking pull heals by adopting the
    // applied checkpoint's component state. Until the post-switch
    // `advance` goes only to the field owner (submitted after tick
    // N+5, applying at N+6), the demoted peer's quiesced run otherwise
    // stays the uninterrupted reference.
    for tick in (N + 1)..=(N + M) {
        let quiesced = active.advance(1).unwrap();
        let continued = standby.advance(1).unwrap();
        let alone = reference.advance(1).unwrap();
        assert_eq!(continued, alone, "tick {tick}");
        if tick == N + 1 {
            assert_eq!(quiesced.tick, continued.tick, "tick {tick}");
            assert!(
                matches!(
                    active.receipts().unwrap()[1].outcome,
                    CommandOutcome::Accepted { .. }
                ),
                "the demoted peer carries the reset (#689)"
            );
        } else if tick <= N + 5 {
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
    // same tick with the same payload, save the tick-4 invoke the
    // tracker carried rather than settled (#689, as in phase 1) — its
    // receipt log is identical, and the `reset` carried across the
    // boundary settled exactly once at tick N+1: one receipt, one
    // journaled outcome.
    assert_eq!(
        emitted_except_tick_4(&standby),
        emitted_except_tick_4(&reference)
    );
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

/// QA finding `promote-final-sync-drops-pending-command`: in the driven
/// cadence the tracking peer rests one tick past its last applied
/// checkpoint — the pull applies `ckpt@T` and the scan advances to
/// `T+1` — so the freshest checkpoint the active can serve carrying a
/// just-admitted command is `@T`, stale by the standby's clock. The
/// promote boundary's final pull used to skip it whole: the pending
/// write never reached the promoted run, and the superseded peer's own
/// pending copy later settled `applied` on its fenced image. Now the
/// stale checkpoint's newer receipts still carry, the promoted peer
/// settles the write `applied` on the live image, and the superseded
/// peer's own boundary settlement re-suspends — the carried `applied`
/// is the one verdict its journal converges on.
#[test]
fn a_pending_command_at_the_promote_boundary_survives_the_driven_cadence() {
    let dir = std::env::temp_dir().join(format!("dcs-promote-pending-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converge the standby on the active's checkpoints: each driven
    // scan pulls then scans, so both peers rest at tick N.
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

    // The reproduction's tick skew: the standby's last pull applied
    // ckpt@N and its scan advanced to N+1 while the active still
    // serves @N — so the checkpoint carrying the pending write the
    // boundary pull fetches is the stale one.
    standby.advance(1).unwrap();

    // The admission lands on the active between the standby's last
    // pull and the promote — `Accepted`, applying at the active's next
    // scan, riding its checkpointed receipt log.
    let write = Command::WriteValue {
        point: HELD,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    };
    let receipt = active.command(&write).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(N + 1)
        },
        "{receipt:?}"
    );
    assert!(standby.receipts().unwrap().is_empty());

    // `POST /promote` on the tracking standby: the final pull's
    // checkpoint is stale by the standby's clock, yet the pending
    // write carries into the promoted run.
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    let receipts = standby.receipts().unwrap();
    assert_eq!(receipts.len(), 1, "{receipts:?}");
    assert!(
        matches!(receipts[0].outcome, CommandOutcome::Accepted { .. }),
        "{receipts:?}"
    );

    // The promoted peer's first scan settles the carried write —
    // applied, and the value reaches the internal image the run owns.
    let continued = standby.advance(1).unwrap();
    assert_eq!(image_value(&continued, HELD), Value::Bool(true));
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(
        standby.receipts().unwrap()[0].outcome,
        CommandOutcome::Applied { tick: Tick(N + 2) }
    );

    // The QA finding
    // `demote-boundary-superseded-mint-then-carried-applied`: the
    // fenced peer's detection scan applied the write onto the
    // abandoned image, and the demotion used to mint a provisional
    // `Rejected`/`Superseded` for it — which the adopted checkpoint's
    // `Applied` then contradicted in the same journal, two terminal
    // outcomes for one admission. The boundary now re-suspends its
    // settlements instead: whether the line carried the admission is
    // the surviving run's verdict to make, so the receipt stays
    // `Accepted` and no `command_settled` journals on the demoted
    // peer yet.
    active.advance(1).unwrap();
    let suspended = active.receipts().unwrap();
    assert_eq!(
        suspended[0].outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(N + 1)
        },
        "the demote boundary must re-suspend, not settle: {suspended:?}"
    );
    assert_eq!(active.role().unwrap().role, Role::Demoting);
    assert_eq!(
        settlements_of(&active, &write),
        vec![],
        "no settlement may journal before the line adjudicates"
    );
    assert_eq!(
        settlements_of(&standby, &write),
        vec![(N + 2, CommandOutcome::Applied { tick: Tick(N + 2) })]
    );

    // The demoted peer reconverges on its announced successor: the
    // adopted log carries the line's `Applied` verdict at the same
    // index — the carried admission — so nothing settles `superseded`
    // and the journal records the one terminal outcome every peer
    // agrees on. One admission, one `command_settled` per journal, and
    // never a `Superseded` beside the `Applied`.
    active.advance(1).unwrap();
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must reconverge on its successor: {report:?}"
    );
    assert_eq!(active.receipts().unwrap(), standby.receipts().unwrap());
    assert_eq!(
        settlements_of(&active, &write),
        vec![(N + 2, CommandOutcome::Applied { tick: Tick(N + 2) })],
        "the demoted peer journals the line's one verdict — applied — \
         never the superseded-then-applied pair the defect produced"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

/// QA finding `demote-boundary-pending-command-lost-or-phantom-applied`,
/// the carried outcome: a command admitted on the active and still
/// `Accepted` when `POST /demote` lands used to race the demoted run's
/// first quiesced scan — settling `applied` on an image the gate
/// already fenced, erased by the next adoption. Demotion now suspends
/// the pending queue, so the receipt stays `Accepted` in the
/// checkpoint the successor's final-sync pull carries: the command
/// settles once, `applied`, on the run that actually wrote it — and
/// the demoted peer's own scan can never mint a phantom application.
#[test]
fn a_pending_command_at_the_demote_boundary_rides_the_final_sync_carry() {
    let dir = std::env::temp_dir().join(format!("dcs-demote-carry-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // Converge the pair on the active's checkpoints — both rest at
    // tick N.
    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }

    // The admission lands on the active between the standby's last
    // pull and the demote — `Accepted`, queued for the active's next
    // scan.
    let write = Command::WriteValue {
        point: HELD,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    };
    let receipt = active.command(&write).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(N + 1)
        },
        "{receipt:?}"
    );

    // The reproduction's ordering: demote before the active's next
    // scan, then promote the standby. The demotion suspends the
    // pending write — its receipt stays `Accepted` in the checkpoint
    // the promote boundary's final pull carries.
    assert_eq!(active.demote().unwrap().role, Role::Demoting);
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    let carried = standby.receipts().unwrap();
    assert_eq!(carried.len(), 1, "{carried:?}");
    assert!(
        matches!(carried[0].outcome, CommandOutcome::Accepted { .. }),
        "{carried:?}"
    );

    // The promoted peer's first scan settles the carried write —
    // applied, on the run that owns the field.
    let continued = standby.advance(1).unwrap();
    assert_eq!(image_value(&continued, HELD), Value::Bool(true));
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(
        standby.receipts().unwrap()[0].outcome,
        CommandOutcome::Applied { tick: Tick(N + 1) }
    );

    // The demoted peer's tracking pull adopts the line's checkpoint:
    // the carried entry covers its suspended copy — nothing
    // supersedes, and its own quiesced scan applies nothing the line
    // did not. Its journal's `applied` settlement is the honest one —
    // the field really took the write — and the adopted image keeps
    // the value instead of erasing it a tick later.
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
    assert_eq!(
        image_value(&active.snapshot().unwrap(), HELD),
        Value::Bool(true)
    );
    assert_eq!(active.receipts().unwrap(), standby.receipts().unwrap());
    assert_eq!(
        settlements_of(&standby, &write),
        vec![(N + 1, CommandOutcome::Applied { tick: Tick(N + 1) })]
    );
    assert_eq!(
        settlements_of(&active, &write),
        vec![(N + 1, CommandOutcome::Applied { tick: Tick(N + 1) })]
    );

    let _ = std::fs::remove_dir_all(&dir);
}

/// QA finding `superseded-command-still-settles-applied`: the demote
/// boundary's stale-checkpoint collision — the reproduction's paced
/// pair. The demoted peer's first tracking pull lands a checkpoint
/// the standby captured before it ever observed the admission, and
/// that absence is the capture's staleness, not the line's verdict:
/// the adopted receipt window's submission high-water never reached
/// the suspended entry's index. The adoption must keep the receipt
/// suspended — still `Accepted` in the checkpoint the successor's
/// final-sync pull carries — so the command settles exactly once,
/// `applied`, on the promoted run, with no provisional `superseded`
/// journaled beside it.
#[test]
fn a_stale_checkpoint_at_the_demote_boundary_cannot_supersede_the_raced_command() {
    let dir = std::env::temp_dir().join(format!("dcs-demote-stale-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

    let active_process = spawn_controller(&pair_model, &[], DT);
    let standby_process = spawn_controller(
        &pair_model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    for _ in 0..N {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
    }

    let write = Command::WriteValue {
        point: HELD,
        kind: ValueKind::Bool,
        value: Value::Bool(true),
    };
    let receipt = active.command(&write).unwrap();
    assert_eq!(
        receipt.outcome,
        CommandOutcome::Accepted {
            apply_tick: Tick(N + 1)
        },
        "{receipt:?}"
    );

    // The reproduction's ordering: demote, then let the demoted peer's
    // own tracking cycle run *before* the standby's promote-boundary
    // pull can carry the command — the pulled checkpoint is the
    // standby's own, whose receipt window predates the admission.
    // Stale, not adjudicating: the suspended receipt is restored
    // behind the adopted window, nothing settles, and the checkpoint
    // the demoted peer still serves keeps offering the command.
    assert_eq!(active.demote().unwrap().role, Role::Demoting);
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
    assert!(standby.receipts().unwrap().is_empty());
    let kept = active.receipts().unwrap();
    assert_eq!(kept.len(), 1, "{kept:?}");
    assert!(
        matches!(kept[0].outcome, CommandOutcome::Accepted { .. }),
        "{kept:?}"
    );
    // No phantom: no settlement journaled on either peer, and the
    // quiesced image never minted the point change.
    assert_eq!(settlements_of(&active, &write), vec![]);
    assert_eq!(settlements_of(&standby, &write), vec![]);
    assert_eq!(
        image_value(&active.snapshot().unwrap(), HELD),
        Value::Bool(false)
    );

    // The switchover completes on the line's own terms: the standby's
    // promote-boundary final sync carries the still-offered command
    // and settles it `applied` on the live run; the demoted peer's
    // reconvergence adopts the same verdict — the one terminal
    // outcome, journaled once on each peer, never a `superseded`
    // beside it.
    assert_eq!(standby.promote().unwrap().role, Role::Promoting);
    standby.advance(1).unwrap();
    assert_eq!(standby.role().unwrap().role, Role::Active);
    active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
    assert_eq!(active.receipts().unwrap(), standby.receipts().unwrap());
    assert_eq!(
        image_value(&active.snapshot().unwrap(), HELD),
        Value::Bool(true)
    );
    let settled = active.receipts().unwrap();
    assert_eq!(settled.len(), 1, "{settled:?}");
    assert!(
        matches!(settled[0].outcome, CommandOutcome::Applied { .. }),
        "{settled:?}"
    );
    assert_eq!(
        settlements_of(&active, &write),
        settlements_of(&standby, &write),
    );
    assert_eq!(settlements_of(&active, &write).len(), 1);
    assert!(matches!(
        settlements_of(&active, &write)[0].1,
        CommandOutcome::Applied { .. }
    ));

    let _ = std::fs::remove_dir_all(&dir);
}

/// QA finding `stale-checkpoint-resurrects-receipted-unforce` (#639):
/// the driven-pair reproduction. A forces a point, then demotes with
/// its checkpoint fetch severed — no `--peer`, the announced-fallback
/// analog — so its served image freezes on the force. B promotes,
/// unforces the point (applied, journaled), and restarts onto its own
/// persisted state; its first tracking pull then adopts A's staler
/// checkpoint, whose force set predates the release. The applied
/// `unforce` receipt in B's own log is durable truth: the force must
/// not re-stand, no `Substituted` quality may return, and any
/// force-set change the adoption did author must journal a receipt
/// naming the adopting source.
#[test]
fn a_receipted_unforce_survives_the_restarted_standbys_stale_pull() {
    let dir = std::env::temp_dir().join(format!("dcs-stale-unforce-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(&dir, "pair.json", pair_plant.addr);

    // The tank loop's writable field `In` — the reproduction's forced
    // point.
    const FORCED: PointId = PointId(11);
    let force = Command::ForcePoint {
        point: FORCED,
        kind: ValueKind::Float,
        value: Value::Float(5.0),
    };
    let unforce = Command::UnforcePoint { point: FORCED };
    let substituted = || Quality::Uncertain(QualityReason::Substituted);

    let a_state = dir.join("a.state");
    let a_journal = dir.join("a.journal");
    let b_state = dir.join("b.state");
    let b_journal = dir.join("b.journal");
    let persistent = |state: &Path, journal: &Path| {
        vec![
            "--state-file".to_string(),
            state.to_str().unwrap().to_string(),
            "--journal-file".to_string(),
            journal.to_str().unwrap().to_string(),
        ]
    };

    // A is launched active without `--peer`: demoted, it tracks
    // nothing — the severed fetch — so the checkpoint it keeps
    // serving is the pre-release image.
    let a_process = spawn_controller(&pair_model, &persistent(&a_state, &a_journal), DT);
    let mut b_args = vec!["--standby".to_string(), a_process.addr.to_string()];
    b_args.extend(persistent(&b_state, &b_journal));
    let mut b_process = spawn_controller(&pair_model, &b_args, DT);
    let a = MonitorClient::new(a_process.addr);
    let b = MonitorClient::new(b_process.addr);

    // Converge the pair, then force the point on A and let B adopt it.
    for _ in 0..N {
        b.advance(1).unwrap();
        a.advance(1).unwrap();
    }
    let receipt = a.command(&force).unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "{receipt:?}"
    );
    a.advance(1).unwrap();
    let adopted = b.advance(1).unwrap();
    assert!(
        adopted.forces.iter().any(|forced| forced.point == FORCED),
        "the standby must adopt the standing force: {:?}",
        adopted.forces
    );
    assert_eq!(image_sample(&adopted, FORCED).quality, substituted());

    // The severed switchover: A demotes and its served checkpoint
    // freezes — it never observes the release. B promotes and owns
    // the field from its next scan.
    assert_eq!(a.demote().unwrap().role, Role::Demoting);
    assert_eq!(b.promote().unwrap().role, Role::Promoting);
    b.advance(1).unwrap();
    assert_eq!(b.role().unwrap().role, Role::Active);

    // The release applies on B and journals there — the durable truth
    // the restart must not lose.
    let receipt = b.command(&unforce).unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "{receipt:?}"
    );
    let released = b.advance(1).unwrap();
    assert!(
        released.forces.is_empty(),
        "the release must lift the force: {:?}",
        released.forces
    );
    assert!(image_sample(&released, FORCED).quality.is_good());
    let settled = settlements_of(&b, &unforce);
    assert_eq!(settled.len(), 1, "{settled:?}");
    assert!(
        matches!(settled[0].1, CommandOutcome::Applied { .. }),
        "the unforce must journal applied: {settled:?}"
    );

    // The restart: B resumes its persisted run — receipt log and
    // released force set — then tracks A's stale image on its next
    // driven scan.
    kill(&mut b_process);
    let (b_process, preamble) = spawn_controller_logged(&pair_model, &b_args, DT);
    assert!(
        preamble
            .iter()
            .any(|line| line.starts_with("resumed from state file")),
        "the restart must resume B's persisted run: {preamble:?}"
    );
    let b = MonitorClient::new(b_process.addr);

    // The defect's window: the tracking pull lands the stale image
    // and the scans that follow it are where the `Substituted`
    // quality silently returned. With the receipted release
    // re-asserted over the adopted force set, neither happens.
    for _ in 0..3 {
        let snap = b.advance(1).unwrap();
        assert!(
            snap.forces.is_empty(),
            "the receipted release is durable truth — the stale \
             checkpoint must not resurrect the force: {:?}",
            snap.forces
        );
        assert_ne!(
            image_sample(&snap, FORCED).quality,
            substituted(),
            "substituted quality must not return for {FORCED:?}"
        );
    }
    // The reconvergence verdict: A demoted sourceless stamps its
    // served checkpoint ownerless, so the converged verdict on the
    // restarted pull is `Orphaned` with the alignment the pulls land
    // — the ownerless-stream semantics, not the `Tracking` verdict
    // the finding predates. Either proves the re-adoption converges
    // on A's image while the assertions above prove the release
    // survives it.
    let sync = b.role().unwrap().sync;
    assert!(
        matches!(
            sync,
            Some(StandbySync::Tracking { .. }) | Some(StandbySync::Orphaned { .. })
        ),
        "the restarted standby must be tracking A"
    );

    // The audit fallback the finding names: a force change the merged
    // receipt log cannot explain journals an `Applied` receipt whose
    // actor names the adopting checkpoint. The fixed path leaves none
    // — every post-restart force settlement on B must either not
    // exist or carry that attribution.
    let journal = b.journal(0).unwrap();
    let last_boundary = journal
        .iter()
        .rposition(|entry| matches!(entry.event, JournalEvent::RunBoundary { .. }))
        .expect("the restart journals a run boundary");
    for entry in &journal[last_boundary..] {
        if let JournalEvent::CommandSettled { receipt } = &entry.event
            && matches!(
                &receipt.command,
                Command::ForcePoint { point, .. } | Command::UnforcePoint { point }
                    if *point == FORCED
            )
        {
            assert!(
                receipt
                    .actor
                    .as_deref()
                    .is_some_and(|actor| actor.starts_with("checkpoint")),
                "a post-restart force change must journal naming the \
                 adopting source: {receipt:?}"
            );
        }
    }
    // And the release itself survives the restart in the durable
    // record — replayed, not re-settled.
    assert_eq!(
        settled_receipts(&journal)
            .iter()
            .filter(|receipt| receipt.command == unforce)
            .count(),
        1,
        "the unforce must appear exactly once in B's durable journal: {journal:?}"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
