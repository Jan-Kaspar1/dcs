//! The M8 operations verification — the milestone's done criteria walked
//! end to end as one scripted, tick-paced run over the driven two-pair
//! rig, composing the shipped operations features rather than adding
//! machinery — the same closing shape #161 gave M5.
//!
//! The rig: two `dcs-plant-server` processes each own a shared tank-loop
//! plant, and each plant serves one `dcs-controller --driven` pair — an
//! active plus a tracking standby attached over `sim-tcp` — so the
//! plant-wide surface has two real pairs to aggregate. Every controller
//! runs with `--journal-file`; pair A's standby also runs `--state-file`
//! so its mid-run restart resumes the interrupted run rather than
//! starting cold. Every scan happens inside a `POST /scan` request —
//! nothing is wall-clock paced.
//!
//! The legs, in script order:
//!
//! 1. **The durable journal across a restart (#163)** — pair A's
//!    standby dies mid-run and its replacement replays the journal
//!    file: `GET /journal` answers the pre-restart entries verbatim,
//!    the file's run-boundary marker separates the two lifetimes at
//!    the restored tick, and the resumed run's new entries continue
//!    the `seq` numbering. Pair A's active — journaled to its own
//!    file, never restarted — shows the uninterrupted half.
//! 2. **The plant-wide overview (#164)** — the served page carries
//!    the overview mode, and the card poll it runs — `GET /role` on
//!    each of a pair's peers, then `GET /snapshot` on the selected
//!    source — produces one summary card per configured pair: the
//!    active peer's identity, per-peer role and convergence, the
//!    I/O-health line. Dropping pair B degrades it to its named
//!    faulted card while pair A's card keeps updating.
//! 3. **Actor attribution (#165)** — a command submitted to pair A's
//!    active with a declared actor settles into a receipt carrying
//!    it, and the journaled `CommandSettled` shows the attribution —
//!    in the served journal and the durable file alike — while an
//!    unattributed command journals as before.
//!
//! Every named behavior is asserted through the monitor-client and
//! plant-protocol payloads, never printed output; the digest the run
//! returns proves repeated scripted runs identical.

use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent, LinkState,
    PointId, Role, RoleReport, StandbySync, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_net::RemoteDriver;
use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, Command as Process, Stdio};

/// The controller binary under test.
const CONTROLLER: &str = env!("CARGO_BIN_EXE_dcs-controller");
/// The shared plant's model — the dcs-plant tank loop: level raw (10)
/// and setpoint (11) in, valve command (20) out.
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
/// Ticks both pairs run before pair A's standby restarts mid-run.
const PRE: u64 = 6;
/// Ticks both pairs run after the restart — the post-restart entries
/// the seq-continuity assertions read.
const POST: u64 = 4;
const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// The declared actor identity the attribution leg submits under — the
/// page's `?operator=` equivalent.
const OPERATOR: &str = "ops-lead";
/// The operator values the attributed and unattributed commands land.
const MOVED_SETPOINT: f64 = 55.0;
const RESTED_SETPOINT: f64 = 61.0;

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

