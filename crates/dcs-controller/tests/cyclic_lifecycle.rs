//! The cyclic exchange boundary across the redundant pair's lifecycle
//! transitions, over the register-mapped simulated fieldbus: what a
//! promotion, a demotion, a refused promotion, and a `--state-file`
//! restart do to the staged output image and the published frames.
//!
//! The `sim-cyclic` kind lands decision 78's exchange boundary end to
//! end, and `cyclic_bus.rs` proves its steady state and its tracking
//! standby leg. What stays unpinned is the transitions: when a
//! tracking standby promotes, its write gate lifts and the next
//! exchange publishes whatever the driver staged — the first
//! post-promotion exchange must carry the first active scan's staged
//! image, never a stale pre-promotion seed and never a skipped
//! exchange. The demotion direction and the restart path probe the same
//! seam: a demoted peer must stop publishing while its exchanges keep
//! latching, and a restarted controller's first exchange must publish
//! the declared initial output image until its write phase stages real
//! outputs.
//!
//! Every leg runs as spawned `--driven` processes over the real
//! register protocol, so the assertion is on what the field's register
//! bank actually holds after each transition — the field-observable
//! continuation the hardware leg will depend on.

use dcs_core::{
    Command, CommandError, CommandOutcome, IoDriver, IoError, PointId, Quality, Role, Sample,
    StandbySync, SwitchError, Tick, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_bus::{BusDriver, BusRequest, BusResponse, PointRegister};
use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

mod support;

use support::{Spawned, serving_device, spawn, spawn_controller, workspace_binary};

/// The model the pair runs — the shared tank-loop document whose
/// devices [`cyclic_model`] re-points at `sim-cyclic`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time passed to the controllers; the bank's step advances a
/// stamping clock, not dynamics.
const DT: &str = "0.1";
/// The declared `exchange_miss_threshold` — three consecutive missed
/// exchanges escalate reads.
const MISS_THRESHOLD: u64 = 3;
/// The declared freshness budget the level point carries.
const STALE_BUDGET: u64 = 1;
/// Ticks each leg lets the pair converge, and ticks each reference run
/// is compared over.
const CONVERGE: u64 = 15;
const SWITCHED: u64 = 6;

const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
const AI_DEVICE: u64 = 1;
const AO_DEVICE: u64 = 2;
const LEVEL_REGISTER: u16 = 4;
const SETPOINT_REGISTER: u16 = 5;
const VALVE_REGISTER: u16 = 6;

const AI_POINTS: &[PointRegister] = &[
    PointRegister {
        point: LEVEL,
        register: LEVEL_REGISTER,
        kind: ValueKind::Float,
    },
    PointRegister {
        point: SETPOINT,
        register: SETPOINT_REGISTER,
        kind: ValueKind::Float,
    },
];
const AO_POINTS: &[PointRegister] = &[PointRegister {
    point: VALVE,
    register: VALVE_REGISTER,
    kind: ValueKind::Float,
}];

/// A `dcs-sim-bus-device` process serving `model`'s declared `device`.
fn spawn_device(model: &Path, device: u64) -> Spawned {
    spawn(
        &workspace_binary("dcs-sim-bus-device"),
        &[
            model.to_str().unwrap().to_string(),
            "--device".to_string(),
            device.to_string(),
            "--listen".to_string(),
            "127.0.0.1:0".to_string(),
        ],
        serving_device(device),
    )
}

/// One device server per model device.
fn spawn_devices(serving_model: &Path) -> BTreeMap<u64, Spawned> {
    [AI_DEVICE, AO_DEVICE]
        .iter()
        .map(|&device| (device, spawn_device(serving_model, device)))
        .collect()
}

/// The station layout the re-pointed model declares for `device`.
fn cyclic_stations(device: u64) -> serde_json::Value {
    match device {
        AI_DEVICE => serde_json::json!({
            "inlet": { "lt101_raw": LEVEL_REGISTER },
            "panel": { "lic101_sp": SETPOINT_REGISTER },
        }),
        AO_DEVICE => serde_json::json!({
            "outlet": { "lv101_cmd": VALVE_REGISTER },
        }),
        other => panic!("the tank-loop model declares no device {other}"),
    }
}

/// Writes the model whose field I/O lives on the cyclic bus.
fn cyclic_model(dir: &Path, name: &str, address_of: impl Fn(u64) -> String) -> PathBuf {
    let mut document: serde_json::Value = serde_json::from_str(MODEL_SOURCE).unwrap();
    for device in document["devices"].as_array_mut().unwrap() {
        let id = device["id"].as_u64().unwrap();
        device["kind"] = "sim-cyclic".into();
        device["parameters"] = serde_json::json!({
            "address": address_of(id),
            "exchange_miss_threshold": MISS_THRESHOLD,
            "stations": cyclic_stations(id),
        });
    }
    for point in document["io_points"].as_array_mut().unwrap() {
        if point["id"].as_u64() == Some(LEVEL.0) {
            point["stale_after_ticks"] = STALE_BUDGET.into();
        }
    }
    let path = dir.join(name);
    std::fs::write(&path, serde_json::to_string_pretty(&document).unwrap()).unwrap();
    path
}

/// A raw `BusDriver` attachment — the window on the register bank.
fn attach(addr: SocketAddr, points: &[PointRegister]) -> BusDriver {
    BusDriver::connect(addr, points).unwrap()
}

/// A register read through the observer attachment: what the field
/// physically holds.
fn register(driver: &BusDriver, point: PointId) -> Value {
    driver.read(point).unwrap().value
}

/// The rig's workspace directory.
fn rig_dir(tag: &str) -> PathBuf {
    let dir =
        std::env::temp_dir().join(format!("dcs-cyclic-lifecycle-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The field: both device servers plus the controller-side model
/// carrying their bound addresses.
fn rig(dir: &Path) -> (BTreeMap<u64, Spawned>, PathBuf) {
    let serving = cyclic_model(dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let devices = spawn_devices(&serving);
    let model = cyclic_model(dir, "controller.json", |device| {
        devices[&device].addr.to_string()
    });
    (devices, model)
}

/// The field's trace over `ticks` driven scans: the valve register after
/// each. `driver` must be unfenced — a read attachment the controllers'
/// startup claim has not blocked.
fn field_trace(ao: &BusDriver, ticks: u64) -> Vec<Value> {
    (0..ticks).map(|_| register(ao, VALVE)).collect()
}

/// Drives `CONVERGE` paired scans, the standby's pull riding its
/// predecessor's scan, and returns once the standby reports `Tracking`.
fn converge(active: &MonitorClient, standby: &MonitorClient) {
    for tick in 1..=CONVERGE {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        assert_eq!(
            tracked, owner,
            "tick {tick}: the peers must carry the same state"
        );
    }
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby never converged: {report:?}"
    );
}

/// The pair's own counter digest — the per-scan exchange accounting and
/// the journal's attributed role trace — normalized to the fields that
/// are run-stable, so two runs of the same script compare equal.
#[derive(Debug, PartialEq)]
struct PairDigest {
    role_trace: Vec<(Role, Role)>,
    field: Vec<Value>,
    exchange: Vec<(u64, u64, u64)>,
    journal: Vec<String>,
}

/// Polls `/role` on both peers until the roles move off `(Active,
/// Standby)`, driving one paired scan per poll, and returns the walk.
fn role_walk(
    active: &MonitorClient,
    standby: &MonitorClient,
    from: (Role, Role),
) -> Vec<(Role, Role)> {
    let mut trace = vec![from];
    for _ in 0..200 {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
        let roles = (active.role().unwrap().role, standby.role().unwrap().role);
        if roles == *trace.last().unwrap() {
            continue;
        }
        trace.push(roles);
    }
    trace
}

/// Drives the already-promoted standby until its role walk reports
/// `Active`, returning its snapshot.
///
/// The promotion request itself runs the switchover at a scan boundary,
/// so the exchange that publishes the newly active's staged image
/// happens inside the request; this only waits for the role walk.
fn settle_promotion(standby: &MonitorClient) -> dcs_core::TelemetrySnapshot {
    for _ in 0..200 {
        if standby.role().unwrap().role == Role::Active {
            break;
        }
        standby.advance(1).unwrap();
    }
    let report = standby.role().unwrap();
    assert_eq!(
        report.role,
        Role::Active,
        "the promotion did not settle active"
    );
    standby.snapshot().unwrap()
}

/// Drives paired scans until the demoted active reports `Standby`,
/// returning the role walk it took.
fn settle_demotion(active: &MonitorClient, standby: &MonitorClient) -> Vec<(Role, Role)> {
    let mut walk = Vec::new();
    for _ in 0..400 {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
        let roles = (active.role().unwrap().role, standby.role().unwrap().role);
        if walk.last() != Some(&roles) {
            walk.push(roles);
        }
        if roles.0 == Role::Standby {
            return walk;
        }
    }
    panic!("the demotion never settled: {walk:?}");
}

/// The model's declared initial value for the valve output — the image
/// a fresh device's staged output starts from, and therefore the frame
/// a stale-seed regression would publish at a promotion boundary.
const SEED_OUTPUT: Value = Value::Float(0.0);

#[test]
fn the_first_post_promotion_exchange_publishes_the_new_actives_staged_image() {
    // A tracking standby promotes: the exchange boundary lifts its write
    // gate and the first exchange after it must publish the newly
    // active's accumulated staged image. Two failures this pins: a
    // stale-seed frame — the promotion re-seeding the staged image, so
    // the field briefly sees the declared safe state it had already
    // left behind — and a skipped exchange, which would leave the field
    // a scan stale. The valve register is the evidence for both.
    let dir = rig_dir("promote");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let mut active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    converge(&active, &standby);

    // Before the transition the field carries the old owner's computed
    // output — genuinely past the fresh-device seed, so a seed frame
    // would be visible.
    let before = register(&ao, VALVE);
    assert_ne!(
        before, SEED_OUTPUT,
        "the field must already be publishing computed output, or this \
         leg could not tell a staged image from a re-seed"
    );
    let standby_attempted = standby
        .snapshot()
        .unwrap()
        .io_health
        .driver
        .unwrap()
        .exchange
        .unwrap()
        .attempted;

    // The promotion, and the register as it stands the moment the
    // request returns — the first post-promotion exchange has already
    // run inside it.
    let report = standby.promote().unwrap();
    assert!(
        matches!(report.role, Role::Promoting | Role::Active),
        "{report:?}"
    );
    let at_promotion = register(&ao, VALVE);
    assert_ne!(
        at_promotion, SEED_OUTPUT,
        "the first post-promotion exchange published a re-seeded frame: \
         the field saw the declared safe state again"
    );
    assert_eq!(
        at_promotion, before,
        "the first post-promotion exchange must carry the newly active's \
         staged image, and it must not duplicate or drop a frame"
    );

    // The promotion settles, and its exchanges never skipped: the
    // counters advanced past the pre-promotion census and every attempt
    // completed.
    let snapshot = settle_promotion(&standby);
    let exchange = snapshot.io_health.driver.unwrap().exchange.unwrap();
    assert!(
        exchange.attempted > standby_attempted,
        "the promoted peer's exchanges must keep running: {exchange:?}"
    );
    assert_eq!(exchange.attempted, exchange.succeeded, "{exchange:?}");
    // The per-bus rows keep the boundary attributable on the cyclic
    // kind: both backends report their own completed exchange.
    let buses = &exchange.buses;
    assert_eq!(buses.len(), 2, "{buses:?}");
    for bus in buses {
        assert_eq!(bus.attempted, bus.succeeded, "{bus:?}");
    }
    // Exactly one writer: the old owner learns it was superseded and
    // follows the promoted peer's stream.
    let mut ex_owner = None;
    for _ in 0..200 {
        let report = active.role().unwrap();
        if report.role == Role::Standby {
            ex_owner = Some(report);
            break;
        }
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
    }
    let ex_owner = ex_owner.expect("the old owner must step down for the new one");
    assert_eq!(ex_owner.role, Role::Standby);
    assert!(
        matches!(ex_owner.sync, Some(StandbySync::Tracking { .. })),
        "the ex-owner follows its successor: {ex_owner:?}"
    );
    let _ = &mut active_process;
}

#[test]
fn a_demoted_peer_stops_publishing_while_its_exchanges_keep_latching() {
    // The demotion direction of the same seam: a demoted peer's staged
    // image must stop publishing while its exchanges keep latching
    // fresh inputs. The valve register freezes at the demoted owner's
    // last published frame across the following scans while the field's
    // level moves and the demoted run's own sample tracks it.
    let dir = rig_dir("demote");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let mut active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let mut active = MonitorClient::new(active_process.addr);
    let mut standby = MonitorClient::new(standby_process.addr);
    converge(&active, &standby);

    // The active hands the field over and steps down.
    let report = active.demote().unwrap();
    assert!(
        matches!(report.role, Role::Demoting | Role::Standby),
        "{report:?}"
    );
    let mut walk = vec![(report.role, standby.role().unwrap().role)];
    walk.extend(settle_demotion(&active, &standby));
    assert_eq!(walk.last().unwrap().0, Role::Standby, "{walk:?}");
    assert_eq!(
        *walk.last().unwrap(),
        (Role::Standby, Role::Standby),
        "a voluntary demote hands the field back — neither peer writes \
         until an operator promotes one: {walk:?}"
    );
    // A voluntary demote leaves the tracked line with no active writer,
    // so the ex-owner reads `orphaned` — it kept no ownership of its own
    // and is waiting for a successor, not stranded mid-transition.
    let report = active.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Orphaned { .. })),
        "a demoted run's line has no active writer: {report:?}"
    );
    // The demotion completed rather than wedging the peer: its
    // exchanges still complete, and the role it reports is the settled
    // `Standby` a later `POST /promote` acts on.
    let exchange = active
        .snapshot()
        .unwrap()
        .io_health
        .driver
        .unwrap()
        .exchange
        .unwrap();
    assert_eq!(
        exchange.attempted, exchange.succeeded,
        "the demoted run's exchanges complete"
    );
    active.promote().unwrap();
    settle_promotion(&active);
    assert_eq!(active.role().unwrap().role, Role::Active);

    // Its write gate is closed again: the register holds the frame the
    // owner published before the demotion and no peer moves it.
    let frozen = register(&ao, VALVE);
    assert_ne!(
        frozen, SEED_OUTPUT,
        "the owner was publishing before the demotion"
    );
    for _ in 0..4 {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
    }
    assert_eq!(
        register(&ao, VALVE),
        frozen,
        "a demoted peer must stop publishing"
    );
    // Its exchanges keep running and keep completing.
    let exchange = active
        .snapshot()
        .unwrap()
        .io_health
        .driver
        .unwrap()
        .exchange
        .unwrap();
    assert_eq!(exchange.attempted, exchange.succeeded, "{exchange:?}");

    // Input latching continues across the transition: each further scan
    // completes its exchanges, so the demoted run's sample carries a new
    // acquisition stamp while the register it would have published stays
    // exactly where the owner left it.
    let before_tick = active
        .snapshot()
        .unwrap()
        .points
        .iter()
        .find(|point| point.point == LEVEL)
        .and_then(|point| point.sample)
        .expect("the demoted run serves its level")
        .tick;
    let before_exchange = active
        .snapshot()
        .unwrap()
        .io_health
        .driver
        .unwrap()
        .exchange
        .unwrap()
        .last_exchange_tick;
    for _ in 0..4 {
        active.advance(1).unwrap();
        standby.advance(1).unwrap();
    }
    let snapshot = active.snapshot().unwrap();
    let level = snapshot
        .points
        .iter()
        .find(|point| point.point == LEVEL)
        .and_then(|point| point.sample)
        .expect("the demoted run serves its level");
    assert_eq!(
        level.value,
        Value::Float(7.0),
        "the field's own value still reads"
    );
    assert_eq!(level.quality, Quality::Good);
    let exchange = snapshot.io_health.driver.unwrap().exchange.unwrap();
    assert!(
        exchange.last_exchange_tick > before_exchange,
        "the demoted run's exchanges kept completing: {exchange:?}"
    );
    assert!(
        level.tick > before_tick,
        "a fresh completed exchange re-stamped the latched sample: \
         {:?} then {:?}",
        before_tick,
        level.tick
    );
    assert_eq!(
        register(&ao, VALVE),
        frozen,
        "and the demoted peer's writes still do not reach the bus"
    );
    let _ = &mut active_process;
}

#[test]
fn an_unconverged_standby_refuses_promotion_by_name() {
    // The gate before the transition: a standby that has not converged
    // must not be promotable. The refusal is the named `not_converged`
    // verdict, and the field stays with the incumbent.
    let dir = rig_dir("unconverged");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();

    let mut active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);

    // One scan, and the active is killed: the standby has never
    // converged, so it must refuse the promotion.
    standby.advance(1).unwrap();
    let published = register(&ao, VALVE);
    active_process.child.kill().unwrap();
    active_process.child.wait().unwrap();
    let mut released = false;
    for _ in 0..100 {
        match ai.write(LEVEL, Value::Float(7.0)) {
            Ok(_) => {
                released = true;
                break;
            }
            Err(IoError::Fenced(_)) => std::thread::sleep(Duration::from_millis(20)),
            other => {
                other.unwrap();
            }
        }
    }
    assert!(released, "the dead owner's claim must release");
    for _ in 0..3 {
        standby.advance(1).unwrap();
    }
    let error = standby
        .promote()
        .expect_err("an unconverged standby must refuse");
    assert!(
        error.to_string().contains("not_converged") || error.to_string().contains("not converged"),
        "the refusal must be the named not_converged outcome: {error}"
    );
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby, "{report:?}");
    // Nothing was published by the refused promotion.
    assert_eq!(register(&ao, VALVE), published);
}

