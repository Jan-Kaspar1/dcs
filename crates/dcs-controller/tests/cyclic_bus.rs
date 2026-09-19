//! End-to-end cyclic-exchange contract over the register-mapped
//! simulated fieldbus: `dcs-controller --driven` processes attach to
//! `dcs-sim-bus-device` servers through the `sim-cyclic` device kind,
//! so every `POST /scan` runs the decision-78 boundary over the real
//! wire protocol — one `Exchange` request per scan publishing the
//! staged output image and latching the register census, never a
//! per-point transport.
//!
//! The rig is `failover_bus.rs`'s carried onto the cyclic kind: the
//! same spawned-controller conventions, the same observer attachments,
//! but the field's outcomes are scripted through the register
//! protocol's `ScriptExchange` request — misses dropping the exchange
//! connection unanswered, station-attributed and unattributable short
//! censuses — so the held input image's aging, the declared
//! `exchange_miss_threshold`'s escalation, the staged output image's
//! retention across a failed exchange, the once-per-boundary
//! `failed_exchanges` accounting, and the exchange section on the
//! served snapshot's I/O-health surface are all exercised where a
//! monitoring consumer sees them. The standby leg proves the tracking
//! peer keeps latching fresh inputs through census-only exchanges
//! while its closed write gate drops every staged write before it can
//! reach the field.

use dcs_core::{
    Command, CommandError, CommandOutcome, IoDriver, IoError, PointId, Quality, QualityReason,
    Role, StandbySync, Tick, Value, ValueKind,
};
use dcs_monitor::MonitorClient;
use dcs_sim_bus::{BusDriver, BusRequest, BusResponse, ExchangeOutcome, PointRegister};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::time::Duration;

mod support;

use support::{
    Spawned, image_sample as sample, serving_device, spawn, spawn_controller, workspace_binary,
};

/// The model the rig runs — the shared tank-loop document whose
/// devices [`cyclic_model`] re-points at `sim-cyclic`.
const MODEL_SOURCE: &str = include_str!("../../dcs-plant/fixtures/tank_loop.json");

/// Process time passed to the controllers for parity with the paced
/// and `sim-tcp` rigs; the bank's step advances a stamping clock, not
/// dynamics.
const DT: &str = "0.1";
/// The declared `exchange_miss_threshold` every `sim-cyclic` device in
/// the rig runs under — three consecutive missed exchanges escalate
/// reads.
const MISS_THRESHOLD: u64 = 3;
/// The declared freshness budget the rig's level point carries — one
/// tick — so a held sample reports `Uncertain(Stale)` the second scan
/// after the last completed exchange.
const STALE_BUDGET: u64 = 1;

const LEVEL: PointId = PointId(10);
const SETPOINT: PointId = PointId(11);
const VALVE: PointId = PointId(20);
/// The model devices the rig serves: device 1 carries the input
/// channels across two stations, device 2 the valve command.
const AI_DEVICE: u64 = 1;
const AO_DEVICE: u64 = 2;
/// The channel→register map [`cyclic_stations`] declares and every
/// device server builds its bank from.
const LEVEL_REGISTER: u16 = 4;
const SETPOINT_REGISTER: u16 = 5;
const VALVE_REGISTER: u16 = 6;
/// The observer attachments' point→register maps on each bank — the
/// same mapping the controllers' `sim-cyclic` backends resolve.
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

/// A `dcs-sim-bus-device` process serving `model`'s declared `device`
/// on an ephemeral port: it announces `serving device <id> on <addr>
/// (declared <listen>)` once bound.
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

/// One `dcs-sim-bus-device` process per model device — the shared field
/// the controllers attach to. The returned map feeds the
/// controller-side model's `address` parameters.
fn spawn_devices(serving_model: &Path) -> std::collections::BTreeMap<u64, Spawned> {
    [AI_DEVICE, AO_DEVICE]
        .iter()
        .map(|&device| (device, spawn_device(serving_model, device)))
        .collect()
}

/// The station layout the re-pointed model declares for `device` —
/// every channel partitioned into named stations. Device 1 splits its
/// two input channels across `inlet` and `panel` so a station-attributed
/// short exchange degrades one point while the other stays fresh;
/// device 2's single channel is `outlet`. Both ends read this map: the
/// device servers build their banks and station maps from it and the
/// controllers' `sim-cyclic` backends resolve it.
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