/// A plant-server process serving the shared tank-loop plant on an
/// ephemeral port.
fn spawn_plant() -> Spawned {
    spawn(
        &plant_server(),
        &[
            PLANT_MODEL.to_string(),
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

/// Writes the controller-side model for a plant server at `plant`: the
/// shared tank-loop model with every device's kind re-pointed at
/// `sim-tcp` carrying the plant's address — the remote-sim path through
/// the assembly driver registry — and the setpoint point 11 moved
/// image-side: an internal writable `In` point, the operator value the
/// attributed commands target.
fn controller_model(dir: &Path, name: &str, plant: SocketAddr) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        device["kind"] = "sim-tcp".into();
        device["parameters"] = serde_json::json!({ "address": plant.to_string() });
    }
    let setpoint = document["io_points"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|point| point["id"] == SETPOINT.0)
        .unwrap();
    setpoint.as_object_mut().unwrap().remove("channel");
    setpoint["initial"] = serde_json::json!({ "Float": 25.0 });
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// The value `snapshot`'s image reports for `point`.
fn image_value(snapshot: &TelemetrySnapshot, point: PointId) -> Value {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .and_then(|telemetry| telemetry.sample)
        .unwrap()
        .value
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

/// One scripted tick on a pair: the tracking peer scans first — its
/// checkpoint pull lands inside its `POST /scan` — then the field
/// owner scans and steps the shared plant. Returns the owner's
/// snapshot; the pair must tick together.
fn tick_pair(standby: &MonitorClient, active: &MonitorClient) -> TelemetrySnapshot {
    let tracked = standby.advance(1).unwrap();
    let owner = active.advance(1).unwrap();
    assert_eq!(tracked.tick, owner.tick, "the pair must tick together");
    owner
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

/// One configured overview pair, as a `?pair=<name>=<host:port>,…`
/// parameter parses it: the card's identity plus its peers' monitor
/// addresses.
struct PairSpec {
    name: String,
    peers: Vec<SocketAddr>,
}

/// Parses one `?pair=` value — `[name=]host:port[,host:port],…` — the
/// way the page's `parsePairSpec` does: comma-separated peer addresses,
/// empties and duplicates dropped, and the name the card keeps even
/// when every peer is unreachable — the first peer's address when the
/// parameter names none.
fn parse_pair_spec(raw: &str) -> Option<PairSpec> {
    let (name, spec) = match raw.split_once('=') {
        Some((name, spec)) => (name.trim().to_string(), spec),
        None => (String::new(), raw),
    };
    let mut peers: Vec<SocketAddr> = Vec::new();
    for item in spec.split(',') {
        let trimmed = item.trim();
        if trimmed.is_empty() {
            continue;
        }
        let peer: SocketAddr = trimmed.parse().unwrap();
        if !peers.contains(&peer) {
            peers.push(peer);
        }
    }
    if peers.is_empty() {
        return None;
    }
    let name = if name.is_empty() {
        peers[0].to_string()
    } else {
        name
    };
    Some(PairSpec { name, peers })
}

/// One overview card's poll, mirroring the page's `pollPairCard`: a
/// `GET /role` fetch on each of the pair's peers — a failure recorded
/// as that peer's unreachable fault — then `GET /snapshot` on the
/// selected source for the I/O-health line. Selection follows the
/// page's `selectPairSource` order: the peer reporting settled
/// `active`, then a `promoting` peer, then any reachable peer. Nothing
/// here throws: a failed peer or pair degrades its own card and
/// nothing else.
struct CardPoll {
    /// Each configured peer's role fetch — its report, or the recorded
    /// failure the card marks `unreachable` with.
    peers: Vec<Result<RoleReport, String>>,
    /// The selected snapshot source under the page's rule.
    source: Option<usize>,
    /// The index of the peer reporting settled `active` — the card's
    /// "active peer" identity — if one reported.
    active: Option<usize>,
    /// The selected source's snapshot, or the recorded fetch failure;
    /// `None` when no peer was reachable — the page leaves such a card
    /// awaiting its first snapshot rather than recording an error.
    snapshot: Option<Result<TelemetrySnapshot, String>>,
}

fn poll_pair_card(pair: &PairSpec) -> CardPoll {
    let peers: Vec<Result<RoleReport, String>> = pair
        .peers
        .iter()
        .map(|addr| {
            MonitorClient::new(*addr)
                .role()
                .map_err(|error| error.to_string())
        })
        .collect();
    let reporting = |role: Role| {
        peers
            .iter()
            .position(|report| matches!(report, Ok(report) if report.role == role))
    };
    let active = reporting(Role::Active);
    let source = active
        .or_else(|| reporting(Role::Promoting))
        .or_else(|| peers.iter().position(|report| report.is_ok()));
    let snapshot = source.map(|index| {
        MonitorClient::new(pair.peers[index])
            .snapshot()
            .map_err(|error| error.to_string())
    });
    CardPoll {
        peers,
        source,
        active,
        snapshot,
    }
}

/// The card's I/O-health line, mirroring the page's `ioHealthLine`:
/// link degradation, boundary failures, the attributed last fault,
/// scan overruns — read from the source peer's snapshot. A card with
/// no landed snapshot reports the fetch's failure, or waits out its
/// first poll, instead of inventing health.
fn io_health_line(poll: &CardPoll) -> (String, bool) {
    let health = match &poll.snapshot {
        None => return ("awaiting first snapshot".to_string(), false),
        Some(Err(error)) => return (format!("no snapshot — {error}"), true),
        Some(Ok(snapshot)) => &snapshot.io_health,
    };
    let mut troubles = Vec::new();
    if let Some(driver) = &health.driver
        && driver.link != LinkState::Connected
    {
        troubles.push(format!(
            "link {}",
            serde_json::to_value(driver.link).unwrap().as_str().unwrap()
        ));
    }
    if health.failed_reads > 0 || health.failed_writes > 0 {
        troubles.push(format!(
            "{} failed read(s), {} failed write(s)",
            health.failed_reads, health.failed_writes
        ));
    }
    if let Some(fault) = &health.last_error {
        troubles.push(format!(
            "last fault {} at tick {}",
            fault.error, fault.tick.0
        ));
    }
    if health.scan_overruns > 0 {
        troubles.push(format!("{} scan overrun(s)", health.scan_overruns));
    }
    if troubles.is_empty() {
        ("I/O healthy".to_string(), false)
    } else {
        (format!("I/O degraded: {}", troubles.join("; ")), true)
    }
}

/// What the card renders, derived from the poll exactly as the page's
/// `pairCardMarkup` derives it: the card's configured name, the active
/// peer's identity, a per-peer role/convergence/reachability row set,
/// the redundancy summary, and the I/O-health line. A fully
/// unreachable pair still renders — under its name, with every peer's
/// fault recorded — so the card degrades rather than disappears.
fn card_view(pair: &PairSpec, poll: &CardPoll) -> serde_json::Value {
    let mut faults = Vec::new();
    let mut active_name = None;
    let mut rows = Vec::new();
    for (index, peer) in pair.peers.iter().enumerate() {
        let name = peer.to_string();
        match &poll.peers[index] {
            Ok(report) => {
                if report.role == Role::Active {
                    active_name = Some(name.clone());
                }
                match &report.sync {
                    Some(StandbySync::Degraded { detail }) => {
                        faults.push(format!("{name} sync degraded: {detail}"));
                    }
                    Some(StandbySync::Diverged { mismatches }) => faults.push(format!(
                        "{name} standby diverged: staged outputs mismatch the field at {}",
                        mismatches
                            .iter()
                            .map(|mismatch| mismatch.point.0.to_string())
                            .collect::<Vec<_>>()
                            .join(", ")
                    )),
                    _ => {}
                }
                rows.push(serde_json::json!({
                    "peer": name,
                    "role": report.role,
                    "sync": report.sync,
                    "tick": report.tick,
                    "status": "reachable",
                }));
            }
            Err(error) => {
                faults.push(format!("{name} unreachable"));
                rows.push(serde_json::json!({
                    "peer": name,
                    "status": "unreachable",
                    "error": error,
                }));
            }
        }
    }
    if active_name.is_none() {
        faults.push("no peer reports role active".to_string());
    }
    let (io, io_bad) = io_health_line(poll);
    serde_json::json!({
        "card": pair.name,
        "faulted": !faults.is_empty() || io_bad,
        "active_peer": active_name,
        "peers": rows,
        "summary": if faults.is_empty() {
            format!("redundant pair healthy — active on {}", active_name.as_deref().unwrap())
        } else {
            format!("redundancy fault: {}", faults.join("; "))
        },
        "io": io,
    })
}

/// Replaces the run-varying strings inside a serialized value —
/// monitor and plant addresses are ephemeral ports — so two runs'
/// digests compare. Longer strings mask first: one address can never
/// be a prefix of another, but the rule keeps the replacement honest
/// for any run-varying string.
fn masked(value: serde_json::Value, masks: &[(String, String)]) -> serde_json::Value {
    let mut text = serde_json::to_string(&value).unwrap();
    for (from, to) in masks {
        text = text.replace(from.as_str(), to);
    }
    serde_json::from_str(&text).unwrap()
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

/// One scripted run of the M8 operations scenario documented in the
/// module header. Returns the run's auditable record as JSON two runs
/// must reproduce exactly: both pairs' field traces, the journal
/// leg's boundary markers and entry streams, the overview cards in
/// health and in fault, and the attributed and unattributed command
/// receipts with their journaled echoes.
fn run_operations(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-operations-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // Two shared plants, each serving one redundant pair — the
    // plant-wide surface has two real pairs to aggregate.
    let plant_a = spawn_plant();
    let plant_b = spawn_plant();
    let model_a = controller_model(&dir, "pair-a.json", plant_a.addr);
    let model_b = controller_model(&dir, "pair-b.json", plant_b.addr);

    // Every controller journals to its own durable file — the
    // operations surface's audit trail. Pair A's standby also persists
    // its checkpoint, so its mid-run restart resumes the run.
    let journal_a_active = dir.join("pair-a-active.jsonl");
    let journal_a_standby = dir.join("pair-a-standby.jsonl");
    let journal_b_active = dir.join("pair-b-active.jsonl");
    let journal_b_standby = dir.join("pair-b-standby.jsonl");
    let state_a_standby = dir.join("pair-a-standby-state.json");

    let a_active_process = spawn_controller(
        &model_a,
        &[
            "--journal-file".to_string(),
            journal_a_active.to_str().unwrap().to_string(),
        ],
    );
    let a_standby_args = vec![
        "--standby".to_string(),
        a_active_process.addr.to_string(),
        "--journal-file".to_string(),
        journal_a_standby.to_str().unwrap().to_string(),
        "--state-file".to_string(),
        state_a_standby.to_str().unwrap().to_string(),
    ];
    let mut a_standby_process = spawn_controller(&model_a, &a_standby_args);
    let b_active_process = spawn_controller(
        &model_b,
        &[
            "--journal-file".to_string(),
            journal_b_active.to_str().unwrap().to_string(),
        ],
    );
    let b_standby_process = spawn_controller(
        &model_b,
        &[
            "--standby".to_string(),
            b_active_process.addr.to_string(),
            "--journal-file".to_string(),
            journal_b_standby.to_str().unwrap().to_string(),
        ],
    );

    let a_active = MonitorClient::new(a_active_process.addr);
    let mut a_standby = MonitorClient::new(a_standby_process.addr);
    let b_active = MonitorClient::new(b_active_process.addr);
    let b_standby = MonitorClient::new(b_standby_process.addr);
    let field_a = RemoteDriver::connect(plant_a.addr).unwrap();
    let field_b = RemoteDriver::connect(plant_b.addr).unwrap();
    let mut trace_a = Vec::new();
    let mut trace_b = Vec::new();

    // Both pairs run their scripted ticks in lockstep: each tick the
    // tracking peer pulls the active's checkpoint and scans quiesced,
    // then the field owner scans and steps its plant.
    for _ in 0..PRE {
        let owner = tick_pair(&a_standby, &a_active);
        field_carry(&field_a, &owner, &mut trace_a);
        let owner = tick_pair(&b_standby, &b_active);
        field_carry(&field_b, &owner, &mut trace_b);
    }

    // -- Leg 1: the durable journal across a restart (#163) ------------
    // The standby's pre-restart journal: entries the file already holds
    // verbatim behind the run-1 boundary marker.
    let before_restart = a_standby.journal(0).unwrap();
    assert!(
        !before_restart.is_empty(),
        "the pre-restart run must have journaled entries"
    );
    assert_eq!(file_entries(&journal_a_standby), before_restart);
    assert_eq!(file_boundaries(&journal_a_standby), vec![(1, 0)]);

    // The restart: the process dies; its replacement resumes the run
    // from the state file — the preamble reports the restored tick —
    // and replays the journal file into the served ring.
    kill(&mut a_standby_process);
    let (resumed_process, preamble) = spawn_controller_logged(&model_a, &a_standby_args);
    a_standby_process = resumed_process;
    a_standby = MonitorClient::new(a_standby_process.addr);
    assert!(
        preamble.iter().any(|line| {
            line.contains("resumed from state file") && line.contains(&format!("at tick {PRE}"))
        }),
        "the restart must report the resume: {preamble:?}"
    );

    // `GET /journal` answers the pre-restart entries verbatim —
    // replayed, not re-journaled — and the file's run-2 marker
    // separates the two lifetimes at the restored tick.
    assert_eq!(a_standby.journal(0).unwrap(), before_restart);
    assert_eq!(file_boundaries(&journal_a_standby), vec![(1, 0), (2, PRE)]);

    // The pair cadence resumes: the restarted standby reconverges on
    // the active's checkpoints inside its first requested scan.
    for _ in 0..POST {
        let owner = tick_pair(&a_standby, &a_active);
        field_carry(&field_a, &owner, &mut trace_a);
        let owner = tick_pair(&b_standby, &b_active);
        field_carry(&field_b, &owner, &mut trace_b);
    }

    // The resumed run's entries continue the `seq` numbering, and the
    // file holds the whole record — both lifetimes' entries in order.
    let after_restart = a_standby.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1,
        "the first post-restart entry continues the seq numbering"
    );
    assert_eq!(file_entries(&journal_a_standby), after_restart);

    // The file's record order separates the lifetimes: the run-1
    // marker, the run-1 entries, the run-2 marker at the restored
    // tick, then the resumed run's entries.
    let records = file_records(&journal_a_standby);
    assert_eq!(records.len(), after_restart.len() + 2);
    assert_eq!(
        records[0],
        serde_json::json!({ "run_boundary": { "run": 1, "tick": 0 } })
    );
    assert_eq!(
        records[before_restart.len() + 1],
        serde_json::json!({ "run_boundary": { "run": 2, "tick": PRE } })
    );
    for (index, entry) in after_restart.iter().enumerate() {
        let record = &records[index + 1 + usize::from(index >= before_restart.len())];
        assert_eq!(
            record["entry"],
            serde_json::to_value(entry).unwrap(),
            "file record {} must hold the served entry verbatim",
            index + 1
        );
    }

    // The restarted standby is tracking again — the mid-run restart
    // cost the pair nothing the checkpoint stream did not earn back.
    let restarted_report = a_standby.role().unwrap();
    assert_eq!(restarted_report.role, Role::Standby);
    assert_eq!(
        restarted_report.sync,
        Some(StandbySync::Tracking {
            aligned: Tick(PRE + POST - 1)
        }),
        "the resumed standby must reconverge on the active's checkpoints"
    );

    // Pair A's active journaled the uninterrupted run to its own file;
    // pair B's controllers hold their run-1 records too.
    assert_eq!(file_boundaries(&journal_a_active), vec![(1, 0)]);
    assert_eq!(
        file_entries(&journal_a_active),
        a_active.journal(0).unwrap()
    );
    for path in [&journal_b_active, &journal_b_standby] {
        assert_eq!(file_boundaries(path), vec![(1, 0)]);
        assert!(!file_entries(path).is_empty());
    }

    // -- Leg 2: the plant-wide overview (#164) -------------------------
    // The served page carries the overview mode: the `?pair=`
    // parameter parsing, the per-card poll over each pair's own
    // `/role` and `/snapshot` payloads, the per-pair fault
    // degradation, and the card's link into the pair's own view —
    // carrying the configured operator identity into it. Aggregation
    // stays page-side; the endpoints are the pair view's own.
    let page = a_active.page().unwrap();
    for needle in [
        "id=\"overview\"",
        "id=\"overview-cards\"",
        "getAll(\"pair\")",
        "function parsePairSpec(raw)",
        "function pollPairCard(pair)",
        "function selectPairSource(pair)",
        "function pairCardMarkup(pair)",
        "function pairHealth(pairPeers, states)",
        "function ioHealthLine(pair)",
        "function pairHref(pair)",
        "overviewPairs.map(pollPairCard)",
        "unreachable",
        "snapshot.io_health",
        "\"/role\"",
        "\"/snapshot\"",
        // The declared operator identity: configured by ?operator= and
        // carried by the card's link into the pair view.
        "urlParams.get(\"operator\")",
        "actor: operator",
        "\"operator=\" + encodeURIComponent(operator)",
    ] {
        assert!(page.contains(needle), "page lacks {needle}");
    }

    // The overview configuration — repeated `?pair=` parameters each
    // naming one pair's peers — parsed as the page's parsePairSpec
    // does. The restarted standby's new address is the card's peer.
    let overview_pairs = [
        parse_pair_spec(&format!(
            "pair-a={},{}",
            a_active_process.addr, a_standby_process.addr
        ))
        .unwrap(),
        parse_pair_spec(&format!(
            "pair-b={},{}",
            b_active_process.addr, b_standby_process.addr
        ))
        .unwrap(),
    ];
    assert_eq!(overview_pairs[0].name, "pair-a");
    assert_eq!(overview_pairs[1].name, "pair-b");

    // One summary card per pair: the active peer's identity, each
    // peer's reported role and convergence, and the I/O-health line —
    // all read from the pairs' existing payloads.
    let card_a = poll_pair_card(&overview_pairs[0]);
    assert_eq!(card_a.active, Some(0));
    assert_eq!(card_a.source, Some(0));
    let report = card_a.peers[0].as_ref().unwrap();
    assert_eq!(report.role, Role::Active);
    assert_eq!(report.sync, None);
    let report = card_a.peers[1].as_ref().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert_eq!(
        report.sync,
        Some(StandbySync::Tracking {
            aligned: Tick(PRE + POST - 1)
        })
    );
    let snapshot = card_a.snapshot.as_ref().unwrap().as_ref().unwrap();
    assert_eq!(snapshot.tick, Tick(PRE + POST));
    assert_eq!(snapshot.io_health.failed_reads, 0);
    assert_eq!(snapshot.io_health.failed_writes, 0);
    assert_eq!(snapshot.io_health.last_error, None);
    assert_eq!(snapshot.io_health.scan_overruns, 0);
    assert_eq!(
        snapshot.io_health.driver.as_ref().unwrap().link,
        LinkState::Connected,
        "the card's I/O-health line reads a connected sim-tcp link"
    );
    let view_a = card_view(&overview_pairs[0], &card_a);
    assert_eq!(view_a["faulted"], false);
    assert_eq!(view_a["io"], "I/O healthy");

    // Card B is independent: its own peers' reports, its own run.
    let card_b = poll_pair_card(&overview_pairs[1]);
    assert_eq!(card_b.active, Some(0));
    assert_eq!(card_b.peers[1].as_ref().unwrap().role, Role::Standby);
    assert_eq!(
        card_b.snapshot.as_ref().unwrap().as_ref().unwrap().tick,
        Tick(PRE + POST)
    );
    let view_b = card_view(&overview_pairs[1], &card_b);
    assert_eq!(view_b["faulted"], false);

    // Pair B drops entirely — both controllers die, the plant keeps
    // running unattended. Every peer fetch fails: the card degrades
    // to its named fault, page-side, while pair A's card keeps
    // updating.
    let mut b_active_process = b_active_process;
    let mut b_standby_process = b_standby_process;
    kill(&mut b_active_process);
    kill(&mut b_standby_process);

    let dropped_b = poll_pair_card(&overview_pairs[1]);
    assert!(
        dropped_b.peers.iter().all(|peer| peer.is_err()),
        "every peer of the dropped pair records unreachable"
    );
    assert_eq!(dropped_b.active, None);
    assert_eq!(dropped_b.source, None);
    assert!(dropped_b.snapshot.is_none());
    let dropped_view_b = card_view(&overview_pairs[1], &dropped_b);
    assert_eq!(dropped_view_b["card"], "pair-b");
    assert_eq!(dropped_view_b["faulted"], true);
    assert_eq!(dropped_view_b["active_peer"], serde_json::Value::Null);

    // The surviving pair advances; its card reads the new tick.
    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);
    let card_a_after = poll_pair_card(&overview_pairs[0]);
    assert_eq!(card_a_after.active, Some(0));
    assert_eq!(
        card_a_after
            .snapshot
            .as_ref()
            .unwrap()
            .as_ref()
            .unwrap()
            .tick,
        Tick(PRE + POST + 1),
        "the surviving pair's card keeps updating"
    );
    let view_a_after = card_view(&overview_pairs[0], &card_a_after);
    assert_eq!(view_a_after["faulted"], false);

    // -- Leg 3: actor attribution on the command path (#165) -----------
    // A command submitted with a declared actor — the attributed
    // envelope the page sends under ?operator= — settles into a
    // receipt carrying it.
    let attributed = Command::WriteValue {
        point: SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(MOVED_SETPOINT),
    };
    let receipt = a_active.command_as(&attributed, Some(OPERATOR)).unwrap();
    let apply_tick = match receipt.outcome {
        CommandOutcome::Accepted { apply_tick } => apply_tick,
        other => panic!("the attributed command must be accepted: {other:?}"),
    };
    assert_eq!(receipt.actor.as_deref(), Some(OPERATOR));

    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);
    assert_eq!(
        image_value(&owner, SETPOINT),
        Value::Float(MOVED_SETPOINT),
        "the attributed command applied at its scan boundary"
    );

    // The journaled CommandSettled echoes the settled receipt —
    // attribution included — at the apply tick.
    let settled = settled_receipts(&a_active.journal(0).unwrap());
    assert_eq!(
        settled.last().unwrap(),
        &CommandReceipt {
            command: attributed.clone(),
            outcome: CommandOutcome::Applied { tick: apply_tick },
            actor: Some(OPERATOR.to_string()),
        },
        "the journaled CommandSettled must carry the declared actor"
    );

    // An unattributed command — the bare `Command` body a
    // pre-attribution client sends — journals as before: settled with
    // no actor, never a rejection.
    let bare = Command::WriteValue {
        point: SETPOINT,
        kind: ValueKind::Float,
        value: Value::Float(RESTED_SETPOINT),
    };
    let bare_receipt = a_active.command(&bare).unwrap();
    assert_eq!(bare_receipt.actor, None);
    assert!(matches!(
        bare_receipt.outcome,
        CommandOutcome::Accepted { .. }
    ));
    let owner = tick_pair(&a_standby, &a_active);
    field_carry(&field_a, &owner, &mut trace_a);
    let settled = settled_receipts(&a_active.journal(0).unwrap());
    let unattributed = settled.last().unwrap();
    assert_eq!(unattributed.command, bare);
    assert_eq!(unattributed.actor, None);
    assert!(matches!(
        unattributed.outcome,
        CommandOutcome::Applied { .. }
    ));

    // The durable file carries the same record the endpoint serves —
    // the attributed entry included.
    let served_journal = a_active.journal(0).unwrap();
    assert_eq!(file_entries(&journal_a_active), served_journal);

    // Strings that legitimately differ run to run — every address is
    // an ephemeral port — are masked before the digests compare; the
    // assertions above already pinned each value to its named source.
    let mut masks: Vec<(String, String)> = [
        (a_active_process.addr, "pair-a-active"),
        (a_standby_process.addr, "pair-a-standby"),
        (b_active_process.addr, "pair-b-active"),
        (b_standby_process.addr, "pair-b-standby"),
        (plant_a.addr, "plant-a"),
        (plant_b.addr, "plant-b"),
    ]
    .iter()
    .map(|(addr, label)| (addr.to_string(), format!("<{label}>")))
    .collect();
    masks.sort_by_key(|mask| std::cmp::Reverse(mask.0.len()));

    let digest = serde_json::json!({
        "field_trace": {
            "pair_a": trace_a.iter().map(|(valve, level)| [valve, level]).collect::<Vec<_>>(),
            "pair_b": trace_b.iter().map(|(valve, level)| [valve, level]).collect::<Vec<_>>(),
        },
        "journal": {
            "pre_restart": before_restart,
            "post_restart": after_restart,
            "boundaries": file_boundaries(&journal_a_standby),
            "restarted_sync": restarted_report.sync,
        },
        "overview": {
            "pair_a": masked(view_a, &masks),
            "pair_b": masked(view_b, &masks),
            "pair_b_dropped": masked(dropped_view_b, &masks),
            "pair_a_after_drop": masked(view_a_after, &masks),
        },
        "actor": {
            "attributed_receipt": receipt,
            "unattributed_receipt": bare_receipt,
            "settled": settled_receipts(&served_journal),
        },
    });

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

#[test]
fn the_m8_operations_criteria_walk_end_to_end_in_one_scripted_run() {
    run_operations("once");
}

#[test]
fn identical_operations_runs_reproduce_the_identical_digest() {
    let first = run_operations("a");
    let second = run_operations("b");
    if first != second {
        let dir =
            std::env::temp_dir().join(format!("dcs-operations-digest-{}", std::process::id()));
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
