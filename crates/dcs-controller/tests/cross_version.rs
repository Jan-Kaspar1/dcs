//! Cross-version checkpoint negotiation through the redundant pair —
//! the issue's acceptance rig: the rolling-upgrade path exercised end
//! to end, with the standby as the new build and a checkpoint stream
//! carrying the wire shapes other builds produce.
//!
//! Two `Peer`s assemble the same model against `RemoteDriver`s on one
//! `PlantServer`, each behind a `WriteGate` and each serving a driven
//! monitor — the same `POST /scan` request path a `--driven` controller
//! runs, so each requested standby scan carries the checkpoint pull the
//! peer-transport decision specifies: `MonitorClient::checkpoint`
//! against the track address, then `Peer::apply` inside the request's
//! boundary.
//!
//! The recorded serving choice: rather than the active's own monitor,
//! the standby's track address names a **fixture checkpoint endpoint**
//! — a test-side `GET /checkpoint` that proxies the active's live
//! document and rewrites it into the negotiated shapes. Serving a JSON
//! document rather than a `Checkpoint` value is what lets the older
//! version carry the true version-0 wire shape: `format_version`
//! absent entirely, which no struct value can express — serde reads it
//! as the supported version 0. The unsupported-version and
//! fingerprint-mismatch legs substitute the negotiated fields the same
//! way. Every other piece is the production path: the pull, the
//! deserialization, the apply, the staged-output divergence check, the
//! write gate, and the journal.
//!
//! The scripted run: the standby converges to `tracking` on the
//! version-0 stream; an unsupported version and then a foreign
//! fingerprint are each refused by name — the reported `degraded`
//! state carrying the `UnsupportedVersion`/`FingerprintMismatch`
//! content, promotion answering `409 not_converged`, the gate staying
//! closed, and the active's run and field writes undisturbed
//! throughout; the version-0 stream then reconverges the standby and
//! the documented demote/promote switchover proceeds, the promoted
//! peer's continuation equal to the demoted peer's quiesced run.
//! Identical scripted runs produce identical outcomes.

use dcs_assembly::{assemble, sim_channel_map};
use dcs_controller::registry;
use dcs_core::{
    IoDriver, JournalEvent, ModelFingerprint, PointId, Role, RoleReport, StandbySync, SwitchError,
    TelemetrySnapshot, Tick, Value,
};
use dcs_model::PlantModel;
use dcs_monitor::{Driven, Monitor, MonitorClient};
use dcs_runtime::{
    ApplyError, Checkpoint, Peer, RestoreError, SUPPORTED_FORMAT_VERSIONS, WriteGate,
};
use dcs_sim::SimDriver;
use dcs_sim_net::{PlantServer, RemoteDriver};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

mod support;

use support::image_value;

const TANK_LOOP: &str = include_str!("../../dcs-assembly/fixtures/tank_loop.json");

/// The process time the shared plant advances per scan — the fixture's
/// PID is parameterized for dt 0.1.
const DT: f64 = 0.1;
/// Ticks the pair runs on the version-0 stream before the refusals.
const N: u64 = 20;
/// Post-switchover ticks proven bumpless.
const M: u64 = 5;
const SETPOINT: PointId = PointId(10);
const LEVEL: PointId = PointId(11);
const VALVE: PointId = PointId(12);
/// The pair secret both monitors key with — the keyed deployment the
/// announced-demotion contract requires for its endpoint proof.
const PAIR_KEY: u64 = 0x517c_c1b7_2722_0a95;
/// The `format_version` the unsupported-version leg serves — outside
/// `SUPPORTED_FORMAT_VERSIONS` at the newer end.
const UNSUPPORTED_FORMAT_VERSION: u32 = 99;

/// The foreign fingerprint a mismatched leg's checkpoint carries — a
/// checkpoint captured under a different plant model.
fn foreign() -> ModelFingerprint {
    ModelFingerprint::of(b"a different plant model")
}