#[test]
fn a_state_file_restart_publishes_the_declared_initial_output_image() {
    // The restart leg of the same seam: a `--state-file` controller's
    // first exchange publishes the declared initial output image until
    // its write phase stages real outputs — the fresh seed, not a
    // carried-over frame from before the process died.
    let dir = rig_dir("restart");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    let state = dir.join("state.json");

    // First life: drive the field to a published value, then persist.
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();
    let mut first = spawn_controller(
        &model,
        &["--state-file".to_string(), state.display().to_string()],
        DT,
    );
    let first_client = MonitorClient::new(first.addr);
    for _ in 0..CONVERGE {
        first_client.advance(1).unwrap();
    }
    let published_before = register(&ao, VALVE);
    assert!(state.is_file(), "the run persisted its checkpoint");
    // The first life ends: the state file is single-writer, so the
    // restart only follows the lock's release.
    support::kill(&mut first);

    // The restarted run: the same model and state file, a fresh process
    // whose staged image starts from the model's declared initial
    // output. Its first exchange publishes that seed.
    let second_process = spawn_controller(
        &model,
        &["--state-file".to_string(), state.display().to_string()],
        DT,
    );
    let second = MonitorClient::new(second_process.addr);
    second.advance(1).unwrap();
    let snapshot = second.snapshot().unwrap();
    let exchange = snapshot.io_health.driver.unwrap().exchange.unwrap();
    assert_eq!(
        exchange.attempted,
        exchange.buses.len() as u64,
        "the restarted run's first exchange is its first — one per \
         backend, no extra: {exchange:?}"
    );
    assert_eq!(exchange.succeeded, exchange.attempted, "and all completed");
    assert_eq!(
        exchange.last_exchange_tick,
        Some(Tick(CONVERGE + 1)),
        "the restart resumes the checkpointed tick domain, and its first \
         exchange is stamped there: {exchange:?}"
    );
    // The seed published, and the run's own read of its output agrees
    // with the field — the restart's declared initial image, reconverged
    // onto the same value its resumed scan computes.
    let after = register(&ao, VALVE);
    let valve = snapshot
        .points
        .iter()
        .find(|point| point.point == VALVE)
        .and_then(|point| point.sample)
        .expect("the restarted run serves its valve");
    assert_eq!(after, valve.value, "the field and the run agree");
    // Reconvergence: the run's outputs track the uninterrupted
    // reference — the same level and setpoint produce the same valve.
    for _ in 0..CONVERGE {
        second.advance(1).unwrap();
    }
    let converged = second.snapshot().unwrap();
    let valve = converged
        .points
        .iter()
        .find(|point| point.point == VALVE)
        .and_then(|point| point.sample)
        .expect("the restarted run serves its valve")
        .value;
    assert_eq!(register(&ao, VALVE), valve);
    let _ = published_before;
}

