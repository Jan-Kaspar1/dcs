//! The restart-as-active incumbent consult — the
//! `restart-as-active-stale-checkpoint-rollback` finding's acceptance
//! rig: a relaunched launched-active must consult the promoted peer's
//! checkpoint stream before its startup claim, so the claim lands on
//! the incumbent's live line instead of the restartee's stale
//! persisted checkpoint — which is what silently reverted every unit of
//! run state the incumbent accumulated across the gap.
//!
//! The rig mirrors the QA reproduction on the shared simulated plant:
//! the launched active runs `--state-file`/`--journal-file` and the
//! standby `--standby <a>` under `--driven` pacing. The pair converges,
//! the active demotes and the standby promotes — the demoted peer now
//! tracking its successor through the announced follow-peer source —
//! the duty container dies, the incumbent takes a receipted tune and a
//! force, and the duty container restarts.
//!
//! Three legs, because the merged baseline answers the seize itself in
//! two of the three shapes:
//!
//! 1. **The reproduction, verbatim** — the duty container restarts with
//!    the same arguments it was launched with. The consult must pull
//!    the incumbent's strictly newer checkpoint and adopt it *before*
//!    the claim, so the relaunched run's own state is the incumbent's
//!    tuned line; the born-active contract's conditional startup grant
//!    then refuses the live incumbent's claim — decision 89's answer —
//!    so the run exits naming the `--standby` remedy instead of
//!    seizing. Either way the field never serves the pre-tune values
//!    again, and the consult is on the durable record.
//! 2. **The demote-then-restart shape** — QA finding
//!    `stale-checkpoint-rollback-via-demote-then-restart`: the same
//!    restart one step later, once the incumbent has stood down and
//!    released the claim. Nothing refuses the claim there, so the
//!    consult is the only thing standing between the restartee's stale
//!    checkpoint and the field, and the peer's own non-ownership stamp
//!    must not read as licence to seize on it. The reclaimed field must
//!    serve the tuned parameter and the force the gap accumulated.
//! 3. **The quiesced tracker** — the same rig with no gap commands, so
//!    the peer that stood down carries nothing the resumed baseline
//!    lacks. A standby admits no command of its own, so its receipt log
//!    converges to the baseline's: there is nothing of the line's in
//!    its document, and the consult must still decline it and let the
//!    restart resume its own tick axis.

use dcs_core::{
    Command, CommandOutcome, IoDriver, JournalEvent, PointId, RestartConsultOutcome, Role,
    StandbySync, Value, ValueKind,
};
use dcs_monitor::{MonitorClient, read_journal_file};
use dcs_sim_net::RemoteDriver;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, Stdio};
use std::time::Duration;

mod support;

