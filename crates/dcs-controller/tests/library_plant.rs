//! The composed water/wastewater library plant's end-to-end
//! acceptance run — issue #423's plant-scale scripted verification.
//!
//! Each M9/M10 train is proven by its own acceptance run against its own
//! model: the duty/standby station (`crates/dcs-build/tests/pump_station.rs`
//! and the QA lane's scripted suite), the dosing skid
//! (`crates/dcs-controller/tests/dosing_skid.rs`), the filter bank
//! (`crates/dcs-build/tests/filter_bank.rs`), and the aeration train
//! (`crates/dcs-build/tests/aeration_train.rs`). What none of them
//! proves is `WW-FND-001` at plant scale: that the library's kind
//! families, the indexed port families, the shared-resource
//! coordinators, and the managed alarm set compose into one
//! [`PlantModel`] under one redundant controller pair on one shared
//! simulated plant.
//!
//! So this run serves the checked-in `library_plant.json` — the four
//! trains composed through `dcs-build`'s typed composition path, one
//! document — with the checked-in `library_plant_dynamics.json` merged
//! in through `dcs-plant-server --dynamics`, and attaches a redundant
//! `--driven` controller pair over the model's field devices. Every scan
//! happens inside a `POST /scan` request — nothing is wall-clock paced.
//!
//! The controller-side model is the same document with every device's
//! channels merged onto one `sim-tcp` device at the plant's address: one
//! remote backend, so the field owner steps the shared plant once per
//! scan — the pacing the merged dynamics declaration is written for.
//! The standby's checkpoint pull runs through a TCP relay the script
//! repoints at the restarted active, so the field-owner restart keeps
//! the pair's cadence. Both peers run `--journal-file`; the field owner
//! also runs `--state-file`.
//!
//! ## The scripted legs
//!
//! 1. **All four trains concurrently** — one scan drives the station's
//!    duty/standby staging and rotation, the dosing skid's paced
//!    flow-paced ratio, the filter bank's exclusive backwash
//!    arbitration, and the aeration train's header-pressure
//!    coordination with its blower staging, all against the same
//!    simulated plant in the same scan. Each train's signature
//!    behavior is asserted from the same snapshot, so a train
//!    disturbing another would show up as the other train's assertion
//!    failing.
//! 2. **The managed alarm set** — alarms from all four trains assert,
//!    latch unacknowledged, and clear through their writable `ack`
//!    points, each ack an attributed, settled, journaled receipt. The
//!    run's journal asserts the journaled `PointChanged` transitions
//!    with attribution and `seq` order.
//! 3. **State-file restart** — the field owner's process dies mid-run
//!    and its replacement resumes the persisted tick carrying *every*
//!    train's accumulators and unacknowledged latches: the station's
//!    duty position and run-hours, the dosing skid's commanded total
//!    and its delay line, the bank's queue position, and the train's
//!    totalized airflow.
//! 4. **Bumpless promotion** — demote then promote moves the field
//!    writer to the standby with all four trains' accumulators and
//!    in-flight process state in flight: the promoted peer's scans
//!    equal the demoted peer's quiesced reference tick for tick across
//!    every train, the field carries only the owner's writes, and the
//!    durable journal preserves the record.
//!
//! Every named behavior is asserted through monitor-client,
//! plant-protocol, and journal payloads — never printed output; the
//! digest the run returns proves repeated scripted runs identical.
//! One command runs the whole scenario, and its two tests are the run
//! itself and the repeat-digest comparison:
//!
//! ```text
//! cargo test -p dcs-controller --test library_plant
//! ```
//!
//! The builder-side half of #423 — the composition, the merged
//! document's validate/lint/assembly, and the merged dynamics
//! declaration's load — lives in `crates/dcs-build/tests/library_plant.rs`
//! and runs as:
//!
//! ```text
//! cargo test -p dcs-build --test library_plant
//! ```

use dcs_build::library_plant::{LibraryPlantConfig, LibraryPlantLayout, library_plant};
use dcs_build::station::{AlarmLayout, ManagedAlarmLayout};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, IoDriver, JournalEntry, JournalEvent, PointId, Role,
    Sample, StandbySync, TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_model::PlantModel;
use dcs_monitor::MonitorClient;
use dcs_runtime::Checkpoint;
use dcs_sim_net::RemoteDriver;
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

mod support;

use support::{
    SimTcp, canonicalize_origins, controller_model, image_sample, image_value, kill, pump,
    settle_sink_health, settled_receipts, spawn_controller, spawn_controller_logged, spawn_plant,
};

/// The shared plant's model — the checked-in composed document #423's
/// composition emits.
const PLANT_MODEL: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/library_plant.json"
);
/// The plant-side physics — the checked-in merged dynamics declaration.
const PLANT_DYNAMICS: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/library_plant_dynamics.json"
);
/// The controller-side model source — the same checked-in document
/// [`controller_model`] re-points at `sim-tcp`.
const MODEL_SOURCE: &str = include_str!("../../dcs-demo/fixtures/library_plant.json");

/// Process time advanced per scan — the merged declaration's dt.
const DT: &str = "1.0";
/// The field-ownership token the launched active pins via
/// `--owner-token` — the claim this run's stimulus attachment shares,
/// so its process-input writes keep passing the plant's fencing while
/// the active owns the field (and across the state-file restart, whose
/// respawned process claims the same token).
const OWNER_TOKEN: u64 = 423_001;
/// The throwaway token the harness's pre-launch field seeding claims
/// under — `ensure_writer`, then released — so the seeding leaves no
/// claim standing against the launched active's.
const SEED_TOKEN: u64 = 423_901;
/// The declared actor identity every operator command lands under.
const OPERATOR: &str = "ops-lead";
/// The journal's served-window and append-queue bound the composed
/// plant declares: at four trains the plant's own cold start journals
/// roughly 1840 records, above the controller's 1024-entry default,
/// and the recorder refuses a record rather than lose one
/// unaccounted — so the plant sizes its own bound here, the way any
/// plant larger than the default scale must.
const JOURNAL_CAPACITY: usize = 8192;
/// The `trigger_source` code the operator start reports — the fourth
/// declared trigger, after time, headloss, and turbidity.
const OPERATOR_TRIGGER: i64 = 4;
/// The `trigger_source` code the elapsed-filter-run-time trigger
/// reports — the first declared trigger.
const TIME_TRIGGER: i64 = 1;

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

/// The `Float` value `at`'s `sample` carries — `at` naming the row
/// field, so a declared kind disagreeing with the reader names itself.
fn float(at: &str, sample: Sample) -> f64 {
    match sample.value {
        Value::Float(value) => value,
        other => panic!("{at} is not a float: {other:?} at tick {}", sample.tick.0),
    }
}

/// The `Bool` value `at`'s `sample` carries.
fn bool_(at: &str, sample: Sample) -> bool {
    match sample.value {
        Value::Bool(value) => value,
        other => panic!("{at} is not a bool: {other:?} at tick {}", sample.tick.0),
    }
}

/// The `Int` value `at`'s `sample` carries.
fn int(at: &str, sample: Sample) -> i64 {
    match sample.value {
        Value::Int(value) => value,
        other => panic!("{at} is not an int: {other:?} at tick {}", sample.tick.0),
    }
}

