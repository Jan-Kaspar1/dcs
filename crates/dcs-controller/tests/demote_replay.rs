//! The QA findings `demote-verify-replayable-redirects-tracking` (#818)
//! and `announced-verify-standby-shape-forgery` (#872) at the process
//! boundary their reproductions ran against: a lone controller process —
//! no `--standby`, nothing to track — whose public `GET /checkpoint` an
//! attacker endpoint replays verbatim — or transforms into the standby
//! shape, `source_owns_field: false` at a bumped tick — then announces
//! itself through `GET /checkpoint?peer=<attacker>` and posts `/demote`.
//!
//! On the defective build the demotion verified only the announced
//! endpoint's *document* — which replaying the victim's own checkpoint
//! satisfies — then pinned it, journaled the adoption, and let the
//! endpoint's forged follow-up checkpoints re-domain the demoted peer.
//! Under the fix the verify pull's document checks refuse the
//! replayable shape: the victim's own `/checkpoint` is a field-owning
//! document, and no unproven pull may serve one — verbatim, stale, or
//! tick-bumped alike. The standby-shaped variant is different in kind:
//! every document check accepts it, since it is what a real tracking
//! standby serves — so the announced-source contract is keyed-only, and
//! an unkeyed run refuses the demotion whatever the endpoint serves.
//! A `--pair-token` run demands more: the keyed
//! `line_proof` only a peer holding the token stamps — yet even a
//! transparent relay that proxies the `?prove=` nonce and returns a
//! genuinely signed answer still fails, because the signed document
//! is the victim's own field-owning checkpoint.
//!
//! The reproduction is the issue's required shape: one controller
//! process plus a standard-library TCP interposer serving the
//! controller's own checkpoint, announced from the interposer's own
//! address family. The in-process cover lives in `dcs-monitor`'s
//! tracking tests; the QA lane re-verifies on the rig.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use dcs_core::{JournalEvent, PointId, Role, Sample, Tick, Value};
use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;

mod support;

use support::{CONTROLLER, Spawned, kill, listening_on, pump, spawn_controller, spawn_logged};

/// The model the lone controller loads — local `sim` devices, so the
/// reproduction needs no plant server.
const MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-monitor/fixtures/monitor.json"
);

