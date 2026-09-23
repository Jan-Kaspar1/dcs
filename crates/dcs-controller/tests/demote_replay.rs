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
//! Under the fix an announced hint has to prove the endpoint: the keyed
//! `line_proof` a `--pair-token` deployment demands. An unkeyed run
//! cannot prove anything and refuses `no_tracking_source`; a keyed run
//! demands the proof, and the transparent relay — which proxies even
//! the `?prove=` nonce and returns genuinely signed answers — still
//! fails because the signed document is the victim's own field-owning
//! checkpoint, the replayable shape the document checks refuse.
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
                    Ok((client, _)) => thread::spawn(move || pump(client, upstream)),
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
        client
            .journal(since)
            .unwrap()
            .iter()
            .all(|entry| !matches!(
                entry.event,
                JournalEvent::TrackingSourceAdopted { .. } | JournalEvent::RoleChanged { .. }
            )),
        "a refused demotion journals neither an adoption nor a role change"
    );
}

/// The reproduction on its own launch shape: no `--standby`, no
/// `--pair-token` — there is no endpoint proof to demand, so the
/// announced-only demotion refuses outright.
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
            "50ms".to_string(),
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
    let mut controller = spawn_controller(Path::new(MODEL), &[], "50ms");
    replay_refusal(controller.addr);
    kill(&mut controller);
}