/// `shutdown`-equivalent on drop, so a panicking test still lets the
/// scoped serve threads exit — the same pattern `divergence.rs` and
/// `standby.rs` use.
trait Stoppable {
    fn stop(&self);
}

impl Stoppable for PlantServer {
    fn stop(&self) {
        self.shutdown();
    }
}

impl Stoppable for Monitor<'_> {
    fn stop(&self) {
        self.shutdown();
    }
}

impl Stoppable for CheckpointFixture {
    fn stop(&self) {
        self.running.store(false, Ordering::Relaxed);
    }
}

struct ShutdownOnDrop<'s, T: Stoppable>(&'s T);

impl<T: Stoppable> Drop for ShutdownOnDrop<'_, T> {
    fn drop(&mut self) {
        self.0.stop();
    }
}

/// How the fixture endpoint rewrites each checkpoint document it
/// proxies — the negotiated-wire-shape knob the scripted run turns.
#[derive(Clone, Copy)]
enum Rewrite {
    /// Serve the document exactly as the active's monitor produced it.
    Intact,
    /// Drop `format_version` — the version-0 wire shape a checkpoint
    /// serialized before the format was versioned has.
    VersionZero,
    /// Serve `format_version` outside `SUPPORTED_FORMAT_VERSIONS` — a
    /// checkpoint a build with a newer format would write.
    Unsupported,
    /// Serve a foreign `model_fingerprint` — a checkpoint captured
    /// under a different model.
    ForeignFingerprint,
}

/// A stand-in checkpoint endpoint: answers `GET /checkpoint` with the
/// active's *live* document rewritten per [`Rewrite`], so the standby's
/// real pull path negotiates the wire shapes other builds produce —
/// the recorded choice for how the crafted checkpoint is served.
struct CheckpointFixture {
    addr: SocketAddr,
    active: SocketAddr,
    listener: TcpListener,
    rewrite: Mutex<Rewrite>,
    running: AtomicBool,
}

impl CheckpointFixture {
    /// Binds the fixture on an ephemeral loopback port, proxying the
    /// checkpoints the active's monitor at `active` serves.
    fn bind(active: SocketAddr) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        Self {
            addr: listener.local_addr().unwrap(),
            active,
            listener,
            rewrite: Mutex::new(Rewrite::Intact),
            running: AtomicBool::new(true),
        }
    }

    /// Switches the wire shape served from now on.
    fn set(&self, rewrite: Rewrite) {
        *self.rewrite.lock().unwrap() = rewrite;
    }

    /// The accept loop: one `GET /checkpoint` answer per connection —
    /// `MonitorClient` opens a fresh one per pull.
    fn serve(&self) {
        let client = MonitorClient::new(self.active);
        while self.running.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((mut stream, _)) => self.answer(&client, &mut stream),
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(1));
                }
                Err(_) => break,
            }
        }
    }

    /// Answers one connection: read the request head, then serve the
    /// proxied-and-rewritten document.
    fn answer(&self, client: &MonitorClient, stream: &mut TcpStream) {
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let mut head = Vec::new();
        let mut chunk = [0_u8; 1024];
        while !head.windows(4).any(|window| window == b"\r\n\r\n") {
            match stream.read(&mut chunk) {
                Ok(0) | Err(_) => return,
                Ok(n) => head.extend_from_slice(&chunk[..n]),
            }
        }
        let request = String::from_utf8_lossy(&head);
        let target = request.split_whitespace().nth(1).unwrap_or("");
        // A tracking pull announces the pulling monitor on `?peer=` —
        // forward it so the proxied active learns its follow-peer
        // source exactly like an unproxied pull's.
        let announcing = announced_peer(target);
        let (status, body) = if !(target == "/checkpoint" || target.starts_with("/checkpoint?")) {
            (404, "not found".to_string())
        } else {
            let rewrite = *self.rewrite.lock().unwrap();
            let pulled = match announcing {
                Some(peer) => client.checkpoint_announcing(peer),
                None => client.checkpoint(),
            };
            match pulled {
                Ok(checkpoint) => (200, rewritten(&checkpoint, rewrite)),
                Err(error) => (500, error.to_string()),
            }
        };
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = stream.write_all(response.as_bytes());
    }
}