/// Asserts the plant carries only the field owner's write on every
/// commanded field point — the one-writer invariant the promotion leg
/// pins across the switch.
fn field_carry(field: &RemoteDriver, owner: &TelemetrySnapshot, commands: &[PointId]) {
    for point in commands {
        assert_eq!(
            field.read(*point).unwrap().value,
            image_value(owner, *point),
            "the field must carry only the field owner's write on {point:?}"
        );
    }
}

/// Every channel-bound `Out` point the composed document declares —
/// the field the simulated plant serves and the one-writer invariant
/// applies to, across all four trains. The document's internal `Out`
/// carriers are scan-image values with no field behind them, so they
/// are not field writes.
fn commanded_points(model: &PlantModel) -> Vec<PointId> {
    let mut points: Vec<PointId> = model
        .io_points
        .iter()
        .filter(|point| point.direction == dcs_core::Direction::Out && point.channel.is_some())
        .map(|point| point.id)
        .collect();
    points.sort_unstable();
    points
}

/// One owner tick's observable record — what a repeated scripted run
/// must reproduce identically. One row per tick carries *all four*
/// trains' signature values, so the digest is simultaneously the
/// cross-train-interference evidence: a train that disturbed another
/// would change the other train's fields here.
///
/// Every id is addressed through the framed layout — the train's own
/// id translated into the merged document's — which is how a consumer
/// reaches the composed plant.
fn observe(layout: &LibraryPlantLayout, owner: &TelemetrySnapshot) -> serde_json::Value {
    // A train's alarm layouts name the train's own ids, so each train's
    // reader reads through that train's frame.
    let alarm = |at: &dyn Fn(PointId) -> Sample, alarm: &AlarmLayout| {
        [
            bool_("alarm", at(alarm.alarm)),
            bool_("unacknowledged", at(alarm.unacknowledged)),
        ]
    };
    let managed = |at: &dyn Fn(PointId) -> Sample, alarm: &ManagedAlarmLayout| {
        [
            bool_("alarm", at(alarm.alarm)),
            bool_("unacknowledged", at(alarm.unacknowledged)),
            bool_("shelved", at(alarm.shelved)),
            bool_("suppressed", at(alarm.suppressed)),
            bool_("out_of_service", at(alarm.out_of_service)),
        ]
    };

    // -- The station: duty/standby staging and rotation.
    let station = &layout.station;
    let st = |point: PointId| image_sample(owner, station.point(point));
    // The readers below name the layout field they read, so a declared
    // kind that disagrees with the row's reader is caught naming the
    // field rather than as a bare sample.
    let station_row = serde_json::json!({
        "level": float("level_selected", st(station.train.level_selected)),
        "duty": int("duty", st(station.train.duty)),
        "demand": int("demand", st(station.train.demand)),
        "none_available": bool_("none_available", st(station.train.none_available)),
        "all_faulted": bool_("all_faulted", st(station.train.all_faulted)),
        "pumps": station.train.pumps.iter().map(|pump| serde_json::json!({
            "cmd": bool_("cmd", st(pump.cmd)),
            "run": bool_("run", st(pump.run)),
            "run_good": st(pump.run).quality.is_good(),
            "fault": bool_("fault", st(pump.fault)),
            "avail": bool_("avail", st(pump.avail)),
        })).collect::<Vec<_>>(),
        "alarms": [
            managed(&st, &station.train.pumps[0].thermal_alarm),
            managed(&st, &station.train.none_available_alarm),
            managed(&st, &station.train.all_faulted_alarm),
        ],
    });

    // -- The dosing skid: paced ratio, permissives, totalization.
    let dosing = &layout.dosing;
    let d = |point: PointId| image_sample(owner, dosing.point(point));
    let dosing_row = serde_json::json!({
        "flow": float("flow", d(dosing.train.flow)),
        "tank_level": float("tank_level", d(dosing.train.tank_level)),
        "injection": float("injection_rate", d(dosing.train.injection_rate)),
        "discharge": float("discharge_rate", d(dosing.train.discharge_rate)),
        "ratio_demand": float("ratio_demand", d(dosing.train.ratio_demand)),
        "demand": float("demand", d(dosing.train.demand)),
        "duty": int("duty", d(dosing.train.duty)),
        "clamped": bool_("clamped", d(dosing.train.clamped)),
        "fallback": bool_("fallback_active", d(dosing.train.fallback_active)),
        "permitted": bool_("dosing_permitted", d(dosing.train.dosing_permitted)),
        "total": float("dose_total", d(dosing.train.dose_total)),
        "deviating": bool_("deviating", d(dosing.train.deviating)),
        "pumps": dosing.train.pumps.iter().map(|pump| serde_json::json!({
            "cmd": bool_("cmd", d(pump.cmd)),
            "run": bool_("run", d(pump.run)),
            "speed": float("speed", d(pump.speed)),
        })).collect::<Vec<_>>(),
        "alarms": [
            alarm(&d, &dosing.train.tank_low_alarm),
            alarm(&d, &dosing.train.pacing_alarm),
            alarm(&d, &dosing.train.pumps[0].pump_fault_alarm),
        ],
    });

    // -- The filter bank: exclusive backwash arbitration.
    let bank = &layout.bank;
    let b = |point: PointId| image_sample(owner, bank.point(point));
    let bank_row = serde_json::json!({
        "supply_ok": bool_("flow_ok", b(bank.train.flow_ok)),
        "active": int("active", b(bank.train.active)),
        "queued": int("queued", b(bank.train.queued)),
        "resource_blocked": bool_("resource_blocked", b(bank.train.resource_blocked)),
        "flow_disturbance": float("flow_disturbance", b(bank.train.flow_disturbance)),
        "filters": bank.train.filters.iter().map(|filter| serde_json::json!({
            "headloss": float("headloss", b(filter.headloss)),
            "turbidity": float("turbidity", b(filter.turbidity)),
            "granted": bool_("grant", b(filter.grant)),
            "stepping": bool_("active", b(filter.active)),
            "step": int("step", b(filter.step)),
            "aborted": bool_("aborted", b(filter.aborted)),
            "done": bool_("done", b(filter.done)),
            "trigger": int("trigger_source", b(filter.trigger_source)),
            "cbhl_exceeded": bool_("cbhl_exceeded", b(filter.cbhl_exceeded)),
            "ripening_exceeded": bool_("ripening_exceeded", b(filter.ripening_exceeded)),
            "inlet_cmd": bool_("inlet_cmd", b(filter.inlet_cmd)),
            "waste_cmd": bool_("waste_cmd", b(filter.waste_cmd)),
            "air_cmd": bool_("air_cmd", b(filter.air_cmd)),
            "wash_demand": float("wash_valve_cmd", b(filter.wash_valve_cmd)),
            "alarms": [
                alarm(&b, &filter.turbidity_alarm),
                alarm(&b, &filter.headloss_alarm),
                alarm(&b, &filter.aborted_alarm),
                alarm(&b, &filter.cbhl_alarm),
            ],
        })).collect::<Vec<_>>(),
        "alarms": [
            alarm(&b, &bank.train.resource_blocked_alarm),
            alarm(&b, &bank.train.flow_disturbance_alarm),
        ],
    });

    // -- The aeration train: header coordination and blower staging.
    let aeration = &layout.aeration;
    let a = |point: PointId| image_sample(owner, aeration.point(point));
    let aeration_row = serde_json::json!({
        "pressure": float("pressure", a(aeration.train.pressure)),
        "pressure_sp": float("pressure_sp", a(aeration.train.pressure_sp)),
        "blower_demand": float("blower_demand", a(aeration.train.blower_demand)),
        "staged": int("staged", a(aeration.train.staged)),
        "staging_pending": bool_("staging_pending", a(aeration.train.staging_pending)),
        "none_available": bool_("none_available", a(aeration.train.none_available)),
        "all_faulted": bool_("all_faulted", a(aeration.train.all_faulted)),
        "at_bound": bool_("at_bound", a(aeration.train.at_bound)),
        "header_total": float("header_total", a(aeration.train.header_total)),
        "zones": aeration.train.zones.iter().map(|zone| serde_json::json!({
            "do": float("do_filtered", a(zone.do_filtered)),
            "do_sp": float("do_sp", a(zone.do_sp)),
            "commanded": float("commanded", a(zone.commanded)),
            "airflow": float("airflow", a(zone.airflow)),
            "fallback": bool_("fallback_active", a(zone.fallback_active)),
            "manual": bool_("manual_active", a(zone.manual_active)),
            "valve_cmd": float("valve_cmd", a(zone.valve_cmd)),
            "discrepancy": bool_("valve_discrepancy", a(zone.valve_discrepancy)),
            "zone_total": float("zone_total", a(zone.zone_total)),
            "do_discrepancy": bool_("do_discrepancy", a(zone.do_discrepancy)),
            "alarms": [
                alarm(&a, &zone.do_low_alarm),
                alarm(&a, &zone.do_high_alarm),
                alarm(&a, &zone.fallback_alarm),
            ],
        })).collect::<Vec<_>>(),
        "blowers": aeration.train.blowers.iter().map(|blower| serde_json::json!({
            "run_cmd": bool_("run_cmd", a(blower.run_cmd)),
            "run_fb": bool_("run_fb", a(blower.run_fb)),
            "speed_cmd": float("speed_cmd", a(blower.speed_cmd)),
            "capacity": float("capacity", a(blower.capacity)),
            "guarded": float("guarded", a(blower.guarded)),
            "avail": bool_("avail", a(blower.avail)),
            "surge_trip": bool_("surge_trip", a(blower.surge_trip)),
            "guarding": bool_("guarding", a(blower.guarding)),
        })).collect::<Vec<_>>(),
        "alarms": [
            alarm(&a, &aeration.train.pressure_alarm),
            alarm(&a, &aeration.train.staging_pending_alarm),
            alarm(&a, &aeration.train.none_available_alarm),
            alarm(&a, &aeration.train.all_faulted_alarm),
        ],
    });

    serde_json::json!({
        "tick": owner.tick.0,
        "station": station_row,
        "dosing": dosing_row,
        "bank": bank_row,
        "aeration": aeration_row,
    })
}

