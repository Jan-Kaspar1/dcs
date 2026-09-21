//! The restart-as-active incumbent consult — the
//! `restart-as-active-stale-checkpoint-rollback` finding's acceptance
//! rig: a relaunched launched-active must consult the promoted peer's
//! checkpoint stream before the startup claim and adopt the incumbent's
//! newer line, instead of seizing the field with its stale persisted
//! checkpoint and silently rolling back every unit of run state the
//! incumbent accumulated across the gap.
//!
//! The rig mirrors the QA reproduction on the shared simulated plant:
//! the launched active runs `--state-file`/`--journal-file` and the
//! standby `--standby <a>` under `--driven` pacing. The pair converges,
//! the active demotes and the standby promotes — the demoted peer now
//! tracking its successor through the announced follow-peer source —
//! the duty container dies, the incumbent takes a receipted tune and a
//! force, and the duty container restarts. The consult must pull the
//! incumbent's served checkpoint before the preemptive claim: its tick
//! is strictly newer, so the restartee adopts it — tune, force, and
//! receipted-command audit all surviving the takeover — and journals
//! the `restart_consult` outcome on the new line.

use dcs_core::{
    Command, CommandOutcome, IoDriver, JournalEvent, PointId, RestartConsultOutcome, Role,
    StandbySync, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::path::Path;

mod support;

use support::{
    SimTcp, controller_model, image_value, kill, spawn_controller, spawn_controller_logged,
    spawn_plant,
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
/// The controller-side model source — the shared tank-loop model whose
/// devices [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// Ticks the pair runs converged before the switchover.
const N: u64 = 10;
/// Ticks the demoted peer tracks its successor before the stop — each
/// persisting a checkpoint stamped with the announced tracking source.
const K: u64 = 3;
/// The throwaway token the pre-spawn setpoint seed claims under.
const SEED: u64 = 499_910;
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// The tuned parameter the incumbent's receipted command drives — the
/// rollback signature the finding captured: post-takeover served
/// pre-tune parameter values.
const KP: f64 = 9.9;
/// The forced value the incumbent's receipted `force_point` pins the
/// writable setpoint input to.
const FORCED: f64 = 60.0;

#[test]
fn a_stale_restart_as_active_adopts_the_incumbents_checkpoint_before_claiming() {
    let dir = std::env::temp_dir().join(format!("dcs-restart-consult-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("duty.state.json");
    let journal_file = dir.join("duty.journal.jsonl");

    let pair_plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let pair_model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        pair_plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences this attachment from boot, so every
    // later access is a read.
    let field = RemoteDriver::connect(pair_plant.addr).unwrap();
    field.ensure_writer(SEED).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    field.release_writer().unwrap();

    // The pair: the duty container launched active with its persisted
    // run state and journal file, the standby tracking it.
    let mut duty = spawn_controller(
        &pair_model,
        &[
            "--state-file".to_string(),
            state_file.to_str().unwrap().to_string(),
            "--journal-file".to_string(),
            journal_file.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let duty_args = [
        "--state-file".to_string(),
        state_file.to_str().unwrap().to_string(),
        "--journal-file".to_string(),
        journal_file.to_str().unwrap().to_string(),
    ];
    let peer = spawn_controller(
        &pair_model,
        &["--standby".to_string(), duty.addr.to_string()],
        DT,
    );
    let duty_client = MonitorClient::new(duty.addr);
    let peer_client = MonitorClient::new(peer.addr);

    // Converge: the standby's pulls announce its monitor address to the
    // active — the announced follow-peer source the demotion later
    // tracks and the persisted checkpoint stamps.
    for _ in 0..N {
        peer_client.advance(1).unwrap();
        duty_client.advance(1).unwrap();
    }
    assert!(
        matches!(
            peer_client.role().unwrap().sync,
            Some(StandbySync::Tracking { .. })
        ),
        "the standby never converged: {:?}",
        peer_client.role().unwrap()
    );

    // The switchover: the duty container demotes — tracking the peer
    // its pulls announced — and the converged standby takes the field.
    duty_client.demote().unwrap();
    peer_client.promote().unwrap();
    peer_client.advance(1).unwrap();
    for _ in 0..K {
        duty_client.advance(1).unwrap();
        peer_client.advance(1).unwrap();
    }
    assert_eq!(peer_client.role().unwrap().role, Role::Active);
    assert_eq!(duty_client.role().unwrap().role, Role::Standby);
    // The demoted peer's persisted checkpoints name the incumbent's
    // checkpoint stream — the source the restart consult will dial.
    assert_eq!(
        duty_client.checkpoint().unwrap().tracking_source,
        Some(peer.addr),
        "the demoted peer's checkpoint must stamp the announced tracking source"
    );
    let stale_tick = duty_client.checkpoint().unwrap().tick;

    // The duty container dies; its last persisted checkpoint is now
    // stale the moment the incumbent scans again.
    kill(&mut duty);

    // The incumbent's gap: a receipted tune and a force — exactly the
    // durable run state the finding watched roll back — plus ordinary
    // scans so its stream is strictly newer than the persisted one.
    let tune = Command::SetParameter {
        component: "pid:2".to_string(),
        name: "kp".to_string(),
        value: Value::Float(KP),
    };
    let force = Command::ForcePoint {
        point: SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(FORCED),
    };
    for command in [&tune, &force] {
        let receipt = peer_client.command(command).unwrap();
        assert!(
            matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
            "the incumbent must receipt the command accepted: {receipt:?}"
        );
    }
    peer_client.advance(1).unwrap();
    peer_client.advance(1).unwrap();
    let incumbent = peer_client.checkpoint().unwrap();
    assert!(
        incumbent.tick > stale_tick,
        "the incumbent's stream must be newer than the persisted checkpoint"
    );
    assert_eq!(
        incumbent.components["pid:2"].get("kp"),
        Some(Value::Float(KP))
    );
    assert_eq!(incumbent.forces[&SETPOINT], Value::Float(FORCED));

    // The restart: the stale persisted checkpoint resumes, the consult
    // pulls the incumbent's stream — strictly newer — and the run
    // adopts it before the startup claim.
    let (duty, preamble) = spawn_controller_logged(&pair_model, &duty_args, DT);
    let duty_client = MonitorClient::new(duty.addr);
    assert!(
        preamble
            .iter()
            .any(|line| line.starts_with("resumed from state file")),
        "the restart must resume the persisted checkpoint: {preamble:?}"
    );
    assert!(
        preamble
            .iter()
            .any(|line| line.starts_with("restart consult")),
        "the restart must journal its incumbent consult: {preamble:?}"
    );

    // The claim landed on the adopted line: the restarted peer owns the
    // field at the incumbent's tick — tune and force carried, not the
    // stale checkpoint's pre-tune state.
    assert_eq!(duty_client.role().unwrap().role, Role::Active);
    let adopted = duty_client.checkpoint().unwrap();
    assert_eq!(
        adopted.tick, incumbent.tick,
        "the restartee must resume at the adopted checkpoint's tick"
    );
    assert_eq!(
        adopted.components["pid:2"].get("kp"),
        Some(Value::Float(KP)),
        "the receipted tune must survive the takeover — the finding's \
         rollback signature"
    );
    assert_eq!(adopted.forces[&SETPOINT], Value::Float(FORCED));

    // The durable record: the claiming line's own journal carries the
    // consult — its source the incumbent's monitor, its outcome the
    // adopted supersession — behind the restarted run's boundary.
    let journal = duty_client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::RunBoundary { run } if *run == 2
        )),
        "the restarted run must journal its boundary: {journal:?}"
    );
    let consult = journal
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::RestartConsult { source, outcome } => Some((source, outcome)),
            _ => None,
        })
        .unwrap_or_else(|| panic!("the claiming line must journal the consult: {journal:?}"));
    assert_eq!(consult.0, &peer.addr.to_string());
    assert_eq!(
        consult.1,
        &RestartConsultOutcome::Adopted {
            superseded_at: Some(stale_tick),
            resumed_at: incumbent.tick,
        }
    );
    // The adopted applied receipts are the line's own — the consult's
    // adoption must not re-journal them as this run's settlements.
    assert!(
        !journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt } if receipt.command == tune
        )),
        "the adopted applied tune must not re-journal as settled: {journal:?}"
    );

    // The field rolled forward: the restartee's first scan writes the
    // post-tune outputs and serves the forced input — no pre-tune
    // values surface anywhere.
    let scanned = duty_client.advance(1).unwrap();
    assert_eq!(image_value(&scanned, SETPOINT), Value::Float(FORCED));
    assert_eq!(
        field.read(VALVE).unwrap().value,
        image_value(&scanned, VALVE)
    );
    assert_eq!(
        duty_client.checkpoint().unwrap().components["pid:2"].get("kp"),
        Some(Value::Float(KP))
    );

    // The incumbent's own switchover audit is unchanged: its next scan
    // meets the restartee's claim, the fence demotes it, and its
    // journal carries the claim loss.
    peer_client.advance(1).unwrap();
    let peer_journal = peer_client.journal(0).unwrap();
    assert!(
        peer_journal.iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { point } if point == VALVE
        )),
        "the fenced incumbent must journal its claim loss: {peer_journal:?}"
    );
    assert_eq!(peer_client.role().unwrap().role, Role::Demoting);
    peer_client.advance(1).unwrap();
    assert_eq!(peer_client.role().unwrap().role, Role::Standby);

    let _ = std::fs::remove_dir_all(&dir);
}
