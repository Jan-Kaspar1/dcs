//! The M5 lifecycle verification — the milestone's done criteria walked
//! end to end as one scripted, tick-paced run on the two-peer rig over
//! the shared simulated plant, composing the shipped lifecycle features
//! rather than adding machinery.
//!
//! The rig: a `dcs-plant-server` process owns the shared tank-loop
//! plant, and the pair runs `--driven` — every scan happens inside a
//! `POST /scan` request, so the whole run is request-timed. The
//! standby's checkpoint-pull heartbeat runs through a TCP relay the
//! script partitions, into a checkpoint fixture that proxies the
//! active's `GET /checkpoint` and can rewrite the wire shape — the two
//! knobs the loss-detection and negotiation legs need.
//!
//! The legs, in script order — the launched active holds the plant's
//! writer claim from boot and the script's field attachment shares it
//! under the pinned `--owner-token`, so legs needing a field-side write
//! still run before the takeover legs re-token the claim:
//!
//! 1. **Checkpoint negotiation (#66, #133)** — the standby converges on
//!    a version-0 wire shape (`format_version` absent); an unsupported
//!    `format_version` and a foreign `model_fingerprint` are each
//!    refused with their named negotiation error — `degraded` carrying
//!    the `RestoreError` text, `POST /promote` answering `409` with
//!    `not_converged` — and the intact stream reconverges it.
//! 2. **Standby divergence (#88)** — a field-side write lands between
//!    the standby's staged scan and its next pull, so the same-tick
//!    compare reports `diverged` naming both sides' values, promotion
//!    answers `not_converged`, and a clean transfer resynchronizes it.
//! 3. **State-file restart (#115)** — the active's process dies and its
//!    replacement resumes the run from `--state-file` at the persisted
//!    tick, the held operator value carried; the pair cadence continues
//!    without a missed transfer.
//! 4. **Automatic failover (#65)** — the relay drops the heartbeat path;
//!    the converged standby self-promotes at the budget-th miss's scan
//!    boundary, takes the plant's writer claim, and the superseded
//!    peer's next field write answers `fenced` — the plant's verdict,
//!    which demotes the still-running peer in place: gate re-closed,
//!    role walking `demoting` to `standby`, the claim loss journaled —
//!    the shared-field single-writer rule honored without killing the
//!    superseded process.
//! 5. **Rolling model revision (#87)** — a `--revised` standby on model
//!    v2 crosses the fingerprint boundary into the named `reinitialized`
//!    state carrying its carryover report; the documented
//!    demote-then-promote order moves the field writer to the revised
//!    model.
//!
//! Every named state and error is asserted inside the run; the digest
//! the run returns proves repeated scripted runs identical.

