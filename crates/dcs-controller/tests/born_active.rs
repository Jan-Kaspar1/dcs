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
//! - `deferred-startup-refusal-strands-undeclared-born`: the finding's
//!   own sequencing — the incumbent's claim stands *before* the pause,
//!   and `docker pause`/`unpause` holds every field link open but
//!   unanswered rather than severing it — the pending run's deferred
//!   ask must still reach the refused verdict's named disposition
//!   instead of standing as an invisible dead seat.
//! - `deferred-claim-refusal-strands-undeclared-standby`: the same
//!   disposition on the reproduction's dead-IP arm — the run launched
//!   against nothing listening stands pending, and the field then binds
//!   on that address *already claimed*, so the first answered retry
//!   meets a standing claim. Nothing else races the born-active's grant
//!   here: the refusal must end the launch rather than leave a
//!   sourceless standby indistinguishable from a correctly idling one.
//!
//! The rig is the failover harness's shape: a `dcs-plant-server`
//! process owns the shared `tank_loop` plant and the controllers load
//! the same model re-pointed at `sim-tcp`, `--driven` so every scan —
//! and therefore every claim probe and deferred ask — happens inside a
//! `POST /scan` request, or wall-clock `--scan-ms` paced for the QA
//! deployment's own launch shape.

use dcs_core::{
    Command, CommandError, CommandOutcome, FieldClaim, JournalEvent, PointId, Role, StandbySync,
    Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::{ClaimGrant, RemoteDriver};
use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::Command as Process;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

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

/// A pausable TCP relay between the controllers and the plant — the
/// `docker pause`/`unpause` cycle the finding ran, made of real
/// sockets: while `paused` stands the pump threads hold every
/// connection open and forward nothing, so a link's in-flight request
/// sits unanswered exactly as it does inside a frozen plant process —
/// the claim table and the incumbent's hold included, where
/// [`FrozenField`]'s dropped listener is the colder *restart* shape.
/// Released, the buffered bytes flow and the stalled exchanges
/// complete or time out on their own clocks.
struct PausableRelay {
    addr: SocketAddr,
    paused: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
}

impl PausableRelay {
    /// Binds a relay on an ephemeral loopback port forwarding to
    /// `upstream`, its accept loop on a spawned thread. Accepts and
    /// upstream connects run through the pause — a stopped process
    /// still completes handshakes at the kernel — so a launch on the
    /// frozen field attaches and then hears nothing, the pending
    /// born-active's exact boot shape.
    fn forwarding(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let relay = Self {
            addr: listener.local_addr().unwrap(),
            paused: Arc::new(AtomicBool::new(false)),
            stop: Arc::new(AtomicBool::new(false)),
        };
        let (paused, stop) = (relay.paused.clone(), relay.stop.clone());
        std::thread::spawn(move || {
            while !stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((downstream, _)) => {
                        let Ok(served) = TcpStream::connect(upstream) else {
                            continue;
                        };
                        for (from, to) in [
                            (downstream.try_clone().unwrap(), served.try_clone().unwrap()),
                            (served, downstream),
                        ] {
                            let (paused, stop) = (paused.clone(), stop.clone());
                            std::thread::spawn(move || Self::pump(from, to, paused, stop));
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) if stop.load(Ordering::Relaxed) => return,
                    Err(_) => std::thread::sleep(Duration::from_millis(5)),
                }
            }
        });
        relay
    }

    /// The address controllers point their `sim-tcp` devices at.
    fn addr(&self) -> SocketAddr {
        self.addr
    }

    /// `docker pause`: the pumps stop moving bytes while every socket
    /// stays open — a frozen plant process's exact wire shape.
    fn pause(&self) {
        self.paused.store(true, Ordering::Relaxed);
    }

    /// `docker unpause`: buffered bytes flow again and every in-flight
    /// exchange completes on the surviving link or fails on the
    /// requester's own timeout.
    fn resume(&self) {
        self.paused.store(false, Ordering::Relaxed);
    }

    /// Copies `from` to `to` until either side closes, polling its
    /// read so a pause or the relay's stop lands within a beat.
    fn pump(
        mut from: TcpStream,
        mut to: TcpStream,
        paused: Arc<AtomicBool>,
        stop: Arc<AtomicBool>,
    ) {
        let _ = from.set_read_timeout(Some(Duration::from_millis(10)));
        let mut buf = [0u8; 8192];
        loop {
            if stop.load(Ordering::Relaxed) {
                return;
            }
            if paused.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(5));
                continue;
            }
            match from.read(&mut buf) {
                Ok(0) => {
                    let _ = to.shutdown(Shutdown::Write);
                    return;
                }
                Ok(n) => {
                    if to.write_all(&buf[..n]).is_err() {
                        return;
                    }
                }
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
                    ) => {}
                Err(_) => return,
            }
        }
    }
}