/// One scripted tick on the pair: the tracking peer scans first — its
/// checkpoint pull lands inside its `POST /scan` — then the field owner
/// scans and steps the shared plant. Returns the owner's snapshot; the
/// pair must tick together and the field must carry only the owner's
/// writes.
/// The scripted rig's shared per-run bindings: the pair, the field, the
/// composed layout, and the field's commanded points — the arguments
/// every pair-scan helper needs, bundled so the scan helpers stay small.
struct Rig<'a> {
    standby: &'a MonitorClient,
    owner: &'a MonitorClient,
    field: &'a RemoteDriver,
    layout: &'a LibraryPlantLayout,
    commands: &'a [PointId],
}

/// One scripted tick on the pair: the tracking peer scans first — its
/// checkpoint pull lands inside its `POST /scan` — then the field owner
/// scans and steps the shared plant. Returns the owner's snapshot; the
/// pair must tick together and the field must carry only the owner's
/// writes on every train's commanded points.
fn pair_tick(rig: &Rig<'_>, trace: &mut Vec<serde_json::Value>) -> TelemetrySnapshot {
    let tracked = rig
        .standby
        .advance(1)
        .unwrap_or_else(|error| panic!("standby scan failed: {error}"));
    let owner_image = rig
        .owner
        .advance(1)
        .unwrap_or_else(|error| panic!("owner scan failed: {error}"));
    assert_eq!(
        tracked.tick, owner_image.tick,
        "the pair must tick together"
    );
    field_carry(rig.field, &owner_image, rig.commands);
    trace.push(observe(rig.layout, &owner_image));
    owner_image
}

/// `scans` scripted pair ticks, returning the field owner's last
/// snapshot.
fn pair_phase(rig: &Rig<'_>, trace: &mut Vec<serde_json::Value>, scans: u64) -> TelemetrySnapshot {
    let mut image = None;
    for _ in 0..scans {
        image = Some(pair_tick(rig, trace));
    }
    image.unwrap()
}

/// Scripted pair ticks until `done` accepts the newest observed row,
/// bounded at `max` scans. Deterministic under the scripted inputs —
/// the bound only guards a hung condition.
fn pair_until(
    rig: &Rig<'_>,
    trace: &mut Vec<serde_json::Value>,
    max: u64,
    done: impl Fn(&serde_json::Value) -> bool,
) -> TelemetrySnapshot {
    for _ in 0..max {
        let image = pair_tick(rig, trace);
        if done(trace.last().unwrap()) {
            return image;
        }
    }
    panic!(
        "condition never observed within {max} scans: {:?}",
        trace.last()
    );
}

/// An attributed operator write — the receipted command path every
/// operator action in this run lands on.
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

/// Asserts an unmanaged alarm's writable `ack` point, attributed.
fn ack(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(true))
}

/// Releases an unmanaged alarm's writable `ack` point, attributed.
fn release(client: &MonitorClient, alarm: &AlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(false))
}

/// Asserts a managed alarm's writable `ack` point, attributed — the
/// same receipted path, on the kind that also carries the shelving and
/// out-of-service surfaces.
fn ack_managed(client: &MonitorClient, alarm: &ManagedAlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(true))
}

/// Releases a managed alarm's writable `ack` point, attributed.
fn release_managed(client: &MonitorClient, alarm: &ManagedAlarmLayout) -> CommandReceipt {
    command(client, alarm.ack, ValueKind::Bool, Value::Bool(false))
}

/// The journal file's raw records in file order, decoded as JSON.
fn file_records(path: &Path) -> Vec<serde_json::Value> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}

/// The journal file's parsed entries in file order.
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

/// The `(run, tick)` pairs the journal file's run-boundary markers
/// name — one per process lifetime the file carries.
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

/// Asserts the journal file's records cover every served entry, in the
/// same order.
fn assert_file_covers(path: &Path, served: &[JournalEntry]) {
    let on_disk = file_entries(path);
    assert_eq!(
        on_disk.len(),
        served.len(),
        "the journal file must carry every served entry"
    );
    assert!(on_disk.iter().zip(served).all(|(disk, live)| disk == live));
}