use support::{
    PAIR_TOKEN, SimTcp, controller_model, image_value, kill, spawn_controller,
    spawn_controller_logged, spawn_plant, workspace_binary,
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

/// The converged pair at the moment the finding's reproduction stops:
/// the duty container dead, its persisted checkpoint stale, and the
/// promoted incumbent carrying a receipted tune and a force no later
/// restart may discard.
struct Stalled {
    /// The pair's shared plant process — held so the field stays up
    /// for the whole rig, not dropped back at the helper's return.
    _plant: support::Spawned,
    /// The pair's shared plant — the field the takeover writes.
    field: RemoteDriver,
    /// The controller-side model both peers run.
    model: PathBuf,
    /// The promoted incumbent's process — held so the peer the
    /// consult dials and the restart races stays up for the whole rig.
    _incumbent: support::Spawned,
    /// The promoted incumbent — the peer the restart consults.
    incumbent: MonitorClient,
    /// The incumbent's monitor address — the `source` the consult's
    /// journal entry names.
    incumbent_addr: std::net::SocketAddr,
    /// The stopped duty container's launch arguments, so the restart
    /// relaunches exactly what the first launch did.
    duty_args: Vec<String>,
    /// The duty container's durable journal file.
    journal_file: PathBuf,
    /// The tick the duty container's persisted checkpoint stood at when
    /// it died — the state the finding saw the restart roll back to.
    stale_tick: dcs_core::Tick,
    /// The incumbent's checkpoint across the gap: the tuned parameter,
    /// the force, and the strictly newer tick the consult must adopt.
    incumbent_tick: dcs_core::Tick,
    /// Keeps the scratch directory alive for the legs' assertions.
    _dir: PathBuf,
}

/// Brings the pair up to the finding's reproduction point and returns
/// it. `tag` names the scratch directory so two legs in one binary do
/// not share fixtures; `gap` stages the incumbent's receipted tune and
/// force — the run state a silent revert discards — or leaves the gap
/// empty, so the stood-down peer carries nothing the restartee's
/// baseline lacks and the consult's tracker refusal is what stands.
fn stalled_pair(tag: &str, gap: bool) -> Stalled {
    let dir =
        std::env::temp_dir().join(format!("dcs-restart-consult-{tag}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("duty.state.json");
    let journal_file = dir.join("duty.journal.jsonl");

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::PerDevice,
    )
    .0;

    // The setpoint lands before the controllers spawn: the launched
    // active's startup claim fences this attachment from boot, so every
    // later access is a read.
    let field = RemoteDriver::connect(plant.addr).unwrap();
    field.ensure_writer(SEED).unwrap();
    field.write(SETPOINT, Value::Float(50.0)).unwrap();
    field.release_writer().unwrap();

    // The pair: the duty container launched active with its persisted
    // run state and journal file, the standby tracking it.
    let duty_args = vec![
        "--state-file".to_string(),
        state_file.to_str().unwrap().to_string(),
        "--journal-file".to_string(),
        journal_file.to_str().unwrap().to_string(),
    ];
    let mut duty = spawn_controller(&model, &duty_args, DT);
    let peer = spawn_controller(
        &model,
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
    if gap {
        for command in [&tune, &force] {
            let receipt = peer_client.command(command).unwrap();
            assert!(
                matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
                "the incumbent must receipt the command accepted: {receipt:?}"
            );
        }
    }
    peer_client.advance(1).unwrap();
    peer_client.advance(1).unwrap();
    let incumbent = peer_client.checkpoint().unwrap();
    assert!(
        incumbent.tick > stale_tick,
        "the incumbent's stream must be newer than the persisted checkpoint"
    );
    if gap {
        assert_eq!(
            incumbent.components["pid:2"].get("kp"),
            Some(Value::Float(KP))
        );
        assert_eq!(incumbent.forces[&SETPOINT], Value::Float(FORCED));
        assert!(
            incumbent.command_admission.attempts > stale_attempts(&state_file),
            "the gap's receipts must stand past the restartee's own admission high-water"
        );
    }
    assert_eq!(incumbent.tracking_source, Some(duty.addr));

    Stalled {
        _plant: plant,
        incumbent_addr: peer.addr,
        _incumbent: peer,
        field,
        model,
        incumbent: peer_client,
        duty_args,
        journal_file,
        stale_tick,
        incumbent_tick: incumbent.tick,
        _dir: dir,
    }
}

/// The restartee's own admission high-water, read off its persisted
/// checkpoint — the settled-command count the consulted peer's document
/// must stand ahead of for its state to be the line's to carry.
fn stale_attempts(state_file: &Path) -> u64 {
    let body = std::fs::read(state_file).expect("the duty container persisted its checkpoint");
    let checkpoint: dcs_runtime::Checkpoint =
        serde_json::from_slice(&body).expect("the state file holds a checkpoint");
    checkpoint.command_admission.attempts
}

/// The consult entry the restart's durable record carries — the
/// claiming line's own audit of what the pre-claim pull found.
fn consult_entry(entries: &[dcs_core::JournalEntry]) -> (&String, &RestartConsultOutcome) {
    entries
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::RestartConsult { source, outcome } => Some((source, outcome)),
            _ => None,
        })
        .unwrap_or_else(|| panic!("the claiming line must journal the consult: {entries:?}"))
}

#[test]
fn the_reproduction_restart_adopts_the_incumbents_line_and_never_rolls_the_field_back() {
    let pair = stalled_pair("reproduction", true);

    // The reproduction, verbatim: the duty container's container comes
    // back with the arguments it was launched with. The consult pulls
    // the incumbent's strictly newer checkpoint and adopts it, and the
    // born-active contract then refuses the live incumbent's claim —
    // so this run never writes and the field keeps the tuned line.
    let mut launch = vec![pair.model.to_str().unwrap().to_string()];
    launch.extend(pair.duty_args.iter().cloned());
    launch.extend([
        "--pair-token".to_string(),
        PAIR_TOKEN.to_string(),
        "--listen".to_string(),
        "127.0.0.1:0".to_string(),
        "--driven".to_string(),
        "--dt".to_string(),
        DT.to_string(),
    ]);
    let mut child = Process::new(workspace_binary("dcs-controller"))
        .args(&launch)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the duty container restarts");
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "the relaunched duty container neither served nor refused the claim"
        );
        std::thread::sleep(Duration::from_millis(10));
    };
    let mut stderr = String::new();
    {
        use std::io::Read;
        child
            .stderr
            .take()
            .unwrap()
            .read_to_string(&mut stderr)
            .unwrap();
    }
    assert!(
        !status.success(),
        "the restart must not take the live incumbent's field: {stderr}"
    );
    assert!(
        stderr.contains("restart consult") && stderr.contains("adopted the incumbent's checkpoint"),
        "the consult must adopt before the claim: {stderr}"
    );
    assert!(
        stderr.contains("live peer holds the field's write-ownership claim"),
        "the startup claim must refuse the live incumbent: {stderr}"
    );
    assert!(
        stderr.contains("--standby"),
        "the refusal must name the contract's own remedy: {stderr}"
    );

    // The durable record carries the consult on the claiming line,
    // behind the restarted run's boundary — the finding's other half:
    // the takeover was audited nowhere at all.
    let entries = read_journal_file(&pair.journal_file).unwrap().entries;
    assert!(
        entries
            .iter()
            .any(|entry| matches!(&entry.event, JournalEvent::RunBoundary { run } if *run == 2)),
        "the restarted run must journal its boundary: {entries:?}"
    );
    let (source, outcome) = consult_entry(&entries);
    assert_eq!(source, &pair.incumbent_addr.to_string());
    assert_eq!(
        outcome,
        &RestartConsultOutcome::Adopted {
            superseded_at: Some(pair.stale_tick),
            resumed_at: pair.incumbent_tick,
        }
    );
    // The adopted applied receipts are the line's own — the adoption
    // must not re-journal them as this run's settlements, which is how
    // the finding's audit came to assert `applied` for a tune whose
    // effects this run had reverted.
    let tune = Command::SetParameter {
        component: "pid:2".to_string(),
        name: "kp".to_string(),
        value: Value::Float(KP),
    };
    assert!(
        !entries.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt } if receipt.command == tune
        )),
        "the adopted applied tune must not re-journal as settled: {entries:?}"
    );

    // The field never rolled back: the incumbent still owns it and
    // still serves the tuned parameter and the force.
    assert_eq!(pair.incumbent.role().unwrap().role, Role::Active);
    let served = pair.incumbent.checkpoint().unwrap();
    assert_eq!(served.components["pid:2"].get("kp"), Some(Value::Float(KP)));
    assert_eq!(served.forces[&SETPOINT], Value::Float(FORCED));

    // The field is still the incumbent's to write: the next scan's
    // output is what the plant actually carries, and the forced input
    // is still in force — the restartee's stale line never reached the
    // field for even one scan.
    let scanned = pair.incumbent.advance(1).unwrap();
    assert_eq!(image_value(&scanned, SETPOINT), Value::Float(FORCED));
    assert_eq!(
        pair.field.read(VALVE).unwrap().value,
        image_value(&scanned, VALVE)
    );

    let _ = std::fs::remove_dir_all(&pair._dir);
}