impl Drop for PausableRelay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
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

/// QA finding `deferred-startup-refusal-strands-undeclared-born` — the
/// reproduction's own sequencing, which the earlier deferred legs do
/// not run: the incumbent's claim stands *before* the pause, and the
/// field freezes in place rather than severing. A `PausableRelay` in
/// front of the plant is the `docker pause` wire shape — every link
/// held open, nothing answered — so the thawed field still serves the
/// incumbent's live hold, where a respawned plant's claim table would
/// have cleared. The undeclared born-active launches against the
/// silent field, its startup ask meeting no verdict; the unpause's
/// first answered contact settles the incumbent's refusal and the
/// pairless run reaches the named disposition — process exit naming
/// the verdict — instead of the invisible dead seat the finding
/// watched: no tracking source, adoption refused, promotion gated, and
/// the startup ask permanently disarmed.
#[test]
fn a_deferred_startup_claim_refusal_on_a_paused_claimed_field_exits() {
    let dir = scratch("deferred-exit-paused-claimed");
    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let relay = PausableRelay::forwarding(plant.addr);
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        relay.addr(),
        SimTcp::Merged,
    )
    .0;

    // The incumbent claims the field ahead of the freeze — a driven
    // run, so the pause costs its claim-holding link no timed-out
    // request and the hold stays live for the deferred ask to meet.
    let incumbent = spawn_controller(
        &model,
        &["--owner-token".to_string(), INCUMBENT.to_string()],
        DT,
    );
    let incumbent_client = MonitorClient::new(incumbent.addr);
    incumbent_client.advance(1).unwrap();
    let report = incumbent_client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));

    // `docker pause`: every link to the field stays open and
    // unanswered — the launch's attach still completes while its
    // startup ask waits on a verdict the frozen field never sends.
    relay.pause();
    let mut launched = spawn_controller_paced(
        &model,
        &["--owner-token".to_string(), LAUNCHED.to_string()],
        50,
        "127.0.0.1:0",
    );
    // The pending surface the finding watched strand: standby,
    // unsynchronized, no claim verdict observed — the run serves but
    // owns nothing it could promote from.
    let client = MonitorClient::new(launched.addr);
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    assert_command_refused(&client);

    // `docker unpause`: buffered and fresh exchanges flow again. The
    // pending run's first answered probe re-issues the conditional
    // grant, the incumbent's still-live claim refuses it, and the
    // pairless run takes the launch-time refusal's disposition —
    // exit-or-rejoin parity with `activate()`'s identical verdict —
    // rather than standing inert until an external restart.
    relay.resume();
    let status = launched
        .wait_exit(Duration::from_secs(20))
        .expect("the deferred-refused pairless run must exit, not wedge standby");
    assert!(!status.success(), "the refused run must exit nonzero");
    let stderr = launched.stderr_tail();
    assert!(
        stderr.contains("a live peer holds the field's write-ownership claim"),
        "{stderr}"
    );
    assert!(
        stderr.contains("no --peer was declared, so there is no pair to rejoin"),
        "{stderr}"
    );

    // The incumbent held the field through the whole episode — the
    // deferred refusal never touched its claim, and its run keeps
    // owning the field when its link answers again.
    incumbent_client.advance(1).unwrap();
    let report = incumbent_client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.field_claim, Some(FieldClaim::Held));
    let probe = RemoteDriver::connect(plant.addr).unwrap();
    assert_eq!(probe.ensure_writer(INCUMBENT).unwrap(), ClaimGrant::Shared);
    probe.release_writer().unwrap();

    let _ = std::fs::remove_dir_all(&dir);
}

