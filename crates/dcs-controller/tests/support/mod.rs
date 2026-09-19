//! The scripted-rig helpers every `dcs-controller` integration target
//! shares: process spawning against the `listening on` announcement,
//! the `dcs-plant-server` and `--driven` controller launch helpers,
//! the `sim-tcp` model re-pointing, and the snapshot/journal
//! inspectors the scripted runs assert through.
//!
//! Each consuming target declares `mod support;` — this subdirectory
//! file is not compiled as its own test target — so every item sees
//! use in some targets and not others.
#![allow(dead_code)]

use dcs_core::{
    CommandReceipt, JournalEntry, JournalEvent, PointId, Sample, TelemetrySnapshot, Value,
};
use dcs_model::PlantModel;
use std::io::{BufRead, BufReader};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

/// The controller binary under test.
pub const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");

/// A `dcs-*` binary sibling of the controller binary under test in the
/// workspace target dir; workspace builds produce them side by side.
pub fn workspace_binary(name: &str) -> PathBuf {
    let binary = Path::new(CONTROLLER)
        .parent()
        .unwrap()
        .join(format!("{name}{}", std::env::consts::EXE_SUFFIX));
    assert!(
        binary.is_file(),
        "{} not found — build the workspace first",
        binary.display()
    );
    binary
}

/// A spawned process: its bound address learned from the announcement
/// line the spawn's parser matched on stderr, stderr held open so a
/// later diagnostic write never meets a closed pipe, and a kill on
/// drop so a panicking test leaves no stray processes behind.
pub struct Spawned {
    pub child: Child,
    pub addr: SocketAddr,
    _stderr: BufReader<ChildStderr>,
}