/// The demote-then-restart shape — QA finding
/// `stale-checkpoint-rollback-via-demote-then-restart`, the same
/// applied-state loss one step past #735's closed window. The
/// incumbent stands down, so its claim releases and nothing refuses the
/// ex-owner's startup claim any more: the consult is the only gate
/// between the restartee's stale pre-tune checkpoint and the field. The
/// reclaimed run must serve the tuned parameter and the force the gap
/// accumulated — carrying the stood-down peer's settled line across the
/// takeover — where the finding's revision resumed the stale checkpoint,
/// claimed the field, and served pre-tune values while its own journal
/// recorded nothing about the divergence.
#[test]
fn a_restart_after_the_incumbent_stands_down_carries_its_settled_line() {
    let pair = stalled_pair("demoted", true);

    // The incumbent stands down: its claim releases, and the ex-owner
    // comes back with the field genuinely free to take.
    pair.incumbent.demote().unwrap();
    pair.incumbent.advance(1).unwrap();
    assert_eq!(pair.incumbent.role().unwrap().role, Role::Standby);
    let stood_down = pair.incumbent.checkpoint().unwrap();
    assert_eq!(
        stood_down.source_owns_field,
        Some(false),
        "the stood-down peer declares it writes nothing — the stamp the \
         consult must read past, on this peer's evidence"
    );
    assert!(
        stood_down.tick > pair.stale_tick,
        "the stood-down peer's stream leads the stale checkpoint — the \
         line whose receipted state a stale seize would have reverted"
    );
    // Where the line stood when the restartee dials it — the position
    // the adoption must land on, ahead of the stale baseline and behind
    // nothing.
    let stood_down_at = stood_down.tick;

    // The restart: the persisted checkpoint resumes, the consult finds
    // the same line strictly newer and carrying the gap's settled
    // submissions, and adopts it in place — so the claim lands on the
    // tuned line rather than the pre-tune one.
    let (duty, preamble) = spawn_controller_logged(&pair.model, &pair.duty_args, DT);
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
            .any(|line| line.contains("adopted the incumbent's checkpoint")),
        "the restart must adopt the stood-down peer's line before it \
         claims: {preamble:?}"
    );
    assert!(
        preamble
            .iter()
            .any(|line| line.contains("settled submissions past this run's")),
        "the adoption must name the receipted state it carried, not \
         revert it silently: {preamble:?}"
    );
    assert_eq!(
        duty_client.role().unwrap().role,
        Role::Active,
        "the released field is this run's to claim: {:?}",
        duty_client.role().unwrap()
    );

    // The reclaimed field serves the gap's run state: the tuned
    // parameter and the force, on the adopted line's own tick.
    let reclaimed = duty_client.checkpoint().unwrap();
    assert_eq!(reclaimed.tick, stood_down_at);
    assert_eq!(
        reclaimed.components["pid:2"].get("kp"),
        Some(Value::Float(KP)),
        "the reclaimed field must serve the tuned parameter, not the \
         pre-tune value the stale checkpoint held"
    );
    assert_eq!(
        reclaimed.forces[&SETPOINT],
        Value::Float(FORCED),
        "the reclaimed field must keep the gap's force"
    );
    let snapshot = duty_client.snapshot().unwrap();
    let tuned = snapshot
        .parameters
        .iter()
        .find(|parameters| parameters.name == "pid:2")
        .and_then(|parameters| parameters.values.get("kp"));
    assert_eq!(
        tuned,
        Some(&Value::Float(KP)),
        "the operator surface must report the tuned gain: {tuned:?}"
    );

    // The takeover is on the durable record: the consult names the peer
    // it asked and the baseline its adoption superseded, and the
    // adopted `applied` receipts are the line's own rather than this
    // run's settlements.
    let journal = duty_client.journal(0).unwrap();
    let (source, outcome) = consult_entry(&journal);
    assert_eq!(source, &pair.incumbent_addr.to_string());
    assert_eq!(
        outcome,
        &RestartConsultOutcome::Adopted {
            superseded_at: Some(pair.stale_tick),
            resumed_at: stood_down_at,
        }
    );
    let tune = Command::SetParameter {
        component: "pid:2".to_string(),
        name: "kp".to_string(),
        value: Value::Float(KP),
    };
    assert!(
        !journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::CommandSettled { receipt } if receipt.command == tune
        )),
        "the adopted applied tune must not re-journal as this run's \
         settlement: {journal:?}"
    );

    // And the field keeps running the tuned line under the reclaim: the
    // forced input stands and the next scan's output is what the plant
    // carries.
    let scanned = duty_client.advance(1).unwrap();
    assert_eq!(image_value(&scanned, SETPOINT), Value::Float(FORCED));
    assert_eq!(
        pair.field.read(VALVE).unwrap().value,
        image_value(&scanned, VALVE)
    );

    let _ = std::fs::remove_dir_all(&pair._dir);
}

