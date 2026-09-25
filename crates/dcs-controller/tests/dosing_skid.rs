//! The dosing-skid end-to-end acceptance run — issue #267's scripted
//! slice over the composition #258 landed: `dcs-plant-server` serves
//! the checked-in `dosing_skid.json` with the checked-in
//! `dosing_skid_dynamics.json` merged in through `--dynamics`, and a
//! redundant `dcs-controller --driven` pair attaches over the model's
//! field devices. Every scan happens inside a `POST /scan` request —
//! nothing is wall-clock paced.
//!
//! The controller-side model is the same document with every device's
//! channels merged onto one `sim-tcp` device at the plant's address:
//! one remote backend, so the field owner steps the shared plant once
//! per scan — the pacing the dynamics declaration is written for — and
//! the declared run-feedback wires land as backend-internal loopbacks
//! on the plant. The standby's checkpoint pull runs through a TCP
//! relay the script repoints at the restarted active, so the
//! field-owner restart keeps the pair's cadence. Both peers run
//! `--journal-file`; the field owner also runs `--state-file`.
//!
//! The dynamics declaration carries the transport delay the dosing
//! research names: the summed metered rates feed `injection-rate`, and
//! a `dead_time` element delays that injected flow into the
//! `discharge-rate` measurement — the downstream reading lags the
//! injection point by the declared three scans at dt 1.0.
//!
//! The scripted legs, in order:
//!
//! 1. **Normal operation** — at the declared dose the flow-paced
//!    demand tracks a flow sweep, clamping at the declared dose and
//!    rate bounds with `clamped` asserted; the loop closes through the
//!    duty pump's run contact and the delayed discharge measurement.
//! 2. **Permissive loss and return** — the flow-proven contact drops
//!    the demand to the safe value through the pair, and the restart
//!    stays inhibited — no pump commanded, none running — until the
//!    permissive returns.
//! 3. **Bad pacing flow** — a non-`Good` flow engages the declared
//!    `on_bad_flow` fallback: `fallback_active` asserts, the paced
//!    demand stands at `fallback_rate` marked untrusted, the interlock
//!    refuses it, and the quality transition is journaled.
//! 4. **Pump fault and standby handover** — a proven motor fault on
//!    the duty pump hands the run to the standby pump per the
//!    reference declaration; the delivery gap trips the
//!    `deviation-monitor`.
//! 5. **Operator paths** — the dose write and the manual takeover ride
//!    the journaled, receipted command path on the writable internal
//!    points, every receipt actor-attributed; the dose step's delayed
//!    measurement lands tick by tick, `delay` scans behind the
//!    injected rate.
//! 6. **Accounting** — the commanded-consumption totalizer integrates
//!    the demand; the `deviation-monitor` flags the sustained
//!    divergence the handover gap produces.
//! 7. **The alarm set** — every declared skid alarm asserts, latches
//!    unacknowledged, and clears its latch through the writable `ack`
//!    point, each ack an attributed, settled, journaled receipt.
//! 8. **State-file restart** — the field owner's process dies and its
//!    replacement resumes at the persisted tick with the held dose,
//!    the accumulated total, and an unacknowledged latch intact; the
//!    durable journal replays the attributed record verbatim and the
//!    file's run-boundary marker separates the lifetimes.
//! 9. **Mid-delay bumpless promotion** — a dose step still in flight
//!    through the transport delay when demote then promote moves the
//!    field writer to the standby: the promoted peer's scans equal the
//!    demoted peer's quiesced reference tick for tick while the
//!    pending sample lands on schedule — no field discontinuity, no
//!    replayed window, no skipped sample.
//!
//! A standalone test alongside the scripted run proves the plant-side
//! half: the merged map's `capture_state`/`restore_state` carries the
//! dead-time element's in-flight line across a mid-delay checkpoint.
//!
//! Every named behavior is asserted through monitor-client,
//! plant-protocol, and journal payloads — never printed output; the
//! digest the run returns proves repeated scripted runs identical.

use dcs_build::dosing::{DosingSkidConfig, DosingSkidLayout, dosing_skid};
use dcs_build::station::AlarmLayout;
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent, PointId,
    Quality, QualityReason, Role, Sample, StandbySync, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;
use dcs_sim::{Fault, ProcessElement, SimDriver};
use dcs_sim_net::RemoteDriver;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

mod support;

use support::{
    SimTcp, controller_model, image_sample, image_value, kill, pump, settle_sink_health,
    settled_receipts, spawn_controller, spawn_controller_logged, spawn_plant,
};

/// The shared plant's model — the checked-in dosing-skid document
/// #258 emits.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid.json"
);
/// The plant-side physics — the checked-in decision-44 dynamics
/// declaration merged over the model.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/dosing_skid_dynamics.json"
);
/// The controller-side model source — the same checked-in document
/// [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-demo/fixtures/dosing_skid.json");

/// Process time advanced per scan — the dosing declaration's dt.
const DT: &str = "1.0";
/// The field-ownership token the launched active pins via
/// `--owner-token` — the claim the test's stimulus attachment shares,
/// so its process-input writes keep passing the plant's fencing while
/// the active owns the field (and across the state-file restart, whose
/// respawned process claims the same token).
const OWNER_TOKEN: u64 = 499_001;
/// The throwaway token the harness's pre-launch field seeding claims
/// under — `ensure_writer`, then released — so the seeding leaves no
/// claim standing against the launched active's.
const SEED_TOKEN: u64 = 499_901;
/// The declared actor identity every operator command lands under —
/// the receipted path's attribution the journal keeps.
const OPERATOR: &str = "ops-lead";

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

/// Asserts the field carries exactly `owner`'s last writes on every
/// declared field `Out` point — the write-gate proof the shared field
/// answers only to the field owner.
fn field_carry(field: &RemoteDriver, owner: &TelemetrySnapshot, layout: &DosingSkidLayout) {
    for point in [
        layout.pumps[0].cmd,
        layout.pumps[1].cmd,
        layout.pumps[0].speed,
        layout.pumps[1].speed,
    ] {
        assert_eq!(
            field.read(point).unwrap().value,
            image_value(owner, point),
            "the field must carry only the field owner's write on {point:?}"
        );
    }
}

