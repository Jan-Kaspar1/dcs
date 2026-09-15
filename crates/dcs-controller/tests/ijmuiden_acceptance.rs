//! The IJmuiden consequential-annunciation end-to-end acceptance run —
//! issue #302: the checked-in composition #297 emitted driven through
//! the redundant `dcs-controller --driven` pair over `dcs-plant-server`,
//! asserting the decision-75 seam map and the decision-77 protection
//! boundary through monitor-client, plant-protocol, and journal
//! surfaces. Every scan happens inside a `POST /scan` request — nothing
//! is wall-clock paced.
//!
//! `dcs-build/tests/ijmuiden.rs` proves the same scenario against the
//! in-process monitor; this run is its process-level mirror. The
//! checked-in `ijmuiden.json` declares the incident's two field sources
//! — the remote operating-position repeater and the independent
//! protection panel — as `sim-scripted` devices, a kind the plant
//! server's `SimDriver` does not implement: a scripted channel is a
//! driver-owned playback the shared plant cannot serve. The plant-side
//! document therefore keeps every point id, direction, flag, and wiring
//! verbatim and declares those channels plain simulated field points;
//! the scripted run then drives the model's *declared* schedule — read
//! out of the emitted document, not re-derived here — through the plant
//! protocol's write seam, the same boundary a real repeater's driver
//! would produce samples across. The controller-side model is the same
//! document with every device's channels merged onto one `sim-tcp`
//! device at the plant's address: one remote backend, so the field
//! owner steps the shared plant once per scan — the pacing the
//! dynamics declaration is written for. The standby's checkpoint pull
//! runs through a TCP relay the script repoints at the restarted
//! active, so the field-owner restart keeps the pair's cadence. Both
//! peers run `--journal-file`; the field owner also runs
//! `--state-file`.
//!
//! The scripted legs, in order — what an operator sees at each step:
//!
//! 1. **Normal/automatic operation** — the canal level rises on the
//!    declared tide plus the confirmed-open gate's admitted flow; the
//!    threshold chain walks its `duty_call`/`lag_call`/`high_level`
//!    ladder and the station drives `gate-cmd` in automatic.
//! 2. **Automatic-to-manual** — the declared schedule switches the
//!    field-reported `gate-mode` to local manual: `manual_active`
//!    asserts, the never-shelvable mode alarm asserts and latches
//!    `unacknowledged`, and the burst lands in the durable record
//!    `point_changed` entries in `seq` order — the field's report
//!    standing first.
//! 3. **Unsafe confirmed state** — the operator's manual demand of 0
//!    commands the gate safe while `gate-fb` holds confirmed open:
//!    `valve`'s `discrepancy` asserts after its declared
//!    consecutive-deviating budget and its managed alarm latches.
//! 4. **Worsening process condition** — the rising level trips the
//!    managed high-level alarm at the declared `high`, the divergence
//!    detector's composed rate-of-rise annunciation latches, and the
//!    operator ack clears `unacknowledged` while the hazard stands.
//! 5. **Bad and stale measurement** — the remote repeater's declared
//!    silence presents `Uncertain(Stale)` past `stale_after_ticks`,
//!    never a healthy last-known value; the `Bad` primary flips the
//!    failover onto it (`backup_active` asserts and alarms), and the
//!    quality transitions journal as `QualityChanged` entries.
//! 6. **The protection boundary (decision 77)** — the layer's reported
//!    states (`sis-available`/`sis-fault`/`sis-trip`/`sis-proof-test`)
//!    land on schedule; the scenario drives the `sis-active` actuation
//!    contact the dynamics' emergency draw gates on, and the plant
//!    demonstrably moves with no controller scan running — the
//!    protective action is the dynamics' own. The states and their
//!    alarms sit in the signal index's `protection` group, distinct
//!    from the ordinary `alarms` group, and every transition journals.
//! 7. **Designed suppression (decisions 73/76)** — the discrepancy
//!    alarm's `suppressed` stands while the actuation stands,
//!    withholding `unacknowledged` while `alarm` keeps reporting the
//!    standing mismatch; releasing the contact re-annunciates the
//!    standing condition as a fresh latch.
//! 8. **Managed lifecycle** — the high-level alarm's `shelve` request
//!    asserts `shelved` inside `max_shelve_ticks` and expires while
//!    the request still stands (re-shelving requires the request to
//!    cycle), a second request is manually released, `oos` holds
//!    `out_of_service` until manual return, and the never-shelvable
//!    mode alarm's only shelving surface — its `shelved` status point
//!    — refuses the receipted write with `not_writable`.
//! 9. **Operator bypass** — `sis-bypass` is the plant-exposed writable
//!    field point the lint names: the operator's write rides the
//!    attributed, receipted command path onto the field and its
//!    alarm annunciates.
//! 10. **State-file restart** — the field owner's process dies with the
//!     high-level latch and a standing shelve countdown held; its
//!     replacement resumes at the persisted tick with both intact, the
//!     durable journal replays the attributed record verbatim behind
//!     the run-2 boundary marker, and the shelve bound continues to
//!     expiry.
//! 11. **Bumpless promotion** — demote then promote moves the field
//!     writer to the standby without a field discontinuity: the plant's
//!     write-ownership claim fences the old attachment's writes, and
//!     the promoted peer keeps the run — and the receipted attributed
//!     command path — going.
//!
//! Every named behavior is asserted through monitor-client,
//! plant-protocol, and journal payloads — never printed output; the
//! digest the run returns proves repeated scripted runs identical.
//! One command runs the whole scenario:
//!
//! ```text
//! cargo test -p dcs-controller --test ijmuiden_acceptance
//! ```

use dcs_build::ijmuiden::{
    IjmuidenConfig, IjmuidenLayout, ManagedAlarmLayout, ijmuiden, points, schedule,
};
use dcs_build::station::AlarmLayout;
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent,
    PointId, Quality, QualityReason, Role, Sample, StandbySync, TelemetrySnapshot, Tick, Value,
    ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;
use dcs_sim::Fault;
use dcs_sim_net::{RemoteDriver, RemoteError};
use std::collections::BTreeMap;
use std::io::{BufRead, BufReader};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The plant-side dynamics — the checked-in decision-75 declaration
/// merged over the served model: the gate's admitted flow, the
/// independent layer's draw, the tide, and the level integrator.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/ijmuiden_dynamics.json"
);
/// The model source — the checked-in emitted document both the
/// plant-side and controller-side documents derive from.
const MODEL_SOURCE: &str = include_str!("../../dcs-demo/fixtures/ijmuiden.json");

/// Process time advanced per scan — the dynamics declaration's dt.
const DT: &str = "1.0";
/// The declared actor identity every operator command lands under —
/// the receipted path's attribution the journal keeps.
const OPERATOR: &str = "ops-lead";
/// The scripted schedule's extent — the second excursion's approach;
/// the re-trip itself is found dynamically since the extra plant-side
/// step shifts it a scan or two.
const SCRIPTED_SCANS: u64 = 78;

/// The `dcs-plant-server` binary — a sibling of the controller binary
/// under test in the workspace target dir; workspace builds produce it.
fn plant_server() -> PathBuf {
    let binary = Path::new(CONTROLLER)
        .parent()
        .unwrap()
        .join(format!("dcs-plant-server{}", std::env::consts::EXE_SUFFIX));
    assert!(
        binary.is_file(),
        "{} not found — build the workspace first",
        binary.display()
    );
    binary
}

/// A spawned process: its bound address learned from the `listening on`
/// stderr line, stderr held open so a later diagnostic write never meets
/// a closed pipe, and a kill on drop so a panicking test leaves no stray
/// processes behind.
struct Spawned {
    child: Child,
    addr: SocketAddr,
    _stderr: BufReader<ChildStderr>,
}