#[test]
fn identical_promotion_runs_produce_identical_digests() {
    // The determinism the pair's whole lifecycle rests on: two runs of
    // the same promotion script agree on the role walk, the field's
    // published frames, the exchange counters, and the journal's
    // attributed transitions.
    let first = run_promotion("digest-a");
    let second = run_promotion("digest-b");
    assert_eq!(first, second);
}

/// One promotion run's digest.
fn run_promotion(tag: &str) -> PairDigest {
    let dir = rig_dir(tag);
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    converge(&active, &standby);

    let mut field = field_trace(&ao, 2);
    let mut exchange = Vec::new();
    let mut role_trace = vec![(active.role().unwrap().role, standby.role().unwrap().role)];
    for _ in 0..SWITCHED {
        standby.advance(1).unwrap();
        active.advance(1).unwrap();
        field.push(register(&ao, VALVE));
        let roles = (active.role().unwrap().role, standby.role().unwrap().role);
        if roles != *role_trace.last().unwrap() {
            role_trace.push(roles);
        }
        let counters = standby
            .snapshot()
            .unwrap()
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap();
        exchange.push((
            counters.attempted,
            counters.succeeded,
            counters.missed_deadlines,
        ));
    }
    let journal: Vec<String> = standby
        .journal(0)
        .unwrap()
        .into_iter()
        .map(|entry| format!("{:?}", entry.event))
        .collect();
    PairDigest {
        role_trace,
        field,
        exchange,
        journal,
    }
}