/// The `peer=` announcement a tracking pull carries on its request
/// target — `None` for a plain `GET /checkpoint`.
fn announced_peer(target: &str) -> Option<SocketAddr> {
    target.split_once('?')?.1.split('&').find_map(|pair| {
        pair.strip_prefix("peer=")
            .and_then(|value| value.parse().ok())
    })
}

/// The document the fixture serves under `rewrite`: the live active
/// checkpoint as the older, newer, or foreign build would have
/// serialized it.
fn rewritten(checkpoint: &Checkpoint, rewrite: Rewrite) -> String {
    let mut document = serde_json::to_value(checkpoint).unwrap();
    match rewrite {
        Rewrite::Intact => {}
        Rewrite::VersionZero => {
            document.as_object_mut().unwrap().remove("format_version");
        }
        Rewrite::Unsupported => {
            document["format_version"] = UNSUPPORTED_FORMAT_VERSION.into();
        }
        Rewrite::ForeignFingerprint => {
            document["model_fingerprint"] = serde_json::to_value(foreign()).unwrap();
        }
    }
    serde_json::to_string(&document).unwrap()
}

/// The raw JSON document the fixture currently serves on
/// `GET /checkpoint` — the wire shape itself, for asserting which
/// negotiated fields the document carries.
fn served_document(client: &MonitorClient) -> serde_json::Value {
    let (status, body) = client.request("GET", "/checkpoint", None).unwrap();
    assert_eq!(status, 200, "{body}");
    serde_json::from_str(&body).unwrap()
}

/// What one scripted run observed — the acceptance criteria in
/// comparable form.
#[derive(Debug)]
struct Outcome {
    /// The standby's reported convergence at the end of the version-0
    /// tracking phase.
    converged: StandbySync,
    /// The degraded sync state each refused leg reported on `GET /role`.
    unsupported_sync: StandbySync,
    fingerprint_sync: StandbySync,
    /// The structured apply error the crafted pull produced — the named
    /// `RestoreError` the negotiation answered with.
    unsupported_apply: ApplyError,
    fingerprint_apply: ApplyError,
    /// What `POST /promote` answered on each degraded state.
    unsupported_promote: SwitchError,
    fingerprint_promote: SwitchError,
    /// The standby's reported convergence after the version-0 stream
    /// resynchronized it — `tracking` again.
    recovered: StandbySync,
    /// Per-cycle `(valve, level)` the shared field carried across the
    /// whole run — the active's writes while the standby was degraded,
    /// the promoted peer's after the switch.
    field_trace: Vec<(Value, Value)>,
    /// The role `POST /promote` reported once converged.
    promoted_role: Role,
    /// The role transitions each peer's journal recorded.
    standby_roles: Vec<(Role, Role)>,
    active_roles: Vec<(Role, Role)>,
    /// The peers' settled role reports at the end.
    final_standby: RoleReport,
    final_active: RoleReport,
    /// Divergence detections journaled on the standby — none expected:
    /// a refused checkpoint leaves the run on its last-good alignment,
    /// which keeps tracking the field.
    divergences: usize,
    /// The model's fingerprint — the `expected` half of the mismatch.
    model_fingerprint: ModelFingerprint,
}