use dcs_core::{
    CarriedPoint, Command, CommandOutcome, Divergence, DroppedElement, IoDriver, IoError,
    JournalEvent, ModelFingerprint, PointId, Role, StandbySync, SwitchError, TelemetrySnapshot,
    Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::{CHECKPOINT_FORMAT_VERSION, Checkpoint, RestoreError, SUPPORTED_FORMAT_VERSIONS};
use dcs_sim_net::RemoteDriver;
use std::io::{ErrorKind, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

mod support;

use support::{
    SimTcp, image_value, kill, pump, sim_tcp_document, spawn_controller, spawn_controller_logged,
    spawn_plant, write_model,
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
/// The model document the controller-side models revise from.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time advanced per scan — the model PID's configured dt.
const DT: &str = "0.1";
/// The consecutive-miss failover budget the standby is armed with —
/// the stated bound: self-promotion lands at the third lost pull's
/// scan boundary.
const BUDGET: u32 = 3;
/// Ticks the active runs ahead before the standby's first pull — long
/// enough for the commanded operator values to enter the checkpoint
/// stream, so the standby's first staged image describes the run the
/// field already carries.
const LEAD: u64 = 4;
/// Converged ticks the pair runs on the version-0 stream.
const N: u64 = 8;
/// Promoted-peer field ticks between the failover and revision legs.
const M: u64 = 6;
/// Revised-model field ticks closing the run.
const M2: u64 = 10;
/// The field-ownership token the launched active pins via
/// `--owner-token` — the claim the test's field attachment shares, so
/// the divergence leg's skewed write keeps passing the plant's fencing
/// while the active owns the field (and across the state-file restart,
/// whose respawned process claims the same token).
const OWNER_TOKEN: u64 = 499_003;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// v1's second held operator value — the revision drops it.
const DROPPED_KNOB: PointId = PointId(31);
/// The revision's new held operator value — initialized, never carried.
const NEW_KNOB: PointId = PointId(32);
/// The operator setpoint value the run carries across the boundary.
const OPERATING_POINT: f64 = 33.5;
/// The format version no build accepts — the refused newer wire shape.
const UNSUPPORTED_FORMAT_VERSION: u32 = 99;
/// The field-side write the divergence leg lands on the shared valve —
/// outside the controller's clamped operating range, so the same-tick
/// compare cannot mistake it for the staged output.
const SKEWED_FIELD: f64 = -42.5;

/// The base controller-side document: the tank loop with its devices
/// re-pointed at `sim-tcp` carrying the plant's address, the setpoint
/// point 11 moved image-side — an internal writable `In` point, the
/// operator value the carryover rule is about — and a second held
/// operator value at point 31.
fn base_model(plant: SocketAddr) -> serde_json::Value {
    let mut document = sim_tcp_document(MODEL_SOURCE, plant, SimTcp::PerDevice);
    let points = document["io_points"].as_array_mut().unwrap();
    let setpoint = points
        .iter_mut()
        .find(|point| point["id"] == SETPOINT.0)
        .unwrap();
    setpoint.as_object_mut().unwrap().remove("channel");
    setpoint["initial"] = serde_json::json!({ "float": 25.0 });
    points.push(serde_json::json!({
        "id": DROPPED_KNOB.0,
        "direction": "in",
        "value_type": "float",
        "writable": true,
        "initial": { "float": 0.0 }
    }));
    document
}

/// Model v1, as written under `dir`: the base document unchanged.
fn v1_model(dir: &Path, name: &str, plant: SocketAddr) -> (PathBuf, PlantModel) {
    write_model(dir, name, &base_model(plant))
}

/// Model v2, as written under `dir`: point 31 gone, a new held operator
/// value at point 32, and the PID component re-idd 2 → 3 — the
/// checkpoint's `"pid:2"` state entry names a component the revision
/// does not register.
fn v2_model(dir: &Path, name: &str, plant: SocketAddr) -> (PathBuf, PlantModel) {
    let mut document = base_model(plant);
    let points = document["io_points"].as_array_mut().unwrap();
    points.retain(|point| point["id"] != DROPPED_KNOB.0);
    points.push(serde_json::json!({
        "id": NEW_KNOB.0,
        "direction": "in",
        "value_type": "float",
        "writable": true,
        "initial": { "float": 5.0 }
    }));
    let pid = document["components"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|component| component["kind"] == "pid")
        .unwrap();
    pid["id"] = 3.into();
    for connection in document["connections"].as_array_mut().unwrap() {
        for end in ["from", "to"] {
            if let Some(component) = connection[end]["port"]["component"].as_u64()
                && component == 2
            {
                connection[end]["port"]["component"] = 3.into();
            }
        }
    }
    write_model(dir, name, &document)
}

/// Asserts the field carries exactly `owner`'s last write and records
/// the `(valve, level)` pair the run's trace must reproduce.
fn field_carry(field: &RemoteDriver, owner: &TelemetrySnapshot, trace: &mut Vec<(Value, Value)>) {
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(
        carried,
        image_value(owner, VALVE),
        "the field must carry only the field owner's writes"
    );
    trace.push((carried, field.read(LEVEL).unwrap().value));
}

/// A fingerprint no checkpoint in this run can legitimately carry —
/// the foreign-document knob.
fn foreign() -> ModelFingerprint {
    ModelFingerprint::of(b"a different plant model")
}

/// The wire shapes the fixture can serve.
#[derive(Clone, Copy)]
enum Rewrite {
    /// The active's document unchanged.
    Intact,
    /// A pre-versioning build's document: `format_version` absent —
    /// the oldest supported wire shape.
    VersionZero,
    /// A document declaring `UNSUPPORTED_FORMAT_VERSION` — the refused
    /// too-new shape.
    Unsupported,
    /// A document captured under a different model — the refused
    /// foreign fingerprint.
    ForeignFingerprint,
}

/// The state the fixture's serve thread shares with the test.
struct FixtureShared {
    /// The monitor the fixture currently proxies — switched to the
    /// restarted process at the state-file leg.
    upstream: Mutex<SocketAddr>,
    /// The wire shape to serve.
    rewrite: Mutex<Rewrite>,
    /// Set false to stop the accept loop.
    running: AtomicBool,
}

/// A `GET /checkpoint` proxy in front of the active monitor: each
/// connection is answered with the upstream checkpoint rewritten to the
/// configured wire shape. The standby's `--standby` names the relay in
/// front of this fixture, so the scripted run controls what every
/// driven pull delivers.
struct CheckpointFixture {
    addr: SocketAddr,
    shared: Arc<FixtureShared>,
    serve: Option<JoinHandle<()>>,
}

impl CheckpointFixture {
    /// Binds a fixture proxying `upstream`'s checkpoints.
    fn bind(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let addr = listener.local_addr().unwrap();
        let shared = Arc::new(FixtureShared {
            upstream: Mutex::new(upstream),
            rewrite: Mutex::new(Rewrite::Intact),
            running: AtomicBool::new(true),
        });
        let serve = {
            let shared = Arc::clone(&shared);
            thread::spawn(move || {
                while shared.running.load(Ordering::Relaxed) {
                    match listener.accept() {
                        Ok((mut stream, _)) => answer_fixture(&shared, &mut stream),
                        Err(error) if error.kind() == ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(1));
                        }
                        Err(_) => break,
                    }
                }
            })
        };
        Self {
            addr,
            shared,
            serve: Some(serve),
        }
    }

    /// Switches the wire shape served from now on.
    fn set(&self, rewrite: Rewrite) {
        *self.shared.rewrite.lock().unwrap() = rewrite;
    }

    /// Points the proxy at a different upstream — the restarted active.
    fn set_upstream(&self, upstream: SocketAddr) {
        *self.shared.upstream.lock().unwrap() = upstream;
    }
}

impl Drop for CheckpointFixture {
    fn drop(&mut self) {
        self.shared.running.store(false, Ordering::Relaxed);
        if let Some(serve) = self.serve.take() {
            let _ = serve.join();
        }
    }
}

/// Answers one fixture connection: read the request head, then serve
/// the proxied-and-rewritten checkpoint document.
fn answer_fixture(shared: &FixtureShared, stream: &mut TcpStream) {
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
    // forward it so the proxied upstream learns its follow-peer source
    // exactly like an unproxied pull's.
    let announcing = announced_peer(target);
    let (status, body) = if !(target == "/checkpoint" || target.starts_with("/checkpoint?")) {
        (404, "not found".to_string())
    } else {
        let upstream = *shared.upstream.lock().unwrap();
        let rewrite = *shared.rewrite.lock().unwrap();
        let client = MonitorClient::new(upstream);
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

/// A controllable network path for the checkpoint-pull heartbeat: while
/// `partitioned` is clear the relay forwards each connection to the
/// fixture; while set it accepts and immediately drops them — the
/// refused/EOF failure a partitioned or dead peer produces. The flag is
/// only ever flipped between scripted ticks, so a pull's verdict is
/// never racy.
struct Relay {
    addr: SocketAddr,
    partitioned: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<()>>,
}

impl Relay {
    /// A relay forwarding to `upstream` — the fixture address the
    /// standby's `--standby` is pointed at.
    fn forwarding(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let partitioned = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let accept = {
            let partitioned = Arc::clone(&partitioned);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let Ok(stream) = stream else { continue };
                    if partitioned.load(Ordering::Relaxed) {
                        // Dropped on the floor: the pull sees a refused
                        // or immediately closed connection — fast, never
                        // a hang.
                        drop(stream);
                    } else {
                        thread::spawn(move || pump(stream, upstream));
                    }
                }
            })
        };
        Self {
            addr,
            partitioned,
            stop,
            accept: Some(accept),
        }
    }

    /// Drops or restores the heartbeat path mid-run.
    fn partition(&self, cut: bool) {
        self.partitioned.store(cut, Ordering::Relaxed);
    }
}