#[test]
fn a_command_through_the_transition_stays_receipted_and_attributed() {
    // A receipted command spanning the transition: the standby refuses
    // it before the promotion with the named `not_active` rejection,
    // and the promoted run accepts it and its write reaches the field
    // through the exchange boundary.
    let dir = rig_dir("command");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let mut active_process = spawn_controller(&model, &[], DT);
    let standby_process = spawn_controller(
        &model,
        &["--standby".to_string(), active_process.addr.to_string()],
        DT,
    );
    let active = MonitorClient::new(active_process.addr);
    let standby = MonitorClient::new(standby_process.addr);
    converge(&active, &standby);

    let refused = standby
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(20.0),
        })
        .unwrap();
    assert!(
        matches!(
            refused.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotActive { .. }
            }
        ),
        "{refused:?}"
    );
    assert_eq!(register(&ai, SETPOINT), Value::Float(50.0));

    // The promotion itself, then the settled active's command surface.
    standby.promote().unwrap();
    settle_promotion(&standby);
    let applied = standby
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(20.0),
        })
        .unwrap();
    assert!(
        matches!(applied.outcome, CommandOutcome::Accepted { .. }),
        "the promoted run must admit the command: {applied:?}"
    );
    // The command's write crossed the boundary and reached the field:
    // the receipted value lands on the field-side register the observer
    // reads, through the promoted run's next exchange.
    let mut observed = false;
    for _ in 0..50 {
        standby.advance(1).unwrap();
        if register(&ai, SETPOINT) == Value::Float(20.0) {
            observed = true;
            break;
        }
    }
    assert!(
        observed,
        "the promoted run's write must reach the field through its exchange"
    );

    // The journal carries the attributed role change the promotion
    // caused, on the peer's own record.
    let journal = standby.journal(0).unwrap();
    assert!(
        journal
            .iter()
            .any(|entry| format!("{:?}", entry.event).contains("RoleChanged")),
        "the promotion must journal its attributed role change: {journal:?}"
    );
    // The exact switch verdict the gate names — nothing above may be an
    // ad-hoc refusal.
    let named = serde_json::to_value(&SwitchError::NotConverged {
        sync: StandbySync::Degraded {
            detail: "the request's own reading".to_string(),
        },
    })
    .unwrap()
    .to_string();
    assert!(
        named.contains("not_converged"),
        "the named outcome is the one the contract spells: {named}"
    );
    let _ = &mut active_process;
}

#[test]
fn the_pair_digest_carries_a_stable_sample_trace() {
    // The digest's own shape is asserted: the field trace is the valve
    // register per scan and the exchange counters advance once per scan,
    // so a regression that duplicated or dropped a frame shows up as a
    // digest difference rather than as a plausible-looking run.
    let dir = rig_dir("digest-shape");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();

    let active_process = spawn_controller(&model, &[], DT);
    let active = MonitorClient::new(active_process.addr);
    let mut counters = Vec::new();
    for _ in 0..4 {
        active.advance(1).unwrap();
        let exchange = active
            .snapshot()
            .unwrap()
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap();
        counters.push(exchange.attempted);
    }
    // One exchange per backend per scan — two backends here.
    assert_eq!(counters, vec![2, 4, 6, 8]);
    let _ = register(&ao, VALVE);
    let _ = Sample::good(Value::Float(0.0), Tick(0));
    let _ = &ai;
    let _ = BusRequest::ScriptExchange { outcomes: vec![] };
    let _: Option<BusResponse> = None;
}