/// Writes the model whose field I/O lives on the cyclic bus: the
/// tank-loop document with every device re-pointed at `sim-cyclic` —
/// parameters carrying the address `address_of` reports, the declared
/// station map, and the miss threshold — and the level point carrying
/// the rig's freshness budget.
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

/// A raw `BusDriver` attachment to a device server — the rig's window
/// on the register bank and its exchange-outcome scripter.
fn attach(addr: SocketAddr, points: &[PointRegister]) -> BusDriver {
    BusDriver::connect(addr, points).unwrap()
}

/// Appends `outcomes` to the device server at `addr`'s scripted
/// exchange queue — each consumed by the next `Exchange` request any
/// attachment sends.
fn script_exchange(driver: &BusDriver, outcomes: Vec<ExchangeOutcome>) {
    match driver.request(&BusRequest::ScriptExchange { outcomes }) {
        Ok(BusResponse::Done) => {}
        other => panic!("script-exchange was refused: {other:?}"),
    }
}

/// A `RegisterBank`-side register read through the observer attachment.
fn register(driver: &BusDriver, point: PointId) -> Value {
    driver.read(point).unwrap().value
}

/// The rig's workspace directory.
fn rig_dir(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-cyclic-bus-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// Spawns the field — both device servers — and returns them plus the
/// controller-side model carrying their bound addresses.
fn rig(dir: &Path) -> (std::collections::BTreeMap<u64, Spawned>, PathBuf) {
    let serving = cyclic_model(dir, "serving.json", |_| "127.0.0.1:0".to_string());
    let devices = spawn_devices(&serving);
    let model = cyclic_model(dir, "controller.json", |device| {
        devices[&device].addr.to_string()
    });
    (devices, model)
}

#[test]
fn the_driven_controller_steps_the_field_through_the_boundary_exchange() {
    let dir = rig_dir("driven");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let _ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);

    // The field's starting state, written before any controller
    // attaches — no claim exists to fence the observer.
    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let active_process = spawn_controller(&model, &[], DT);
    let active = MonitorClient::new(active_process.addr);

    // Two converged scans: each boundary's exchange latched the census
    // — the level point reads the field through the held image — and
    // staged the PID's valve output for the next scan's exchange.
    for _ in 0..2 {
        active.advance(1).unwrap();
    }
    assert_eq!(
        sample(&active.snapshot().unwrap(), LEVEL).value,
        Value::Float(7.0)
    );
    // The one-scan actuation delay at the command path: a `WriteValue`
    // submitted between scans is applied at the next scan's boundary,
    // staged, and published by that scan's exchange — the field sees it
    // after exactly one `POST /scan`, not before.
    active
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(42.0),
        })
        .unwrap();
    assert_eq!(
        register(&ai, SETPOINT),
        Value::Float(50.0),
        "an accepted command must not publish before the next exchange"
    );
    active.advance(1).unwrap();
    assert_eq!(register(&ai, SETPOINT), Value::Float(42.0));

    // A scripted miss: the staged output image is retained — the
    // command-staged setpoint write submitted now publishes only when
    // the next exchange completes.
    script_exchange(&ai, vec![ExchangeOutcome::Miss]);
    active
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(55.0),
        })
        .unwrap();
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 1);
    assert_eq!(snapshot.io_health.failed_reads, 0);
    assert_eq!(
        register(&ai, SETPOINT),
        Value::Float(42.0),
        "a missed exchange must not publish the staged image"
    );
    // The retained image publishes on the next completed exchange.
    active.advance(1).unwrap();
    assert_eq!(register(&ai, SETPOINT), Value::Float(55.0));

    // Three consecutive scripted misses reach the declared threshold:
    // the held image serves the last latched level through its
    // freshness budget — Good while the acquisition stamp is inside
    // it, Uncertain(Stale) past it — then every device-1 point's read
    // escalates to a named failure.
    script_exchange(&ai, vec![ExchangeOutcome::Miss; MISS_THRESHOLD as usize]);
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 2);
    assert_eq!(
        sample(&snapshot, LEVEL).quality,
        Quality::Good,
        "the held sample is inside its freshness budget one scan after the last exchange"
    );
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 3);
    assert_eq!(
        sample(&snapshot, LEVEL).quality,
        Quality::Uncertain(QualityReason::Stale),
        "the held sample ages to Stale past its budget"
    );
    assert_eq!(sample(&snapshot, LEVEL).value, Value::Float(7.0));
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 4);
    assert_eq!(
        snapshot.io_health.failed_reads, 2,
        "the threshold escalates the device's whole image — both its points"
    );
    for point in [LEVEL, SETPOINT] {
        assert_eq!(
            sample(&snapshot, point).quality,
            Quality::Bad(QualityReason::CommunicationFault),
            "point {point:?} must escalate at the miss threshold"
        );
    }

    // Recovery: the script exhausted, the next exchange reconnects and
    // latches — fresh quality returns and the counters stop.
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 4);
    assert_eq!(sample(&snapshot, LEVEL).quality, Quality::Good);
    assert_eq!(sample(&snapshot, LEVEL).value, Value::Float(7.0));

    // The exchange counters reached the served snapshot's I/O-health
    // surface: device 1's backend attempted every scan's exchange —
    // five completions, four misses — and device 2's ran only the scans
    // device 1's completed, the fan-out stopping at the first failure.
    let exchange = snapshot.io_health.driver.unwrap().exchange.unwrap();
    assert_eq!(exchange.attempted, 9 + 5);
    assert_eq!(exchange.succeeded, 5 + 5);
    assert_eq!(exchange.last_exchange_tick, Some(Tick(9)));
    assert_eq!(exchange.working_counter_mismatches, 0);
    assert_eq!(exchange.missed_deadlines, 0);
}