impl Drop for Spawned {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawns `binary`, reads stderr until its `listening on <addr>` line,
/// and returns the running process plus the lines that preceded it —
/// the state-file resume report lives there.
fn spawn_logged(binary: &Path, args: &[String]) -> (Spawned, Vec<String>) {
    let mut child = Process::new(binary)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap_or_else(|error| panic!("cannot spawn {}: {error}", binary.display()));
    let mut stderr = BufReader::new(child.stderr.take().unwrap());
    let mut preamble = Vec::new();
    let addr = loop {
        let mut line = String::new();
        if stderr.read_line(&mut line).unwrap() == 0 {
            panic!("{} exited before reporting its address", binary.display());
        }
        match line.trim().strip_prefix("listening on ") {
            Some(addr) => break addr.parse().unwrap(),
            None => preamble.push(line.trim().to_string()),
        }
    };
    (
        Spawned {
            child,
            addr,
            _stderr: stderr,
        },
        preamble,
    )
}

/// Spawns `binary` and returns the running process — the plain shape
/// for processes that report nothing before their address.
fn spawn(binary: &Path, args: &[String]) -> Spawned {
    spawn_logged(binary, args).0
}

/// A plant-server process serving the plant-side model document with
/// the checked-in dynamics merged in, on an ephemeral port.
fn spawn_plant(model: &Path) -> Spawned {
    spawn(
        &plant_server(),
        &[
            model.to_str().unwrap().to_string(),
            "--dynamics".to_string(),
            PLANT_DYNAMICS.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ],
    )
}

/// A `--driven` controller process on `model`: the monitor serves on an
/// ephemeral port and scans run only when `POST /scan` requests them.
fn spawn_controller(model: &Path, extra: &[String]) -> Spawned {
    spawn_controller_logged(model, extra).0
}

/// The state-file restart's spawn: the resume report is a stderr line
/// before `listening on`, so the preamble comes back with the process.
fn spawn_controller_logged(model: &Path, extra: &[String]) -> (Spawned, Vec<String>) {
    let mut args = vec![model.to_str().unwrap().to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", DT] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args)
}

fn kill(spawned: &mut Spawned) {
    spawned.child.kill().unwrap();
    spawned.child.wait().unwrap();
}

/// Writes the plant-side document: the checked-in model with the two
/// `sim-scripted` devices re-declared as plain simulated devices. The
/// scripted driver is a controller-side playback the shared plant
/// server does not implement — `sim_channel_map`'s local factory takes
/// no parameters — so the served field carries the same channels and
/// the run drives the declared schedule through the plant protocol.
fn plant_model(dir: &Path) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        if device["kind"].as_str() == Some("sim-scripted") {
            let all_bool = device["channels"]
                .as_object()
                .unwrap()
                .values()
                .all(|channel| channel["value_type"].as_str() == Some("bool"));
            device["kind"] = serde_json::json!(if all_bool { "sim-di" } else { "sim-io" });
            device.as_object_mut().unwrap().remove("parameters");
        }
    }
    let path = dir.join("ijmuiden-plant.json");
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// Writes the controller-side model for a plant server at `plant`: the
/// checked-in document with every device's channels merged onto one
/// `sim-tcp` device carrying the plant's address. One remote backend
/// means one plant step per owner scan — the pacing the dynamics
/// declaration assumes. Returns the written path plus the loaded model
/// for its fingerprint and declared-`journaled` set.
fn controller_model(dir: &Path, plant: SocketAddr) -> (PathBuf, PlantModel) {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    let mut channels = serde_json::Map::new();
    for device in document["devices"].as_array().unwrap() {
        for (name, channel) in device["channels"].as_object().unwrap() {
            channels.insert(name.clone(), channel.clone());
        }
    }
    document["devices"] = serde_json::json!([{
        "id": 1,
        "kind": "sim-tcp",
        "parameters": { "address": plant.to_string() },
        "channels": channels,
    }]);
    for point in document["io_points"].as_array_mut().unwrap() {
        if let Some(channel) = point["channel"].as_object_mut() {
            channel["device"] = 1.into();
        }
    }
    let path = dir.join("ijmuiden-controller.json");
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    let model = PlantModel::load(&serde_json::to_string(&document).unwrap()).unwrap();
    (path, model)
}

/// The schedule the checked-in document declares: every scripted
/// device's `script` parameter read out of the emitted model and
/// resolved to `(tick, point, value)` entries the scripted run writes
/// through the plant protocol — the declared timeline itself, not a
/// transcription of it.
fn declared_schedule(model: &PlantModel) -> BTreeMap<u64, Vec<(PointId, Value)>> {
    let mut schedule: BTreeMap<u64, Vec<(PointId, Value)>> = BTreeMap::new();
    for device in &model.devices {
        let Some(script) = device.parameters.get("script") else {
            continue;
        };
        for (channel, entries) in script.as_object().unwrap() {
            let point = model
                .io_points
                .iter()
                .find(|point| {
                    point.channel.as_ref().is_some_and(|reference| {
                        reference.device == device.id && reference.name == *channel
                    })
                })
                .map(|point| point.id)
                .unwrap_or_else(|| {
                    panic!(
                        "no io_point binds scripted channel {channel} of device {}",
                        device.id.0
                    )
                });
            for entry in entries.as_array().unwrap() {
                let tick = entry["tick"].as_u64().unwrap();
                let raw = &entry["value"];
                let value = match device.channels[channel].value_type {
                    ValueKind::Bool => Value::Bool(raw.as_bool().unwrap()),
                    ValueKind::Float => Value::Float(raw.as_f64().unwrap()),
                    ValueKind::Int => Value::Int(raw.as_i64().unwrap()),
                };
                schedule.entry(tick).or_default().push((point, value));
            }
        }
    }
    schedule
}

/// Writes the declared schedule's entries for `tick` to the shared
/// plant — the scripted channels' playback riding the plant protocol's
/// write seam, stamped with the same driver tick a scripted driver's
/// playback would stamp.
fn apply_script(field: &RemoteDriver, schedule: &BTreeMap<u64, Vec<(PointId, Value)>>, tick: u64) {
    if let Some(entries) = schedule.get(&tick) {
        for (point, value) in entries {
            field.write(*point, *value).unwrap();
        }
    }
}

/// Pumps one accepted relay connection against the real monitor: two
/// copy loops, one per direction, each ending by half-closing the other
/// side so the request/response pair completes and the sockets close
/// cleanly.
fn pump(client: TcpStream, upstream: SocketAddr) {
    let Ok(server) = TcpStream::connect(upstream) else {
        return;
    };
    let Ok(client_reader) = client.try_clone() else {
        return;
    };
    let Ok(server_reader) = server.try_clone() else {
        return;
    };
    let writer = thread::spawn(move || {
        let mut from = client_reader;
        let mut to = server;
        let _ = std::io::copy(&mut from, &mut to);
        let _ = to.shutdown(Shutdown::Write);
    });
    let mut from = server_reader;
    let mut to = client;
    let _ = std::io::copy(&mut from, &mut to);
    let _ = to.shutdown(Shutdown::Write);
    let _ = writer.join();
}

/// The standby's checkpoint-pull path: a TCP forwarder whose upstream
/// the script re-points — at the restarted active, so the pair's cadence
/// survives the field-owner restart. The flag is only ever repointed
/// between scripted ticks, so a pull's verdict is never racy.
struct PeerRelay {
    addr: SocketAddr,
    upstream: Arc<Mutex<SocketAddr>>,
    stop: Arc<AtomicBool>,
    accept: Option<JoinHandle<()>>,
}

impl PeerRelay {
    /// A relay forwarding every connection to `upstream`.
    fn forwarding(upstream: SocketAddr) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let addr = listener.local_addr().unwrap();
        let upstream = Arc::new(Mutex::new(upstream));
        let stop = Arc::new(AtomicBool::new(false));
        let accept = {
            let upstream = Arc::clone(&upstream);
            let stop = Arc::clone(&stop);
            thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let Ok(stream) = stream else { continue };
                    let upstream = *upstream.lock().unwrap();
                    thread::spawn(move || pump(stream, upstream));
                }
            })
        };
        Self {
            addr,
            upstream,
            stop,
            accept: Some(accept),
        }
    }

    /// Points the relay at a different upstream — the restarted active.
    fn set_upstream(&self, upstream: SocketAddr) {
        *self.upstream.lock().unwrap() = upstream;
    }
}

impl Drop for PeerRelay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        // Wake the blocking accept so the loop observes the flag.
        let _ = TcpStream::connect(self.addr);
        if let Some(accept) = self.accept.take() {
            let _ = accept.join();
        }
    }
}

/// The sample `snapshot`'s image reports for `point`.
fn image_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap_or_else(|| panic!("no sample for {point:?}"))
}

/// The value `snapshot`'s image reports for `point`.
fn image_value(snapshot: &TelemetrySnapshot, point: PointId) -> Value {
    image_sample(snapshot, point).value
}

fn float(sample: Sample) -> f64 {
    match sample.value {
        Value::Float(value) => value,
        other => panic!("expected a Float sample, got {other:?}"),
    }
}

fn bool_(sample: Sample) -> bool {
    match sample.value {
        Value::Bool(value) => value,
        other => panic!("expected a Bool sample, got {other:?}"),
    }
}

fn int(sample: Sample) -> i64 {
    match sample.value {
        Value::Int(value) => value,
        other => panic!("expected an Int sample, got {other:?}"),
    }
}

/// Asserts the field carries exactly `owner`'s last write on the one
/// declared field `Out` point — the write-gate proof the shared field
/// answers only to the field owner.
fn field_carry(field: &RemoteDriver, owner: &TelemetrySnapshot, layout: &IjmuidenLayout) {
    assert_eq!(
        field.read(layout.gate_cmd).unwrap().value,
        image_value(owner, layout.gate_cmd),
        "the field must carry only the field owner's write on {:?}",
        layout.gate_cmd
    );
}

/// One owner tick's observable record — the scenario's full seam map —
/// what a repeated scripted run must reproduce identically.
fn observe(layout: &IjmuidenLayout, owner: &TelemetrySnapshot) -> serde_json::Value {
    let s = |point: PointId| image_sample(owner, point);
    let f = |point: PointId| float(s(point));
    let b = |point: PointId| bool_(s(point));
    let managed = |alarm: &ManagedAlarmLayout| {
        serde_json::json!([
            b(alarm.alarm),
            b(alarm.unacknowledged),
            b(alarm.shelved),
            b(alarm.suppressed),
            b(alarm.out_of_service),
        ])
    };
    let unmanaged =
        |alarm: &AlarmLayout| serde_json::json!([b(alarm.alarm), b(alarm.unacknowledged)]);
    serde_json::json!({
        "tick": owner.tick.0,
        "level": f(layout.level),
        "remote": f(layout.level_remote),
        "remote_quality": s(layout.level_remote).quality,
        "selected": f(layout.level_selected),
        "filtered": f(layout.level_filtered),
        "gate_demand": f(layout.gate_demand),
        "gate_fb": f(layout.gate_fb),
        "gate_cmd": f(layout.gate_cmd),
        "sis_draw": f(layout.sis_draw),
        "gate_mode": b(layout.gate_mode),
        "manual_active": b(layout.manual_active),
        "discrepancy": b(layout.discrepancy),
        "backup_active": b(layout.backup_active),
        "deviating": b(layout.deviating),
        "duty_call": b(layout.duty_call),
        "lag_call": b(layout.lag_call),
        "below_cutoff": b(layout.below_cutoff),
        "high_level": b(layout.high_level),
        "chain_demand": int(s(layout.chain_demand)),
        "sis_active": b(layout.sis_active),
        "sis_available": b(layout.sis_available),
        "sis_fault": b(layout.sis_fault),
        "sis_trip": b(layout.sis_trip),
        "sis_proof_test": b(layout.sis_proof_test),
        "sis_bypass": b(layout.sis_bypass),
        "lah": managed(&layout.high_level_alarm),
        "mode": managed(&layout.mode_alarm),
        "disc": managed(&layout.discrepancy_alarm),
        "ror": unmanaged(&layout.rate_of_rise_alarm),
        "backup": unmanaged(&layout.backup_active_alarm),
        "trip": unmanaged(&layout.sis_trip_alarm),
        "bypass": unmanaged(&layout.sis_bypass_alarm),
        "fault": unmanaged(&layout.sis_fault_alarm),
    })
}