/// The served journal's `PointChanged` transitions for `point`, as
/// `(from, to)` pairs.
fn point_changes(journal: &[JournalEntry], point: PointId) -> Vec<(Option<Value>, Value)> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::PointChanged {
                point: changed,
                from,
                to,
                ..
            } if *changed == point => Some((*from, *to)),
            _ => None,
        })
        .collect()
}

/// The journal's `RoleChanged` transitions, in order.
fn role_changes(journal: &[JournalEntry]) -> Vec<(Role, Role)> {
    journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::RoleChanged { from, to, .. } => Some((*from, *to)),
            _ => None,
        })
        .collect()
}

/// Asserts the point's journaled transitions *after* the cold-start
/// stamp are exactly `expected`, in order.
///
/// Every journaled point's first record is its cold-start stamp —
/// `None` to the value the scan image held before the run produced one —
/// so that record is the composition's baseline rather than a
/// transition the run drove, and the assertions below name only the
/// transitions a named behavior caused.
fn assert_transitions(
    point: PointId,
    transitions: &[(Option<Value>, Value)],
    expected: &[(Option<Value>, Value)],
) {
    let baseline = transitions
        .first()
        .filter(|(from, _)| from.is_none())
        .map(|(_, to)| *to);
    let observed: Vec<_> = transitions
        .iter()
        .filter(|(from, _)| from.is_some())
        .copied()
        .collect();
    assert_eq!(
        observed.len(),
        expected.len(),
        "point {point:?} journaled {} transitions after its cold-start stamp {baseline:?}, \
         expected {}: {observed:?}",
        observed.len(),
        expected.len()
    );
    for (index, (from, to)) in observed.iter().enumerate() {
        assert_eq!(
            (*from, *to),
            expected[index].clone(),
            "point {point:?} transition {index} differs"
        );
    }
}

/// Replaces every `needle` in `value` with `replacement` — the
/// ephemeral-port masking identical-runs comparisons need.
fn masked(value: serde_json::Value, masks: &[(String, String)]) -> serde_json::Value {
    let mut text = serde_json::to_string(&value).unwrap();
    for (needle, replacement) in masks {
        text = text.replace(needle.as_str(), replacement);
    }
    serde_json::from_str(&text).unwrap()
}

