//! The born-active startup-failure contract — architecture decision 103
//! (#985, implemented by #1017): every startup-failure class a launched
//! active's deferred startup claim can meet settles to the recorded
//! disposition rather than wedging unreported or dying silently. The two
//! QA findings the contract answers replay here:
//!
//! - `launched-active-boot-requires-reachable-plant`: an unreachable
//!   field at boot is no longer a process exit inside driver assembly —
//!   the run stands pending behind the served standby surface (role
//!   `standby`, sync `unsynchronized`, gate closed, commands refused),
//!   and the conditional startup grant retries on each answered field
//!   contact until a verdict lands.
//! - `startup-claim-refusal-leaves-pair-unpaired`: a live incumbent's
//!   refusal settles the launch onto the declared pair's standby —
//!   tracking the `--peer` it named — or exits the process where no
//!   pair was declared.
//! - `deferred-startup-claim-refusal-strands-unpaired-standby`: the
//!   same refusal verdict landing mid-scan — a pending born-active's
//!   re-issued ask answered at the first contact the thawed field
//!   takes — settles under the identical contract rather than leaving
//!   a peerless standby wedged where the boot-time verdict exits.
//!
//! The rig is the failover harness's shape: a `dcs-plant-server`
//! process owns the shared `tank_loop` plant and the controllers load
//! the same model re-pointed at `sim-tcp`, `--driven` so every scan —
//! and therefore every claim probe and deferred ask — happens inside a
//! `POST /scan` request.

use dcs_core::{
    Command, CommandError, CommandOutcome, FieldClaim, JournalEvent, PointId, Role, StandbySync,
    Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::{ClaimGrant, RemoteDriver};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Command as Process;

mod support;

use support::{
    CONTROLLER, SimTcp, controller_model, kill, listening_on, spawn, spawn_controller,
    spawn_controller_logged, spawn_controller_paced, spawn_plant, workspace_binary,
};

/// The shared plant's model — the dcs-plant tank loop: level raw (10)
/// and setpoint (11) in, valve command (20) out.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop.json"
);
/// The plant-side physics: the raw level lags the valve.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-plant/fixtures/tank_loop_dynamics.json"
);
/// The controller-side model source — the shared tank-loop model whose
/// devices [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// The incumbent's pinned field-ownership token.
const INCUMBENT: u64 = 90;
/// The born-active's pinned field-ownership token.
const LAUNCHED: u64 = 94;

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-born-active-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Serializes a frozen field's `thaw` → `spawn_plant_at` instant: the
/// freed port returns to the kernel's `bind(:0)` pool for that beat,
/// and the parallel tests in this binary each chase ephemeral ports —
/// without the lock one's pick could land on another's just-thawed
/// address.
static PLANT_PORT: std::sync::Mutex<()> = std::sync::Mutex::new(());

/// A port nothing listens on: bound to learn the address, dropped so
/// the plant server can take it later — the unreachable-field endpoint
/// the pending run retries toward.
fn unclaimed_addr() -> std::net::SocketAddr {
    let held = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = held.local_addr().unwrap();
    drop(held);
    addr
}

/// A frozen field — the `docker pause` reproduction: a bound listener
/// that never accepts, so the pending run's connects complete at the
/// kernel while no request is ever answered. The address stays bound
/// through the whole pending phase, so nothing — not even this test
/// binary's own ephemeral binds — can claim the port between the
/// pending launches and the plant's thaw, the race a drop-then-rebind
/// pick would leave open.
struct FrozenField(TcpListener);

impl FrozenField {
    fn bind() -> Self {
        Self(TcpListener::bind("127.0.0.1:0").unwrap())
    }

    fn addr(&self) -> std::net::SocketAddr {
        self.0.local_addr().unwrap()
    }

    /// The field's return: the listener drops — the queued links
    /// reset — freeing the port for the caller's `spawn_plant_at` to
    /// bind. The caller holds [`PLANT_PORT`] across the drop→bind
    /// instant.
    fn thaw(self) -> std::net::SocketAddr {
        let addr = self.addr();
        drop(self.0);
        addr
    }
}

/// A `dcs-plant-server` process bound at `addr` — the late-arriving
/// field the pending launch's deferred claim retries toward.
fn spawn_plant_at(model: &Path, dynamics: &Path, addr: std::net::SocketAddr) -> support::Spawned {
    spawn(
        &workspace_binary("dcs-plant-server"),
        &[
            model.to_str().unwrap().to_string(),
            "--dynamics".to_string(),
            dynamics.to_str().unwrap().to_string(),
            "--listen".to_string(),
            addr.to_string(),
        ],
        listening_on,
    )
}