/// QA finding `pending-born-active-frozen-field-endpoint-starvation` —
/// the pending born-active's scans must stay bounded near one field
/// timeout. On the defect build a failed exchange never armed the
/// remote driver's re-attach window, so a field that completes the
/// handshake but never answers — the reproduction's `docker pause`,
/// here the [`FrozenField`] listener that accepts nothing — charged
/// every request its own full timeout: the per-scan claim probe plus
/// the quiesced scan's per-point reads serialized ~14 stalls into one
/// ~72s scan under the monitor's executor lock, and every lock-taking
/// endpoint — `GET /checkpoint`, `POST /promote` — starved with it.
/// The pending contract still owes the served surface: standby,
/// unsynchronized, commands refused `not_active`, promote refused
/// `not_converged` — each inside a bound near the request timeout, and
/// the paced cadence held to a floor rather than collapsed ~700x.
#[test]
fn a_pending_born_active_on_a_frozen_field_keeps_serving_bounded_scans() {
    let dir = scratch("pending-frozen-bounded");
    let field = FrozenField::bind();
    let model = controller_model(
        &dir,
        "pair.json",
        MODEL_SOURCE,
        field.addr(),
        SimTcp::Merged,
    )
    .0;

    // The reproduction's launch shape: a wall-clock-paced run serving
    // the monitor, attached to the never-answering field.
    let launched = spawn_controller_paced(
        &model,
        &[
            "--remote".to_string(),
            field.addr().to_string(),
            "--owner-token".to_string(),
            LAUNCHED.to_string(),
        ],
        100,
        "127.0.0.1:0",
    );

    // The bound every lock-taking request must answer inside: near one
    // field timeout, never the defect's ~72s — a client carrying it
    // turns a wedged scan into a request failure the assertions read.
    let bound = RemoteDriver::DEFAULT_TIMEOUT + Duration::from_secs(4);
    let client = MonitorClient::with_timeout(launched.addr, bound);

    // The pending surface itself: standby, unsynchronized, the claim
    // unobserved while no probe has answered, commands refused.
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    assert_command_refused(&client);

    // The finding's starved reads, polled across more than one
    // re-attach window: every answer lands inside the bound — the
    // defect held them 11s to 72s, past this client's patience.
    for _ in 0..3 {
        client
            .checkpoint()
            .expect("GET /checkpoint starved behind a wedged scan");
    }

    // The pending run's promote refusal is the contract's
    // `not_converged` verdict — served, not stalled.
    let error = client
        .promote()
        .expect_err("the pending run has nothing to promote from")
        .to_string();
    assert!(
        error.contains("not_converged"),
        "the pending promote must answer the named refusal: {error}"
    );

    // The paced cadence held to its floor: `GET /role` reads the
    // published mirror — lock-free, so the observation never queues
    // behind the lock — and the tick keeps advancing between the
    // bounded field stalls, where the defect ran ~1 tick per ~70s on a
    // 100ms schedule.
    let first = client.role().unwrap().tick;
    std::thread::sleep(
        RemoteDriver::DEFAULT_TIMEOUT + RemoteDriver::REATTACH_INTERVAL + Duration::from_secs(2),
    );
    let advanced = client.role().unwrap().tick.0 - first.0;
    assert!(
        advanced >= 4,
        "the pending run's scans must keep cadence through the frozen \
         field — {advanced} ticks inside the observation window"
    );

    // And the liveness probe reports the freshness honestly: the scan
    // age stays bounded near one field timeout — not the defect's
    // tens of seconds.
    let health = client.health().unwrap();
    assert!(
        health.last_scan_age_ms.unwrap_or(u64::MAX)
            < (RemoteDriver::DEFAULT_TIMEOUT + Duration::from_secs(3)).as_millis() as u64,
        "last_scan_age_ms must stay near one field timeout: {health:?}"
    );

    let _ = std::fs::remove_dir_all(&dir);
}