/// The pane's managed-state row model, computed over the served signal
/// index and one snapshot the way the page-side lists would: which
/// named statuses stand at this moment.
fn managed_lists(
    index: &SignalIndex,
    snapshot: &TelemetrySnapshot,
    layout: &IjmuidenLayout,
) -> serde_json::Value {
    let name = |point: PointId| {
        index
            .get(point)
            .map(|entry| entry.name.clone())
            .unwrap_or_else(|| format!("point-{}", point.0))
    };
    let names = |points: &[PointId]| -> Vec<String> {
        points
            .iter()
            .filter(|point| bool_(image_sample(snapshot, **point)))
            .map(|point| name(*point))
            .collect()
    };
    let managed = [
        &layout.high_level_alarm,
        &layout.mode_alarm,
        &layout.discrepancy_alarm,
    ];
    let unmanaged = [
        &layout.rate_of_rise_alarm,
        &layout.backup_active_alarm,
        &layout.sis_trip_alarm,
        &layout.sis_bypass_alarm,
        &layout.sis_fault_alarm,
    ];
    let mut unacknowledged: Vec<PointId> =
        managed.iter().map(|alarm| alarm.unacknowledged).collect();
    unacknowledged.extend(unmanaged.iter().map(|alarm| alarm.unacknowledged));
    let mut standing: Vec<PointId> = managed.iter().map(|alarm| alarm.alarm).collect();
    standing.extend(unmanaged.iter().map(|alarm| alarm.alarm));
    serde_json::json!({
        "shelved": names(&managed.iter().map(|a| a.shelved).collect::<Vec<_>>()),
        "suppressed": names(&managed.iter().map(|a| a.suppressed).collect::<Vec<_>>()),
        "out_of_service": names(&managed.iter().map(|a| a.out_of_service).collect::<Vec<_>>()),
        "annunciating": names(&unacknowledged),
        "standing": names(&standing),
    })
}

/// One scripted tick on the pair: the tracking peer scans first — its
/// checkpoint pull lands inside its `POST /scan` — then the field
/// owner scans and steps the shared plant. Returns the owner's
/// snapshot; the pair must tick together and the field must carry only
/// the owner's writes.
fn pair_tick(
    standby: &MonitorClient,
    owner: &MonitorClient,
    field: &RemoteDriver,
    layout: &IjmuidenLayout,
    trace: &mut Vec<serde_json::Value>,
) -> TelemetrySnapshot {
    let tracked = standby.advance(1).unwrap();
    let owner_image = owner.advance(1).unwrap();
    assert_eq!(
        tracked.tick, owner_image.tick,
        "the pair must tick together"
    );
    field_carry(field, &owner_image, layout);
    trace.push(observe(layout, &owner_image));
    owner_image
}

/// `scans` scripted pair ticks, returning the field owner's last
/// snapshot.
fn pair_phase(
    standby: &MonitorClient,
    owner: &MonitorClient,
    field: &RemoteDriver,
    layout: &IjmuidenLayout,
    trace: &mut Vec<serde_json::Value>,
    scans: u64,
) -> TelemetrySnapshot {
    let mut image = None;
    for _ in 0..scans {
        image = Some(pair_tick(standby, owner, field, layout, trace));
    }
    image.unwrap()
}

/// Scripted pair ticks until `done` accepts the newest observed row,
/// bounded at `max` scans. Deterministic under the scripted inputs —
/// the bound only guards a hung condition.
fn pair_until(
    standby: &MonitorClient,
    owner: &MonitorClient,
    field: &RemoteDriver,
    layout: &IjmuidenLayout,
    trace: &mut Vec<serde_json::Value>,
    max: u64,
    done: impl Fn(&serde_json::Value) -> bool,
) -> TelemetrySnapshot {
    for _ in 0..max {
        let image = pair_tick(standby, owner, field, layout, trace);
        if done(trace.last().unwrap()) {
            return image;
        }
    }
    panic!(
        "condition never observed within {max} scans: {:?}",
        trace.last()
    );
}

/// `scans` ticks on the promoted owner alone — the post-switchover
/// run: the peer scans and steps the plant, the field carries its
/// writes.
fn owner_phase(
    owner: &MonitorClient,
    field: &RemoteDriver,
    layout: &IjmuidenLayout,
    trace: &mut Vec<serde_json::Value>,
    scans: u64,
) -> TelemetrySnapshot {
    let mut image = None;
    for _ in 0..scans {
        let owner_image = owner.advance(1).unwrap();
        field_carry(field, &owner_image, layout);
        trace.push(observe(layout, &owner_image));
        image = Some(owner_image);
    }
    image.unwrap()
}

/// An attributed operator write — the receipted command path every
/// operator action in this run lands on: the declared actor rides the
/// receipt and the journaled `CommandSettled`.
fn command(
    client: &MonitorClient,
    point: PointId,
    kind: ValueKind,
    value: Value,
) -> CommandReceipt {
    let receipt = client
        .command_as(&Command::WriteValue { point, kind, value }, Some(OPERATOR))
        .unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
    receipt
}

/// Asserts a managed alarm's writable ack point, attributed.
fn ack(client: &MonitorClient, alarm: &ManagedAlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(true))
}

/// Releases a managed alarm's writable ack point, attributed.
fn release(client: &MonitorClient, alarm: &ManagedAlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(false))
}

/// Asserts an unmanaged alarm's writable ack point, attributed.
fn ack_unmanaged(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(true))
}

/// Releases an unmanaged alarm's writable ack point, attributed.
fn release_unmanaged(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(false))
}

/// The journal file's raw records in file order, decoded as JSON —
/// `{"run_boundary": …}` markers and `{"entry": …}` records alike.
fn file_records(path: &Path) -> Vec<serde_json::Value> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// The file's `entry` records, in file order, decoded back into the
/// contract's `JournalEntry` — marker lines are skipped.
fn file_entries(path: &Path) -> Vec<JournalEntry> {
    file_records(path)
        .iter()
        .filter_map(|record| {
            record
                .get("entry")
                .map(|entry| serde_json::from_value(entry.clone()).unwrap())
        })
        .collect()
}

/// The file's `run_boundary` marker records as `(run, tick)` pairs.
fn file_boundaries(path: &Path) -> Vec<(u64, u64)> {
    file_records(path)
        .iter()
        .filter_map(|record| {
            record.get("run_boundary").map(|marker| {
                (
                    marker["run"].as_u64().unwrap(),
                    marker["tick"].as_u64().unwrap(),
                )
            })
        })
        .collect()
}

/// The settled-command journal entries of a served journal, in order.
fn settled_receipts(journal: &[JournalEntry]) -> Vec<CommandReceipt> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt.clone()),
            _ => None,
        })
        .collect()
}

/// The role transitions a journal stream recorded.
fn role_changes_in(journal: &[JournalEntry]) -> Vec<(Role, Role)> {
    journal
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to } => Some((from, to)),
            _ => None,
        })
        .collect()
}

/// The role transitions a peer's journal recorded.
fn role_changes(client: &MonitorClient) -> Vec<(Role, Role)> {
    role_changes_in(&client.journal(0).unwrap())
}

/// The `PointChanged` transitions `journal` recorded for `point`, in
/// file order — `(seq, from, to)` per entry.
fn point_changes(journal: &[JournalEntry], point: PointId) -> Vec<(u64, Option<Value>, Value)> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed,
                from,
                to,
            } if *changed == point => Some((entry.seq, *from, *to)),
            _ => None,
        })
        .collect()
}

/// The seq of `point`'s `PointChanged` transition `to` `value`.
fn transition_seq(journal: &[JournalEntry], point: PointId, value: Value) -> u64 {
    journal
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed, to, ..
            } if *changed == point && *to == value => Some(entry.seq),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no journaled transition of {point:?} to {value:?}"))
}

/// The tick `point`'s first `PointChanged` transition `to` `value` is
/// attributed to — the causal-order key; `seq` orders same-tick entries
/// only by the recorder's ascending-point iteration.
fn transition_tick(journal: &[JournalEntry], point: PointId, value: Value) -> u64 {
    journal
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed, to, ..
            } if *changed == point && *to == value => Some(entry.tick.0),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no journaled transition of {point:?} to {value:?}"))
}