/// A write command any run can be asked: the valve output — refused or
/// applied, the receipt's outcome is the served evidence.
fn write_valve() -> Command {
    Command::WriteValue {
        point: PointId(20),
        kind: ValueKind::Float,
        value: Value::Float(0.5),
    }
}

/// Asserts `client` answers `POST /command` with the not-active
/// rejection the pending/refused surface owes: a receipt exists — the
/// served evidence — and its reason names the run's standby role.
fn assert_command_refused(client: &MonitorClient) {
    let receipt = client.command(&write_valve()).unwrap();
    let CommandOutcome::Rejected {
        reason: CommandError::NotActive { role, .. },
    } = receipt.outcome
    else {
        panic!("a pending or rejoined standby must refuse commands: {receipt:?}");
    };
    assert_eq!(role, Role::Standby);
}

/// The launch's stand-down is the journaled evidence: one `RoleChanged`
/// entry `active` → `standby` under the field-arbitration origin — the
/// refusal or inconclusive ask the run stood down on.
fn assert_stood_down(client: &MonitorClient) {
    let journal = client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::RoleChanged {
                from: Role::Active,
                to: Role::Standby,
                origin: Some(dcs_core::SwitchOrigin::Fenced),
                ..
            }
        )),
        "the stand-down transition must journal: {journal:?}"
    );
}

/// The unreachable-field-at-boot class: the launched active neither
/// exits nor wedges silent — it serves the monitor pending behind the
/// honest standby surface, refuses commands and issues no writes while
/// the field cannot be asked, and the first answering contact completes
/// the deferred startup: the grant lands, the gate lifts, and the role
/// settles `promoting` → `active` on the next scan.
#[test]
fn an_unreachable_field_stands_the_launch_pending_until_contact_answers() {
    let dir = scratch("unreachable");
    let plant_addr = unclaimed_addr();
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;

    // The finding's reproduction: the model-declared `sim-tcp` field
    // answers nothing at boot — assembly used to die here.
    let launched = spawn_controller(
        &model,
        &["--owner-token".to_string(), LAUNCHED.to_string()],
        DT,
    );
    let client = MonitorClient::new(launched.addr);

    // The pending surface: served, honest, and inert — standby,
    // unsynchronized, no observed claim, commands refused, scans
    // quiesced.
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    assert_command_refused(&client);
    assert_stood_down(&client);

    // Answering nothing still asks nothing: driven scans issue probes
    // the field cannot answer, and the run stays pending — no claim
    // verdict, no role move.
    client.advance(2).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    client.health().unwrap();

    // The field arrives: the first scan to reach it re-issues the
    // deferred conditional grant, which takes the claim — `promoting`
    // reports the granted transition mid-flight, the next scan settles
    // `active` under the lifted gate.
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );
    // The remote kind's re-attach cadence paces the retry — the
    // recorded one-contact-per-interval bound — so the wait is the
    // contract's own cadence, not a grace.
    std::thread::sleep(
        dcs_sim_net::RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200),
    );
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert!(
        matches!(report.role, Role::Promoting | Role::Active),
        "the answered grant must move the run toward active: {report:?}"
    );
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    // The claim the deferred grant took is this run's: a probe joining
    // under its token meets the shared hold — proof the field never
    // saw a write or claim before the verdict.
    let probe = RemoteDriver::connect(plant_addr).unwrap();
    assert_eq!(probe.ensure_writer(LAUNCHED).unwrap(), ClaimGrant::Shared);
    probe.release_writer().unwrap();

    // The completed startup is journaled too: the deferred grant's
    // landing walks `standby` → `promoting` → `active` under the
    // reclaim origin — the conditional ask finally answered.
    let journal = client.journal(0).unwrap();
    for (from, to) in [
        (Role::Standby, Role::Promoting),
        (Role::Promoting, Role::Active),
    ] {
        assert!(
            journal.iter().any(|entry| matches!(
                &entry.event,
                JournalEvent::RoleChanged {
                    from: f,
                    to: t,
                    ..
                } if *f == from && *t == to
            )),
            "the deferred grant's {from:?} -> {to:?} must journal: {journal:?}"
        );
    }

    let _ = std::fs::remove_dir_all(&dir);
}