/// One owner tick's observable record — what a repeated scripted run
/// must reproduce identically.
fn observe(layout: &DosingSkidLayout, owner: &TelemetrySnapshot) -> serde_json::Value {
    let s = |point: PointId| image_sample(owner, point);
    let alarm = |layout: &AlarmLayout| [bool_(s(layout.alarm)), bool_(s(layout.unacknowledged))];
    serde_json::json!({
        "tick": owner.tick.0,
        "flow": float(s(layout.flow)),
        "flow_good": s(layout.flow).quality.is_good(),
        "tank_level": float(s(layout.tank_level)),
        "discharge": float(s(layout.discharge_rate)),
        "injection": float(s(layout.injection_rate)),
        "ratio_demand": float(s(layout.ratio_demand)),
        "ratio_good": s(layout.ratio_demand).quality.is_good(),
        "gated": float(s(layout.gated_demand)),
        "demand": float(s(layout.demand)),
        "stage": int(s(layout.stage_demand)),
        "duty": int(s(layout.duty)),
        "staged": int(s(layout.staged)),
        "clamped": bool_(s(layout.clamped)),
        "fallback": bool_(s(layout.fallback_active)),
        "permitted": bool_(s(layout.dosing_permitted)),
        "tripped": bool_(s(layout.interlock_tripped)),
        "manual_active": bool_(s(layout.manual_active)),
        "none_available": bool_(s(layout.none_available)),
        "all_faulted": bool_(s(layout.all_faulted)),
        "total": float(s(layout.dose_total)),
        "deviation": float(s(layout.deviation)),
        "deviating": bool_(s(layout.deviating)),
        "cmd": [
            bool_(s(layout.pumps[0].cmd)),
            bool_(s(layout.pumps[1].cmd)),
        ],
        "run": [
            bool_(s(layout.pumps[0].run)),
            bool_(s(layout.pumps[1].run)),
        ],
        "run_good": [
            s(layout.pumps[0].run).quality.is_good(),
            s(layout.pumps[1].run).quality.is_good(),
        ],
        "speed": [
            float(s(layout.pumps[0].speed)),
            float(s(layout.pumps[1].speed)),
        ],
        "alarms": [
            alarm(&layout.tank_low_alarm),
            alarm(&layout.tank_empty_alarm),
            alarm(&layout.pacing_alarm),
            alarm(&layout.bund_alarm),
            alarm(&layout.external_alarm),
            alarm(&layout.deviation_alarm),
            alarm(&layout.pumps[0].fault_alarm),
            alarm(&layout.pumps[0].pump_fault_alarm),
            alarm(&layout.pumps[1].fault_alarm),
            alarm(&layout.pumps[1].pump_fault_alarm),
        ],
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
    layout: &DosingSkidLayout,
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
    layout: &DosingSkidLayout,
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
    layout: &DosingSkidLayout,
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
    layout: &DosingSkidLayout,
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

/// Asserts the alarm's writable ack point, attributed.
fn ack(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(true))
}

/// Releases the alarm's writable ack point, attributed.
fn release(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
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

/// The durable record covers the served page: `served` was fetched
/// through `GET /journal`, which waits the sink's drain out, so the
/// file holds every served entry in order — while the paced run keeps
/// journaling, the tail a post-flush append may add behind them.
fn assert_file_covers(path: &Path, served: &[JournalEntry]) {
    let file = file_entries(path);
    assert!(
        file.len() >= served.len(),
        "the durable journal {} is shorter than the served record",
        path.display()
    );
    assert_eq!(
        &file[..served.len()],
        served,
        "the durable journal {} must hold the served record in order",
        path.display()
    );
}

/// The role transitions a journal stream recorded.
fn role_changes_in(journal: &[JournalEntry]) -> Vec<(Role, Role)> {
    journal
        .iter()
        .filter_map(|entry| match entry.event {
            JournalEvent::RoleChanged { from, to, .. } => Some((from, to)),
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

/// Asserts `needle`'s `(from, to)` pairs are an in-order subsequence
/// of `actual`'s — the scripted transitions landing in the order the
/// script produced them, among the record's other entries (a resumed
/// run diffs its restored state, re-emitting nothing unchanged).
fn assert_transitions(
    point: PointId,
    actual: &[(u64, Option<Value>, Value)],
    needle: &[(Option<Value>, Value)],
) {
    let mut cursor = 0;
    for want in needle {
        cursor = actual[cursor..]
            .iter()
            .position(|(_, from, to)| (from, to) == (&want.0, &want.1))
            .map(|index| cursor + index + 1)
            .unwrap_or_else(|| {
                panic!("{point:?}'s {want:?} never followed the earlier transitions: {actual:?}")
            });
    }
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

/// One scripted run of the dosing acceptance scenario documented in the
/// module header. Returns the run's auditable record as JSON two runs
/// must reproduce exactly: the per-tick trace the field owner's
/// snapshots produced, the journaled record across the field-owner
/// restart, the role transitions, and the promoted peer's tail.
fn run_dosing(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-dosing-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();

    // The emitted skid's layout — the declared point ids the run
    // addresses, from the same builder #258 checks the document in
    // through.
    let layout = dosing_skid(&DosingSkidConfig::reference()).unwrap().layout;

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let (model_path, model) = controller_model(
        &dir,
        "dosing-skid.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::Merged,
    );

    // The plant serves exactly the skid's channel-bound field points.
    let field = RemoteDriver::connect(plant.addr).unwrap();
    let served: std::collections::BTreeSet<u64> = field
        .list_points()
        .unwrap()
        .iter()
        .map(|info| info.point.0)
        .collect();
    assert_eq!(
        served,
        [
            10, 11, 12, 13, 14, 15, 16, 20, 21, 22, 30, 31, 34, 35, 40, 41, 50, 51, 60, 61, 70, 71,
            100, 101, 110, 111
        ]
        .into_iter()
        .collect(),
        "the plant serves the skid's declared field surface"
    );

    // The declared process flow, the flow-proven contact, and the refill
    // line holding the tank while a pump runs — plus one step so the
    // first scan sees the dynamics' declared state. The field fails
    // closed while unclaimed, so the seeding rides a conditional claim
    // released afterward — the tool's shape — leaving no dead token
    // standing against the launched active's claim.
    field.ensure_writer(SEED_TOKEN).unwrap();
    field.write(layout.flow_source, Value::Float(20.0)).unwrap();
    field.write(layout.flow_proven, Value::Bool(true)).unwrap();
    field.write(layout.tank_refill, Value::Float(10.0)).unwrap();
    field.step(1.0).unwrap();
    field.release_writer().unwrap();

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
        "--owner-token".to_string(),
        OWNER_TOKEN.to_string(),
    ];
    let mut active_process = spawn_controller_logged(&model_path, &active_args, DT).0;
    // The launched active holds the plant's single-writer claim from
    // startup: this stimulus attachment joins that claim — the pinned
    // token's other half — so its process-input writes keep passing
    // where any third attachment's would fence.
    field.claim_writer(OWNER_TOKEN).unwrap();
    let relay = PeerRelay::forwarding(active_process.addr);
    let standby_process = spawn_controller(
        &model_path,
        &[
            "--standby".to_string(),
            relay.addr.to_string(),
            "--journal-file".to_string(),
            journal_standby.to_str().unwrap().to_string(),
        ],
        DT,
    );
    let mut active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    assert_eq!(active.role().unwrap().role, Role::Active);
    assert_eq!(standby.role().unwrap().role, Role::Standby);

    // The operator commands this run issues, in issue order — each one
    // attributed, each one matched against its journaled settle below.
    let mut issued: Vec<CommandReceipt> = Vec::new();
    let mut trace: Vec<serde_json::Value> = Vec::new();
    let row = last_row;
    let f64_of = |row: &serde_json::Value, key: &str| row[key].as_f64().unwrap();
    let bool_of = |row: &serde_json::Value, key: &str| row[key].as_bool().unwrap();
    let int_of = |row: &serde_json::Value, key: &str| row[key].as_i64().unwrap();

    // -- Leg 1: normal operation — the paced loop closes ----------------
    // Settle: the paced demand at the declared 2 mg/L dose over a 20
    // m3/h flow commands the duty pump, its run contact proves through
    // the plant loopback, and the metered discharge reads the command.
    let image = pair_phase(&standby, &active, &field, &layout, &mut trace, 14);
    assert_eq!(f64_of(row(&trace), "demand"), 40.0, "{:?}", row(&trace));
    assert_eq!(f64_of(row(&trace), "flow"), 20.0);
    assert_eq!(
        f64_of(row(&trace), "discharge"),
        40.0,
        "the metered discharge tracks the command: {:?}",
        row(&trace)
    );
    assert_eq!(int_of(row(&trace), "duty"), 1);
    assert!(bool_of(row(&trace), "permitted") && !bool_of(row(&trace), "tripped"));
    assert!(!bool_of(row(&trace), "clamped") && !bool_of(row(&trace), "fallback"));
    assert!(row(&trace)["cmd"][0].as_bool().unwrap() && row(&trace)["run"][0].as_bool().unwrap());
    assert!(!row(&trace)["cmd"][1].as_bool().unwrap() && !row(&trace)["run"][1].as_bool().unwrap());
    // The tracking peer ran the identical scan — the pair is one
    // controller: same tick, same image.
    let tracked = standby.snapshot().unwrap();
    assert_eq!(tracked, image, "the tracking peer's image is the owner's");

    // The flow sweep: demand follows dose × flow through the declared
    // bounds — 15 m3/h paces 30 g/h, 60 m3/h saturates at `max_rate`
    // clamped, 3 m3/h saturates at `min_rate` clamped, 20 m3/h returns
    // mid-range with the flag clear.
    field.write(layout.flow_source, Value::Float(15.0)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert_eq!(f64_of(row(&trace), "demand"), 30.0, "{:?}", row(&trace));
    assert!(!bool_of(row(&trace), "clamped"));
    field.write(layout.flow_source, Value::Float(60.0)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert_eq!(f64_of(row(&trace), "demand"), 100.0, "{:?}", row(&trace));
    assert!(
        bool_of(row(&trace), "clamped"),
        "the upper rate bound must clamp"
    );
    field.write(layout.flow_source, Value::Float(3.0)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert_eq!(f64_of(row(&trace), "demand"), 10.0, "{:?}", row(&trace));
    assert!(
        bool_of(row(&trace), "clamped"),
        "the lower rate bound must clamp"
    );
    field.write(layout.flow_source, Value::Float(20.0)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert_eq!(f64_of(row(&trace), "demand"), 40.0, "{:?}", row(&trace));
    assert!(!bool_of(row(&trace), "clamped"));

    // -- Leg 2/5: operator dose writes on the journaled receipted path --
    // 5 mg/L exceeds `max_dose` (4.0): the dose term clamps and the
    // demand reads 80 g/h with `clamped` asserted. The 2.5 mg/L write
    // is the held value the restart leg later proves.
    issued.push(command(
        &active,
        layout.dose,
        ValueKind::Float,
        Value::Float(5.0),
    ));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 5);
    assert_eq!(f64_of(row(&trace), "demand"), 80.0, "{:?}", row(&trace));
    assert!(bool_of(row(&trace), "clamped"), "the dose bound must clamp");
    // The transport delay, row by row: the dose write steps the paced
    // demand 80 → 50. The step reaches the field — and the injected
    // rate — at base+5; the downstream measurement keeps replaying the
    // delay line's recorded history — the pre-step 40s still in flight,
    // then the 80 step landing on its own schedule — until the pending
    // 50 surfaces exactly `delay` (three) plant steps after the
    // injected rate turned.
    issued.push(command(
        &active,
        layout.dose,
        ValueKind::Float,
        Value::Float(2.5),
    ));
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 10);
    for lagged in &trace[base..base + 3] {
        assert_eq!(
            lagged["discharge"].as_f64().unwrap(),
            40.0,
            "the measurement must replay the samples still in flight: {lagged:?}"
        );
    }
    assert_eq!(
        f64_of(&trace[base + 5], "injection"),
        50.0,
        "the injected rate must turn as the step reaches the field: {:?}",
        trace[base + 5]
    );
    for lagged in &trace[base + 3..base + 8] {
        assert_eq!(
            lagged["discharge"].as_f64().unwrap(),
            80.0,
            "the delayed measurement must replay the earlier step on schedule: {lagged:?}"
        );
    }
    assert_eq!(
        f64_of(&trace[base + 8], "discharge"),
        50.0,
        "the pending delivery must land at the declared delay: {:?}",
        trace[base + 8]
    );
    assert!(!bool_of(row(&trace), "clamped"));

    // -- Leg 2: permissive loss and return through the redundant pair --
    // Flow not proven drops the permissive: the interlock drives
    // `safe_value`, no pump may be commanded or run, and the restart
    // stays inhibited until the contact returns.
    field.write(layout.flow_proven, Value::Bool(false)).unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 10);
    assert!(
        trace[base + 2..].iter().all(|row| {
            !row["permitted"].as_bool().unwrap()
                && row["tripped"].as_bool().unwrap()
                && row["demand"].as_f64().unwrap() == 0.0
        }),
        "loss of flow-proven must hold the demand at the safe value"
    );
    assert!(
        trace[base + 6..]
            .iter()
            .all(|row| !row["cmd"][0].as_bool().unwrap()
                && !row["cmd"][1].as_bool().unwrap()
                && !row["run"][0].as_bool().unwrap()
                && !row["run"][1].as_bool().unwrap()),
        "no pump may be commanded or run while flow is unproven"
    );
    field.write(layout.flow_proven, Value::Bool(true)).unwrap();
    let image = pair_phase(&standby, &active, &field, &layout, &mut trace, 7);
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));
    assert!(bool_of(row(&trace), "permitted"));
    assert!(
        trace[trace.len() - 4..]
            .iter()
            .any(|row| row["run"][0].as_bool().unwrap() || row["run"][1].as_bool().unwrap()),
        "the skid must resume once flow-proven returns"
    );
    assert_eq!(
        standby.snapshot().unwrap(),
        image,
        "the pair stayed one controller through the permissive loss"
    );

    // -- Leg 3: the declared bad-flow fallback ---------------------------
    // The pacing flow goes Bad: `on_bad_flow = 2` drives the paced
    // demand to `fallback_rate` marked untrusted — `fallback_active`
    // asserts and raises its alarm — and the interlock refuses the
    // untrusted demand, so the skid stops dosing until the signal
    // recovers.
    field
        .inject_fault(
            layout.flow_source,
            Fault::Quality(Quality::Bad(QualityReason::CommunicationFault)),
        )
        .unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 8);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["fallback"].as_bool().unwrap()
                && !row["flow_good"].as_bool().unwrap()
                && !row["ratio_good"].as_bool().unwrap()),
        "the declared fallback must engage on a Bad pacing flow"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["ratio_demand"].as_f64().unwrap() == 40.0),
        "the fallback rate must stand at the ratio's output"
    );
    assert!(
        trace[base + 4..]
            .iter()
            .all(|row| row["demand"].as_f64().unwrap() == 0.0),
        "the interlock must refuse an untrusted demand"
    );
    // The pacing-loss alarm asserted and latched.
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][2] == serde_json::json!([true, true])),
        "the pacing-loss alarm must assert and latch"
    );
    // The quality transition is journaled — the alarmed transition the
    // journal contract keeps.
    let journal = active.journal(0).unwrap();
    assert!(
        journal.iter().any(|entry| matches!(
            &entry.event,
            JournalEvent::QualityChanged { point, to, .. }
                if *point == layout.flow && !to.is_good()
        )),
        "the pacing signal's quality transition must journal"
    );
    // Acknowledge through the writable ack point — attributed — then
    // release; the latch clears while the condition stands.
    issued.push(ack(&active, &layout.pacing_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(
        row(&trace)["alarms"][2][1],
        false,
        "the ack must clear the latch"
    );
    issued.push(release(&active, &layout.pacing_alarm));
    field.clear_fault(layout.flow_source).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert!(!bool_of(row(&trace), "fallback"));
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));
    assert_eq!(row(&trace)["alarms"][2], serde_json::json!([false, false]));

    // -- Leg 4/6: proven pump fault, standby handover, the delivery gap --
    // Pump 201's run contact reads Bad: the motor proves the command/
    // feedback disagreement, the group hands duty to pump 202, delivery
    // resumes on the standby — and the measured-consumption gap trips
    // the deviation window. A quality fault, not a disconnect: the
    // declared run-feedback wire lands as a fan-out route the field
    // owner writes each tick, and only a quality fault keeps that write
    // legal while reporting the contact untrusted.
    field
        .inject_fault(
            layout.pumps[0].run,
            Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
        )
        .unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 12);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][6] == serde_json::json!([true, true])),
        "the proven motor fault must latch its alarm"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["duty"].as_i64().unwrap() == 2 && row["run"][1].as_bool().unwrap()),
        "the standby pump must take duty on the proven fault"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["discharge"].as_f64().unwrap() == 50.0),
        "delivery must resume on the standby pump"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["deviating"].as_bool().unwrap()
                && row["deviation"].as_f64().unwrap() < -0.2),
        "the measured-consumption gap must trip the deviation window"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][5] == serde_json::json!([true, true])),
        "the dose-not-confirmed alarm must assert and latch"
    );
    issued.push(ack(&active, &layout.pumps[0].fault_alarm));
    issued.push(ack(&active, &layout.deviation_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(row(&trace)["alarms"][6][1], false);
    assert_eq!(row(&trace)["alarms"][5][1], false);
    issued.push(release(&active, &layout.pumps[0].fault_alarm));
    issued.push(release(&active, &layout.deviation_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 1);

    // The pump's own fault / no-discharge contacts drop availability:
    // pump 201's contact holds it out while pump 202's run fault proves
    // and its contact reports — `none_available` trips the chain and
    // every pump stops until the contacts clear.
    field
        .write(layout.pumps[0].pump_fault, Value::Bool(true))
        .unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 5);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][7] == serde_json::json!([true, true])),
        "the pump-fault contact must latch its alarm"
    );
    issued.push(ack(&active, &layout.pumps[0].pump_fault_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    issued.push(release(&active, &layout.pumps[0].pump_fault_alarm));
    field
        .inject_fault(
            layout.pumps[1].run,
            Fault::Quality(Quality::Bad(QualityReason::DeviceFault)),
        )
        .unwrap();
    field
        .write(layout.pumps[1].pump_fault, Value::Bool(true))
        .unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 8);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][8] == serde_json::json!([true, true])),
        "pump 202's proven fault must latch its alarm"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][9] == serde_json::json!([true, true])),
        "pump 202's fault contact must latch its alarm"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["none_available"].as_bool().unwrap()
                && row["demand"].as_f64().unwrap() == 0.0),
        "no pump available must drop the demand"
    );
    issued.push(ack(&active, &layout.pumps[1].fault_alarm));
    issued.push(ack(&active, &layout.pumps[1].pump_fault_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    issued.push(release(&active, &layout.pumps[1].fault_alarm));
    issued.push(release(&active, &layout.pumps[1].pump_fault_alarm));
    field.clear_fault(layout.pumps[1].run).unwrap();
    field.clear_fault(layout.pumps[0].run).unwrap();
    field
        .write(layout.pumps[0].pump_fault, Value::Bool(false))
        .unwrap();
    field
        .write(layout.pumps[1].pump_fault, Value::Bool(false))
        .unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 13);
    assert!(bool_of(row(&trace), "permitted"), "{:?}", row(&trace));
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));
    assert!(
        row(&trace)["run"][0].as_bool().unwrap() || row(&trace)["run"][1].as_bool().unwrap(),
        "a pump must run again once the contacts clear: {:?}",
        row(&trace)
    );
    assert!(
        trace[trace.len() - 3..]
            .iter()
            .all(|row| row["alarms"][8][1] == false && row["alarms"][9][1] == false),
        "pump 202's latches must stay cleared"
    );

    // -- Leg 7 continues: bund flood and external inhibit ---------------
    field.write(layout.bund_flood, Value::Bool(true)).unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][3] == serde_json::json!([true, true])),
        "the bund-flood alarm must assert and latch"
    );
    assert!(
        trace[base + 1..]
            .iter()
            .all(|row| row["tripped"].as_bool().unwrap() && row["demand"].as_f64().unwrap() == 0.0),
        "bund flood must hold the demand at the safe value"
    );
    issued.push(ack(&active, &layout.bund_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(row(&trace)["alarms"][3][1], false);
    issued.push(release(&active, &layout.bund_alarm));
    field.write(layout.bund_flood, Value::Bool(false)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 5);
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));

    field
        .write(layout.external_inhibit, Value::Bool(true))
        .unwrap();
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][4] == serde_json::json!([true, true])),
        "the external-inhibit alarm must assert and latch"
    );
    assert!(
        trace[base + 1..]
            .iter()
            .all(|row| row["demand"].as_f64().unwrap() == 0.0),
        "the external inhibit must hold the demand at the safe value"
    );
    issued.push(ack(&active, &layout.external_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(row(&trace)["alarms"][4][1], false);
    issued.push(release(&active, &layout.external_alarm));
    field
        .write(layout.external_inhibit, Value::Bool(false))
        .unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 5);
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));

    // -- Leg 5: manual takeover through the writable internal points ----
    issued.push(command(
        &active,
        layout.manual_mode,
        ValueKind::Bool,
        Value::Bool(true),
    ));
    issued.push(command(
        &active,
        layout.manual_rate,
        ValueKind::Float,
        Value::Float(55.0),
    ));
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["manual_active"].as_bool().unwrap()
                && row["demand"].as_f64().unwrap() == 55.0),
        "manual mode must drive the operator's rate"
    );
    issued.push(command(
        &active,
        layout.manual_mode,
        ValueKind::Bool,
        Value::Bool(false),
    ));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 5);
    assert!(
        trace[trace.len() - 3..]
            .iter()
            .any(|row| !row["manual_active"].as_bool().unwrap()
                && row["demand"].as_f64().unwrap() == 50.0),
        "releasing manual must return to the paced demand"
    );

    // -- Leg 7 continues: the tank alarms — the drain through the
    // declared low (150 L) and empty (60 L) thresholds. Driving the
    // refill line negative moves the tank through its declared
    // thresholds in a few ticks; the empty condition is itself a
    // permissive leg, so the skid stops dosing while the tank stands
    // empty. The tank-low latch is deliberately left unacknowledged —
    // the restart leg proves it carried.
    field
        .write(layout.tank_refill, Value::Float(-40.0))
        .unwrap();
    let base = trace.len();
    pair_until(&standby, &active, &field, &layout, &mut trace, 40, |row| {
        row["alarms"][1][0].as_bool().unwrap()
            && !row["permitted"].as_bool().unwrap()
            && row["demand"].as_f64().unwrap() == 0.0
    });
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][0] == serde_json::json!([true, true])),
        "the tank-low alarm must assert and latch"
    );
    assert!(
        trace[base..]
            .iter()
            .any(|row| row["alarms"][1] == serde_json::json!([true, true])),
        "the tank-empty alarm must assert and latch"
    );
    let empty_at = trace[base..]
        .iter()
        .position(|row| row["alarms"][1][0].as_bool().unwrap())
        .map(|i| base + i)
        .unwrap();
    assert!(
        trace[empty_at]["tank_level"].as_f64().unwrap() <= 60.0 + 1e-9,
        "tank-empty tripped above its limit: {:?}",
        trace[empty_at]
    );
    // Acknowledge the empty latch only — tank-low stays unacknowledged
    // for the restart leg.
    issued.push(ack(&active, &layout.tank_empty_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(row(&trace)["alarms"][1][1], false);
    assert_eq!(
        row(&trace)["alarms"][0][1],
        true,
        "tank-low stays latched for the restart"
    );
    issued.push(release(&active, &layout.tank_empty_alarm));
    // The delivery arrives: the refill line lifts the tank back above
    // the empty inhibit and the skid restarts.
    field.write(layout.tank_refill, Value::Float(60.0)).unwrap();
    pair_until(&standby, &active, &field, &layout, &mut trace, 30, |row| {
        row["permitted"].as_bool().unwrap()
            && row["demand"].as_f64().unwrap() == 50.0
            && (row["run"][0].as_bool().unwrap() || row["run"][1].as_bool().unwrap())
    });
    field.write(layout.tank_refill, Value::Float(10.0)).unwrap();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 4);
    // The deviation latch re-arms on every delivery transient; a final
    // ack leaves the journal with every latch but tank-low's cleared.
    issued.push(ack(&active, &layout.deviation_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    issued.push(release(&active, &layout.deviation_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(row(&trace)["alarms"][5][1], false, "{:?}", row(&trace));

    // Every declared skid alarm asserted and latched — and every latch
    // but tank-low's already cleared through its writable ack point.
    for (index, name) in [
        "tank-low",
        "tank-empty",
        "pacing-lost",
        "bund-flood",
        "external-inhibit",
        "dose-not-confirmed",
        "p201 motor fault",
        "p201 pump fault",
        "p202 motor fault",
        "p202 pump fault",
    ]
    .iter()
    .enumerate()
    {
        assert!(
            trace.iter().any(|row| row["alarms"][index][0] == true),
            "the {name} alarm never asserted"
        );
        assert!(
            trace.iter().any(|row| row["alarms"][index][1] == true),
            "the {name} alarm never latched unacknowledged"
        );
        if index != 0 {
            assert_eq!(
                row(&trace)["alarms"][index][1],
                false,
                "the {name} latch is still set at the restart boundary"
            );
        }
    }
    // The commanded total accumulated while the demand stood.
    assert!(
        f64_of(row(&trace), "total") > f64_of(&trace[0], "total"),
        "the totalizer must integrate the demand"
    );
    let held_total = f64_of(row(&trace), "total");

    // -- Leg 8: the field owner's state-file restart ---------------------
    // The active's process dies; its replacement resumes the persisted
    // run — the file's checkpoint names the model, the tick, and the
    // held dose — and the pair cadence continues without a missed
    // transfer. The durable journal replays the attributed record
    // verbatim behind the run-2 boundary marker.
    let interrupted = active.snapshot().unwrap().tick;
    let before_restart = active.journal(0).unwrap();
    assert!(
        !before_restart.is_empty(),
        "the pre-restart run must have journaled entries"
    );
    assert_file_covers(&journal_active, &before_restart);
    assert_eq!(file_boundaries(&journal_active), vec![(1, 0)]);

    kill(&mut active_process);
    let persisted: Checkpoint =
        serde_json::from_slice(&std::fs::read(&state_active).unwrap()).unwrap();
    assert_eq!(persisted.tick, interrupted);
    assert_eq!(persisted.model_fingerprint, Some(model.fingerprint()));
    assert_eq!(
        persisted.internal[&layout.dose].value,
        Value::Float(2.5),
        "the held dose persists in the state file"
    );

    let (resumed_process, preamble) = spawn_controller_logged(&model_path, &active_args, DT);
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
    // The named state resumes with the run: the held dose, the
    // accumulated total, and tank-low's unacknowledged latch.
    assert_eq!(
        image_value(&resumed_snapshot, layout.dose),
        Value::Float(2.5),
        "the resumed run carries the held dose"
    );
    assert_eq!(
        float(image_sample(&resumed_snapshot, layout.dose_total)),
        held_total,
        "the resumed run carries the accumulated total"
    );
    assert!(
        bool_(image_sample(
            &resumed_snapshot,
            layout.tank_low_alarm.unacknowledged
        )),
        "the resumed run carries the unacknowledged latch"
    );
    let served = active.journal(0).unwrap();
    assert_eq!(
        &served[..before_restart.len()],
        &before_restart[..],
        "the journal replays verbatim"
    );
    assert_eq!(
        served[before_restart.len()],
        JournalEntry {
            seq: before_restart.last().unwrap().seq + 1,
            tick: interrupted,
            event: JournalEvent::RunBoundary { run: 2 },
        },
        "the restart marker must be served at the restored tick: {served:?}"
    );
    assert_eq!(served.len(), before_restart.len() + 1);
    assert_eq!(
        file_boundaries(&journal_active),
        vec![(1, 0), (2, interrupted.0)]
    );

    // The pull path repoints at the resumed active; the pair cadence
    // continues as if the process never died.
    relay.set_upstream(active_process.addr);
    let image = pair_phase(&standby, &active, &field, &layout, &mut trace, 4);
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
    assert_eq!(f64_of(row(&trace), "demand"), 50.0, "{:?}", row(&trace));

    // The resumed run's entries continue the `seq` numbering — the
    // post-restart attributed ack lands on the same durable record the
    // file holds.
    issued.push(ack(&active, &layout.tank_low_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    assert_eq!(
        row(&trace)["alarms"][0][1],
        false,
        "the held latch clears through its ack point"
    );
    issued.push(release(&active, &layout.tank_low_alarm));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 2);
    let after_restart = active.journal(0).unwrap();
    assert!(after_restart.len() > before_restart.len());
    assert_eq!(&after_restart[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        after_restart[before_restart.len()].seq,
        before_restart.last().unwrap().seq + 1,
        "the first post-restart entry continues the seq numbering"
    );

    // The operator dose write resumes on the receipted path: back to
    // the declared 2 mg/L, paced 40 g/h.
    issued.push(command(
        &active,
        layout.dose,
        ValueKind::Float,
        Value::Float(2.0),
    ));
    pair_phase(&standby, &active, &field, &layout, &mut trace, 8);
    assert_eq!(f64_of(row(&trace), "demand"), 40.0, "{:?}", row(&trace));

    // -- Leg 9: the mid-delay bumpless promotion -------------------------
    // A dose write puts a rate step in flight through the transport
    // delay: the injected rate turns as the stepped demand reaches the
    // field while the downstream measurement still reads the in-flight
    // delivery — the switchover lands with pending samples in the
    // delay line, and the promoted peer must continue the line, not
    // restart it.
    issued.push(command(
        &active,
        layout.dose,
        ValueKind::Float,
        Value::Float(2.4),
    ));
    let base = trace.len();
    pair_phase(&standby, &active, &field, &layout, &mut trace, 6);
    assert_eq!(
        f64_of(&trace[base + 5], "injection"),
        48.0,
        "the injected rate must turn as the step reaches the field: {:?}",
        trace[base + 5]
    );
    assert_eq!(
        f64_of(&trace[base + 5], "discharge"),
        40.0,
        "the promotion must land mid-delay — pending samples in flight: {:?}",
        trace[base + 5]
    );

    // The documented switchover at the scan boundary: demote the
    // resumed active — its gate closes with the request — then promote
    // the converged standby, whose claim the plant takes before the
    // gate lifts. Exactly one writer throughout: the field holds the
    // old run's last write between the two requests, and the promoted
    // peer's first owner scan carries the same values forward.
    let pre_promotion = standby.role().unwrap();
    assert!(
        matches!(pre_promotion.sync, Some(StandbySync::Tracking { .. })),
        "the standby must be converged to promote: {pre_promotion:?}"
    );
    let held: Vec<Value> = [
        layout.pumps[0].cmd,
        layout.pumps[1].cmd,
        layout.pumps[0].speed,
        layout.pumps[1].speed,
    ]
    .iter()
    .map(|point| field.read(*point).unwrap().value)
    .collect();
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    // The field still carries the demoted peer's last write — nothing
    // moved between the requests.
    for (point, value) in [
        layout.pumps[0].cmd,
        layout.pumps[1].cmd,
        layout.pumps[0].speed,
        layout.pumps[1].speed,
    ]
    .iter()
    .zip(&held)
    {
        assert_eq!(
            &field.read(*point).unwrap().value,
            value,
            "the field must hold the old owner's write across the switch: {point:?}"
        );
    }
    // The promoted run for the stated tick count: every tick the
    // demoted peer scans quiesced — the uninterrupted reference the
    // switchover never touched — and the promoted owner's image must
    // equal it exactly while the field carries only the owner's
    // writes. The delay line is shared field state, so the pending
    // sample lands on schedule: rows still read the prior rate, then
    // the step lands at the declared delay — never a replayed window,
    // never a skipped sample.
    let mut first_owner = None;
    for compared in 0..5u64 {
        let reference = active.advance(1).unwrap();
        let continued = standby.advance(1).unwrap();
        assert_eq!(
            continued, reference,
            "the promoted run must match the uninterrupted reference tick for tick"
        );
        field_carry(&field, &continued, &layout);
        trace.push(observe(&layout, &continued));
        if compared == 0 {
            assert_eq!(
                continued.tick,
                Tick(promoted.tick.0 + 1),
                "the gate must lift inside the requested tick"
            );
            first_owner = Some(continued);
        }
    }
    let first_owner = first_owner.unwrap();
    for (point, value) in [
        layout.pumps[0].cmd,
        layout.pumps[1].cmd,
        layout.pumps[0].speed,
        layout.pumps[1].speed,
    ]
    .iter()
    .zip(&held)
    {
        assert_eq!(
            &image_value(&first_owner, *point),
            value,
            "the promoted peer's first owner scan must not move the field: {point:?}"
        );
    }
    assert_eq!(standby.role().unwrap().role, Role::Active);
    assert_eq!(active.role().unwrap().role, Role::Standby);
    // The in-flight delivery lands through the promoted run exactly on
    // the pre-switch schedule — the pending 48 surfaces at the declared
    // delay boundary, inside the compared ticks.
    for lagged in &trace[base + 6..base + 8] {
        assert_eq!(
            lagged["discharge"].as_f64().unwrap(),
            40.0,
            "the delay line must not restart across the switch: {lagged:?}"
        );
    }
    assert_eq!(
        f64_of(&trace[base + 8], "discharge"),
        48.0,
        "the pending delivery must land on schedule through the promoted run: {:?}",
        trace[base + 8]
    );
    // The demoted peer is retired.
    kill(&mut active_process);

    // The promoted peer owns the run: the receipted command path lands
    // on it, the paced demand follows the new dose, and the field
    // carries only its writes.
    issued.push(command(
        &standby,
        layout.dose,
        ValueKind::Float,
        Value::Float(2.0),
    ));
    let image = owner_phase(&standby, &field, &layout, &mut trace, 6);
    assert_eq!(f64_of(row(&trace), "demand"), 40.0, "{:?}", row(&trace));
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
                reason: None,
            }),
            "no journaled settle matches {receipt:?}"
        );
    }
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
    assert_file_covers(&journal_standby, &served_standby);
    assert_eq!(file_boundaries(&journal_standby), vec![(1, 0)]);

    // -- Close-out: decision 74's durable lifecycle record -------------
    // Every `point_changed` entry names a point the model declared
    // `journaled`; the file's `seq` order is strict across the
    // restart.
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

    // Activation, return, and managed-state transitions journaled in
    // order — the permissive contact's loss and return, the pacing
    // alarm's assert/clear lifecycle, the mode select's engage and
    // release, and the protection status the run exercised.
    let b = Value::Bool;
    let transitions = |point: PointId| point_changes(&served, point);
    assert_transitions(
        layout.flow_proven,
        &transitions(layout.flow_proven),
        &[(Some(b(true)), b(false)), (Some(b(false)), b(true))],
    );
    assert_transitions(
        layout.pacing_alarm.alarm,
        &transitions(layout.pacing_alarm.alarm),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout.pacing_alarm.unacknowledged,
        &transitions(layout.pacing_alarm.unacknowledged),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout.manual_mode,
        &transitions(layout.manual_mode),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout.manual_active,
        &transitions(layout.manual_active),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout.interlock_tripped,
        &transitions(layout.interlock_tripped),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout.deviating,
        &transitions(layout.deviating),
        &[(Some(b(false)), b(true))],
    );
    assert_transitions(
        layout.pumps[0].motor_fault,
        &transitions(layout.pumps[0].motor_fault),
        &[(Some(b(false)), b(true))],
    );
    assert_transitions(
        layout.pumps[0].avail,
        &transitions(layout.pumps[0].avail),
        &[(Some(b(true)), b(false)), (Some(b(false)), b(true))],
    );
    // The durable lifecycle persisted across the `--journal-file`
    // restart: the carried latch state re-observed unchanged is no
    // transition, and the post-restart ack landed as the same
    // record's next transition.
    assert_transitions(
        layout.tank_low_alarm.unacknowledged,
        &transitions(layout.tank_low_alarm.unacknowledged),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    // Beside the attributed receipts, in `seq` order: the mode
    // write's settle precedes the journaled transition it produced.
    let mode_settle = served
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt }
                    if receipt.command
                        == (Command::WriteValue {
                            point: layout.manual_mode,
                            kind: ValueKind::Bool,
                            value: b(true),
                        })
            )
        })
        .expect("the manual-mode write's settle must be journaled");
    let mode_changed = served
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::PointChanged { point, from: Some(_), to }
                    if *point == layout.manual_mode && *to == b(true)
            )
        })
        .expect("the manual-mode transition must be journaled");
    assert!(
        mode_changed.seq > mode_settle.seq,
        "the transition must follow its attributed settle in seq order"
    );
    // Undeclared points journaled no value transitions — the float
    // setpoints and measurements, the stroke and count churn, the
    // delivered request, and the receipts-only ack all stayed off the
    // durable record.
    for point in [
        layout.dose,
        layout.manual_rate,
        layout.deviation,
        layout.pumps[0].stroke,
        layout.pumps[0].strokes,
        layout.pumps[0].cmd,
        layout.pumps[0].group_cmd,
        layout.pumps[0].speed_eng,
        layout.tank_low_alarm.ack,
    ] {
        assert!(
            transitions(point).is_empty(),
            "undeclared point {point:?} journaled a value transition"
        );
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
            "resumed_dose": image_value(&resumed_snapshot, layout.dose),
            "resumed_total": float(image_sample(&resumed_snapshot, layout.dose_total)),
            "resumed_low_unack": bool_(image_sample(&resumed_snapshot, layout.tank_low_alarm.unacknowledged)),
        },
        "promotion": {
            "demoted": masked(serde_json::to_value(&demoted).unwrap(), &masks),
            "promoted": masked(serde_json::to_value(&promoted).unwrap(), &masks),
            "held": held,
            "pre_promotion_sync": pre_promotion.sync,
        },
        "issued": issued,
        // The journal sink's live counters ride the writer thread's
        // beat — pin the run-stable fields so the digests compare.
        "final": masked(
            serde_json::to_value({
                let mut image = image.clone();
                settle_sink_health(&mut image);
                image
            })
            .unwrap(),
            &masks,
        ),
    });

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

