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
//! Two legs, because the merged baseline answers the seize itself:
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
//! 2. **The granted-claim form** — the same restart once the incumbent
//!    stands down and releases the claim, which is the only shape in
//!    which a launched-active restart can still take the field. The
//!    claim must land on the *adopted* line: the tuned parameter and
//!    the force the gap accumulated survive the takeover, and the
//!    adopted `applied` receipts are not re-journaled as this run's own
//!    settlements.

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
/// not share fixtures.
fn stalled_pair(tag: &str) -> Stalled {
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
    let pair = stalled_pair("reproduction");

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

#[test]
fn a_restart_never_adopts_a_trackers_stream_even_when_its_claim_is_granted() {
    let pair = stalled_pair("granted");

    // The incumbent stands down: its claim releases, so this is the
    // only shape in which a launched-active restart can still take the
    // field. The peer is now a *tracker* of this same line — its stream
    // leads the stale persisted checkpoint only because it scanned on
    // past the duty container's death with its own writes quiesced.
    pair.incumbent.demote().unwrap();
    pair.incumbent.advance(1).unwrap();
    assert_eq!(pair.incumbent.role().unwrap().role, Role::Standby);
    let tracker = pair.incumbent.checkpoint().unwrap();
    assert_eq!(
        tracker.source_owns_field,
        Some(false),
        "the stood-down peer must declare it does not own the field"
    );
    assert!(
        tracker.tick > pair.stale_tick,
        "the tracker's stream must lead the stale checkpoint — the \
         window a stale seize would have rolled the tuned line back on"
    );

    // The restart: the persisted checkpoint resumes, the consult finds
    // a strictly newer stream and declines it as a tracker's, and the
    // claim lands on the resumed run's own line — its persisted tick,
    // not the tracker's local scans (decision 26's own-tick-ahead rule).
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
            .any(|line| line.starts_with("restart consult")),
        "the restart must run the incumbent consult: {preamble:?}"
    );
    assert!(
        duty_client.role().unwrap().role == Role::Active,
        "the unheld field is this run's to claim: {:?}",
        duty_client.role().unwrap()
    );
    let resumed = duty_client.checkpoint().unwrap();
    assert_eq!(
        resumed.tick, pair.stale_tick,
        "the restart must own the field on its own resumed tick, never a \
         tracker's"
    );

    // The consult's own audit names the decline: the restart neither
    // adopted a tracker's stream nor passed unexamined.
    let journal = duty_client.journal(0).unwrap();
    let (source, outcome) = consult_entry(&journal);
    assert_eq!(source, &pair.incumbent_addr.to_string());
    assert!(
        matches!(outcome, RestartConsultOutcome::Unadopted { detail } if detail.contains("does not own the field")),
        "the consult must journal why it declined the tracker's stream: {outcome:?}"
    );

    let _ = std::fs::remove_dir_all(&pair._dir);
}