impl Drop for Relay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        // Wake the blocking accept so the loop observes the flag.
        let _ = TcpStream::connect(self.addr);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

/// Replaces the run-varying strings inside a serialized value —
/// fingerprints carry the plant's ephemeral address, degraded details
/// name the configured pull source — so two runs' digests compare.
fn masked(value: serde_json::Value, masks: &[(String, String)]) -> serde_json::Value {
    let mut text = serde_json::to_string(&value).unwrap();
    for (from, to) in masks {
        text = text.replace(from.as_str(), to);
    }
    serde_json::from_str(&text).unwrap()
}

/// The role transitions a peer's journal recorded.
fn role_changes(client: &MonitorClient) -> Vec<(Role, Role)> {
    client
        .journal(0)
        .unwrap()
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to } => Some((from, to)),
            _ => None,
        })
        .collect()
}

/// One scripted run of the full lifecycle — the M5 done criteria in
/// sequence on the two-peer rig, each named state and error asserted as
/// it lands. Returns the run's auditable record as JSON two runs must
/// reproduce exactly: the field's per-cycle trace, every named sync
/// state and error, the failover boundary, the carryover report, the
/// role transitions, and the settled final reports.
fn run_lifecycle(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-lifecycle-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let (v1_path, v1) = v1_model(&dir, "v1.json", plant.addr);
    let (v2_path, v2) = v2_model(&dir, "v2.json", plant.addr);
    assert_ne!(
        v1.fingerprint(),
        v2.fingerprint(),
        "the revision must fingerprint differently by design"
    );
    let state_path = dir.join("state.json");

    // The field-owning peer persists its run for the restart leg; the
    // standby's pull path runs relay → fixture → active so the script
    // controls both the path's liveness and each pull's wire shape.
    let mut active_process = spawn_controller(
        &v1_path,
        &[
            "--state-file".to_string(),
            state_path.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            OWNER_TOKEN.to_string(),
        ],
        DT,
    );
    let fixture = CheckpointFixture::bind(active_process.addr);
    let relay = Relay::forwarding(fixture.addr);
    let standby_process = spawn_controller(
        &v1_path,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--auto-promote".to_string(),
            BUDGET.to_string(),
        ],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    let fixture_client = MonitorClient::new(fixture.addr);
    let field = RemoteDriver::connect(plant.addr).unwrap();
    // The launched active holds the plant's single-writer claim from
    // startup: this attachment joins that claim — the pinned token's
    // other half — so the divergence leg's field-side write still lands
    // where any third attachment's would fence.
    field.claim_writer(OWNER_TOKEN).unwrap();

    // The run's operator inputs land by command — the held setpoint the
    // revision carries and the v1-only knob the revision drops.
    for (point, value) in [
        (SETPOINT, Value::Float(OPERATING_POINT)),
        (DROPPED_KNOB, Value::Float(7.0)),
    ] {
        let receipt = active
            .command(&Command::WriteValue {
                point,
                kind: ValueKind::Float,
                value,
            })
            .unwrap();
        assert!(
            matches!(
                receipt.outcome,
                CommandOutcome::Applied { .. } | CommandOutcome::Accepted { .. }
            ),
            "{receipt:?}"
        );
    }

    let mut trace: Vec<(Value, Value)> = Vec::new();

    // The field owner runs ahead — the commands apply at its first scan
    // boundary, so the checkpoint stream carries the commanded operator
    // values before the standby's first pull lands on one.
    for _ in 0..LEAD {
        let owner = active.advance(1).unwrap();
        field_carry(&field, &owner, &mut trace);
    }

    // -- Leg 1: checkpoint negotiation ---------------------------------
    // The standby's heartbeat pulls the version-0 wire shape — a
    // pre-versioning build's document, `format_version` absent — and
    // converges on it: the oldest supported format still negotiates.
    fixture.set(Rewrite::VersionZero);
    let document = served_document(&fixture_client);
    assert!(
        !document.as_object().unwrap().contains_key("format_version"),
        "the version-0 wire shape carries no format_version field: {document}"
    );
    assert_eq!(fixture_client.checkpoint().unwrap().format_version, 0);

    for _ in 0..N {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        assert_eq!(tracked.tick, owner.tick, "the pair must tick together");
        field_carry(&field, &owner, &mut trace);
    }
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    let converged = report.sync.unwrap();
    assert_eq!(
        converged,
        StandbySync::Tracking {
            aligned: Tick(LEAD + N - 1)
        },
        "the version-0 stream must converge the standby"
    );

    // An unsupported format version: the pull delivers the crafted
    // document, the negotiation refuses it with the named error, the
    // standby reports `degraded` carrying the found and supported
    // versions, and promotion stays refused — while the active's run
    // and field writes continue undisturbed.
    fixture.set(Rewrite::Unsupported);
    let document = served_document(&fixture_client);
    assert_eq!(
        document["format_version"], UNSUPPORTED_FORMAT_VERSION,
        "{document}"
    );
    assert_eq!(
        fixture_client.checkpoint().unwrap().format_version,
        UNSUPPORTED_FORMAT_VERSION
    );
    let unsupported = RestoreError::UnsupportedVersion {
        found: UNSUPPORTED_FORMAT_VERSION,
        supported: SUPPORTED_FORMAT_VERSIONS,
    };

    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let unsupported_sync = standby.role().unwrap().sync.unwrap();
    assert_eq!(
        unsupported_sync,
        StandbySync::Degraded {
            detail: unsupported.to_string()
        },
        "the degraded state must name the UnsupportedVersion content"
    );
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    let unsupported_promote = serde_json::from_str::<SwitchError>(&body).unwrap();
    assert_eq!(
        unsupported_promote,
        SwitchError::NotConverged {
            sync: unsupported_sync.clone()
        }
    );
    assert_eq!(active.role().unwrap().role, Role::Active);

    // A foreign fingerprint: a checkpoint captured under a different
    // model — refused with the named mismatch the same way.
    assert_ne!(foreign(), v1.fingerprint());
    fixture.set(Rewrite::ForeignFingerprint);
    let document = served_document(&fixture_client);
    assert_eq!(
        document["model_fingerprint"],
        serde_json::to_value(foreign()).unwrap(),
        "{document}"
    );
    let mismatch = RestoreError::FingerprintMismatch {
        found: Some(foreign()),
        expected: Some(v1.fingerprint()),
    };

    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let fingerprint_sync = standby.role().unwrap().sync.unwrap();
    assert_eq!(
        fingerprint_sync,
        StandbySync::Degraded {
            detail: mismatch.to_string()
        },
        "the degraded state must name the FingerprintMismatch content"
    );
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    let fingerprint_promote = serde_json::from_str::<SwitchError>(&body).unwrap();
    assert_eq!(
        fingerprint_promote,
        SwitchError::NotConverged {
            sync: fingerprint_sync.clone()
        }
    );

    // The intact stream reconverges the standby — a refused checkpoint
    // left the run on its last-good alignment, which the next clean
    // transfer resumes from.
    fixture.set(Rewrite::Intact);
    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let recovered = standby.role().unwrap().sync.unwrap();
    assert!(
        matches!(recovered, StandbySync::Tracking { .. }),
        "a clean transfer must reconverge a degraded standby: {recovered:?}"
    );

    // -- Leg 2: standby divergence --------------------------------------
    // The standby stages a tick, then a field-side write — an
    // attachment outside the pair, the rogue-writer case the check
    // exists for — moves the shared output the staged image describes.
    // The next pull's same-tick compare reports `diverged` naming both
    // sides' values.
    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    let staged_tick = owner.tick;
    let staged_valve = image_value(&standby.snapshot().unwrap(), VALVE);
    field_carry(&field, &owner, &mut trace);
    field.write(VALVE, Value::Float(SKEWED_FIELD)).unwrap();

    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let diverged = standby.role().unwrap().sync.unwrap();
    assert_eq!(
        diverged,
        StandbySync::Diverged {
            mismatches: vec![Divergence {
                point: VALVE,
                staged: staged_valve,
                field: Value::Float(SKEWED_FIELD),
            }]
        },
        "the same-tick compare must name the skewed write"
    );
    let journal = standby.journal(0).unwrap();
    let divergences: Vec<_> = journal
        .iter()
        .filter(|entry| matches!(entry.event, JournalEvent::DivergenceDetected { .. }))
        .collect();
    assert_eq!(
        divergences.len(),
        1,
        "the transition is journaled once: {journal:?}"
    );
    assert_eq!(
        divergences[0].tick, staged_tick,
        "the divergence is attributed to the compared tick"
    );

    // The named promotion refusal carries the named state.
    let (status, body) = standby.request("POST", "/promote", None).unwrap();
    assert_eq!(status, 409, "{body}");
    let diverged_promote = serde_json::from_str::<SwitchError>(&body).unwrap();
    assert_eq!(
        diverged_promote,
        SwitchError::NotConverged {
            sync: diverged.clone()
        }
    );

    // The next clean transfer resynchronizes it — the field carried the
    // active's write again after its scan overwrote the skew.
    standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let resynced = standby.role().unwrap().sync.unwrap();
    assert!(
        matches!(resynced, StandbySync::Tracking { .. }),
        "a clean transfer must resync a diverged standby: {resynced:?}"
    );

    // -- Leg 3: state-file restart of the field owner -------------------
    // The active's process dies; its replacement resumes the persisted
    // run — the file's checkpoint names the model, the tick, and the
    // held operator value — and the pair cadence continues without a
    // missed transfer.
    let interrupted = active.snapshot().unwrap().tick;
    kill(&mut active_process);
    let persisted: Checkpoint =
        serde_json::from_slice(&std::fs::read(&state_path).unwrap()).unwrap();
    assert_eq!(persisted.tick, interrupted);
    assert_eq!(persisted.format_version, CHECKPOINT_FORMAT_VERSION);
    assert_eq!(persisted.model_fingerprint, Some(v1.fingerprint()));
    assert_eq!(
        persisted.internal[&SETPOINT].value,
        Value::Float(OPERATING_POINT),
        "the held operator value persists in the state file"
    );

    let (mut resumed_process, preamble) = spawn_controller_logged(
        &v1_path,
        &[
            "--state-file".to_string(),
            state_path.to_str().unwrap().to_string(),
            "--owner-token".to_string(),
            OWNER_TOKEN.to_string(),
        ],
        DT,
    );
    let resumed = MonitorClient::new(resumed_process.addr);
    assert!(
        preamble.iter().any(|line| {
            line.contains("resumed from state file")
                && line.contains(&format!("at tick {}", interrupted.0))
        }),
        "the restart must report the resume: {preamble:?}"
    );
    let resumed_snapshot = resumed.snapshot().unwrap();
    assert_eq!(resumed_snapshot.tick, interrupted);
    assert_eq!(
        image_value(&resumed_snapshot, SETPOINT),
        Value::Float(OPERATING_POINT),
        "the resumed run carries the held operator value"
    );

    // The fixture's proxy follows the restart: the standby's pulls land
    // on the resumed active, and the cadence continues as if the
    // process never died.
    fixture.set_upstream(resumed_process.addr);
    let tracked = standby.advance(1).unwrap();
    let owner = resumed.advance(1).unwrap();
    assert_eq!(tracked.tick, Tick(interrupted.0 + 1));
    assert_eq!(owner.tick, Tick(interrupted.0 + 1));
    field_carry(&field, &owner, &mut trace);
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the resumed active's checkpoints must keep the standby tracking: {report:?}"
    );
    let mut continued_tick = owner.tick;
    for _ in 0..2 {
        standby.advance(1).unwrap();
        let owner = resumed.advance(1).unwrap();
        continued_tick = owner.tick;
        field_carry(&field, &owner, &mut trace);
    }

    // -- Leg 4: automatic failover with field-side fencing --------------
    // The heartbeat path partitions; the converged standby counts the
    // misses without promoting — the field keeps carrying the old
    // owner's writes under its held claim — and the budget-th miss's
    // scan boundary self-promotes it: the plant's writer claim taken
    // and the gate lifted inside the same requested scan.
    let at_partition = standby.snapshot().unwrap().tick;
    relay.partition(true);
    for miss in 1..BUDGET {
        standby.advance(1).unwrap();
        let owner = resumed.advance(1).unwrap();
        let report = standby.role().unwrap();
        assert_eq!(report.role, Role::Standby, "miss {miss} promoted early");
        assert_eq!(report.tick, Tick(at_partition.0 + miss as u64));
        assert!(
            matches!(report.sync, Some(StandbySync::Degraded { .. })),
            "miss {miss} should report degraded: {report:?}"
        );
        field_carry(&field, &owner, &mut trace);
    }

    let promotion_tick = at_partition.0 + BUDGET as u64;
    let promoted = standby.advance(1).unwrap();
    let report = standby.role().unwrap();
    assert_eq!(
        report.role,
        Role::Active,
        "self-promotion missed its boundary: {report:?}"
    );
    assert_eq!(report.tick.0, promotion_tick);
    let carried = field.read(VALVE).unwrap().value;
    assert_eq!(carried, image_value(&promoted, VALVE));
    trace.push((carried, field.read(LEVEL).unwrap().value));

    // The superseded peer — still running — is fenced at the field:
    // the plant refused its write inside the requested scan, counted
    // as the `fenced` fault while the scan completes degraded, and the
    // fenced boundary walks it down the demote path rather than ending
    // the process — gate re-closed, the reported role `demoting`, the
    // claim loss journaled. The field keeps only the new owner's
    // output throughout.
    let fenced_scan = resumed.advance(1).unwrap();
    assert_eq!(fenced_scan.tick, Tick(promotion_tick));
    assert!(
        matches!(
            fenced_scan
                .io_health
                .last_error
                .as_ref()
                .map(|fault| &fault.error),
            Some(IoError::Fenced(_))
        ),
        "the returning peer's write must be refused fenced: {:?}",
        fenced_scan.io_health
    );
    assert_eq!(field.read(VALVE).unwrap().value, carried);
    let superseded = resumed.role().unwrap();
    assert_eq!(
        superseded.role,
        Role::Demoting,
        "the fenced peer must adopt the demote path, not die: {superseded:?}"
    );
    assert!(
        resumed.journal(0).unwrap().iter().any(|entry| matches!(
            entry.event,
            JournalEvent::FieldClaimLost { point, .. } if point == VALVE
        )),
        "the fenced owner's journal must record the claim loss"
    );

    // The link heals — the demoted peer's monitor answers again — and
    // its first quiesced scan settles `standby`, its writes staying
    // behind the re-closed gate: the survivable degraded state the
    // single-writer rule resolves a superseded owner to. The promoted
    // peer announced itself through its pulls, so the demoted run's
    // first tracking cycle reconverges on its successor.
    relay.partition(false);
    let quiesced_scan = resumed.advance(1).unwrap();
    assert_eq!(quiesced_scan.tick, Tick(promotion_tick + 1));
    let settled = resumed.role().unwrap();
    assert_eq!(settled.role, Role::Standby);
    assert!(
        matches!(settled.sync, Some(StandbySync::Tracking { .. })),
        "the demoted peer must follow its successor and reconverge: {settled:?}"
    );
    assert_eq!(field.read(VALVE).unwrap().value, carried);
    assert_eq!(
        role_changes(&resumed),
        vec![
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby)
        ],
        "the demotion the fenced boundary drove must journal in order"
    );

    // The promoted peer owns the run: each tick the field carries
    // exactly its image's staged output.
    for _ in 0..M {
        let owner = standby.advance(1).unwrap();
        field_carry(&field, &owner, &mut trace);
    }
    // The fenced peer is retired — the run it superseded is over.
    kill(&mut resumed_process);

    // -- Leg 5: rolling model revision ----------------------------------
    // A `--revised` standby on model v2 joins the promoted peer: its
    // first pull crosses the fingerprint boundary through the
    // documented carryover rule — the named `reinitialized` state
    // carrying the report of what transferred, what initialized, and
    // what was named dropped.
    let revised_process = spawn_controller(
        &v2_path,
        &[
            "--standby".to_string(),
            standby_process.addr.to_string(),
            "--revised".to_string(),
        ],
        DT,
    );
    let revised = MonitorClient::new(revised_process.addr);
    let crossing_tick = standby.snapshot().unwrap().tick;
    revised.advance(1).unwrap();
    let owner = standby.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);

    let report = match revised.role().unwrap().sync {
        Some(StandbySync::Reinitialized { report }) => *report,
        other => panic!("expected a reinitialized standby, found {other:?}"),
    };
    assert_eq!(report.from, Some(v1.fingerprint()));
    assert_eq!(report.to, Some(v2.fingerprint()));
    assert_eq!(report.resumed_at, crossing_tick);
    // The held operator value carried by declared identity; the output
    // image's last write carried; everything without a continuation is
    // named: the removed operator point, the link wire's synthesized
    // internal point (allocated ids are not declared identities), the
    // new point the checkpoint cannot describe, and the re-idd PID's
    // checkpointed state.
    assert_eq!(
        report.carried,
        vec![CarriedPoint {
            point: SETPOINT,
            value: Value::Float(OPERATING_POINT),
        }]
    );
    assert_eq!(
        report.carried_outputs.len(),
        1,
        "the valve image sample carries: {report:?}"
    );
    assert_eq!(report.carried_outputs[0].point, VALVE);
    assert_eq!(
        report.dropped,
        vec![
            DroppedElement::InternalPoint {
                point: DROPPED_KNOB
            },
            DroppedElement::InternalPoint { point: PointId(33) },
            DroppedElement::OutputPoint { point: PointId(32) },
            DroppedElement::Component {
                name: "pid:2".to_string()
            },
        ]
    );
    assert_eq!(
        report.reinitialized,
        vec!["analog-input:1".to_string(), "pid:3".to_string()]
    );
    assert_eq!(report.initialized, vec![NEW_KNOB]);
    let revised_snapshot = revised.snapshot().unwrap();
    assert_eq!(
        image_value(&revised_snapshot, SETPOINT),
        Value::Float(OPERATING_POINT)
    );
    assert_eq!(image_value(&revised_snapshot, NEW_KNOB), Value::Float(5.0));
    // The crossing is journaled once — the transition, not every pull.
    let reinit_entries = revised
        .journal(0)
        .unwrap()
        .iter()
        .filter(|entry| matches!(entry.event, JournalEvent::Reinitialized { .. }))
        .count();
    assert_eq!(reinit_entries, 1, "one crossing journals once");

    // A second pull refreshes the crossing without re-journaling.
    revised.advance(1).unwrap();
    let owner = standby.advance(1).unwrap();
    field_carry(&field, &owner, &mut trace);
    let reinit_entries = revised
        .journal(0)
        .unwrap()
        .iter()
        .filter(|entry| matches!(entry.event, JournalEvent::Reinitialized { .. }))
        .count();
    assert_eq!(
        reinit_entries, 1,
        "the crossing refresh does not re-journal"
    );

    // The documented switchover at the scan boundary: demote the old
    // active — its gate closes with the request — then promote the
    // reinitialized peer. Exactly one writer throughout: the field
    // holds the old run's last write between the two requests, and the
    // demoted peer's scans never reach it.
    let demoted = standby.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let held = field.read(VALVE).unwrap().value;
    let promoted_revised = revised.promote().unwrap();
    assert_eq!(promoted_revised.role, Role::Promoting);
    // The demoted peer's tracking source is gone — the fenced old
    // active is retired — so its settle scan runs quiesced on a failed
    // pull: it reports `standby` and `degraded`, never promoted again
    // by its armed budget (a demoted peer's convergence proof reset).
    let _quiesced = standby.advance(1).unwrap();
    assert_eq!(field.read(VALVE).unwrap().value, held);
    let demoted_report = standby.role().unwrap();
    assert_eq!(demoted_report.role, Role::Standby);
    assert!(
        matches!(demoted_report.sync, Some(StandbySync::Degraded { .. })),
        "the retired tracking source leaves the demoted peer degraded: {demoted_report:?}"
    );

    // The revised model owns the field for the rest of the run.
    for _ in 0..M2 {
        let owner = revised.advance(1).unwrap();
        field_carry(&field, &owner, &mut trace);
    }
    let final_revised = revised.role().unwrap();
    assert_eq!(final_revised.role, Role::Active);
    assert_eq!(final_revised.sync, None);
    let final_revised_snapshot = revised.snapshot().unwrap();
    assert_eq!(
        image_value(&final_revised_snapshot, SETPOINT),
        Value::Float(OPERATING_POINT)
    );
    assert_eq!(
        image_value(&final_revised_snapshot, NEW_KNOB),
        Value::Float(5.0)
    );

    // The standby's journal carries the lifecycle's role transitions —
    // the self-promotion pair, then the manual demotion pair.
    assert_eq!(
        role_changes(&standby),
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active),
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby),
        ]
    );
    assert_eq!(
        role_changes(&revised),
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active)
        ]
    );

    // Strings that legitimately differ run to run — fingerprints carry
    // the plant's ephemeral address, degraded details name the pull
    // source — are masked before the digests compare; the assertions
    // above already pinned each value to its named source.
    let masks: Vec<(String, String)> = [
        // Fingerprints serialize as numbers in structured fields and as
        // hex inside detail strings — mask both forms.
        (v1.fingerprint().0.to_string(), "\"v1\""),
        (v2.fingerprint().0.to_string(), "\"v2\""),
        (v1.fingerprint().to_string(), "<v1>"),
        (v2.fingerprint().to_string(), "<v2>"),
        (plant.addr.to_string(), "<plant>"),
        (relay.addr.to_string(), "<relay>"),
        (fixture.addr.to_string(), "<fixture>"),
        (active_process.addr.to_string(), "<first-active>"),
        (resumed_process.addr.to_string(), "<resumed-active>"),
        (standby_process.addr.to_string(), "<standby>"),
        (revised_process.addr.to_string(), "<revised>"),
    ]
    .iter()
    .map(|(from, to)| (from.clone(), to.to_string()))
    .collect();

    let mut masked_report = masked(serde_json::to_value(&report).unwrap(), &masks);
    masked_report["from"] = "v1".into();
    masked_report["to"] = "v2".into();

    let digest = serde_json::json!({
        "field_trace": trace
            .iter()
            .map(|(valve, level)| [valve, level])
            .collect::<Vec<_>>(),
        "negotiation": {
            "converged": converged,
            "unsupported_sync": masked(serde_json::to_value(&unsupported_sync).unwrap(), &masks),
            "unsupported_promote": masked(serde_json::to_value(&unsupported_promote).unwrap(), &masks),
            "fingerprint_sync": masked(serde_json::to_value(&fingerprint_sync).unwrap(), &masks),
            "fingerprint_promote": masked(serde_json::to_value(&fingerprint_promote).unwrap(), &masks),
            "recovered": recovered,
        },
        "divergence": {
            "diverged": diverged,
            "journaled_at": divergences[0].tick,
            "promote_refusal": masked(serde_json::to_value(&diverged_promote).unwrap(), &masks),
            "resynced": resynced,
        },
        "restart": {
            "interrupted_at": interrupted,
            "persisted_version": persisted.format_version,
            "resumed_at": resumed_snapshot.tick,
            "continued_to": continued_tick,
        },
        "failover": {
            "partitioned_at": at_partition,
            "promoted_at": promotion_tick,
            "superseded": {
                "fenced_fault": fenced_scan
                    .io_health
                    .last_error
                    .as_ref()
                    .map(|fault| fault.error.to_string()),
                "fenced_scan": fenced_scan.tick,
                "settled_scan": quiesced_scan.tick,
                "roles": [superseded.role, settled.role],
                "sync": settled.sync,
            },
        },
        "revision": {
            "crossing_tick": crossing_tick,
            "report": masked_report,
            "demoted": masked(serde_json::to_value(&demoted).unwrap(), &masks),
            "promoted": masked(serde_json::to_value(&promoted_revised).unwrap(), &masks),
            "demoted_report": masked(serde_json::to_value(&demoted_report).unwrap(), &masks),
        },
        "transitions": {
            "standby": role_changes(&standby),
            "revised": role_changes(&revised),
        },
        "final": {
            "revised": masked(serde_json::to_value(&final_revised).unwrap(), &masks),
            "revised_snapshot": masked(serde_json::to_value(&final_revised_snapshot).unwrap(), &masks),
            "standby": masked(serde_json::to_value(&demoted_report).unwrap(), &masks),
            "field": [
                field.read(VALVE).unwrap().value,
                field.read(LEVEL).unwrap().value,
            ],
        },
    });

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

#[test]
fn the_m5_lifecycle_walks_every_done_criterion_in_one_scripted_run() {
    run_lifecycle("once");
}

#[test]
fn identical_lifecycle_runs_reproduce_the_identical_digest() {
    let first = run_lifecycle("a");
    let second = run_lifecycle("b");
    if first != second {
        let dir = std::env::temp_dir().join(format!("dcs-lifecycle-digest-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("first.json"),
            serde_json::to_string_pretty(&first).unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.join("second.json"),
            serde_json::to_string_pretty(&second).unwrap(),
        )
        .unwrap();
        panic!(
            "identical scripted runs produced different digests — see {}",
            dir.display()
        );
    }
}