/// QA finding `deferred-claim-refusal-strands-undeclared-standby` — the
/// finding's own staging, the one shape the earlier deferred legs do
/// not run. There, the incumbent was itself a born-active racing the
/// same deferred grant over a thawed field. Here the incumbent is a
/// *controller* that already held the field before the launch, and the
/// undeclared born-active never shares its retry with anyone: it is
/// launched against a **dead endpoint** — nothing listening at the
/// declared address, the "point the born at a dead IP" arm of the
/// reproduction — so its boot ask produces no verdict and it stands
/// pending. The field is then bound on that exact address *already
/// claimed by the incumbent*, so the born-active's first answered
/// contact meets a standing claim rather than an open field.
///
/// That is the defect's shape, and the finding's evidence is what the
/// defect produced there: the deferred retry observed the refusal —
/// `role_changed{from: active, to: standby, origin: fenced}` and
/// `field_claim_observed{claimant: 7202}` — and then the run kept
/// serving as an honest-looking `standby`/`unsynchronized` indefinitely
/// (observed >20 min, tick 21744+), never converging because it has no
/// tracking source, never holding the claim, and not promoting, so
/// `POST /promote` refused `not_converged` on a seat nothing would
/// ever heal. The recorded disposition (decision 103 class (b), which
/// `settle_activation` already implements for the boot-time verdict) is
/// the same at both timings: exit nonzero, naming the refusal and the
/// `--standby` remedy, so the supervisor relaunches with the flag that
/// turns the refusal into a rejoin.
///
/// The run here is `--driven`, so its answer is countable: the refused
/// scan request itself carries the verdict, and the process exits
/// immediately behind it. The bound is the contract's own — the grant
/// is never re-asked after a verdict (the field answered), so the
/// refusal settles on the *first* answered contact, never a second.
#[test]
fn a_deferred_startup_claim_refusal_over_a_dead_endpoint_exits_the_pairless_run() {
    let dir = scratch("deferred-dead-endpoint-claimed");
    // Nothing listens here yet: the launch's attach cannot complete,
    // so the startup ask yields no verdict at all — class (c)'s
    // inconclusive leg, the finding's dead-IP arm.
    let plant_addr = unclaimed_addr();
    let model = controller_model(&dir, "pair.json", MODEL_SOURCE, plant_addr, SimTcp::Merged).0;
    let journal_path = dir.join("deferred.jsonl");

    let (mut launched, preamble) = spawn_controller_logged(
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

    // The pending surface the finding watched strand: served, honest,
    // and inert — standby, unsynchronized, no claim verdict observed,
    // commands refused, nothing promotable. Field safety intact, which
    // is exactly why the defect was indistinguishable from a correctly
    // idling standby.
    let client = MonitorClient::new(launched.addr);
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(report.sync, Some(StandbySync::Unsynchronized));
    assert_eq!(report.field_claim, None);
    assert_command_refused(&client);
    let error = client
        .promote()
        .expect_err("a pending run has nothing to promote from")
        .to_string();
    assert!(
        error.contains("not_converged"),
        "the pending promote must answer the named refusal: {error}"
    );

    // Answering nothing still asks nothing: the deferred grant re-issues
    // only on a contact the field answered, so scans against the dead
    // endpoint leave the run pending rather than guessing a verdict.
    client.advance(2).unwrap();
    assert_eq!(client.role().unwrap().field_claim, None);

    // The field arrives on the *same* address already claimed — the
    // incumbent holds the claim the born-active's first answered retry
    // will meet. It claims through a controller attachment the test
    // owns, so the hold is a live unyielded controller claim: exactly
    // the shape decision 91 makes the conditional grant refuse.
    let _port = PLANT_PORT.lock().unwrap();
    let _plant = spawn_plant_at(
        Path::new(PLANT_MODEL),
        Path::new(PLANT_DYNAMICS),
        plant_addr,
    );
    let incumbent = RemoteDriver::connect(plant_addr).unwrap().as_controller();
    assert_eq!(
        incumbent.ensure_writer(INCUMBENT).unwrap(),
        ClaimGrant::Exclusive
    );
    drop(_port);
    // The remote kind's re-attach cadence paces the first contact: one
    // bounded attempt per recorded interval, not a grace period.
    std::thread::sleep(
        dcs_sim_net::RemoteDriver::REATTACH_INTERVAL + std::time::Duration::from_millis(200),
    );

    // The first answered contact settles the refusal, and the scan
    // request carries it — the request the refused verdict ended the
    // launch with. A bounded number of answered contacts, not an open
    // wait: the grant is never re-asked once the field has answered.
    let error = client.advance(1).unwrap_err().to_string();
    assert!(
        error.contains("a live peer holds the field's write-ownership claim"),
        "the deferred refusal must answer the first answered scan: {error}"
    );
    let status = launched
        .wait_exit(Duration::from_secs(10))
        .expect("the deferred-refused pairless run must exit, not strand a standby");
    assert!(!status.success(), "the refused run must exit nonzero");
    assert!(
        launched.child.try_wait().unwrap().is_some(),
        "the exit must not take a second request: the verdict already landed"
    );

    // The exit names the refusal and the remedy an operator can act on
    // — the `--standby` relaunch, by its flag. A stranded run's last
    // words are the only place the remedy can be read, so the text
    // must carry it.
    let stderr = launched.stderr_tail();
    assert!(
        stderr.contains("a live peer holds the field's write-ownership claim"),
        "{stderr}"
    );
    assert!(
        stderr.contains("no --peer was declared, so there is no pair to rejoin"),
        "{stderr}"
    );
    assert!(
        stderr.contains("--standby ADDRESS"),
        "the refusal must name the --standby remedy: {stderr}"
    );

    // The refusal's own trace is durable: the verdict journals beside
    // the incumbent-attributing observation, flushed before the refused
    // scan answered. Without the journal the exit message would be the
    // only record, and a monitorless relaunch reads no stderr.
    let journal = std::fs::read_to_string(&journal_path).unwrap();
    assert!(
        journal.contains("startup_claim_refused"),
        "the deferred refusal must journal its own verdict: {journal}"
    );
    assert!(
        journal.contains(&INCUMBENT.to_string()),
        "the incumbent's claim must be attributed in the record: {journal}"
    );

    // Field safety held throughout: the refusal never preempted the
    // live incumbent, whose claim is still this probe's shared hold
    // after the refused run is gone.
    let probe = RemoteDriver::connect(plant_addr).unwrap();
    assert_eq!(probe.ensure_writer(INCUMBENT).unwrap(), ClaimGrant::Shared);
    probe.release_writer().unwrap();

    let _ = std::fs::remove_dir_all(&dir);
}