/// The refused-startup-claim class with a declared pair: the live
/// incumbent's claim refuses the restart's conditional grant, and the
/// run rejoins as the pair's standby — tracking the `--peer` it
/// declared — rather than standing unpaired or preempting the
/// incumbent.
#[test]
fn a_refused_startup_claim_rejoins_the_declared_pair() {
    let dir = scratch("refused-rejoin");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    // The live incumbent: launched active pinned to its token, the
    // field's standing controller claim under it.
    let incumbent = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let incumbent_client = MonitorClient::new(incumbent.addr);
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    // The stale restart: same field, fresh token, `--peer` naming the
    // pair it rejoins when the field refuses it.
    let restart = spawn_controller(
        &model,
        &[
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--peer".to_string(),
            incumbent.addr.to_string(),
        ],
        DT,
    );
    let client = MonitorClient::new(restart.addr);

    // The verdict already settled at activation: standby behind the
    // served surface, claim observed held, commands refused.
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, Some(FieldClaim::Held));
    assert_command_refused(&client);
    assert_stood_down(&client);

    // The refusal's observed claimant journals the incumbent's token —
    // the attribution the field's arbitration answered with.
    let journal = client.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::FieldClaimObserved { claimant, .. } if *claimant == INCUMBENT
        )),
        "the incumbent's refusal must attribute: {journal:?}"
    );

    // Rejoined, not unpaired: driven scans pull the declared peer's
    // checkpoints and the standby converges.
    client.advance(2).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(
            report.sync,
            Some(StandbySync::Tracking { .. } | StandbySync::Reinitialized { .. })
        ),
        "the declared pair must track, not stand unpaired: {report:?}"
    );

    // The incumbent was never preempted: its claim still names its own
    // token and its run keeps owning the field.
    let probe = RemoteDriver::connect(plant.addr).unwrap();
    assert_eq!(
        probe.ensure_writer(INCUMBENT).unwrap(),
        ClaimGrant::Shared,
        "the incumbent's claim was preempted"
    );
    probe.release_writer().unwrap();
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    let _ = std::fs::remove_dir_all(&dir);
}

/// The refused-startup-claim class without a declared pair: the record
/// chooses process exit — the run has no pair to rejoin and must not
/// stand an unpaired active — with the refusal named on the way out.
/// Both monitored launch shapes take the same disposition.
#[test]
fn a_refused_startup_claim_undeclared_exits() {
    let dir = scratch("refused-exit");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant.addr, SimTcp::Merged).0;

    let incumbent = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let incumbent_client = MonitorClient::new(incumbent.addr);
    incumbent_client.advance(1).unwrap();
    assert_eq!(incumbent_client.role().unwrap().role, Role::Active);

    for mode in [vec!["--driven", "--dt", DT], vec!["--scan-ms", "50"]] {
        let mut args = vec![
            model.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ];
        args.extend(mode.into_iter().map(String::from));
        let output = Process::new(CONTROLLER).args(&args).output().unwrap();
        assert!(!output.status.success(), "the unpaired refusal ran");
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains("no --peer was declared, so there is no pair to rejoin"),
            "{stderr}"
        );
        // The incumbent's claim stands untouched through each exit —
        // a preempted claim would fence its next write.
        let probe = RemoteDriver::connect(plant.addr).unwrap();
        assert_eq!(probe.ensure_writer(INCUMBENT).unwrap(), ClaimGrant::Shared,);
        probe.release_writer().unwrap();
        incumbent_client.advance(1).unwrap();
        assert_eq!(incumbent_client.role().unwrap().role, Role::Active);
    }

    let _ = std::fs::remove_dir_all(&dir);
}