/// One scripted run of the negotiation scenario: converge on the
/// version-0 stream, refuse the unsupported and foreign-fingerprint
/// checkpoints by name while the active runs untouched, reconverge, and
/// complete the decision-15 switchover bumplessly.
fn run_scenario() -> Outcome {
    let model = PlantModel::load(TANK_LOOP).unwrap();
    let registry = registry();
    let plant = std::sync::Arc::new(
        PlantServer::bind(
            ("127.0.0.1", 0),
            SimDriver::new(sim_channel_map(&model).unwrap()).unwrap(),
        )
        .unwrap(),
    );
    let _plant = ShutdownOnDrop(&*plant);
    let plant_addr = plant.local_addr().unwrap();

    // The plant serves before the peers construct: a launched active's
    // startup claim needs the server answering, and a bound-but-unserved
    // listener lets a connect through while the claim request waits for
    // nobody.
    let serving = thread::spawn({
        let plant = std::sync::Arc::clone(&plant);
        move || plant.serve()
    });

    // The active: field-owning from the start, its driven monitor
    // stepping the shared plant inside each requested scan — the
    // `--driven` wiring.
    let active_driver = RemoteDriver::connect(plant_addr).unwrap();
    let active_gate = WriteGate::closed(&active_driver);
    let mut active = Peer::active(
        assemble(&model, &registry, &active_gate).unwrap(),
        Some(&active_gate),
    )
    .with_field_claim(|| {
        active_driver
            .claim_writer(1)
            .map(|_| ())
            .map_err(|error| error.to_string())
    });
    active.activate().unwrap();
    let active_step = &active_driver;
    let active_monitor = Monitor::bind_peer(("127.0.0.1", 0), active, model.signal_index())
        .unwrap()
        .with_pair_key(PAIR_KEY)
        .driven(Driven {
            track: None,
            after_scan: Some(Box::new(move |peer: &Peer<'_>| {
                if peer.owns_field() {
                    active_step
                        .step(DT)
                        .map(|_| ())
                        .map_err(|error| format!("plant step failed: {error}"))
                } else {
                    Ok(())
                }
            })),
        });
    let active_client = MonitorClient::new(active_monitor.local_addr());

    // The new-build standby: tracking through its driven pull — but the
    // track address names the fixture endpoint, so the scripted run
    // controls the wire shape each pull delivers.
    let standby_driver = RemoteDriver::connect(plant_addr).unwrap();
    let standby_gate = WriteGate::closed(&standby_driver);
    let standby = Peer::standby(
        assemble(&model, &registry, &standby_gate).unwrap(),
        Some(&standby_gate),
    )
    .with_field_claim(|| {
        standby_driver
            .claim_writer(2)
            .map(|_| ())
            .map_err(|error| error.to_string())
    });
    let fixture = CheckpointFixture::bind(active_monitor.local_addr());
    let standby_step = &standby_driver;
    let standby_monitor = Monitor::bind_peer(("127.0.0.1", 0), standby, model.signal_index())
        .unwrap()
        .with_pair_key(PAIR_KEY)
        .driven(Driven {
            track: Some(fixture.addr),
            after_scan: Some(Box::new(move |peer: &Peer<'_>| {
                if peer.owns_field() {
                    standby_step
                        .step(DT)
                        .map(|_| ())
                        .map_err(|error| format!("plant step failed: {error}"))
                } else {
                    Ok(())
                }
            })),
        });
    let standby_client = MonitorClient::new(standby_monitor.local_addr());
    let fixture_client = MonitorClient::new(fixture.addr);

    // An observer on the shared field — what the pair's writes actually
    // did, and the run's operating point once the plant is serving. The
    // active already owns the field, so the observer's setup write rides
    // the same claim token.
    let field = RemoteDriver::connect(plant_addr).unwrap();
    field.claim_writer(1).unwrap();

    let outcome = thread::scope(|scope| {
        scope.spawn(|| active_monitor.serve());
        let _active_monitor = ShutdownOnDrop(&active_monitor);
        scope.spawn(|| standby_monitor.serve());
        let _standby_monitor = ShutdownOnDrop(&standby_monitor);
        scope.spawn(|| fixture.serve());
        let _fixture = ShutdownOnDrop(&fixture);

        // A mid-range setpoint, written through the observer attachment:
        // plant-side, so no command receipt divides the peers' runs.
        field.write(SETPOINT, Value::Float(50.0)).unwrap();

        let mut field_trace = Vec::new();
        let mut tick = |owner: &TelemetrySnapshot| {
            field_trace.push((
                field.read(VALVE).unwrap().value,
                field.read(LEVEL).unwrap().value,
            ));
            assert_eq!(
                field.read(VALVE).unwrap().value,
                image_value(owner, VALVE),
                "the field must carry only the field owner's writes"
            );
        };

        // Phase 1 — the older supported version: the fixture serves the
        // version-0 wire shape (`format_version` absent), and the
        // standby converges on it and tracks tick for tick.
        fixture.set(Rewrite::VersionZero);
        let document = served_document(&fixture_client);
        assert!(
            !document.as_object().unwrap().contains_key("format_version"),
            "the version-0 wire shape carries no format_version field: {document}"
        );
        assert_eq!(fixture_client.checkpoint().unwrap().format_version, 0);

        for _ in 0..N {
            let tracked = standby_client.advance(1).unwrap();
            let owner = active_client.advance(1).unwrap();
            assert_eq!(
                tracked, owner,
                "the version-0 checkpoint must track exactly"
            );
            tick(&owner);
        }
        let report = standby_client.role().unwrap();
        assert_eq!(report.role, Role::Standby);
        let converged = report.sync.clone().unwrap();
        assert_eq!(
            converged,
            StandbySync::Tracking {
                aligned: Tick(N - 1)
            },
            "the version-0 stream must converge the standby"
        );

        // Phase 2 — an unsupported format version: the pull delivers
        // the crafted document, the negotiation refuses it by name, the
        // standby reports `degraded` carrying the found and supported
        // versions, and promotion stays refused — while the active's
        // run and field writes continue undisturbed.
        fixture.set(Rewrite::Unsupported);
        let document = served_document(&fixture_client);
        assert_eq!(
            document["format_version"], UNSUPPORTED_FORMAT_VERSION,
            "{document}"
        );
        let unsupported = RestoreError::UnsupportedVersion {
            found: UNSUPPORTED_FORMAT_VERSION,
            supported: SUPPORTED_FORMAT_VERSIONS,
        };

        standby_client.advance(1).unwrap();
        let owner = active_client.advance(1).unwrap();
        tick(&owner);
        let report = standby_client.role().unwrap();
        let unsupported_sync = report.sync.unwrap();
        assert_eq!(report.role, Role::Standby);
        assert_eq!(
            unsupported_sync,
            StandbySync::Degraded {
                detail: unsupported.to_string()
            },
            "the degraded state must name the UnsupportedVersion content"
        );

        // The same pull through the fixture, applied under the monitor's
        // lock, surfaces the structured named rejection.
        let crafted = fixture_client.checkpoint().unwrap();
        assert_eq!(crafted.format_version, UNSUPPORTED_FORMAT_VERSION);
        let unsupported_apply = standby_monitor.apply_checkpoint(&crafted).unwrap_err();
        assert_eq!(unsupported_apply, ApplyError::Restore(unsupported.clone()));

        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        let unsupported_promote = serde_json::from_str::<SwitchError>(&body).unwrap();
        assert_eq!(
            unsupported_promote,
            SwitchError::NotConverged {
                sync: unsupported_sync.clone()
            }
        );
        assert!(!standby_gate.is_open());
        assert_eq!(active_client.role().unwrap().role, Role::Active);

        // Phase 3 — a fingerprint mismatch: a checkpoint captured under
        // a different model, refused by name the same way.
        assert_ne!(foreign(), model.fingerprint());
        fixture.set(Rewrite::ForeignFingerprint);
        let mismatch = RestoreError::FingerprintMismatch {
            found: Some(foreign()),
            expected: Some(model.fingerprint()),
        };

        standby_client.advance(1).unwrap();
        let owner = active_client.advance(1).unwrap();
        tick(&owner);
        let fingerprint_sync = standby_client.role().unwrap().sync.unwrap();
        assert_eq!(
            fingerprint_sync,
            StandbySync::Degraded {
                detail: mismatch.to_string()
            },
            "the degraded state must name the FingerprintMismatch content"
        );

        let crafted = fixture_client.checkpoint().unwrap();
        assert_eq!(crafted.model_fingerprint, Some(foreign()));
        let fingerprint_apply = standby_monitor.apply_checkpoint(&crafted).unwrap_err();
        assert_eq!(fingerprint_apply, ApplyError::Restore(mismatch.clone()));

        let (status, body) = standby_client.request("POST", "/promote", None).unwrap();
        assert_eq!(status, 409, "{body}");
        let fingerprint_promote = serde_json::from_str::<SwitchError>(&body).unwrap();
        assert_eq!(
            fingerprint_promote,
            SwitchError::NotConverged {
                sync: fingerprint_sync.clone()
            }
        );
        assert!(!standby_gate.is_open());
        assert_eq!(active_client.role().unwrap().role, Role::Active);

        // Phase 4 — recovery: the version-0 stream is named-and-
        // recoverable — the next good transfer reconverges the standby.
        fixture.set(Rewrite::VersionZero);
        for _ in 0..2 {
            standby_client.advance(1).unwrap();
            let owner = active_client.advance(1).unwrap();
            tick(&owner);
        }
        let recovered = standby_client.role().unwrap().sync.unwrap();
        assert!(
            matches!(recovered, StandbySync::Tracking { .. }),
            "the version-0 stream must reconverge the standby, got {recovered:?}"
        );

        // Phase 5 — the switchover the negotiation exists to enable:
        // demote the active first, then promote the converged standby —
        // the gate moves at each request's scan boundary, and the
        // promoted peer's continuation equals the demoted peer's
        // quiesced run while the field carries its writes.
        let demoted = active_client.demote().unwrap();
        assert_eq!(demoted.role, Role::Demoting);
        assert!(!active_gate.is_open());
        let promoted = standby_client.promote().unwrap();
        assert_eq!(promoted.role, Role::Promoting);
        assert!(standby_gate.is_open());

        for _ in 0..M {
            let quiesced = active_client.advance(1).unwrap();
            let continued = standby_client.advance(1).unwrap();
            assert_eq!(
                continued, quiesced,
                "the promoted peer must continue the checkpointed run bumplessly"
            );
            tick(&continued);
        }

        let final_standby = standby_client.role().unwrap();
        assert_eq!(final_standby.role, Role::Active);
        assert_eq!(final_standby.sync, None);
        let final_active = active_client.role().unwrap();
        assert_eq!(final_active.role, Role::Standby);
        assert!(
            matches!(final_active.sync, Some(StandbySync::Tracking { .. })),
            "the demoted peer follows its successor's announced address \
             and reconverges: {final_active:?}"
        );

        let role_changes = |client: &MonitorClient| -> Vec<(Role, Role)> {
            client
                .journal(0)
                .unwrap()
                .iter()
                .filter_map(|entry| match entry.event {
                    JournalEvent::RoleChanged { from, to } => Some((from, to)),
                    _ => None,
                })
                .collect()
        };
        let standby_roles = role_changes(&standby_client);
        assert_eq!(
            standby_roles,
            vec![
                (Role::Standby, Role::Promoting),
                (Role::Promoting, Role::Active),
            ]
        );
        let active_roles = role_changes(&active_client);
        assert_eq!(
            active_roles,
            vec![
                (Role::Active, Role::Demoting),
                (Role::Demoting, Role::Standby),
            ]
        );
        let divergences = standby_client
            .journal(0)
            .unwrap()
            .iter()
            .filter(|entry| matches!(entry.event, JournalEvent::DivergenceDetected { .. }))
            .count();
        assert_eq!(
            divergences, 0,
            "a refused checkpoint leaves the run tracking the field — no divergence"
        );

        Outcome {
            converged,
            unsupported_sync,
            fingerprint_sync,
            unsupported_apply,
            fingerprint_apply,
            unsupported_promote,
            fingerprint_promote,
            recovered,
            field_trace,
            promoted_role: promoted.role,
            standby_roles,
            active_roles,
            final_standby,
            final_active,
            divergences,
            model_fingerprint: model.fingerprint(),
        }
    });
    drop(_plant);
    serving.join().unwrap();
    outcome
}