#[test]
fn short_exchanges_degrade_their_station_end_to_end() {
    let dir = rig_dir("short");
    let (devices, model) = rig(&dir);
    let ai = attach(devices[&AI_DEVICE].addr, AI_POINTS);
    let _ao = attach(devices[&AO_DEVICE].addr, AO_POINTS);

    ai.write(SETPOINT, Value::Float(50.0)).unwrap();
    ai.write(LEVEL, Value::Float(7.0)).unwrap();

    let active_process = spawn_controller(&model, &[], DT);
    let active = MonitorClient::new(active_process.addr);
    active.advance(1).unwrap();

    // A station-attributed short exchange: the census withholds
    // `inlet`'s registers — the exchange completes, so no failed
    // exchange counts, but the named station's point escalates while
    // the panel station's serves freshly latched data.
    script_exchange(
        &ai,
        vec![ExchangeOutcome::ShortStation {
            station: "inlet".to_string(),
        }],
    );
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 0);
    assert_eq!(snapshot.io_health.failed_reads, 1);
    assert_eq!(
        sample(&snapshot, LEVEL).quality,
        Quality::Bad(QualityReason::CommunicationFault),
        "the short station's point must escalate"
    );
    assert_eq!(
        sample(&snapshot, SETPOINT).quality,
        Quality::Good,
        "the answered station's point serves the fresh census"
    );
    assert_eq!(sample(&snapshot, SETPOINT).value, Value::Float(50.0));
    assert_eq!(
        snapshot
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap()
            .working_counter_mismatches,
        1
    );

    // A clean exchange clears the attribution: both points serve fresh.
    let snapshot = active.advance(1).unwrap();
    assert_eq!(sample(&snapshot, LEVEL).quality, Quality::Good);

    // An unattributable short exchange — registers named that no single
    // station covers — degrades the device's whole image.
    script_exchange(
        &ai,
        vec![ExchangeOutcome::ShortRegisters {
            registers: vec![LEVEL_REGISTER, SETPOINT_REGISTER],
        }],
    );
    let snapshot = active.advance(1).unwrap();
    assert_eq!(snapshot.io_health.failed_exchanges, 0);
    assert_eq!(snapshot.io_health.failed_reads, 3);
    for point in [LEVEL, SETPOINT] {
        assert_eq!(
            sample(&snapshot, point).quality,
            Quality::Bad(QualityReason::CommunicationFault),
            "point {point:?} must degrade on the unattributable shortfall"
        );
    }
    assert_eq!(
        snapshot
            .io_health
            .driver
            .unwrap()
            .exchange
            .unwrap()
            .working_counter_mismatches,
        2
    );
}