impl Drop for Spawned {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Kills `spawned` and reaps it — the mid-run process loss the restart
/// and failover scripts drive.
pub fn kill(spawned: &mut Spawned) {
    spawned.child.kill().unwrap();
    spawned.child.wait().unwrap();
}

/// The announcement `dcs-controller` and `dcs-plant-server` print once
/// bound: `listening on <addr>`.
pub fn listening_on(line: &str) -> Option<SocketAddr> {
    line.strip_prefix("listening on ")
        .map(|addr| addr.parse().unwrap())
}

/// The announcement `dcs-sim-bus-device` prints once bound: `serving
/// device <id> on <addr> (declared <listen>)` — returned as the parser
/// [`spawn`] loops on, matching only the declared `device`'s line.
pub fn serving_device(device: u64) -> impl Fn(&str) -> Option<SocketAddr> {
    move |line: &str| {
        line.strip_prefix(&format!("serving device {device} on "))
            .and_then(|rest| rest.split_whitespace().next())
            .map(|addr| addr.parse().unwrap())
    }
}

/// Spawns `binary` with the given stdout disposition, reads stderr
/// until `parse` resolves a line to the bound address, and returns the
/// running process plus the lines that preceded it.
fn spawn_inner(
    binary: &Path,
    args: &[String],
    stdout: Stdio,
    parse: impl Fn(&str) -> Option<SocketAddr>,
) -> (Spawned, Vec<String>) {
    let mut child = Process::new(binary)
        .args(args)
        .stdout(stdout)
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
        match parse(line.trim()) {
            Some(addr) => break addr,
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

/// Spawns `binary`, reads stderr until `parse` resolves a line to the
/// bound address — each binary announces its listener differently
/// (`listening on <addr>`, `serving device <id> on <addr>`), so the
/// line's interpretation stays with the caller — and returns the
/// running process plus the lines that preceded it: the state-file
/// resume report lives there.
pub fn spawn_logged(
    binary: &Path,
    args: &[String],
    parse: impl Fn(&str) -> Option<SocketAddr>,
) -> (Spawned, Vec<String>) {
    spawn_inner(binary, args, Stdio::null(), parse)
}

/// [`spawn_logged`] with stdout piped instead of nulled — for a run
/// whose stdout payload the script reads after the process exits.
pub fn spawn_logged_piped(
    binary: &Path,
    args: &[String],
    parse: impl Fn(&str) -> Option<SocketAddr>,
) -> (Spawned, Vec<String>) {
    spawn_inner(binary, args, Stdio::piped(), parse)
}

/// Spawns `binary` and returns the running process — the plain shape
/// for processes whose announcement needs no preamble.
pub fn spawn(
    binary: &Path,
    args: &[String],
    parse: impl Fn(&str) -> Option<SocketAddr>,
) -> Spawned {
    spawn_logged(binary, args, parse).0
}

/// [`spawn`] with stdout piped instead of nulled — for a bounded run
/// whose final stdout payload the script reads.
pub fn spawn_piped(
    binary: &Path,
    args: &[String],
    parse: impl Fn(&str) -> Option<SocketAddr>,
) -> Spawned {
    spawn_logged_piped(binary, args, parse).0
}

/// A `dcs-plant-server` process serving `model` with `dynamics` merged
/// in, on an ephemeral port.
pub fn spawn_plant(model: &Path, dynamics: &Path) -> Spawned {
    spawn(
        &workspace_binary("dcs-plant-server"),
        &[
            model.to_str().unwrap().to_string(),
            "--dynamics".to_string(),
            dynamics.to_str().unwrap().to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ],
        listening_on,
    )
}

/// A `--driven` controller process on `model` paced at `dt` per scan:
/// the monitor serves on an ephemeral port and scans run only when
/// `POST /scan` requests them.
pub fn spawn_controller(model: &Path, extra: &[String], dt: &str) -> Spawned {
    spawn_controller_logged(model, extra, dt).0
}

/// The state-file restart's spawn: the resume report is a stderr line
/// before `listening on`, so the preamble comes back with the process.
pub fn spawn_controller_logged(model: &Path, extra: &[String], dt: &str) -> (Spawned, Vec<String>) {
    let mut args = vec![model.to_str().unwrap().to_string()];
    args.extend(extra.iter().cloned());
    for arg in ["--listen", "127.0.0.1:0", "--driven", "--dt", dt] {
        args.push(arg.to_string());
    }
    spawn_logged(Path::new(CONTROLLER), &args, listening_on)
}

/// How [`sim_tcp_document`] re-points a model's devices at a shared
/// plant server.
#[derive(Clone, Copy)]
pub enum SimTcp {
    /// Every declared device re-kind'd `sim-tcp` at the plant's
    /// address — the fan-out keeps one backend per declared device, so
    /// the field owner's scan steps the plant once per device.
    PerDevice,
    /// Every declared channel merged onto one `sim-tcp` device — a
    /// single backend stepping the plant once per scan, the pacing the
    /// dynamics declarations are written for.
    Merged,
}

/// The controller-side document for a plant server at `plant`: the
/// `source` model JSON with its devices re-pointed at `sim-tcp` per
/// `devices` — the remote-sim path through the assembly driver
/// registry. Tests layering further edits on the document apply them
/// to the returned value and persist it through [`write_model`].
pub fn sim_tcp_document(source: &str, plant: SocketAddr, devices: SimTcp) -> serde_json::Value {
    let mut document: serde_json::Value = serde_json::from_str(source).unwrap();
    match devices {
        SimTcp::PerDevice => {
            for device in document["devices"].as_array_mut().unwrap() {
                device["kind"] = "sim-tcp".into();
                device["parameters"] = serde_json::json!({ "address": plant.to_string() });
            }
        }
        SimTcp::Merged => {
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
        }
    }
    document
}

/// Writes `document` — a model JSON — under `dir` and loads it once so
/// the caller can compare fingerprints.
pub fn write_model(dir: &Path, name: &str, document: &serde_json::Value) -> (PathBuf, PlantModel) {
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(document).unwrap()).unwrap();
    let model = PlantModel::load(&serde_json::to_string(document).unwrap()).unwrap();
    (path, model)
}

/// Writes the controller-side model for a plant server at `plant`:
/// the `source` document re-pointed at `sim-tcp` per [`SimTcp`],
/// persisted under `dir` as `name` and returned with its loaded model.
pub fn controller_model(
    dir: &Path,
    name: &str,
    source: &str,
    plant: SocketAddr,
    devices: SimTcp,
) -> (PathBuf, PlantModel) {
    write_model(dir, name, &sim_tcp_document(source, plant, devices))
}

/// The sample `snapshot`'s image reports for `point`.
pub fn image_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap_or_else(|| panic!("no sample for {point:?}"))
}

/// The value `snapshot`'s image reports for `point`.
pub fn image_value(snapshot: &TelemetrySnapshot, point: PointId) -> Value {
    image_sample(snapshot, point).value
}

/// The settled-command journal entries of a served journal, in order.
pub fn settled_receipts(journal: &[JournalEntry]) -> Vec<CommandReceipt> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => Some(receipt.clone()),
            _ => None,
        })
        .collect()
}

/// Pumps one accepted connection against the real monitor: two copy
/// loops, one per direction, each ending by half-closing the other
/// side so the request/response pair completes and the sockets close
/// cleanly.
pub fn pump(client: TcpStream, upstream: SocketAddr) {
    let Ok(server) = TcpStream::connect(upstream) else {
        return;
    };
    let Ok(client_reader) = client.try_clone() else {
        return;
    };
    let Ok(server_reader) = server.try_clone() else {
        return;
    };
    let writer = std::thread::spawn(move || {
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