#[test]
fn an_older_supported_version_converges_the_standby_and_promotion_proceeds() {
    let outcome = run_scenario();
    assert_eq!(
        outcome.converged,
        StandbySync::Tracking {
            aligned: Tick(N - 1)
        }
    );
    assert_eq!(outcome.promoted_role, Role::Promoting);
    assert_eq!(outcome.final_standby.role, Role::Active);
    assert_eq!(outcome.final_active.role, Role::Standby);
}

#[test]
fn unsupported_versions_and_fingerprint_mismatches_are_named_refusals() {
    let outcome = run_scenario();

    // The degraded state names the found and supported versions.
    let StandbySync::Degraded { detail } = &outcome.unsupported_sync else {
        panic!("sync must be degraded: {:?}", outcome.unsupported_sync);
    };
    assert!(detail.contains("unsupported checkpoint format version"));
    assert!(detail.contains(&UNSUPPORTED_FORMAT_VERSION.to_string()));
    assert!(detail.contains(&format!("{SUPPORTED_FORMAT_VERSIONS:?}")));
    assert_eq!(
        outcome.unsupported_apply,
        ApplyError::Restore(RestoreError::UnsupportedVersion {
            found: UNSUPPORTED_FORMAT_VERSION,
            supported: SUPPORTED_FORMAT_VERSIONS,
        })
    );
    assert_eq!(
        outcome.unsupported_promote,
        SwitchError::NotConverged {
            sync: outcome.unsupported_sync.clone()
        }
    );

    // And the fingerprint mismatch names both fingerprints.
    let StandbySync::Degraded { detail } = &outcome.fingerprint_sync else {
        panic!("sync must be degraded: {:?}", outcome.fingerprint_sync);
    };
    assert!(detail.contains(&foreign().to_string()));
    assert_eq!(
        outcome.fingerprint_apply,
        ApplyError::Restore(RestoreError::FingerprintMismatch {
            found: Some(foreign()),
            expected: Some(outcome.model_fingerprint),
        })
    );
    assert_eq!(
        outcome.fingerprint_promote,
        SwitchError::NotConverged {
            sync: outcome.fingerprint_sync.clone()
        }
    );

    // And the active ran undisturbed throughout, its role and the
    // field's writes its own until the documented switchover.
    assert_eq!(outcome.divergences, 0);
}

#[test]
fn identical_scripted_runs_produce_identical_outcomes() {
    let first = run_scenario();
    let second = run_scenario();
    assert_eq!(first.field_trace, second.field_trace);
    assert_eq!(first.converged, second.converged);
    assert_eq!(first.unsupported_sync, second.unsupported_sync);
    assert_eq!(first.fingerprint_sync, second.fingerprint_sync);
    assert_eq!(first.unsupported_apply, second.unsupported_apply);
    assert_eq!(first.fingerprint_apply, second.fingerprint_apply);
    assert_eq!(first.unsupported_promote, second.unsupported_promote);
    assert_eq!(first.fingerprint_promote, second.fingerprint_promote);
    assert_eq!(first.recovered, second.recovered);
    assert_eq!(first.standby_roles, second.standby_roles);
    assert_eq!(first.active_roles, second.active_roles);
    assert_eq!(first.final_standby, second.final_standby);
    assert_eq!(first.final_active, second.final_active);
    assert_eq!(first.divergences, second.divergences);
}