/// The deferred leg of the refused-startup-claim class — QA finding
/// `deferred-startup-claim-refusal-strands-unpaired-standby`. Two
/// born-actives launch pending on a field that cannot answer; when the
/// field arrives, the first seat's deferred grant claims it and the
/// second's meets the incumbent's refusal mid-scan rather than at
/// activation. The recorded contract's disposition is identical either
/// way: the pairless run exits nonzero naming the refusal and the
/// rejoin remedy — and the deferred settle journals
/// `startup_claim_refused`, not merely the observed claimant.
#[test]
fn a_deferred_startup_claim_refusal_exits_the_pairless_run() {
    let dir = scratch("deferred-exit");
    let field = FrozenField::bind();
    let plant_addr = field.addr();
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;
    let journal_path = dir.join("deferred.jsonl");

    // Both seats launch pending on the frozen field — connects that
    // the kernel completes while no verdict ever answers, the
    // conditional startup grant of each waiting on the first answered
    // contact.
    let mut winner = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let (mut loser, preamble) = spawn_controller_logged(
        &model,
        &[
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--journal-file".to_string(),
            journal_path.to_str().unwrap().to_string(),
        ],
        DT,
    );
    assert!(
        preamble.iter().any(|line| line.contains("stands pending")),
        "the pending launch must report itself: {preamble:?}"
    );
    let winner_client = MonitorClient::new(winner.addr);
    let loser_client = MonitorClient::new(loser.addr);
    for client in [&winner_client, &loser_client] {
        let report = client.role().unwrap();
        assert_eq!(report.role, Role::Standby);
        assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    }

    // The field arrives: driven scans order the race — the first seat
    // to re-ask takes the claim, the second's deferred grant meets the
    // standing incumbent's refusal.
    let _port = PLANT_PORT.lock().unwrap();
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        field.thaw(),
    );
    drop(_port);
    // The remote kind's re-attach cadence bounds the first contact —
    // the recorded one-attempt-per-interval cadence, not a grace.
    std::thread::sleep(
        dcs_sim_net::RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200),
    );
    winner_client.advance(2).unwrap();
    let report = winner_client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    // The loser's first answered contact: the scan settles the refusal
    // — the request itself answers the named verdict — and the
    // pairless run ends the launch the verdict refused rather than
    // wedging a sourceless standby.
    let error = loser_client.advance(1).unwrap_err().to_string();
    assert!(
        error.contains("a live peer holds the field's write-ownership claim"),
        "the deferred refusal must answer the scan request: {error}"
    );
    let status = loser
        .wait_exit(std::time::Duration::from_secs(10))
        .expect("the deferred-refused pairless run must exit, not wedge standby");
    assert!(!status.success(), "the refused run must exit nonzero");
    let stderr = loser.stderr_tail();
    assert!(
        stderr.contains("a live peer holds the field's write-ownership claim"),
        "{stderr}"
    );
    assert!(
        stderr.contains("no --peer was declared, so there is no pair to rejoin"),
        "{stderr}"
    );

    // The deferred refusal's own trace — the journaled verdict beside
    // the observed-claimant record attributing it — flushed before the
    // refused scan answered.
    let journal = std::fs::read_to_string(&journal_path).unwrap();
    assert!(
        journal.contains("startup_claim_refused"),
        "the deferred refusal must journal its own verdict: {journal}"
    );
    assert!(
        journal.contains("field_claim_observed"),
        "the incumbent's attribution journals beside it: {journal}"
    );

    // The winner's claim stands untouched through the refusal — a
    // preempted claim would fence its next write — and once the winner
    // is removed the field settles under a fresh born-active, the seat
    // the defect would have wedged already gone.
    let probe = RemoteDriver::connect(plant_addr).unwrap();
    assert_eq!(probe.ensure_writer(INCUMBENT).unwrap(), ClaimGrant::Shared);
    probe.release_writer().unwrap();
    kill(&mut winner);
    let successor = spawn_controller(
        &model,
        &["--owner-token".to_string(), (LAUNCHED + 10).to_string()],
        DT,
    );
    let successor_client = MonitorClient::new(successor.addr);
    successor_client.advance(2).unwrap();
    let report = successor_client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    let _ = std::fs::remove_dir_all(&dir);
}

