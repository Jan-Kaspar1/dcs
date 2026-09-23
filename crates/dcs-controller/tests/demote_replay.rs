//! The QA finding `demote-verify-replayable-redirects-tracking` (#818)
//! at the process boundary its reproduction ran against: a lone
//! controller process — no `--standby`, nothing to track — whose public
//! `GET /checkpoint` an attacker endpoint replays verbatim, then
//! announces itself through `GET /checkpoint?peer=<attacker>` and posts
//! `/demote`.
//!
//! On the defective build the demotion verified only the announced
//! endpoint's *document* — which replaying the victim's own checkpoint
//! satisfies — then pinned it, journaled the adoption, and let the
//! endpoint's forged follow-up checkpoints re-domain the demoted peer.
//! The document checks then refused only the field-owning shape —
//! leaving the standby-shaped forgery the
//! `announced-source-adopts-standby-shaped-checkpoint` finding served:
//! the victim's checkpoint with `source_owns_field` flipped false.
//! Under the fix the announced-source contract is keyed-only: an
//! unkeyed run can prove nothing about an endpoint the public
//! `/checkpoint` hands every document shape to, so an announced-only
//! demotion refuses whatever the document claims. A `--pair-token`
//! run demands the keyed `line_proof` only a peer holding the token
//! stamps — yet even a transparent relay that proxies the `?prove=`
//! nonce and returns a genuinely signed answer still fails, because
//! the signed document is the victim's own field-owning checkpoint.
//!
//! The reproduction is the issue's required shape: one controller
//! process plus a standard-library TCP interposer serving the
//! controller's own checkpoint, announced from the interposer's own
//! address family. The in-process cover lives in `dcs-monitor`'s
//! tracking tests; the QA lane re-verifies on the rig.

use std::net::{SocketAddr, TcpListener};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use dcs_core::{JournalEvent, Role, Tick};
use dcs_monitor::MonitorClient;

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

/// A forged-document endpoint bound on loopback — the reproduction's
/// forge: it answers every connection with the checkpoint document it
/// was given, whatever shape that claims. Unlike the relay it never
/// touches the victim's monitor, so nothing it serves can carry the
/// keyed `line_proof` — and on an unkeyed run nothing it serves can
/// prove anything at all.
struct Forge {
    addr: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Forge {
    fn serve(forged: &serde_json::Value) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let body = forged.to_string();
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = Arc::clone(&stop);
        let thread = thread::spawn(move || {
            while !stopping.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                        let mut buf = [0u8; 4096];
                        let mut seen = Vec::new();
                        loop {
                            match std::io::Read::read(&mut stream, &mut buf) {
                                Ok(0) => break,
                                Ok(n) => {
                                    seen.extend_from_slice(&buf[..n]);
                                    if seen.windows(4).any(|window| window == b"\r\n\r\n") {
                                        break;
                                    }
                                    if seen.len() > 65536 {
                                        break;
                                    }
                                }
                                Err(_) => break,
                            }
                        }
                        let response = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\
                             Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = std::io::Write::write_all(&mut stream, response.as_bytes());
                        let _ = std::io::Write::flush(&mut stream);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
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

/// The `announced-source-adopts-standby-shaped-checkpoint` finding's
/// exact reproduction at the process boundary: a lone unkeyed
/// controller — no `--standby`, no `--pair-token` — whose checkpoint
/// the forge replays with `source_owns_field` flipped false, the
/// standby shape the document-shape refusal never covered. The
/// same-source announce lands, but the announced-source contract is
/// keyed-only: nothing the forge serves can authenticate it, so
/// `POST /demote` refuses `no_tracking_source`, nothing is adopted or
/// journaled, and the controller stays the field owner.
#[test]
fn an_unkeyed_lone_controller_refuses_a_demote_armed_by_a_standby_shaped_forge() {
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
    let client = MonitorClient::new(controller.addr);
    client.advance(3).unwrap();
    let tick = client.role().unwrap().tick;
    let since = client
        .journal(0)
        .unwrap()
        .last()
        .map(|entry| entry.seq)
        .unwrap_or(0);

    // The reproduction's forge verbatim: the victim's own document
    // with the field claim stripped — the standby shape that used to
    // pass the unkeyed document checks.
    let mut forged = client.checkpoint().unwrap();
    forged.source_owns_field = Some(false);
    let forged_json = serde_json::to_value(&forged).unwrap();
    let forge = Forge::serve(&forged_json);

    client.checkpoint_announcing(forge.addr).unwrap();

    let error = client.demote().unwrap_err();
    assert!(
        error.to_string().contains("no_tracking_source"),
        "a standby-shaped forged document must not arm the demotion: {error}"
    );
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
    kill(&mut controller);
}