/// The merged plant map both `--dynamics` consumers build: the model's
/// resolved channel map with each declared element merged and
/// revalidated in document order — what `dcs-plant-server --dynamics`
/// serves and `RegisterBank::with_dynamics` binds to registers.
fn merged_plant() -> SimDriver {
    let model = PlantModel::load(MODEL_SOURCE).unwrap();
    let mut map = dcs_assembly::sim_channel_map(&model).unwrap();
    for element in serde_json::from_str::<Vec<ProcessElement>>(
        &std::fs::read_to_string(PLANT_DYNAMICS).unwrap(),
    )
    .unwrap()
    {
        map = map.with_element(element);
        map.validate().unwrap();
    }
    SimDriver::new(map).unwrap()
}

#[test]
fn the_delay_line_survives_a_mid_delay_capture_and_restore() {
    // The field-side half of the mid-delay contract #357 landed: the
    // dead-time element's delay line rides the sim capture/restore
    // state map. With the checked-in dynamics merged exactly as the
    // plant server merges them, a capture taken mid-delay — pending
    // samples in flight — restores into an identical driver and
    // continues the pre-capture trajectory rather than replaying
    // `initial`.
    let reference = merged_plant();
    let captured = merged_plant();
    // Pump 201 commanded at 50: the scaled_flow turns the injected
    // rate on the first step while the delayed measurement still
    // reads the seeded line.
    for driver in [&reference, &captured] {
        driver.write(PointId(100), Value::Bool(true)).unwrap();
        driver.write(PointId(110), Value::Float(50.0)).unwrap();
    }
    reference.step(1.0);
    captured.step(1.0);
    assert_eq!(
        float(captured.read(PointId(16)).unwrap()),
        50.0,
        "the injected rate must turn at once"
    );
    assert_eq!(
        float(captured.read(PointId(13)).unwrap()),
        0.0,
        "the measurement must still read the seeded line"
    );
    // Mid-delay: a second step pushes another pending sample — the
    // line now holds the seed and two in-flight 50s.
    reference.step(1.0);
    captured.step(1.0);
    let state = captured.capture_state().unwrap();
    assert_eq!(state.get("element.13.line.t"), Some(Value::Float(2.0)));
    assert_eq!(state.get("element.13.line.len"), Some(Value::Int(3)));
    assert_eq!(state.get("element.13.line.0.u"), Some(Value::Float(0.0)));
    assert_eq!(state.get("element.13.line.1.u"), Some(Value::Float(50.0)));
    assert_eq!(state.get("element.13.line.2.u"), Some(Value::Float(50.0)));

    // A driver that never ran resumes from the captured line: stepping
    // both in lockstep, the restored driver's measurement must equal
    // the uninterrupted reference's at every step — the pending 50
    // lands at the reference's boundary, not a fresh three-step delay
    // later.
    let resumed = merged_plant();
    resumed.restore_state(&state).unwrap();
    for _ in 0..4 {
        reference.step(1.0);
        resumed.step(1.0);
        assert_eq!(
            resumed.read(PointId(13)).unwrap(),
            reference.read(PointId(13)).unwrap(),
            "the restored line must continue the pre-capture trajectory"
        );
    }
    assert_eq!(
        float(resumed.read(PointId(13)).unwrap()),
        50.0,
        "the pending delivery lands on the pre-capture schedule"
    );
}

#[test]
fn the_dosing_skid_walks_every_named_behavior_in_one_scripted_run() {
    run_dosing("once");
}

#[test]
fn identical_dosing_runs_reproduce_the_identical_digest() {
    let first = run_dosing("a");
    let second = run_dosing("b");
    if first != second {
        let dir = std::env::temp_dir().join(format!("dcs-dosing-digest-{}", std::process::id()));
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