/// The deferred refusal's declared-pair leg: the same mid-scan verdict
/// on a launch that declared `--peer` keeps the run — the rejoin the
/// contract prescribes instead of the exit. The refused seat journals
/// the settled verdict, stands tracking its declared peer, and — the
/// armed failover counting the dead incumbent's missed pulls — takes
/// the field once the winner is removed: the wedge the finding
/// recorded becomes an ordinary recovery.
#[test]
fn a_deferred_startup_claim_refusal_rejoins_the_declared_pair() {
    let dir = scratch("deferred-rejoin");
    let field = FrozenField::bind();
    let plant_addr = field.addr();
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;

    let mut winner = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let mut loser = spawn_controller(
        &model,
        &[
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
            "--peer".to_string(),
            winner.addr.to_string(),
            "--auto-promote".to_string(),
            "2".to_string(),
        ],
        DT,
    );
    let winner_client = MonitorClient::new(winner.addr);
    let loser_client = MonitorClient::new(loser.addr);
    assert_eq!(loser_client.role().unwrap().role, Role::Standby);

    // The field arrives under the same ordering: the winner's deferred
    // grant claims it, the loser's meets the refusal — but the pair was
    // declared, so the run keeps the standby surface its pull ahead of
    // the same scan already converged.
    let _port = PLANT_PORT.lock().unwrap();
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        field.thaw(),
    );
    drop(_port);
    std::thread::sleep(
        dcs_sim_net::RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200),
    );
    winner_client.advance(2).unwrap();
    assert_eq!(winner_client.role().unwrap().role, Role::Active);

    loser_client.advance(1).unwrap();
    let report = loser_client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(
            report.sync,
            Some(StandbySync::Tracking { .. } | StandbySync::Reinitialized { .. })
        ),
        "the refused seat tracks its declared peer: {report:?}"
    );
    assert!(
        loser
            .wait_exit(std::time::Duration::from_millis(500))
            .is_none(),
        "a declared pair keeps the refused run"
    );

    // The deferred settle journals its own verdict beside the
    // incumbent-attributing observation — the record the wedged seat
    // never carried.
    let journal = loser_client.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| matches!(&entry.event, JournalEvent::StartupClaimRefused { .. })),
        "the deferred refusal must journal its verdict: {journal:?}"
    );
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::FieldClaimObserved { claimant, .. } if *claimant == INCUMBENT
        )),
        "the incumbent's refusal must attribute: {journal:?}"
    );

    // Remove the winner: the rejoined seat's failover budget counts the
    // dead source's misses and the promotion it could never reach while
    // pending lands — the deferred refusal's settle completing.
    kill(&mut winner);
    loser_client.advance(4).unwrap();
    let report = loser_client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    let _ = std::fs::remove_dir_all(&dir);
}

/// The deferred refusal's paced leg — the same frozen-field race on a
/// wall-clock-paced seat, so the verdict settling inside a paced cycle
/// ends the pairless run under the identical contract the driven
/// request path follows. The incumbent claims the thawed field
/// directly — a controller attachment the test owns — while the paced
/// seat's scan cadence has already armed its re-attach gate on the
/// just-freed port's refusals, so the race orders itself.
#[test]
fn a_deferred_startup_claim_refusal_exits_a_paced_pairless_run() {
    let dir = scratch("deferred-exit-paced");
    let field = FrozenField::bind();
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        field.addr(),
        SimTcp::Merged,
    )
    .0;

    // The paced seat launches pending on the frozen field: the boot
    // ask produced no verdict — the wedged connect's request never
    // answered — and the scan loop keeps the pending state quiet until
    // the field answers again.
    let mut loser = spawn_controller_paced(
        &model,
        &["--owner-token".to_string(), LAUNCHED.to_string()],
        50,
        "127.0.0.1:0",
    );
    let loser_client = MonitorClient::new(loser.addr);
    let report = loser_client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));

    // The field returns: the frozen listener drops — the queued links
    // reset — and a beat on the still-free port lets the paced loop's
    // next re-attach meet the refusal and arm its one-per-interval
    // gate before the plant binds, so the incumbent's claim lands
    // first and the paced run's next answered contact meets a standing
    // claim, never an open field.
    let plant_addr = field.thaw();
    std::thread::sleep(std::time::Duration::from_millis(200));
    let _port = PLANT_PORT.lock().unwrap();
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );
    drop(_port);
    let incumbent = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    assert_eq!(
        incumbent.ensure_writer(INCUMBENT).unwrap(),
        ClaimGrant::Exclusive
    );

    // The recorded contract's disposition, identically to the driven
    // leg: the run exits nonzero naming the refusal and the missing
    // pair — no wedged standby outliving the field's settleability.
    let status = loser
        .wait_exit(std::time::Duration::from_secs(15))
        .expect("the deferred-refused paced run must exit, not wedge standby");
    assert!(!status.success(), "the refused run must exit nonzero");
    let stderr = loser.stderr_tail();
    assert!(
        stderr.contains("a live peer holds the field's write-ownership claim"),
        "{stderr}"
    );
    assert!(
        stderr.contains("no --peer was declared, so there is no pair to rejoin"),
        "{stderr}"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