/// A transparent TCP interposer bound on loopback in front of the
/// controller's own monitor: every connection — `?peer=` announces and
/// `?prove=` challenges alike — is forwarded upstream and the answer
/// relayed verbatim. The strongest attacker shape the finding names:
/// the endpoint serves whatever the victim's real monitor would,
/// including genuinely signed `line_proof` documents, without ever
/// holding the pair key.
struct Relay {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Relay {
    fn serve(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((client, _)) => {
                        thread::spawn(move || pump(client, upstream));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// A static endpoint bound on loopback serving one fixed checkpoint
/// document per connection — the reproduction's busybox-nc responder.
/// Whatever the request asks, the answer is the seeded document: a
/// `?prove=` challenge it cannot sign comes back unsigned.
struct Forge {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Forge {
    fn serve(document: &Checkpoint) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let body = serde_json::to_string(document).unwrap();
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut client, _)) => {
                        let response = response.clone();
                        thread::spawn(move || {
                            let mut seen = [0u8; 8192];
                            let _ = client.read(&mut seen);
                            let _ = client.write_all(response.as_bytes());
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            stop,
            thread: Some(thread),
        }
    }
}

impl Drop for Forge {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// The reproduction's shared spine: on the running controller at `addr`,
/// announce the relay as `?peer=` — the announce lands, the connection's
/// loopback source matching the relay's loopback address — then post
/// `/demote`, which must refuse, and prove the run is undisturbed: the
/// owner stays active on its own tick and journals neither an adoption
/// nor a role change.
fn replay_refusal(addr: SocketAddr) {
    let client = MonitorClient::new(addr);
    client.advance(3).unwrap();
    let tick = client.role().unwrap().tick;
    let since = client
        .journal(0)
        .unwrap()
        .last()
        .map(|entry| entry.seq)
        .unwrap_or(0);

    let relay = Relay::serve(addr);
    client.checkpoint_announcing(relay.addr).unwrap();

    let error = client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a relay of this run's own checkpoint must not arm the demotion: {error}"
    );

    // The forged follow-up the reproduction chains — the same
    // generation at an arbitrary future tick — has nothing to feed:
    // no adoption happened, no source is tracked, and the run keeps
    // its own tick domain.
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, Tick(tick.0 + 1));
    assert!(
        client.journal(since).unwrap().iter().all(|entry| !matches!(
            entry.event,
            JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
        )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The reproduction on its own launch shape: no `--standby`, no
/// `--pair-token` — the relayed answer is the victim's own field-owning
/// document at the run's own tick, the replayable shape the demote-side
/// document checks refuse on any unproven pull.
#[test]
fn an_unkeyed_lone_controller_refuses_a_demote_armed_by_its_replayed_checkpoint() {
    let mut controller: Spawned = spawn_logged(
        Path::new(CONTROLLER),
        &[
            MODEL.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
            "--driven".to_string(),
            "--dt".to_string(),
            "0.05".to_string(),
        ],
        listening_on,
    )
    .0;
    replay_refusal(controller.addr);
    kill(&mut controller);
}

/// The same reproduction on a keyed run — the deployment shape a real
/// redundant pair declares. The relay forwards the verify pull's
/// `?prove=` nonce and returns the victim's genuinely signed document;
/// the signature attests content, and the content is still the victim's
/// own field-owning checkpoint — the replayable shape the document
/// checks refuse.
#[test]
fn a_keyed_lone_controller_refuses_a_demote_armed_by_a_relay_of_its_own_monitor() {
    let mut controller = spawn_controller(Path::new(MODEL), &[], "0.05");
    replay_refusal(controller.addr);
    kill(&mut controller);
}

/// The `announced-verify-standby-shape-forgery` (#872) reproduction's
/// spine: on the running controller at `addr`, announce the forging
/// endpoint as `?peer=` — the announce lands, the endpoint's loopback
/// claim matching the connection's loopback source — then post
/// `/demote`, which must refuse, and prove the run is undisturbed: the
/// owner stays active on its own tick and journals neither an adoption
/// nor a role change.
fn standby_shaped_forgery_refusal(addr: SocketAddr) {
    let client = MonitorClient::new(addr);
    client.advance(3).unwrap();
    let tick = client.role().unwrap().tick;
    let since = client
        .journal(0)
        .unwrap()
        .last()
        .map(|entry| entry.seq)
        .unwrap_or(0);

    // The reproduction's document: the victim's own public checkpoint
    // with the field claim stripped — the `source_owns_field: false`
    // shape every honest tracking standby serves — the tick bumped
    // inside the honest skew window, and an internal `In` sample
    // planted no command produced. Every field is derivable from the
    // public read, which is the whole problem.
    let mut forged = client.checkpoint().unwrap();
    forged.source_owns_field = Some(false);
    forged.tick = Tick(tick.0 + 5);
    forged
        .internal
        .insert(PointId(302), Sample::good(Value::Bool(true), forged.tick));
    let forge = Forge::serve(&forged);

    client.checkpoint_announcing(forge.addr).unwrap();

    let error = client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a standby-shaped forgery must not arm the demotion: {error}"
    );

    // The planted state the reproduction chains through adoption has
    // nothing to feed: no adoption happened, no source is tracked, and
    // the run keeps its own tick domain.
    client.advance(1).unwrap();
    let report = client.role().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.tick, Tick(tick.0 + 1));
    assert!(
        client.journal(since).unwrap().iter().all(|entry| !matches!(
            entry.event,
            JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
        )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The QA finding `announced-verify-standby-shape-forgery` (#872) at
/// the process boundary its reproduction ran against: a lone unkeyed
/// controller whose public `GET /checkpoint` an in-network endpoint
/// transforms into the standby shape — `source_owns_field: false`,
/// same generation, tick+5 — then announces itself and posts
/// `/demote`. The document checks alone could never refuse that shape —
/// it is what every honest tracking standby serves — so the announced
/// contract is keyed-only: on an unkeyed run the demotion refuses
/// `no_tracking_source` whatever the hinted endpoint would serve.
#[test]
fn an_unkeyed_lone_controller_refuses_a_demote_armed_by_a_standby_shaped_forgery() {
    let mut controller: Spawned = spawn_logged(
        Path::new(CONTROLLER),
        &[
            MODEL.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
            "--driven".to_string(),
            "--dt".to_string(),
            "0.05".to_string(),
        ],
        listening_on,
    )
    .0;
    standby_shaped_forgery_refusal(controller.addr);
    kill(&mut controller);
}

/// The same forgery on a keyed run: the verify pull carries a `?prove=`
/// nonce whose keyed `line_proof` an endpoint that only transforms the
/// public checkpoint cannot stamp, so the unsigned answer proves
/// nothing about the endpoint and the demotion refuses all the same.
#[test]
fn a_keyed_lone_controller_refuses_a_demote_armed_by_an_unsigned_standby_shaped_forgery() {
    let mut controller = spawn_controller(Path::new(MODEL), &[], "0.05");
    standby_shaped_forgery_refusal(controller.addr);
    kill(&mut controller);
}