/// The seq of `point`'s first real release — a `PointChanged` to `false`
/// with `from` present, so a restart's `from: None` re-emission never
/// matches.
fn released(journal: &[JournalEntry], point: PointId) -> u64 {
    journal
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed,
                from: Some(_),
                to,
            } if *changed == point && *to == Value::Bool(false) => Some(entry.seq),
            _ => None,
        })
        .unwrap_or_else(|| panic!("{point:?} never journaled its release"))
}

/// The tick `point`'s first real release is attributed to.
fn release_tick(journal: &[JournalEntry], point: PointId) -> u64 {
    journal
        .iter()
        .find_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed,
                from: Some(_),
                to,
            } if *changed == point && *to == Value::Bool(false) => Some(entry.tick.0),
            _ => None,
        })
        .unwrap_or_else(|| panic!("{point:?} never journaled its release"))
}

/// Replaces the run-varying strings inside a serialized value —
/// monitor, relay, and plant addresses are ephemeral ports — so two
/// runs' digests compare. Longer strings mask first: one address can
/// never be a prefix of another, but the rule keeps the replacement
/// honest for any run-varying string.
fn masked(value: serde_json::Value, masks: &[(String, String)]) -> serde_json::Value {
    let mut text = serde_json::to_string(&value).unwrap();
    for (from, to) in masks {
        text = text.replace(from.as_str(), to);
    }
    serde_json::from_str(&text).unwrap()
}

/// The trace's last recorded row.
fn last_row(trace: &[serde_json::Value]) -> &serde_json::Value {
    trace.last().unwrap()
}

/// The row recorded at scripted `scan` — scans are 1-based.
fn at(trace: &[serde_json::Value], scan: u64) -> &serde_json::Value {
    &trace[scan as usize - 1]
}