/// The refusal the fix must not widen: the same rig with no gap
/// commands. The peer that stood down then admits nothing of its own —
/// a standby's receipt log converges by adoption — so its document
/// carries nothing the resumed baseline lacks, and adopting it would
/// import a tracker's local ticks for no state at all. The consult
/// declines it by name and the restart resumes its own line.
#[test]
fn a_restart_still_declines_a_stood_down_peer_carrying_nothing_new() {
    let pair = stalled_pair("tracker", false);

    pair.incumbent.demote().unwrap();
    pair.incumbent.advance(1).unwrap();
    let stood_down = pair.incumbent.checkpoint().unwrap();
    assert_eq!(stood_down.source_owns_field, Some(false));
    assert!(
        stood_down.tick > pair.stale_tick,
        "the tracker's stream leads the persisted checkpoint only \
         through its own quiesced scans"
    );
    assert!(
        stood_down.command_admission.attempts
            <= stale_attempts(
                &Path::new(&pair.model)
                    .parent()
                    .unwrap()
                    .join("duty.state.json")
            ),
        "a quiesced tracker settles nothing of its own — the evidence \
         the consult's continuation proof reads"
    );

    let (duty, preamble) = spawn_controller_logged(&pair.model, &pair.duty_args, DT);
    let duty_client = MonitorClient::new(duty.addr);
    assert!(
        preamble
            .iter()
            .any(|line| line.starts_with("restart consult")),
        "the restart must run the incumbent consult: {preamble:?}"
    );
    assert!(
        duty_client.role().unwrap().role == Role::Active,
        "the unheld field is this run's to claim: {:?}",
        duty_client.role().unwrap()
    );
    assert_eq!(
        duty_client.checkpoint().unwrap().tick,
        pair.stale_tick,
        "the restart must own the field on its own resumed tick, never a \
         tracker's"
    );

    let journal = duty_client.journal(0).unwrap();
    let (source, outcome) = consult_entry(&journal);
    assert_eq!(source, &pair.incumbent_addr.to_string());
    assert!(
        matches!(outcome, RestartConsultOutcome::Unadopted { detail } if detail.contains("does not own the field")),
        "the consult must journal why it declined the tracker's stream: {outcome:?}"
    );

    let _ = std::fs::remove_dir_all(&pair._dir);
}