#[test]
fn the_tracking_standby_latches_inputs_while_its_writes_never_stage() {
    let dir = rig_dir("standby");
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

    // Converged ticks: the standby pulls each checkpoint and scans —
    // its exchange latching the same census the active's does — while
    // its closed write gate keeps every staged output off the field.
    // Enough ticks for the PID to saturate at its clamp: under the
    // cyclic contract the field carries the output staged one exchange
    // ago, so the standby-divergence check's staged-versus-field
    // pairing is honest only once the output is steady — at the clamp,
    // staged and field agree and the peer reports Tracking.
    for tick in 1..=15 {
        let tracked = standby.advance(1).unwrap();
        let owner = active.advance(1).unwrap();
        assert_eq!(tracked, owner, "tick {tick}");
    }
    let report = standby.role().unwrap();
    assert!(
        matches!(report.sync, Some(StandbySync::Tracking { .. })),
        "the standby never converged: {report:?}"
    );
    // The tracking peer's exchange counters count its own census-only
    // exchanges — one per backend per scan — proving the boundary runs
    // on the standby too.
    let exchange = standby
        .snapshot()
        .unwrap()
        .io_health
        .driver
        .unwrap()
        .exchange
        .unwrap();
    assert_eq!(exchange.attempted, 15 + 15);
    assert_eq!(exchange.succeeded, 15 + 15);
    // A command submitted to the standby is refused at admission — only
    // a settled active takes commands.
    let receipt = standby
        .command(&Command::WriteValue {
            point: SETPOINT,
            kind: ValueKind::Float,
            value: Value::Float(99.0),
        })
        .unwrap();
    assert!(
        matches!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::NotActive { .. }
            }
        ),
        "a standby must refuse commands: {receipt:?}"
    );
    assert_eq!(register(&ai, SETPOINT), Value::Float(50.0));

    // The active's loss: the standby keeps scanning — its checkpoint
    // pulls fail, but its census-only exchanges still latch the live
    // field — and the valve register holds the active's last published
    // value because the standby's staged writes never reach the wire.
    let frozen = register(&ao, VALVE);
    active_process.child.kill().unwrap();
    active_process.child.wait().unwrap();
    // The field moves under the orphaned pair — the dead owner's claim
    // released with its connection, the observer's write is unfenced
    // once the release lands: the standby's next exchange must latch
    // it, and its run's computed valve must move off the frozen
    // register without the field following.
    let mut released = false;
    for _ in 0..100 {
        match ai.write(LEVEL, Value::Float(19.0)) {
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
    assert!(
        released,
        "the dead owner's claim must release with its connection"
    );
    for _ in 0..4 {
        standby.advance(1).unwrap();
    }
    let report = standby.role().unwrap();
    assert_eq!(report.role, Role::Standby);
    assert!(
        matches!(report.sync, Some(StandbySync::Degraded { .. })),
        "the lost peer must degrade the standby's sync: {report:?}"
    );
    // Fresh inputs still latch through the standby's exchanges — the
    // census it returns is the live field's.
    let snapshot = standby.snapshot().unwrap();
    assert_eq!(sample(&snapshot, LEVEL).value, Value::Float(19.0));
    assert_eq!(sample(&snapshot, LEVEL).quality, Quality::Good);
    // …while the field never saw a standby write: with the dead active
    // gone the standby is the only attachment left, yet the register
    // holds exactly what the active last published — every staged
    // output the standby's own scans computed dropped at the gate, its
    // computed valve having moved off the frozen value.
    assert_eq!(register(&ao, VALVE), frozen);
    let staged_value = sample(&snapshot, VALVE).value;
    assert_ne!(
        staged_value, frozen,
        "the standby's computed output moved on — the gate dropped every staged write"
    );
    let exchange = snapshot.io_health.driver.unwrap().exchange.unwrap();
    assert_eq!(exchange.attempted, 19 + 19);
    assert_eq!(exchange.succeeded, 19 + 19);
}