/// One scripted run of the IJmuiden acceptance scenario documented in
/// the module header. Returns the run's auditable record as JSON two
/// runs must reproduce exactly: the per-tick trace the field owner's
/// snapshots produced, the journaled record across the field-owner
/// restart, the served monitoring data, and the promoted peer's tail.
fn run_ijmuiden(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-ijmuiden-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // The emitted scenario's layout — the declared point ids the run
    // addresses — and the scripted schedule the document itself carries.
    let emitted = ijmuiden(&IjmuidenConfig::reference()).unwrap();
    let layout = emitted.layout;
    let script = declared_schedule(&emitted.model);
    assert!(
        script.contains_key(&schedule::MODE_TO_MANUAL) && script.contains_key(&schedule::SIS_TRIP),
        "the declared schedule must carry the incident's scripted beats"
    );

    let plant = spawn_plant(&plant_model(&dir));
    let (model_path, model) = controller_model(&dir, plant.addr);

    // The plant serves exactly the scenario's channel-bound field points.
    let field = RemoteDriver::connect(plant.addr).unwrap();
    let served: std::collections::BTreeSet<u64> = field
        .list_points()
        .unwrap()
        .iter()
        .map(|info| info.point.0)
        .collect();
    assert_eq!(
        served,
        [10, 11, 12, 13, 14, 15, 16, 20, 30, 40, 41, 42, 43, 44, 45]
            .into_iter()
            .collect(),
        "the plant serves the scenario's declared field surface"
    );

    // The declared schedule's tick-0 entries, the tide forcing, and the
    // gate confirmed open — plus one step so the first scan sees the
    // dynamics' declared level rather than the bindings' neutral seed.
    apply_script(&field, &script, 0);
    field.write(points::INFLOW, Value::Float(0.08)).unwrap();
    field.write(points::GATE_FB, Value::Float(1.0)).unwrap();
    field.step(1.0).unwrap();

    // The pair: the field owner first — persisted and journaled for the
    // restart and record legs — then the tracking standby pulling
    // through the relay.
    let journal_active = dir.join("active.jsonl");
    let journal_standby = dir.join("standby.jsonl");
    let state_active = dir.join("active-state.json");
    let active_args = vec![
        "--state-file".to_string(),
        state_active.to_str().unwrap().to_string(),
        "--journal-file".to_string(),
        journal_active.to_str().unwrap().to_string(),
    ];
    let mut active_process = spawn_controller_logged(&model_path, &active_args).0;
    let relay = PeerRelay::forwarding(active_process.addr);
    let standby_process = spawn_controller(
        &model_path,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--journal-file".to_string(),
            journal_standby.to_str().unwrap().to_string(),
        ],
    );
    let mut active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    assert_eq!(active.role().unwrap().role, Role::Active);
    assert_eq!(standby.role().unwrap().role, Role::Standby);

    // The served data model the operator's pane renders: the point-to-
    // signal index the run's managed-state lists name.
    let index = active.signals().unwrap();

    // The operator commands this run issues, in issue order — each one
    // attributed, each one matched against its journaled settle below.
    let mut issued: Vec<CommandReceipt> = Vec::new();
    let mut trace: Vec<serde_json::Value> = Vec::new();
    // What the pane's managed-state lists show at the run's named
    // moments — the operator-visible evidence the scenario carries.
    let mut operator_view: Vec<(String, serde_json::Value)> = Vec::new();
    // The plant-side demonstration that the protection layer's action
    // moves the process with no controller scan running.
    let mut no_scan_steps: Vec<serde_json::Value> = Vec::new();
    let row = last_row;
    let f64_of = |row: &serde_json::Value, key: &str| row[key].as_f64().unwrap();
    let bool_of = |row: &serde_json::Value, key: &str| row[key].as_bool().unwrap();
    let alarm_pair =
        |row: &serde_json::Value, key: &str, index: usize| row[key][index].as_bool().unwrap();
    let stale = Quality::Uncertain(QualityReason::Stale);

    // The scripted incident: every iteration applies the declared
    // schedule's entries due this tick, then the pair scans once.
    for scan in 1..=SCRIPTED_SCANS {
        apply_script(&field, &script, scan);
        let image = pair_tick(&standby, &active, &field, &layout, &mut trace);
        if scan == 5 {
            assert_eq!(
                standby.snapshot().unwrap(),
                image,
                "the tracking peer's image is the owner's"
            );
        }
        match scan {
            // Acknowledge the mode annunciation — the incident's
            // alarmed transition.
            8 => issued.push(ack(&active, &layout.mode_alarm)),
            9 => {
                issued.push(release(&active, &layout.mode_alarm));
                // The never-shelvable alarm's only shelving surface is
                // its `shelved` status point — a field `Out`-side
                // point the receipted path refuses outright.
                let refusal = active
                    .command_as(
                        &Command::WriteValue {
                            point: layout.mode_alarm.shelved,
                            kind: ValueKind::Bool,
                            value: Value::Bool(true),
                        },
                        Some(OPERATOR),
                    )
                    .unwrap();
                assert_eq!(
                    refusal.outcome,
                    CommandOutcome::Rejected {
                        reason: CommandError::NotWritable {
                            point: layout.mode_alarm.shelved
                        }
                    },
                    "the never-shelvable alarm must refuse shelving on the documented path"
                );
            }
            // The operator demands the gate safe — the field fault
            // holds it confirmed open.
            13 => issued.push(command(
                &active,
                layout.gate_manual_demand,
                ValueKind::Float,
                Value::Float(0.0),
            )),
            // The primary level transmitter goes Bad — the failover
            // switches to the frozen remote repeater — and the
            // high-level trip is acknowledged.
            21 => {
                field
                    .inject_fault(
                        points::LEVEL,
                        Fault::Quality(Quality::Bad(QualityReason::CommunicationFault)),
                    )
                    .unwrap();
                issued.push(ack(&active, &layout.high_level_alarm));
            }
            22 => issued.push(release(&active, &layout.high_level_alarm)),
            // The independent high-high layer trips: its reported
            // `sis-trip` plays back this scan, and the scenario drives
            // the actuation contact the dynamics' relief is gated on —
            // plant-side action, decision 77's boundary.
            schedule::SIS_TRIP => {
                field.write(points::SIS_ACTIVE, Value::Bool(true)).unwrap();
                issued.push(ack_unmanaged(&active, &layout.sis_trip_alarm));
                issued.push(ack_unmanaged(&active, &layout.rate_of_rise_alarm));
            }
            25 => {
                issued.push(release_unmanaged(&active, &layout.sis_trip_alarm));
                issued.push(release_unmanaged(&active, &layout.rate_of_rise_alarm));
            }
            // The primary recovers.
            26 => field.clear_fault(points::LEVEL).unwrap(),
            // The backup-serving annunciation is acknowledged.
            28 => issued.push(ack_unmanaged(&active, &layout.backup_active_alarm)),
            29 => issued.push(release_unmanaged(&active, &layout.backup_active_alarm)),
            // Shelving: the request stands past the declared bound —
            // `shelved` asserts inside it and expires while the request
            // still stands.
            31 => issued.push(command(
                &active,
                layout.high_level_alarm.shelve.unwrap(),
                ValueKind::Bool,
                Value::Bool(true),
            )),
            40 => issued.push(command(
                &active,
                layout.high_level_alarm.shelve.unwrap(),
                ValueKind::Bool,
                Value::Bool(false),
            )),
            // The trip's consequence has passed: the actuation contact
            // drops, releasing the designed suppression, and the
            // operator bypasses the layer for its proof-test window —
            // the receipted writable-point path — while the high-level
            // alarm goes out of service.
            41 => {
                field.write(points::SIS_ACTIVE, Value::Bool(false)).unwrap();
                issued.push(command(
                    &active,
                    points::SIS_BYPASS,
                    ValueKind::Bool,
                    Value::Bool(true),
                ));
                issued.push(command(
                    &active,
                    layout.high_level_alarm.oos.unwrap(),
                    ValueKind::Bool,
                    Value::Bool(true),
                ));
            }
            // Acknowledge the re-annunciated discrepancy and the bypass.
            43 => {
                issued.push(ack(&active, &layout.discrepancy_alarm));
                issued.push(ack_unmanaged(&active, &layout.sis_bypass_alarm));
            }
            44 => {
                issued.push(release(&active, &layout.discrepancy_alarm));
                issued.push(release_unmanaged(&active, &layout.sis_bypass_alarm));
                issued.push(command(
                    &active,
                    layout.high_level_alarm.oos.unwrap(),
                    ValueKind::Bool,
                    Value::Bool(false),
                ));
            }
            // The proof test complete, the bypass returns.
            47 => issued.push(command(
                &active,
                points::SIS_BYPASS,
                ValueKind::Bool,
                Value::Bool(false),
            )),
            // Shelving again — a manual release inside the bound this
            // time.
            50 => issued.push(command(
                &active,
                layout.high_level_alarm.shelve.unwrap(),
                ValueKind::Bool,
                Value::Bool(true),
            )),
            54 => issued.push(command(
                &active,
                layout.high_level_alarm.shelve.unwrap(),
                ValueKind::Bool,
                Value::Bool(false),
            )),
            // The fault annunciation clears by its schedule; ack its
            // latch.
            39 => issued.push(ack_unmanaged(&active, &layout.sis_fault_alarm)),
            _ => {}
        }
        if scan == 30 {
            // Decision 77's boundary made observable: with the pair
            // parked between scans, the independent layer's actuation
            // still moves the process — the relief draw gates on the
            // contact and the level integrator falls. The protective
            // function is the dynamics', never the controller's.
            let before = float(field.read(points::LEVEL).unwrap());
            field.step(1.0).unwrap();
            let draw = float(field.read(points::SIS_DRAW).unwrap());
            let after = float(field.read(points::LEVEL).unwrap());
            assert!(
                draw < 0.0,
                "the relief draw must gate on the contact: {draw}"
            );
            assert!(
                after < before,
                "the layer's action must draw the level down with no scan run: {before} -> {after}"
            );
            no_scan_steps.push(serde_json::json!({
                "sis_active": bool_(field.read(points::SIS_ACTIVE).unwrap()),
                "sis_draw": draw,
                "level_before": before,
                "level_after": after,
            }));
        }
        // What the pane's managed-state lists show at the incident's
        // named moments.
        if scan == 34 {
            operator_view.push((
                "mid-incident".to_string(),
                managed_lists(&index, &active.snapshot().unwrap(), &layout),
            ));
        }
        if scan == 43 {
            operator_view.push((
                "out-of-service".to_string(),
                managed_lists(&index, &active.snapshot().unwrap(), &layout),
            ));
        }
    }

    // -- Assertions over the scripted trace: the decision-75 seam map --

    // The level climbs on the declared tide plus the confirmed-open
    // gate's admitted flow until the independent layer's draw wins.
    assert!(
        (f64_of(at(&trace, 1), "level") - 3.13).abs() < 1e-9,
        "scan 1: {:?}",
        at(&trace, 1)
    );
    for window in trace[..6].windows(2) {
        assert!(
            f64_of(&window[1], "level") > f64_of(&window[0], "level"),
            "the level must climb on the tide: {:?} -> {:?}",
            window[0],
            window[1]
        );
    }

    // The automatic-to-manual transition: the declared mode point
    // asserts at its tick, the station reports manual, and the
    // never-shelvable alarm latches until the scan-8 ack.
    let manual_at = trace
        .iter()
        .position(|row| bool_of(row, "manual_active"))
        .unwrap() as u64
        + 1;
    assert_eq!(manual_at, schedule::MODE_TO_MANUAL);
    assert!(bool_of(at(&trace, schedule::MODE_TO_MANUAL), "gate_mode"));
    assert!(
        trace
            .iter()
            .any(|row| alarm_pair(row, "mode", 0) && alarm_pair(row, "mode", 1))
    );
    assert!(trace[9..].iter().all(|row| !alarm_pair(row, "mode", 1)));
    // Never shelvable: no request surface was declared and the status
    // never asserts.
    assert!(layout.mode_alarm.shelve.is_none());
    assert!(trace.iter().all(|row| !alarm_pair(row, "mode", 2)));
    // The return to automatic annunciates as the transition's other edge.
    assert!(!bool_of(at(&trace, schedule::MODE_TO_AUTO), "gate_mode"));
    assert!(!bool_of(
        at(&trace, schedule::MODE_TO_AUTO),
        "manual_active"
    ));
    assert!(
        trace[schedule::MODE_TO_AUTO as usize..]
            .iter()
            .all(|row| !alarm_pair(row, "mode", 0))
    );

    // The confirmed-state/demand discrepancy: the close demand slews
    // the station's output to safe while `gate-fb` holds confirmed
    // open — `discrepancy` asserts after its declared budget and its
    // alarm latches.
    assert!(
        trace[14..]
            .iter()
            .any(|row| f64_of(row, "gate_demand") < 0.05)
    );
    let discrepancy_at = trace
        .iter()
        .position(|row| bool_of(row, "discrepancy"))
        .unwrap() as u64
        + 1;
    assert!(
        (f64_of(at(&trace, discrepancy_at), "gate_cmd")
            - f64_of(at(&trace, discrepancy_at), "gate_fb"))
        .abs()
            > 0.05,
        "discrepancy asserted without a mismatch: {discrepancy_at}"
    );
    assert!(
        trace
            .iter()
            .any(|row| alarm_pair(row, "disc", 0) && alarm_pair(row, "disc", 1))
    );

    // The worsening process condition: the filtered level walks the
    // chain's ladder and the managed high-level alarm trips at the
    // declared `high`, latching until the scan-21 ack.
    assert!(trace.iter().any(|row| bool_of(row, "duty_call")));
    assert!(trace.iter().any(|row| bool_of(row, "lag_call")));
    assert!(trace.iter().any(|row| bool_of(row, "high_level")));
    assert!(
        trace
            .iter()
            .any(|row| row["chain_demand"].as_i64().unwrap() >= 1)
    );
    let lah_at = trace
        .iter()
        .position(|row| alarm_pair(row, "lah", 0))
        .unwrap() as u64
        + 1;
    assert!(
        f64_of(at(&trace, lah_at), "filtered") >= 5.0 - 1e-9,
        "LAH tripped below the declared high: {lah_at} -> {:?}",
        at(&trace, lah_at)
    );
    assert!(trace[..22].iter().any(|row| alarm_pair(row, "lah", 1)));
    // The composed rate-of-rise annunciation: the divergence detector
    // flags the sustained rise and its alarm latches until the scan-24
    // ack.
    assert!(trace[..24].iter().any(|row| bool_of(row, "deviating")));
    assert!(trace[..25].iter().any(|row| alarm_pair(row, "ror", 1)));

    // Bad and stale data: the repeater's last declared update stands —
    // past `stale_after_ticks` the point presents `Uncertain(Stale)`,
    // never a healthy last-known value.
    assert_eq!(
        serde_json::from_value::<Quality>(
            at(&trace, schedule::REMOTE_LAST_UPDATE)["remote_quality"].clone()
        )
        .unwrap(),
        Quality::Good
    );
    assert!(
        trace[schedule::REMOTE_LAST_UPDATE as usize + 3..schedule::REMOTE_RECOVERY as usize - 1]
            .iter()
            .all(
                |row| serde_json::from_value::<Quality>(row["remote_quality"].clone()).unwrap()
                    == stale
            ),
        "the frozen repeater must present stale, not a healthy last-known value"
    );
    assert_eq!(
        serde_json::from_value::<Quality>(
            at(&trace, schedule::REMOTE_RECOVERY)["remote_quality"].clone()
        )
        .unwrap(),
        Quality::Good
    );
    // The `Bad` primary flips the failover onto that stale repeater —
    // `backup_active` stands and its alarm latches until the scan-28
    // ack; the selected level carries the degraded quality through.
    assert!(
        trace[22..26]
            .iter()
            .any(|row| bool_of(row, "backup_active"))
    );
    assert!(
        trace
            .iter()
            .any(|row| alarm_pair(row, "backup", 0) && alarm_pair(row, "backup", 1))
    );

    // The independent high-high layer (decision 77): its declared
    // states report on schedule — trip, the fault window, the proof
    // test — while its actuation moved the process without a scan (the
    // demonstration above): the level peaks and turns down the step
    // the contact lands, independently of anything the controller
    // computed.
    assert!(bool_of(at(&trace, schedule::SIS_TRIP), "sis_trip"));
    assert!(
        trace
            .iter()
            .any(|row| alarm_pair(row, "trip", 0) && alarm_pair(row, "trip", 1))
    );
    assert!(
        trace[schedule::SIS_TRIP as usize..]
            .iter()
            .all(|row| bool_of(row, "sis_trip"))
    );
    let peak = trace
        .iter()
        .map(|row| f64_of(row, "level"))
        .fold(f64::NEG_INFINITY, f64::max);
    assert!(peak >= 5.5, "the level must reach the trip region: {peak}");
    let peak_at = trace
        .iter()
        .position(|row| f64_of(row, "level") == peak)
        .unwrap() as u64
        + 1;
    assert!(peak_at >= schedule::SIS_TRIP, "peak at {peak_at}");
    assert!(
        trace[peak_at as usize..peak_at as usize + 4]
            .windows(2)
            .all(|window| f64_of(&window[1], "level") < f64_of(&window[0], "level")),
        "the layer's draw must turn the level down"
    );
    assert!(trace[25..].iter().any(|row| f64_of(row, "sis_draw") < 0.0));
    // Its reported fault and proof-test windows and the operator's
    // bypass all land on their alarmed and journaled surfaces.
    assert!(
        trace[schedule::SIS_FAULT_ON as usize - 1..schedule::SIS_FAULT_OFF as usize - 1]
            .iter()
            .all(|row| bool_of(row, "sis_fault"))
    );
    assert!(
        trace
            .iter()
            .any(|row| alarm_pair(row, "fault", 0) && alarm_pair(row, "fault", 1))
    );
    assert!(
        trace[schedule::PROOF_TEST_ON as usize - 1..schedule::PROOF_TEST_OFF as usize - 1]
            .iter()
            .all(|row| bool_of(row, "sis_proof_test"))
    );
    assert!(trace[42..].iter().any(|row| bool_of(row, "sis_bypass")));
    assert!(
        trace[42..]
            .iter()
            .any(|row| alarm_pair(row, "bypass", 0) && alarm_pair(row, "bypass", 1))
    );

    // The designed suppression (decisions 73/76): while the layer's
    // actuation stands, the standing discrepancy is the trip's
    // consequence — `suppressed` asserts and `unacknowledged` holds
    // clear — while `alarm` keeps reporting the mismatch's truth. The
    // contact is driven at scan 24's boundary and released at 41's, so
    // `suppressed` stands scans 25 through 41.
    assert!(
        trace[24..41].iter().all(|row| alarm_pair(row, "disc", 3)),
        "the suppression must stand while the layer acts"
    );
    assert!(trace[24..41].iter().all(|row| !alarm_pair(row, "disc", 1)));
    assert!(
        trace[25..40].iter().any(|row| alarm_pair(row, "disc", 0)),
        "the alarm must keep reporting the standing mismatch under suppression"
    );
    // Releasing the actuation re-annunciates the standing condition as
    // a fresh trip until the scan-43 ack applies.
    assert!(!alarm_pair(at(&trace, 42), "disc", 3));
    assert!(trace[41..44].iter().any(|row| alarm_pair(row, "disc", 1)));
    assert!(
        trace[44..SCRIPTED_SCANS as usize]
            .iter()
            .all(|row| !alarm_pair(row, "disc", 1))
    );

    // The shelve bound and its expiry: the request standing past
    // `max_shelve_ticks` asserts `shelved` inside the bound and the
    // kind expires it while the request still stands; the later
    // request released by hand clears it without the bound.
    let shelve_from = trace
        .iter()
        .position(|row| alarm_pair(row, "lah", 2))
        .unwrap() as u64
        + 1;
    assert_eq!(shelve_from, 32, "the request applies at the next scan");
    assert!(trace[31..37].iter().all(|row| alarm_pair(row, "lah", 2)));
    assert!(
        trace[37..50].iter().all(|row| !alarm_pair(row, "lah", 2)),
        "the bound must expire the shelve while the request still stands"
    );
    assert!(trace[50..54].iter().all(|row| alarm_pair(row, "lah", 2)));
    assert!(
        trace[54..SCRIPTED_SCANS as usize]
            .iter()
            .all(|row| !alarm_pair(row, "lah", 2)),
        "the manual release must clear the shelve"
    );

    // The out-of-service path: the command asserts the status while it
    // stands and clears on its release.
    assert!(trace[41..44].iter().any(|row| alarm_pair(row, "lah", 4)));
    assert!(
        trace[45..SCRIPTED_SCANS as usize]
            .iter()
            .all(|row| !alarm_pair(row, "lah", 4))
    );

    // The recovery and the second excursion: mode returns to
    // automatic, the demand slews back to open, the confirmed state
    // agrees again, and the discrepancy clears — while the recovering
    // level re-trips the high-level alarm, re-latching
    // `unacknowledged` — the durable record's full arc.
    assert!(trace[45..].iter().any(|row| !bool_of(row, "discrepancy")));

    // The pane's managed-state lists at the captured moments name the
    // standing statuses the scenario produced.
    let mid_incident = &operator_view[0].1;
    assert!(
        mid_incident["suppressed"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "disc-suppressed"),
        "the pane's suppressed list must name the standing suppression: {mid_incident}"
    );
    assert!(
        mid_incident["shelved"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "lah-shelved"),
        "the pane's shelved list must name the bound shelve: {mid_incident}"
    );
    let oos_view = &operator_view[1].1;
    assert!(
        oos_view["out_of_service"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "lah-out-of-service"),
        "the pane's out-of-service list must name the held alarm: {oos_view}"
    );
    assert!(
        oos_view["annunciating"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "disc-unacknowledged"),
        "the re-annunciated discrepancy must stand in the pane's list: {oos_view}"
    );

    // -- The second excursion and the held state the restart proves ---
    // The recovering level re-trips the high-level alarm — the fresh
    // violation re-latches `unacknowledged` — and a shelve request
    // stands inside its bound when the field owner dies.
    pair_until(&standby, &active, &field, &layout, &mut trace, 60, |row| {
        row["lah"][0].as_bool().unwrap() && row["lah"][1].as_bool().unwrap()
    });
    issued.push(command(
        &active,
        layout.high_level_alarm.shelve.unwrap(),
        ValueKind::Bool,
        Value::Bool(true),
    ));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert!(
        alarm_pair(row(&trace), "lah", 2),
        "the third shelve must assert inside the bound"
    );

    // -- Leg 10: the field owner's state-file restart ------------------
    // The active's process dies; its replacement resumes the persisted
    // run — the file's checkpoint names the model, the tick, the held
    // latch, and the standing shelve countdown — and the pair cadence
    // continues without a missed transfer. The durable journal replays
    // the attributed record verbatim behind the run-2 boundary marker.
    let interrupted = active.snapshot().unwrap().tick;
    let before_restart = active.journal(0).unwrap();
    assert!(
        !before_restart.is_empty(),
        "the pre-restart run must have journaled entries"
    );
    assert_eq!(file_entries(&journal_active), before_restart);
    assert_eq!(file_boundaries(&journal_active), vec![(1, 0)]);

    kill(&mut active_process);
    let persisted: Checkpoint =
        serde_json::from_slice(&std::fs::read(&state_active).unwrap()).unwrap();
    assert_eq!(persisted.tick, interrupted);
    assert_eq!(persisted.model_fingerprint, Some(model.fingerprint()));
    let lah_name = format!(
        "managed-latching-alarm:{}",
        layout.high_level_alarm.component.0
    );
    let lah_state = &persisted.components[&lah_name];
    assert_eq!(
        lah_state.get("unacknowledged"),
        Some(Value::Bool(true)),
        "the held latch persists in the state file"
    );
    assert_eq!(
        lah_state.get("shelve_elapsed"),
        Some(Value::Int(2)),
        "the standing shelve countdown persists in the state file"
    );
    assert_eq!(
        persisted.internal[&layout.high_level_alarm.shelve.unwrap()].value,
        Value::Bool(true),
        "the standing shelve request persists in the state file"
    );

    // With the field owner dead the plant still moves — the shared
    // process is no part of the controller's scan.
    let level_before = float(field.read(points::LEVEL).unwrap());
    field.step(1.0).unwrap();
    let level_after = float(field.read(points::LEVEL).unwrap());
    assert_ne!(
        level_before, level_after,
        "the plant must keep moving with the field owner dead"
    );
    no_scan_steps.push(serde_json::json!({
        "level_before": level_before,
        "level_after": level_after,
        "owner": "dead",
    }));

    let (resumed_process, preamble) = spawn_controller_logged(&model_path, &active_args);
    active_process = resumed_process;
    active = MonitorClient::new(active_process.addr);
    assert!(
        preamble.iter().any(|line| {
            line.contains("resumed from state file")
                && line.contains(&format!("at tick {}", interrupted.0))
        }),
        "the restart must report the resume: {preamble:?}"
    );
    let resumed_snapshot = active.snapshot().unwrap();
    assert_eq!(resumed_snapshot.tick, interrupted);
    // The named state resumes with the run: the held latch, the
    // standing shelve, and the level the integrator last served.
    assert!(
        bool_(image_sample(
            &resumed_snapshot,
            layout.high_level_alarm.unacknowledged
        )),
        "the resumed run carries the unacknowledged latch"
    );
    assert!(
        bool_(image_sample(
            &resumed_snapshot,
            layout.high_level_alarm.shelved
        )),
        "the resumed run carries the standing shelve"
    );
    assert_eq!(
        active.journal(0).unwrap(),
        before_restart,
        "the journal replays verbatim"
    );
    assert_eq!(
        file_boundaries(&journal_active),
        vec![(1, 0), (2, interrupted.0)]
    );
    operator_view.push((
        "resumed".to_string(),
        managed_lists(&index, &resumed_snapshot, &layout),
    ));
    assert!(
        operator_view[2].1["shelved"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "lah-shelved"),
        "the pane must show the carried shelve after the restart"
    );
    assert!(
        operator_view[2].1["annunciating"]
            .as_array()
            .unwrap()
            .iter()
            .any(|name| name == "lah-unacknowledged"),
        "the pane must show the carried latch after the restart"
    );

    // The pull path repoints at the resumed active; the pair cadence
    // continues as if the process never died — and the shelve bound,
    // restored mid-countdown, expires while the request still stands.
    relay.set_upstream(active_process.addr);
    let base = trace.len();
    let image = pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert!(
        bool_(image_sample(&image, points::SIS_TRIP)),
        "the resumed run reads the protection layer's standing trip state"
    );
    assert!(
        trace[base..base + 4]
            .iter()
            .all(|row| alarm_pair(row, "lah", 2)),
        "the restored countdown must keep the shelve standing"
    );
    assert!(
        trace[base + 4..base + 6]
            .iter()
            .all(|row| !alarm_pair(row, "lah", 2)),
        "the restored countdown must expire the shelve on the same bound"
    );
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the resumed active's checkpoints must keep the standby tracking: {report:?}"
    );
    assert_eq!(
        standby.snapshot().unwrap(),
        image,
        "the pair is one controller across the restart"
    );
    issued.push(command(
        &active,
        layout.high_level_alarm.shelve.unwrap(),
        ValueKind::Bool,
        Value::Bool(false),
    ));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert!(!alarm_pair(row(&trace), "lah", 2));

    // The resumed run's entries continue the `seq` numbering — the
    // post-restart attributed ack lands on the same durable record the
    // file holds, clearing the latch while the hazard still stands.
    issued.push(ack(&active, &layout.high_level_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert!(
        !alarm_pair(row(&trace), "lah", 1),
        "the held latch clears through its ack point"
    );
    issued.push(release(&active, &layout.high_level_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    let after_restart = active.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1,
        "the first post-restart entry continues the seq numbering"
    );

    // -- Leg 11: the bumpless promotion ----------------------------------
    // The documented switchover at the scan boundary: demote the
    // resumed active — its gate closes with the request — then promote
    // the converged standby, whose claim the plant takes before the
    // gate lifts. Exactly one writer throughout: the field holds the
    // old run's last write between the two requests, and the promoted
    // peer's first owner scan carries the same value forward.
    let pre_promotion = standby.role().unwrap();
    assert!(
        matches!(pre_promotion.sync, Some(StandbySync::Tracking { .. })),
        "the standby must be converged to promote: {pre_promotion:?}"
    );
    let held = field.read(layout.gate_cmd).unwrap().value;
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    // The field still carries the demoted peer's last write — nothing
    // moved between the requests.
    assert_eq!(
        field.read(layout.gate_cmd).unwrap().value,
        held,
        "the field must hold the old owner's write across the switch"
    );
    // The promoted peer's first owner scan lifts the gate inside the
    // requested tick and writes the field — the same value the staged
    // image already carried: the handover is bumpless.
    let image = standby.advance(1).unwrap();
    assert_eq!(image.tick, Tick(promoted.tick.0 + 1));
    field_carry(&field, &image, &layout);
    trace.push(observe(&layout, &image));
    assert_eq!(
        field.read(layout.gate_cmd).unwrap().value,
        held,
        "the promoted peer's first owner scan must not move the field"
    );
    assert_eq!(standby.role().unwrap().role, Role::Active);
    // The promoted peer's claim owns the plant's single-writer
    // arbitration: this attachment's writes and steps now fence.
    assert!(matches!(
        field.write(points::INFLOW, Value::Float(0.08)),
        Err(dcs_core::IoError::Fenced(_))
    ));
    assert!(matches!(field.step(1.0), Err(RemoteError::Fenced)));
    // The demoted peer's settle scan runs quiesced — gate closed, no
    // field writes — and the peer is retired.
    let _quiesced = active.advance(1).unwrap();
    assert_eq!(active.role().unwrap().role, Role::Standby);
    kill(&mut active_process);

    // The promoted peer owns the run: the receipted command path lands
    // on it and the field carries only its writes.
    issued.push(command(
        &standby,
        layout.gate_manual_demand,
        ValueKind::Float,
        Value::Float(0.5),
    ));
    let image = owner_phase(&standby, &field, &layout, &mut trace, 6);
    assert!(
        image
            .components
            .iter()
            .all(|component| component.step_errors == 0),
        "a component failed to step: {:?}",
        image.components
    );

    // -- Close-out: the journaled record ---------------------------------
    // Every issued command settled `Applied` under the declared actor
    // on the journal of the peer that owned the field when it landed —
    // the attributed, receipted record the `--journal-file`s keep.
    let served = file_entries(&journal_active);
    let served_standby = standby.journal(0).unwrap();
    let settled: Vec<CommandReceipt> = settled_receipts(&served)
        .into_iter()
        .chain(settled_receipts(&served_standby))
        .collect();
    for receipt in &issued {
        let apply_tick = match receipt.outcome {
            CommandOutcome::Accepted { apply_tick } => apply_tick,
            ref other => panic!("a command never applied: {other:?}"),
        };
        assert!(
            settled.contains(&CommandReceipt {
                command: receipt.command.clone(),
                outcome: CommandOutcome::Applied { tick: apply_tick },
                actor: Some(OPERATOR.to_string()),
            }),
            "no journaled settle matches {receipt:?}"
        );
    }
    // The refused shelve journaled too — the documented-path rejection
    // on the durable record.
    assert!(
        settled.iter().any(|receipt| {
            receipt.actor.as_deref() == Some(OPERATOR)
                && matches!(
                    receipt.outcome,
                    CommandOutcome::Rejected {
                        reason: CommandError::NotWritable { point }
                    } if point == layout.mode_alarm.shelved
                )
        }),
        "the refused shelve must land as a journaled rejection"
    );
    // The promoted peer's journal holds its own attributed settle and
    // the role transitions the switchover recorded; the demoted peer's
    // file — the durable record across the restart — holds its
    // demotion pair behind the run-2 boundary.
    assert_eq!(
        role_changes(&standby),
        vec![
            (Role::Standby, Role::Promoting),
            (Role::Promoting, Role::Active)
        ]
    );
    assert_eq!(
        role_changes_in(&served[before_restart.len()..]),
        vec![
            (Role::Active, Role::Demoting),
            (Role::Demoting, Role::Standby)
        ],
        "the durable journal records the demotion across the restart"
    );
    assert_eq!(file_entries(&journal_standby), served_standby);
    assert_eq!(file_boundaries(&journal_standby), vec![(1, 0)]);

    // -- Close-out: decision 74's durable lifecycle record -------------
    // Every `point_changed` entry names a point the model declared
    // `journaled`; the file's `seq` order is strict across the restart.
    let declared: std::collections::BTreeSet<PointId> = model
        .io_points
        .iter()
        .filter(|point| point.journaled)
        .map(|point| point.id)
        .collect();
    assert!(
        !declared.is_empty(),
        "the composition declares no journaled points"
    );
    for entry in &served {
        if let JournalEvent::PointChanged { point, .. } = &entry.event {
            assert!(
                declared.contains(point),
                "a `point_changed` entry named undeclared point {point:?}"
            );
        }
    }
    assert!(
        served.windows(2).all(|pair| pair[0].seq < pair[1].seq),
        "the durable record's seq order must be strict across the restart"
    );
    assert!(
        served
            .iter()
            .all(|entry| !matches!(entry.event, JournalEvent::StepFailed { .. })),
        "no component may fail a step across the scripted run"
    );

    // The consequential burst preserves every underlying transition in
    // `seq` order with the initiating cause standing first — the
    // automatic-to-manual transition's journaled order is the causal
    // order: the field mode point, the station's status, the alarm,
    // its latch.
    let yes = Value::Bool(true);
    let no = Value::Bool(false);
    assert!(
        transition_seq(&served, layout.gate_mode, yes)
            < transition_seq(&served, layout.manual_active, yes),
        "the field's mode report must stand first in the burst"
    );
    assert!(
        transition_seq(&served, layout.manual_active, yes)
            < transition_seq(&served, layout.mode_alarm.alarm, yes)
    );
    assert!(
        transition_seq(&served, layout.mode_alarm.alarm, yes)
            < transition_seq(&served, layout.mode_alarm.unacknowledged, yes)
    );
    assert!(
        transition_seq(&served, layout.mode_alarm.unacknowledged, yes)
            < released(&served, layout.mode_alarm.unacknowledged),
        "the acknowledged release must follow the latch"
    );
    // The return to automatic journals too.
    assert!(transition_seq(&served, layout.gate_mode, yes) < released(&served, layout.gate_mode));

    // The discrepancy burst: the flag's assertion and its alarm's
    // latch land before the suppression's assert and release — the
    // suppressed alarm's own transitions stay individually visible.
    assert!(
        transition_tick(&served, layout.discrepancy, yes)
            <= transition_tick(&served, layout.discrepancy_alarm.unacknowledged, yes)
    );
    assert!(
        transition_seq(&served, layout.discrepancy_alarm.unacknowledged, yes)
            < transition_seq(&served, layout.discrepancy_alarm.suppressed, yes)
    );
    assert!(
        transition_seq(&served, layout.discrepancy_alarm.suppressed, yes)
            < released(&served, layout.discrepancy_alarm.suppressed)
    );
    // The released standing condition re-annunciates: the latch
    // re-asserts when the suppression clears.
    let disc_unack_asserts: Vec<u64> = served
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::PointChanged { point, to, .. }
                if *point == layout.discrepancy_alarm.unacknowledged && *to == yes =>
            {
                Some(entry.tick.0)
            }
            _ => None,
        })
        .collect();
    assert!(
        disc_unack_asserts.len() >= 2
            && *disc_unack_asserts.last().unwrap()
                >= release_tick(&served, layout.discrepancy_alarm.suppressed),
        "the standing discrepancy must re-latch when the suppression releases"
    );

    // The managed lifecycle: shelve assert, expiry, the manual
    // releases, out-of-service entry and return — every transition on
    // record, including the countdown the restart carried.
    assert!(
        transition_tick(&served, layout.high_level_alarm.shelve.unwrap(), yes)
            <= transition_tick(&served, layout.high_level_alarm.shelved, yes)
    );
    assert!(
        point_changes(&served, layout.high_level_alarm.shelved)
            .iter()
            .filter(|(_, from, to)| from.is_some() && *to == no)
            .count()
            >= 3,
        "the bound expiry, the manual release, and the resumed expiry must all journal"
    );
    assert!(
        transition_tick(&served, layout.high_level_alarm.oos.unwrap(), yes)
            <= transition_tick(&served, layout.high_level_alarm.out_of_service, yes)
    );
    assert!(
        transition_seq(&served, layout.high_level_alarm.out_of_service, yes)
            < released(&served, layout.high_level_alarm.out_of_service)
    );

    // The protection layer's states all land — availability was
    // declared standing, the rest transition on schedule, and the
    // operator's bypass is on record beside its attributed receipt.
    for point in [
        layout.sis_active,
        layout.sis_trip,
        layout.sis_fault,
        layout.sis_proof_test,
        layout.sis_bypass,
    ] {
        assert!(
            point_changes(&served, point)
                .iter()
                .any(|(_, _, to)| *to == yes),
            "{point:?} never journaled its assertion"
        );
    }
    assert!(transition_seq(&served, layout.sis_bypass, yes) < released(&served, layout.sis_bypass));

    // The stale transition lands as the quality record — the frozen
    // repeater's degradation is durable too, in both directions.
    let stale_transitions: Vec<&JournalEntry> = served
        .iter()
        .filter(|entry| {
            matches!(
                &entry.event,
                JournalEvent::QualityChanged { point, .. } if *point == layout.level_remote
            )
        })
        .collect();
    assert!(
        stale_transitions.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::QualityChanged { to, .. } if *to == stale
        )),
        "the frozen repeater's stale transition must journal"
    );
    assert!(
        stale_transitions.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::QualityChanged { from: Some(from), to, .. }
                if *from == stale && *to == Quality::Good
        )),
        "the repeater's recovery must journal"
    );

    // Beside the attributed receipts, in `seq` order: the bypass
    // write's settle precedes the journaled transition it produced.
    let bypass_settle = served
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.command
                        == (Command::WriteValue {
                            point: layout.sis_bypass,
                            kind: ValueKind::Bool,
                            value: yes,
                        })
            )
        })
        .expect("the bypass write's settle must be journaled");
    let bypass_changed = served
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::PointChanged { point, from: Some(_), to }
                    if *point == layout.sis_bypass && *to == yes
            )
        })
        .expect("the bypass transition must be journaled");
    assert!(
        bypass_changed.seq > bypass_settle.seq,
        "the transition must follow its attributed settle in seq order"
    );

    // -- Close-out: the served data model ------------------------------
    // The protection layer's states and its alarms sit in the index's
    // `protection` group — the pane's distinct protection section —
    // beside the ordinary `alarms` group; every alarm instance's
    // `priority`/`class`/`response_ticks` is served through the
    // snapshot's parameter section and its `rationalization` block
    // through the index's component records.
    for point in [
        layout.sis_active,
        layout.sis_available,
        layout.sis_fault,
        layout.sis_trip,
        layout.sis_proof_test,
        layout.sis_bypass,
    ] {
        assert_eq!(
            index.get(point).unwrap().group.as_deref(),
            Some("protection"),
            "{point:?} must render in the protection group"
        );
    }
    for alarm in [
        &layout.sis_trip_alarm,
        &layout.sis_bypass_alarm,
        &layout.sis_fault_alarm,
    ] {
        for point in [alarm.ack, alarm.alarm, alarm.unacknowledged] {
            assert_eq!(
                index.get(point).unwrap().group.as_deref(),
                Some("protection"),
                "{point:?} must render in the protection group"
            );
        }
    }
    for alarm in [&layout.rate_of_rise_alarm, &layout.backup_active_alarm] {
        for point in [alarm.ack, alarm.alarm, alarm.unacknowledged] {
            assert_eq!(
                index.get(point).unwrap().group.as_deref(),
                Some("alarms"),
                "{point:?} must render in the ordinary alarms group"
            );
        }
    }
    let alarm_names = [
        (
            format!(
                "managed-latching-alarm:{}",
                layout.high_level_alarm.component.0
            ),
            (1, 1, 30, Some(6)),
        ),
        (
            format!(
                "managed-bool-latching-alarm:{}",
                layout.mode_alarm.component.0
            ),
            (1, 1, 15, Some(0)),
        ),
        (
            format!(
                "managed-bool-latching-alarm:{}",
                layout.discrepancy_alarm.component.0
            ),
            (1, 1, 15, Some(0)),
        ),
        (
            format!(
                "bool-latching-alarm:{}",
                layout.rate_of_rise_alarm.component.0
            ),
            (2, 1, 30, None),
        ),
        (
            format!(
                "bool-latching-alarm:{}",
                layout.backup_active_alarm.component.0
            ),
            (2, 1, 30, None),
        ),
        (
            format!("bool-latching-alarm:{}", layout.sis_trip_alarm.component.0),
            (1, 1, 5, None),
        ),
        (
            format!(
                "bool-latching-alarm:{}",
                layout.sis_bypass_alarm.component.0
            ),
            (2, 1, 30, None),
        ),
        (
            format!("bool-latching-alarm:{}", layout.sis_fault_alarm.component.0),
            (2, 1, 30, None),
        ),
    ];
    // The parameters section is served identically by either peer —
    // the pair shares the one assembled image — so the promoted
    // owner's snapshot stands in for the whole pair.
    let final_snapshot = standby.snapshot().unwrap();
    for (name, (priority, class, response_ticks, shelve_bound)) in &alarm_names {
        let record = index
            .components
            .iter()
            .find(|record| record.name == *name)
            .unwrap_or_else(|| panic!("no component record for {name}"));
        let rationalization = record
            .rationalization
            .as_ref()
            .unwrap_or_else(|| panic!("{name} serves no rationalization block"));
        assert!(
            !rationalization.consequence.is_empty()
                && !rationalization.required_action.is_empty()
                && !rationalization.reference.is_empty(),
            "{name} serves an incomplete rationalization block"
        );
        let parameters = final_snapshot
            .parameters
            .iter()
            .find(|parameters| parameters.name == *name)
            .unwrap_or_else(|| panic!("no served parameters for {name}"));
        assert_eq!(
            parameters.values["priority"],
            Value::Int(*priority),
            "{name}"
        );
        assert_eq!(parameters.values["class"], Value::Int(*class), "{name}");
        assert_eq!(
            parameters.values["response_ticks"],
            Value::Int(*response_ticks),
            "{name}"
        );
        match shelve_bound {
            Some(bound) => assert_eq!(
                parameters.values["max_shelve_ticks"],
                Value::Int(*bound),
                "{name}"
            ),
            None => assert!(
                !parameters.values.contains_key("max_shelve_ticks"),
                "{name} reports a shelving bound it does not declare"
            ),
        }
    }

    // Strings that legitimately differ run to run — every address is an
    // ephemeral port — are masked before the digests compare; the
    // assertions above already pinned each value to its named source.
    let mut masks: Vec<(String, String)> = [
        (plant.addr, "<plant>"),
        (relay.addr, "<relay>"),
        (active_process.addr, "<active>"),
        (standby_process.addr, "<standby>"),
    ]
    .iter()
    .map(|(addr, label)| (addr.to_string(), label.to_string()))
    .collect();
    masks.sort_by_key(|mask| std::cmp::Reverse(mask.0.len()));

    let digest = serde_json::json!({
        "trace": trace,
        "journal": {
            "before_restart": before_restart,
            "after_restart": after_restart,
            "boundaries": file_boundaries(&journal_active),
            "standby_boundaries": file_boundaries(&journal_standby),
            "settled": settled_receipts(&served),
            "standby_roles": role_changes(&standby),
            "active_file": masked(serde_json::to_value(&served).unwrap(), &masks),
            "standby_file": masked(serde_json::to_value(&served_standby).unwrap(), &masks),
        },
        "restart": {
            "interrupted_at": interrupted,
            "persisted_version": persisted.format_version,
            "resumed_tick": resumed_snapshot.tick,
            "resumed_unack": bool_(image_sample(&resumed_snapshot, layout.high_level_alarm.unacknowledged)),
            "resumed_shelved": bool_(image_sample(&resumed_snapshot, layout.high_level_alarm.shelved)),
            "resumed_trip": bool_(image_sample(&image, points::SIS_TRIP)),
        },
        "promotion": {
            "demoted": masked(serde_json::to_value(&demoted).unwrap(), &masks),
            "promoted": masked(serde_json::to_value(&promoted).unwrap(), &masks),
            "held": held,
            "pre_promotion_sync": pre_promotion.sync,
        },
        "protection": {
            "no_scan_steps": no_scan_steps,
        },
        "operator_view": operator_view,
        "served_parameters": serde_json::to_value(&final_snapshot.parameters).unwrap(),
        "issued": issued,
        "final": masked(serde_json::to_value(&image).unwrap(), &masks),
    });

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

#[test]
fn the_ijmuiden_scenario_walks_every_named_behavior_in_one_scripted_run() {
    run_ijmuiden("once");
}

#[test]
fn identical_ijmuiden_runs_reproduce_the_identical_digest() {
    let first = run_ijmuiden("a");
    let second = run_ijmuiden("b");
    if first != second {
        let dir = std::env::temp_dir().join(format!("dcs-ijmuiden-digest-{}", std::process::id()));
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