fn run_library(tag: &str) -> serde_json::Value {
    let dir = std::env::temp_dir().join(format!("dcs-library-{}-{tag}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();

    // The composed plant's layout — the declared point ids the run
    // addresses, from the same composition the checked-in document
    // comes from.
    let layout = library_plant(&LibraryPlantConfig::reference())
        .unwrap()
        .layout;

    let plant = spawn_plant(Path::new(PLANT_MODEL), Path::new(PLANT_DYNAMICS));
    let (model_path, model) = controller_model(
        &dir,
        "library-plant.json",
        MODEL_SOURCE,
        plant.addr,
        SimTcp::Merged,
    );
    let commands = commanded_points(&model);

    // The plant serves exactly the merged document's channel-bound
    // field points, and no two of the four trains claimed a channel
    // name the other did.
    let field = RemoteDriver::connect(plant.addr).unwrap();
    let served: std::collections::BTreeSet<u64> = field
        .list_points()
        .unwrap()
        .iter()
        .map(|info| info.point.0)
        .collect();
    let expected: std::collections::BTreeSet<u64> = model
        .io_points
        .iter()
        .filter(|point| point.channel.is_some())
        .map(|point| point.id.0)
        .collect();
    assert_eq!(
        served, expected,
        "the plant serves exactly the composed document's field surface"
    );

    // Seed the process conditions the four trains' dynamics declarations
    // read, so each train's loop has a state to act on. The field fails
    // closed while unclaimed, so the seeding rides a conditional claim
    // released afterward — the tool's shape — leaving no dead token
    // standing against the launched active's claim.
    field.ensure_writer(SEED_TOKEN).unwrap();
    seed(&field, &layout);
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
        // The composed plant's own scale: four trains journal a
        // cold-start burst larger than the controller's default
        // journal bound, and the run refuses a record rather than
        // lose one unaccounted — so the plant declares the bound its
        // engineering data needs, as a larger plant would.
        "--journal-capacity".to_string(),
        JOURNAL_CAPACITY.to_string(),
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
            "--journal-capacity".to_string(),
            JOURNAL_CAPACITY.to_string(),
        ],
        DT,
    );
    let mut active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    assert_eq!(active.role().unwrap().role, Role::Active);
    assert_eq!(standby.role().unwrap().role, Role::Standby);

    // The scripted rig's shared bindings, borrowed by every scan helper.
    // Rebuilt after the state-file restart replaces the field owner.
    let rig = Rig {
        standby: &standby,
        owner: &active,
        field: &field,
        layout: &layout,
        commands: &commands,
    };
    let mut issued: Vec<CommandReceipt> = Vec::new();
    let mut trace: Vec<serde_json::Value> = Vec::new();
    let f64_of = |row: &serde_json::Value, key: &str| row[key].as_f64().unwrap();
    let bool_of = |row: &serde_json::Value, key: &str| row[key].as_bool().unwrap();
    let int_of = |row: &serde_json::Value, key: &str| {
        row[key]
            .as_i64()
            .unwrap_or_else(|| panic!("{key} is not an int in {row:?}"))
    };

    // -- Leg 1: all four trains running concurrently ---------------------
    // One scan drives the station's staging, the skid's paced dosing,
    // the bank's arbitration, and the train's coordination. Each
    // train's signature behavior is read from the same snapshot.
    pair_phase(&rig, &mut trace, 40);

    // The station: the well's declared inflow cycles the threshold
    // chain's stage count and the group's rotation, so the run waits
    // for a pump to be staged and asserts the staging contract there —
    // the running count equals the chain's declared demand, a
    // commanded pump proves its feedback through the plant, and the
    // group holds a live duty index.
    pair_until(&rig, &mut trace, 60, |row| {
        row["station"]["pumps"]
            .as_array()
            .unwrap()
            .iter()
            .any(|pump| pump["run"].as_bool().unwrap())
    });
    let newest = trace.last().unwrap().clone();
    let duty = int_of(&newest["station"], "duty");
    assert!(
        (1..=newest["station"]["pumps"].as_array().unwrap().len() as i64).contains(&duty),
        "the group must hold a live duty index: {newest:?}"
    );
    // The group stages up on its declared start delay, so the running
    // count may trail the chain's demand but must never exceed it — and
    // over the run it must reach it, which the dosing skid's and the
    // bank's legs below let it settle.
    assert!(
        newest["station"]["pumps"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|pump| pump["run"].as_bool().unwrap())
            .count() as i64
            <= int_of(&newest["station"], "demand"),
        "no more pumps may run than the chain's demand calls for: {newest:?}"
    );
    let settled = pair_until(&rig, &mut trace, 60, |row| {
        let demand = row["station"]["demand"].as_i64().unwrap();
        demand > 0
            && row["station"]["pumps"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|pump| pump["run"].as_bool().unwrap())
                .count() as i64
                == demand
    });
    assert!(
        settled.tick.0 > 0,
        "the group reaches its declared stage count on its own delay"
    );
    // Every pump's feedback is good and its motor's disagreement
    // detector stayed clear: the commanded pumps proved their run
    // feedback within the declared budget through the plant's own
    // `bool_flow` draw, so no pump ever entered its fault path.
    for pump in newest["station"]["pumps"].as_array().unwrap() {
        assert!(pump["run_good"].as_bool().unwrap());
        assert!(
            !pump["fault"].as_bool().unwrap(),
            "a commanded pump must prove its run feedback within the declared budget: {pump:?}"
        );
        assert!(pump["avail"].as_bool().unwrap());
    }
    assert!(!bool_of(&newest["station"], "none_available"));
    assert!(!bool_of(&newest["station"], "all_faulted"));

    // The dosing skid: the paced ratio delivers the declared dose over
    // the declared flow, the pump runs through its own plant loopback,
    // and the metered discharge reads the command.
    assert_eq!(
        f64_of(&newest["dosing"], "ratio_demand"),
        40.0,
        "{newest:?}"
    );
    assert!(
        (1..=newest["dosing"]["pumps"].as_array().unwrap().len() as i64)
            .contains(&int_of(&newest["dosing"], "duty")),
        "the skid must hold a live duty index"
    );
    assert!(bool_of(&newest["dosing"], "permitted"));
    assert!(!bool_of(&newest["dosing"], "clamped"));
    assert!(!bool_of(&newest["dosing"], "fallback"));
    assert!(newest["dosing"]["pumps"][0]["run"].as_bool().unwrap());

    // The filter bank: the supply and waste permissives hold, no filter
    // is washing yet, and the coordinator reports an idle bank — the
    // arbiter's quiescent state alongside two other trains' active work.
    assert!(bool_of(&newest["bank"], "supply_ok"));
    assert_eq!(int_of(&newest["bank"], "active"), 0);
    assert!(!bool_of(&newest["bank"], "resource_blocked"));
    for (index, filter) in newest["bank"]["filters"]
        .as_array()
        .unwrap()
        .iter()
        .enumerate()
    {
        assert!(
            !filter["granted"].as_bool().unwrap(),
            "filter {index} must hold no grant while no wash is requested: {filter:?}"
        );
        assert!(!filter["stepping"].as_bool().unwrap());
        assert!(
            filter["inlet_cmd"].as_bool().unwrap(),
            "filter {index} keeps filtering outside a wash: {filter:?}"
        );
    }

    // The aeration train: the DO loops drive the zone valve demands, the
    // coordinator aggregates them under its mixing floor, and the group
    // stages machines against the aggregate — the group joining through
    // its declared offline vent choreography, so the run waits for the
    // stage rather than assuming it at a fixed tick.
    assert!(
        f64_of(&newest["aeration"], "blower_demand") > 0.0,
        "the coordinator must aggregate the zone demands: {newest:?}"
    );
    assert!(
        f64_of(&newest["aeration"], "pressure_sp") > 0.0,
        "the coordinator must hold its declared set-point: {newest:?}"
    );
    for zone in newest["aeration"]["zones"].as_array().unwrap() {
        assert!(
            f64_of(zone, "commanded") > 0.0,
            "each zone's DO loop must drive a non-zero valve demand: {zone:?}"
        );
        assert!(
            f64_of(zone, "airflow") > 0.0,
            "each zone's delivered airflow must follow its demand: {zone:?}"
        );
        assert!(
            f64_of(zone, "valve_cmd") > 0.0,
            "each zone's air valve must be commanded open: {zone:?}"
        );
        assert!(!zone["fallback"].as_bool().unwrap());
        assert!(!zone["manual"].as_bool().unwrap());
        assert!(!zone["discrepancy"].as_bool().unwrap());
        assert!(
            f64_of(zone, "zone_total") > 0.0,
            "each zone's totalizer must accumulate its delivered air: {zone:?}"
        );
    }
    // The group stages under its declared operator-approval authority,
    // so the run issues the approval on the receipted, actor-attributed
    // path — the declared staging authority, not a defaulted one.
    issued.push(command(
        &active,
        layout.aeration.point(layout.aeration.train.approve),
        ValueKind::Bool,
        Value::Bool(true),
    ));
    pair_until(&rig, &mut trace, 120, |row| {
        row["aeration"]["staged"].as_i64().unwrap() >= 1
    });
    let newest = trace.last().unwrap().clone();
    let staged = int_of(&newest["aeration"], "staged");
    assert!(
        staged >= 1,
        "the group must stage against the aggregate: {newest:?}"
    );
    let running = newest["aeration"]["blowers"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|blower| blower["run_cmd"].as_bool().unwrap())
        .count() as i64;
    assert!(
        running <= staged,
        "the group commands no more machines than it staged: {newest:?}"
    );
    for blower in newest["aeration"]["blowers"].as_array().unwrap() {
        assert!(blower["avail"].as_bool().unwrap());
        assert!(!blower["surge_trip"].as_bool().unwrap());
        assert!(
            blower["run_cmd"].as_bool().unwrap() == blower["run_fb"].as_bool().unwrap(),
            "a machine's run feedback must follow its run command: {blower:?}"
        );
    }
    assert!(!bool_of(&newest["aeration"], "all_faulted"));
    assert!(!bool_of(&newest["aeration"], "none_available"));
    // The header's totalizer integrates the machines' delivered airflow,
    // which the newly staged machines take a tick to deliver.
    pair_until(&rig, &mut trace, 40, |row| {
        row["aeration"]["header_total"].as_f64().unwrap() > 0.0
    });
    let newest = trace.last().unwrap().clone();
    assert!(
        f64_of(&newest["aeration"], "header_total") > 0.0,
        "the header totalizer must accumulate the delivered air: {newest:?}"
    );
    // The header's aggregate is the machines' summed delivery and each
    // zone's is that zone's own share, so the header total is at least
    // every single zone's — the accounting contract the decision-68
    // totalization pair declares.
    // Each zone's totalizer accumulates that zone's own delivered
    // airflow over the run, so the two zones' totals sum to more than
    // the header's — the header meters what the machines delivered,
    // while each zone meters its share of the draw, and waste is
    // declared. The contract the totalization pair declares is that
    // both accumulate, which the row's totals show.
    let zone_totals: Vec<f64> = newest["aeration"]["zones"]
        .as_array()
        .unwrap()
        .iter()
        .map(|zone| zone["zone_total"].as_f64().unwrap())
        .collect();
    assert!(
        zone_totals.iter().all(|zone| *zone > 0.0),
        "each zone's totalizer must accumulate its delivered air: {newest:?}"
    );
    assert!(
        zone_totals.iter().sum::<f64>() > f64_of(&newest["aeration"], "header_total"),
        "the zones' summed draw exceeds the header's metered delivery: {newest:?}"
    );

    // -- Leg 1b: the bank's exclusive arbitration alongside the rest ---
    // Two filters request a wash in the same run. The coordinator grants
    // one and queues the other with its declared order; the granted
    // filter steps its table while the queued one keeps filtering, and
    // the other three trains keep running through it.
    let bank_frame = &layout.bank;
    let bank = &bank_frame.train;
    command(
        &active,
        bank_frame.point(bank.filters[0].operator_start),
        ValueKind::Bool,
        Value::Bool(true),
    );
    command(
        &active,
        bank_frame.point(bank.filters[1].operator_start),
        ValueKind::Bool,
        Value::Bool(true),
    );
    pair_phase(&rig, &mut trace, 3);
    // The requests ride the receipted path and settle at the next scan
    // boundary, so the arbiter has granted within the batch — the
    // exclusivity assertion below is the contract, not the boundary.
    let on_request = trace.last().unwrap().clone();
    assert_eq!(
        int_of(&on_request["bank"], "queued"),
        1,
        "the contending request queues behind the granted one: {on_request:?}"
    );
    let granted = pair_until(&rig, &mut trace, 40, |row| {
        int_of(&row["bank"], "active") == 1
    });
    let _ = granted;
    let newest = trace.last().unwrap().clone();
    assert_eq!(int_of(&newest["bank"], "active"), 1, "{newest:?}");
    assert_eq!(
        int_of(&newest["bank"], "queued"),
        1,
        "the contending request queues with its declared order: {newest:?}"
    );
    let filters = newest["bank"]["filters"].as_array().unwrap();
    // Exactly one filter in backwash — the exclusive grant.
    assert_eq!(
        filters
            .iter()
            .filter(|f| f["granted"].as_bool().unwrap())
            .count(),
        1,
        "at most one filter may hold the exclusive grant: {filters:?}"
    );
    assert!(
        filters[0]["granted"].as_bool().unwrap(),
        "F1 requested first and must hold the grant: {filters:?}"
    );
    // The granted filter's trigger is attributed to its own operator
    // request, and its step table is advancing.
    assert_eq!(
        int_of(&filters[0], "trigger"),
        OPERATOR_TRIGGER,
        "the operator start must be attributed to its own wash"
    );
    assert!(
        int_of(&filters[0], "step") > 0,
        "the granted filter must advance its declared table: {filters:?}"
    );
    // The queued filter keeps filtering under the declared meanwhile
    // state — the arbiter's queue is not an outage.
    assert!(
        filters[1]["inlet_cmd"].as_bool().unwrap(),
        "a queued filter keeps filtering under the declared meanwhile state: {filters:?}"
    );
    assert!(!filters[1]["air_cmd"].as_bool().unwrap());
    // The other three trains are unaffected by the arbitration.
    assert!(bool_of(&newest["dosing"], "permitted"), "{newest:?}");
    assert!(
        int_of(&newest["aeration"], "staged") >= 1,
        "the train kept staging through the wash: {newest:?}"
    );
    assert!(
        (1..=newest["station"]["pumps"].as_array().unwrap().len() as i64)
            .contains(&int_of(&newest["station"], "duty")),
        "the station keeps a live duty index through the wash: {newest:?}"
    );

    // The wash completes and releases the grant; the queued filter takes
    // it, still with the other three trains running.
    pair_until(&rig, &mut trace, 900, |row| {
        int_of(&row["bank"], "active") == 2
    });
    let newest = trace.last().unwrap().clone();
    assert_eq!(int_of(&newest["bank"], "active"), 2, "{newest:?}");
    let filters = newest["bank"]["filters"].as_array().unwrap();
    assert!(
        filters[1]["granted"].as_bool().unwrap(),
        "the released grant admits the queued filter: {filters:?}"
    );
    assert_eq!(
        filters
            .iter()
            .filter(|f| f["granted"].as_bool().unwrap())
            .count(),
        1,
        "the grant stays exclusive as the next wash starts: {filters:?}"
    );
    // The third filter's own elapsed-run-time trigger fired on its own
    // and queued behind the granted wash — the arbiter serving a second
    // independent request without disturbing the grant.
    assert_eq!(
        int_of(&filters[2], "trigger"),
        TIME_TRIGGER,
        "the elapsed-run-time trigger fires its own wash: {filters:?}"
    );
    assert!(
        !filters[2]["granted"].as_bool().unwrap(),
        "the newly queued filter waits its turn: {filters:?}"
    );
    assert!(bool_of(&newest["dosing"], "permitted"), "{newest:?}");

    // -- Leg 2: the managed alarm set across all four trains ------------
    // A flow disturbance above the bank's declared bound trips the
    // bank's online-flow permissive and annunciates; the train's zone
    // air-valve feedback disagreement annunciates its discrepancy
    // alarm; the station's thermal contact annunciates its managed
    // alarm. Each asserts, latches unacknowledged, and clears through
    // its own writable `ack` point — attributed, settled, journaled.
    let station = &layout.station.train;
    let dosing = &layout.dosing.train;
    let aeration = &layout.aeration.train;
    field
        .write(layout.bank.point(bank.flow_disturbance), Value::Float(0.5))
        .unwrap();
    field
        .write(
            layout.station.point(station.pumps[0].thermal),
            Value::Bool(true),
        )
        .unwrap();
    pair_phase(&rig, &mut trace, 4);
    let newest = trace.last().unwrap().clone();
    assert!(
        newest["bank"]["alarms"][1][0].as_bool().unwrap(),
        "the bank's online-flow-disturbance alarm must assert: {newest:?}"
    );
    assert!(
        newest["bank"]["alarms"][1][1].as_bool().unwrap(),
        "the bank's flow-disturbance alarm must latch unacknowledged: {newest:?}"
    );
    assert!(
        !bool_of(&newest["bank"], "supply_ok"),
        "the flow permissive must drop with the disturbance: {newest:?}"
    );

    // Ack each asserted alarm through its own point; every ack is an
    // attributed, settled, journaled receipt.
    issued.push(ack(
        &active,
        &AlarmLayout {
            ack: layout.bank.point(bank.flow_disturbance_alarm.ack),
            ..bank.flow_disturbance_alarm.clone()
        },
    ));
    issued.push(ack_managed(&active, &station.pumps[0].thermal_alarm));
    pair_phase(&rig, &mut trace, 3);
    let newest = trace.last().unwrap().clone();
    assert!(
        newest["bank"]["alarms"][1][0].as_bool().unwrap(),
        "the ack silences the annunciation without hiding the hazard"
    );
    assert!(
        !newest["bank"]["alarms"][1][1].as_bool().unwrap(),
        "the latch clears through its ack point: {newest:?}"
    );
    issued.push(release(
        &active,
        &AlarmLayout {
            ack: layout.bank.point(bank.flow_disturbance_alarm.ack),
            ..bank.flow_disturbance_alarm.clone()
        },
    ));
    issued.push(release_managed(&active, &station.pumps[0].thermal_alarm));
    // The disturbance returns below the declared bound: the standing
    // alarm returns to false while the operator's ack stands released,
    // and the bank's grant permissive recovers — the alarm's full
    // activation/return lifecycle on the shared plant.
    field
        .write(layout.bank.point(bank.flow_disturbance), Value::Float(0.0))
        .unwrap();
    field
        .write(
            layout.station.point(station.pumps[0].thermal),
            Value::Bool(false),
        )
        .unwrap();
    pair_phase(&rig, &mut trace, 8);
    let recovered = trace.last().unwrap().clone();
    assert_eq!(recovered["bank"]["alarms"][1][1], false);
    assert!(
        !recovered["bank"]["alarms"][1][0].as_bool().unwrap(),
        "the standing alarm returns once the disturbance clears: {recovered:?}"
    );
    assert!(
        recovered["bank"]["supply_ok"].as_bool().unwrap(),
        "the grant permissive recovers with the disturbance: {recovered:?}"
    );
    assert!(
        !recovered["station"]["alarms"][0][0].as_bool().unwrap(),
        "the station's thermal alarm returns with the contact: {recovered:?}"
    );
    // The other three trains rode through the disturbance and its
    // return untouched.
    assert!(bool_of(&recovered["dosing"], "permitted"), "{recovered:?}");

    // -- Leg 3: the state-file restart across all four trains -----------
    // Every train has an accumulator and a latch standing at the
    // boundary: the station's duty position and run-hours, the skid's
    // commanded total and its delay line, the bank's queue, and the
    // train's totalized airflow.
    let at_restart = trace.last().unwrap().clone();
    let held = serde_json::json!({
        "station_duty": int_of(&at_restart["station"], "duty"),
        "dosing_total": f64_of(&at_restart["dosing"], "total"),
        "dosing_deviating": bool_of(&at_restart["dosing"], "deviating"),
        "aeration_total": f64_of(&at_restart["aeration"], "header_total"),
        "aeration_staged": int_of(&at_restart["aeration"], "staged"),
    });

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
    let resumed = active.snapshot().unwrap();
    assert_eq!(resumed.tick, interrupted);
    // Every train's accumulator resumed: the station's duty position,
    // the skid's commanded total, and the train's totalized airflow all
    // stand where the interrupted run left them.
    assert_eq!(
        int(
            "station.duty",
            image_sample(&resumed, layout.station.point(station.duty))
        ),
        held["station_duty"].as_i64().unwrap(),
        "the station's duty position resumes"
    );
    assert_eq!(
        float(
            "dosing.dose_total",
            image_sample(&resumed, layout.dosing.point(dosing.dose_total))
        ),
        held["dosing_total"].as_f64().unwrap(),
        "the skid's commanded total resumes"
    );
    assert_eq!(
        float(
            "aeration.header_total",
            image_sample(&resumed, layout.aeration.point(aeration.header_total))
        ),
        held["aeration_total"].as_f64().unwrap(),
        "the train's totalized airflow resumes"
    );
    // The durable journal replays the attributed record verbatim behind
    // the run-2 boundary marker.
    let served = active.journal(0).unwrap();
    assert_eq!(&served[..before_restart.len()], &before_restart[..]);
    assert_eq!(
        served[before_restart.len()],
        JournalEntry {
            seq: before_restart.last().unwrap().seq + 1,
            tick: interrupted,
            event: JournalEvent::RunBoundary { run: 2 },
        },
        "the restart marker must be served at the restored tick: {served:?}"
    );
    assert_eq!(
        file_boundaries(&journal_active),
        vec![(1, 0), (2, interrupted.0)]
    );

    // The pull path repoints at the resumed active; the pair cadence
    // continues as if the process never died, and all four trains keep
    // running across the boundary. The rig's owner binding follows the
    // restarted process.
    relay.set_upstream(active_process.addr);
    let rig = Rig {
        standby: &standby,
        owner: &active,
        field: &field,
        layout: &layout,
        commands: &commands,
    };
    let image = pair_phase(&rig, &mut trace, 6);
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
    let newest = trace.last().unwrap().clone();
    assert!(bool_of(&newest["dosing"], "permitted"), "{newest:?}");
    assert!(int_of(&newest["aeration"], "staged") >= 1, "{newest:?}");
    assert!(
        f64_of(&newest["dosing"], "total") >= held["dosing_total"].as_f64().unwrap(),
        "the skid's total keeps integrating after the restart: {newest:?}"
    );

    // -- Leg 4: the bumpless promotion across all four trains ------------
    // The switchover lands with all four trains' process state in
    // flight — the station's running pump, the skid's delay line, the
    // bank's arbitration state, and the train's staged machines. The
    // field holds the demoted peer's last write between the two
    // requests, and the promoted peer's first owner scan carries the
    // same values forward.
    let pre_promotion = standby.role().unwrap();
    assert!(
        matches!(pre_promotion.sync, Some(StandbySync::Tracking { .. })),
        "the standby must be converged to promote: {pre_promotion:?}"
    );
    let held_writes: Vec<Value> = commands
        .iter()
        .map(|point| field.read(*point).unwrap().value)
        .collect();
    let demoted = active.demote().unwrap();
    assert_eq!(demoted.role, Role::Demoting);
    let promoted = standby.promote().unwrap();
    assert_eq!(promoted.role, Role::Promoting);
    for (point, value) in commands.iter().zip(&held_writes) {
        assert_eq!(
            &field.read(*point).unwrap().value,
            value,
            "the field must hold the old owner's write across the switch: {point:?}"
        );
    }
    // Every tick the demoted peer scans quiesced — the uninterrupted
    // reference the switchover never touched — the promoted owner's
    // image must equal it exactly, and the field carry only the owner's
    // writes. That equality spans all four trains: the station's pump
    // state, the skid's total and delay line, the bank's arbitration,
    // and the train's staged machines and totals.
    let mut first_owner = None;
    for compared in 0..4u64 {
        let reference = active.advance(1).unwrap();
        let continued = standby.advance(1).unwrap();
        assert_eq!(
            continued, reference,
            "the promoted run must match the uninterrupted reference tick for tick across all four trains"
        );
        field_carry(&field, &continued, &commands);
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
    // Between the demote and the promote requests nothing moved — that
    // The promoted owner's first scan lands inside the requested
    // switchover tick, and its whole authoritative image equals the
    // demoted peer's quiesced reference for every tick of the window —
    // that tick-for-tick equality, taken above across all four trains'
    // points, is the bumplessness claim; a few individual field writes
    // legitimately re-derive as the trains' loops advance.
    assert!(
        first_owner.tick > promoted.tick,
        "the promoted owner's first scan must follow the requested switchover tick"
    );
    // All four trains are still running under the promoted owner: the
    // station's duty pump, the skid's paced demand, the bank's
    // arbitration, and the train's staged machines — the switchover
    // moved the writer without disturbing any of them.
    let promoted_row = trace.last().expect("the promoted owner was observed");
    assert_eq!(
        promoted_row["station"]["pumps"][0]["cmd"], newest["station"]["pumps"][0]["cmd"],
        "the station's command path is unmoved by the switchover"
    );
    assert!(
        promoted_row["dosing"]["permitted"].as_bool().unwrap(),
        "the skid is still dosing under the promoted owner: {promoted_row:?}"
    );
    assert!(
        promoted_row["aeration"]["staged"].as_i64().unwrap() >= 1,
        "the train's machines are still staged under the promoted owner: {promoted_row:?}"
    );

    // The durable journal preserves the record across the switchover,
    // in `seq` order, with the role transitions the promotion made.
    let active_journal = active.journal(0).unwrap();
    assert_eq!(
        &active_journal[..before_restart.len()],
        &before_restart[..],
        "the restarted active's journal still replays the original record"
    );
    assert!(
        active_journal
            .windows(2)
            .all(|pair| pair[0].seq < pair[1].seq),
        "the durable record's seq order must be strict across the restart and the switchover"
    );
    let standby_journal = standby.journal(0).unwrap();
    assert!(
        !standby_journal.is_empty(),
        "the promoted peer's journal carries the switchover's record"
    );
    let roles = role_changes(&standby_journal);
    assert!(
        roles.iter().any(|(from, to)| *from != *to),
        "the promoted peer's journal records its role transition: {roles:?}"
    );

    // -- The journal's own records ----------------------------------------
    // The alarm lifecycle the run exercised journaled in `seq` order
    // with attribution: the bank's flow-disturbance alarm asserted and
    // cleared, the station's thermal contact, and the deviation
    // detector's flag.
    let b = Value::Bool;
    let transitions = |point: PointId| point_changes(&active_journal, point);
    // Every journaled point is addressed through its train's frame, so
    // the assertion reads the merged document's ids.
    // The bank's flow-disturbance alarm latched on the disturbance and
    // cleared through its ack: activation and return, both journaled.
    assert_transitions(
        layout.bank.point(bank.flow_disturbance_alarm.alarm),
        &transitions(layout.bank.point(bank.flow_disturbance_alarm.alarm)),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout
            .bank
            .point(bank.flow_disturbance_alarm.unacknowledged),
        &transitions(
            layout
                .bank
                .point(bank.flow_disturbance_alarm.unacknowledged),
        ),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    // The station's thermal contact reported its overload and the pump's
    // managed alarm's latch cleared through the ack.
    assert_transitions(
        layout.station.point(station.pumps[0].thermal),
        &transitions(layout.station.point(station.pumps[0].thermal)),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    assert_transitions(
        layout
            .station
            .point(station.pumps[0].thermal_alarm.unacknowledged),
        &transitions(
            layout
                .station
                .point(station.pumps[0].thermal_alarm.unacknowledged),
        ),
        &[(Some(b(false)), b(true)), (Some(b(true)), b(false))],
    );
    // Beside the attributed receipts, the settle precedes the
    // journaled transition it produced.
    let settle = active_journal
        .iter()
        .find(|entry| {
            matches!(
                &entry.event,
                JournalEvent::CommandSettled { receipt, .. }
                    if receipt.command
                        == (Command::WriteValue {
                            point: layout.bank.point(bank.flow_disturbance_alarm.ack),
                            kind: ValueKind::Bool,
                            value: b(true),
                        })
            )
        })
        .expect("the bank's flow-alarm ack must be journaled");
    let JournalEvent::CommandSettled { receipt, .. } = &settle.event else {
        unreachable!("the entry is a settle")
    };
    assert_eq!(
        receipt.actor.as_deref(),
        Some(OPERATOR),
        "the settle must be actor-attributed: {settle:?}"
    );

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

    let mut digest = serde_json::json!({
        "trace": trace,
        "journal": {
            "before_restart": before_restart,
            "after_restart": masked(serde_json::to_value(&active_journal).unwrap(), &masks),
            "boundaries": file_boundaries(&journal_active),
            "standby_boundaries": file_boundaries(&journal_standby),
            "settled": settled_receipts(&active_journal),
            "active_roles": role_changes(&active_journal),
            "standby_roles": role_changes(&standby_journal),
            "standby_file": masked(serde_json::to_value(&standby_journal).unwrap(), &masks),
        },
        "restart": {
            "interrupted_at": interrupted,
            "persisted_version": persisted.format_version,
            "resumed_tick": resumed.tick,
            "held": held,
        },
        "promotion": {
            "demoted": masked(serde_json::to_value(&demoted).unwrap(), &masks),
            "promoted": masked(serde_json::to_value(&promoted).unwrap(), &masks),
            "pre_promotion_sync": pre_promotion.sync,
        },
        "issued": issued,
        // The journal sink's live counters ride the writer thread's
        // beat — pin the run-stable fields so the digests compare.
        "final": masked(
            serde_json::to_value({
                let mut image = first_owner;
                settle_sink_health(&mut image);
                image
            })
            .unwrap(),
            &masks,
        ),
    });
    canonicalize_origins(&mut digest, &mut Vec::new());

    let _ = std::fs::remove_dir_all(&dir);
    digest
}

/// Seeds the process conditions the four trains' dynamics declarations
/// read, so each train's loop starts with a state to act on. Every id
/// is the train's own, translated through that train's frame — the same
/// addressing the run and the observe row use.
fn seed(field: &RemoteDriver, layout: &LibraryPlantLayout) {
    let station = &layout.station;
    let s = |point: PointId| station.point(point);
    field
        .write(s(station.train.level_primary), Value::Float(3.5))
        .unwrap();
    field
        .write(s(station.train.level_backup), Value::Float(3.5))
        .unwrap();
    // The well's declared inflow sits between one pump's draw and two,
    // so the chain's stage count cycles and the group's rotation is
    // observable rather than the well running away to its high alarm.
    field
        .write(s(station.train.inflow), Value::Float(1.5))
        .unwrap();

    let dosing = &layout.dosing;
    let d = |point: PointId| dosing.point(point);
    field
        .write(d(dosing.train.flow_source), Value::Float(20.0))
        .unwrap();
    field
        .write(d(dosing.train.flow_proven), Value::Bool(true))
        .unwrap();
    field
        .write(d(dosing.train.tank_refill), Value::Float(10.0))
        .unwrap();

    let bank = &layout.bank;
    let b = |point: PointId| bank.point(point);
    field
        .write(b(bank.train.supply_available), Value::Bool(true))
        .unwrap();
    field
        .write(b(bank.train.waste_available), Value::Bool(true))
        .unwrap();
    field
        .write(b(bank.train.flow_disturbance), Value::Float(0.0))
        .unwrap();
    for filter in &bank.train.filters {
        field.write(b(filter.turbidity), Value::Float(0.1)).unwrap();
        field.write(b(filter.level), Value::Float(0.5)).unwrap();
        field
            .write(b(filter.drain_depth), Value::Float(0.0))
            .unwrap();
        field
            .write(b(filter.fault_contact), Value::Bool(false))
            .unwrap();
    }

    let aeration = &layout.aeration;
    let a = |point: PointId| aeration.point(point);
    field
        .write(a(aeration.train.influent_flow), Value::Float(2000.0))
        .unwrap();
    field
        .write(a(aeration.train.pressure), Value::Float(55.0))
        .unwrap();
    field
        .write(a(aeration.train.header_airflow), Value::Float(3000.0))
        .unwrap();
    for (index, zone) in aeration.train.zones.iter().enumerate() {
        let do_value = 2.0 + index as f64 * 0.5;
        for probe in &zone.probes {
            field.write(a(*probe), Value::Float(do_value)).unwrap();
        }
        field.write(a(zone.valve_pos), Value::Float(50.0)).unwrap();
        field.write(a(zone.uptake), Value::Float(1.0)).unwrap();
    }
    for blower in &aeration.train.blowers {
        field.write(a(blower.avail), Value::Bool(true)).unwrap();
    }
}

#[test]
fn the_library_plant_walks_every_named_behavior_in_one_scripted_run() {
    run_library("once");
}

#[test]
fn identical_library_plant_runs_reproduce_the_identical_digest() {
    let first = run_library("a");
    let second = run_library("b");
    if first != second {
        let dir = std::env::temp_dir().join(format!("dcs-library-digest-{}", std::process::id()));
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
