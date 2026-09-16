//! The Wago EtherCAT rig's scripted commissioning path — issue #335's
//! WW-FND-002 hardware-independence proof for the HQ-4 milestone.
//!
//! The checked-in rig documents live at `crates/dcs-demo/fixtures/`:
//! `wago_rig.json` declares the `ethercat` device (logical bus
//! `ecat0`, the manifest's expected 750-354 identity, the 750-501
//! DO1/DO2 and 750-400 DI1/DI2 channel mapping, miss threshold, safe
//! outputs, and the `fail` startup policy); `wago_rig_sim.json`
//! declares the identical control path over a local `sim` device plus
//! the `di1` ← `do1` field wire the simulation plays; and
//! `wago_rig_cyclic.json` declares the same control path over a
//! `sim-cyclic` device — the coupler station as the register bank the
//! manifest's channel layout maps onto, the miss threshold carried as
//! device-parameter data — the exchange-per-scan contract the
//! `ethercat` driver implements, rehearsed in software.
//!
//! The scripted run is the commissioning sequence's software half —
//! `docs/wago-ethercat-rig-manifest.md`'s loopback plan, step 1 and 3:
//!
//! 1. a receipted `WriteValue` to the `do1-command` point through the
//!    monitor's operator path;
//! 2. the `digital-output` component staging `do1` on the next scan;
//! 3. the cyclic exchange publishing the staged image;
//! 4. `di1` following `do1` in telemetry one scan later;
//! 5. `di2` staying clear throughout — the channel-independence
//!    witness;
//! 6. the release transition repeating the sequence downward.
//!
//! All three bindings run the identical monitor-backed script — the
//! sim document through the standard registry, the ethercat document
//! through `with_ethercat_buses` over a fake transport playing the
//! rig's wiring, and the cyclic document over a `BusServer` sharing
//! the coupler station's register bank whose declared `do1` → `di1`
//! field wire plays the same physical loopback — and must produce the
//! identical receipted-command → staged-output → exchange → telemetry
//! sequence. The recorded published output images are the ethercat
//! binding's own evidence of step 3; on the cyclic binding the wire
//! lands each published `do1` on `di1`'s register inside the same
//! exchange, so the answered census itself latches the transition —
//! the exchange-per-scan semantics the hardware driver will ride.
//! The missed-exchange leg then proves the contract's failure half on
//! the same rig: a scripted missed exchange holds the held input
//! image rather than aborting the scan, escalates the device's reads
//! at the declared `exchange_miss_threshold`, and retains the staged
//! output image until the next completed exchange publishes it.

use dcs_assembly::{AssemblyError, DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::wago::{
    COUPLER_STATION, CYCLIC_ADDRESS_PLACEHOLDER, ECAT_BUS, EXCHANGE_MISS_THRESHOLD, WAGO_PRODUCT,
    WAGO_REVISION, WAGO_VENDOR, points, registers,
};
use dcs_core::{
    Command, CommandOutcome, CommandReceipt, PointId, Quality, QualityReason, Sample,
    TelemetrySnapshot, Tick, Value, ValueKind,
};
use dcs_ethercat::{BusTransport, CycleOutcome, DiscoveredStation, EthercatBuses, TransportError};
use dcs_model::PlantModel;
use dcs_monitor::{Monitor, MonitorClient};
use dcs_sim_bus::{
    BusDriver, BusRequest, BusResponse, BusServer, ExchangeOutcome, PointRegister, RegisterBank,
    RegisterDecl,
};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::thread;

/// The checked-in `ethercat`-bound document.
const ETHERCAT_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/wago_rig.json"
);
/// The checked-in `sim`-bound document.
const SIM_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/wago_rig_sim.json"
);
/// The checked-in `sim-cyclic`-bound document.
const CYCLIC_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/wago_rig_cyclic.json"
);

/// The scripted run length — baseline, the assert and release
/// transitions, and settling.
const SCANS: u64 = 12;
/// The plant step each scan period covers, in seconds.
const DT: f64 = 1.0;
/// The actor every operator command carries — the attributed,
/// receipted path.
const OPERATOR: &str = "commissioning";

/// The manifest's interface binding: logical bus `ecat0` resolves to
/// the rig's field NIC through deployment configuration — never the
/// model document.
const FIELD_INTERFACE: &str = "enx00e04c751f7c";

/// The checked-in document, loaded through the validating reader.
fn fixture(path: &str) -> PlantModel {
    PlantModel::load(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// One scan's observable record — the commissioning sequence's
/// telemetry surface.
#[derive(Debug, PartialEq)]
struct Scan {
    /// The command point's held value — `true` once the receipted
    /// write has applied.
    command: bool,
    /// The staged DO1 field output.
    do1: bool,
    /// The DO2 telemetry sample — `None` throughout: the composition
    /// never stages a DO2 write, so the point carries no recorded
    /// drive (its channel holds the declared safe state, which the
    /// ethercat binding's published-image log proves).
    do2: Option<bool>,
    /// The latched DI1/DI2 field inputs — DI1 is the loopback witness,
    /// DI2 the independence witness that must stay clear.
    di1: bool,
    di2: bool,
    /// The conditioned copies the `digital-input` components emit.
    di1_witness: bool,
    di2_witness: bool,
}

impl Scan {
    /// A scan record with `di2`, `do2`, and `di2_witness` clear — the
    /// independence witness never moves in this script.
    fn new(command: bool, do1: bool, di1: bool) -> Self {
        Self {
            command,
            do1,
            do2: None,
            di1,
            di2: false,
            // The conditioned copy lands the same scan its input
            // transitions: the component reads and writes at one tick.
            di1_witness: di1,
            di2_witness: false,
        }
    }
}

/// What a scripted run produced: the per-scan record, the command
/// receipts, and the final serialized snapshot.
struct Run {
    scans: Vec<Scan>,
    receipts: Vec<CommandReceipt>,
    snapshot: String,
}

fn bool_(sample: Sample) -> bool {
    match sample.value {
        Value::Bool(value) => value,
        other => panic!("expected a Bool sample, got {other:?}"),
    }
}

fn telemetry(snapshot: &TelemetrySnapshot, point: PointId) -> &dcs_core::PointTelemetry {
    snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("no telemetry for {point:?}"))
}

fn monitor_sample(snapshot: &TelemetrySnapshot, point: PointId) -> Sample {
    telemetry(snapshot, point)
        .sample
        .unwrap_or_else(|| panic!("no sample for {point:?}"))
}

/// Writes `value` to `point` through the attributed, receipted
/// operator command path the monitor exposes.
fn operator_write(client: &MonitorClient, point: PointId, value: bool) {
    let receipt = client
        .command_as(
            &Command::WriteValue {
                point,
                kind: ValueKind::Bool,
                value: Value::Bool(value),
            },
            Some(OPERATOR),
        )
        .unwrap();
    assert!(
        matches!(receipt.outcome, CommandOutcome::Accepted { .. }),
        "write to {point:?} rejected: {receipt:?}"
    );
}

/// The fake transport playing the rig's field wiring for the ethercat
/// binding — the hardware-independent half of the proof: one
/// discovered station at the manifest's position 0 reporting the
/// declared 750-354 identity with a one-byte process image each
/// direction. `exchange` copies output-image bit 0 (DO1) onto
/// input-image bit 0 (DI1) — the wire supervised commissioning
/// verifies — and leaves bit 1 (DI2) clear; every published output
/// image is recorded as the staged-output evidence.
struct WireTransport {
    discovered: Vec<DiscoveredStation>,
    /// Every staged image the cyclic exchange published, in order.
    published: Arc<Mutex<Vec<Vec<u8>>>>,
    op_entered: bool,
}

impl BusTransport for WireTransport {
    fn discovered(&self) -> &[DiscoveredStation] {
        &self.discovered
    }

    fn enter_op(&mut self) -> Result<(), TransportError> {
        self.op_entered = true;
        Ok(())
    }

    fn exchange(
        &mut self,
        outputs: &[u8],
        inputs: &mut [u8],
    ) -> Result<CycleOutcome, TransportError> {
        self.published.lock().unwrap().push(outputs.to_vec());
        // The wired loopback: DO1 (output bit 0) lands on DI1 (input
        // bit 0); DI2 (input bit 1) stays clear — the rig's physical
        // wiring as the commissioning plan describes it.
        inputs[0] = outputs[0] & 0b01;
        Ok(CycleOutcome::Complete)
    }

    fn recover(&mut self) -> Result<(), TransportError> {
        Ok(())
    }
}

/// The shared rig-station declaration a fake transport discovers.
fn discovered_station() -> DiscoveredStation {
    DiscoveredStation {
        position: 0,
        name: "750-354".to_string(),
        vendor_id: WAGO_VENDOR,
        product_id: WAGO_PRODUCT,
        revision: WAGO_REVISION,
        input_bytes: 1,
        output_bytes: 1,
    }
}

/// Builds the `sim` binding's driver side through the standard
/// registry — the local simulated backend serving the declared
/// channels and the declared loopback wire.
fn sim_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// Builds the `ethercat` binding's driver side: the deployment's
/// `ecat0` → field-NIC binding over a fake transport playing the wire.
fn ethercat_driver(model: &PlantModel, published: Arc<Mutex<Vec<Vec<u8>>>>) -> FanoutDriver {
    let buses = EthercatBuses::with_opener(
        BTreeMap::from([(ECAT_BUS.to_string(), FIELD_INTERFACE.to_string())]),
        Arc::new(move |request| {
            assert_eq!(request.bus, ECAT_BUS);
            assert_eq!(request.interface, FIELD_INTERFACE);
            Ok(Box::new(WireTransport {
                discovered: vec![discovered_station()],
                published: Arc::clone(&published),
                op_entered: false,
            }) as Box<dyn BusTransport>)
        }),
    );
    let registry = DriverRegistry::standard().with_ethercat_buses(&buses);
    resolve_drivers(model, &registry).unwrap().build().unwrap()
}

/// The cyclic binding's field side: a `BusServer` sharing the coupler
/// station's register bank — the manifest's channel layout in rail
/// order — whose declared `do1` → `di1` field wire plays the rig's
/// physical loopback: every published `do1` write lands on `di1`'s
/// register inside the same exchange, so the answered census latches
/// the transition — the register-bank analogue of `WireTransport`'s
/// byte-level wire. Returns the server, the checked-in document with
/// its `__BUS_ADDR__` placeholder substituted for the bound address,
/// and the observer attachment the exchange scripter goes through.
fn cyclic_field() -> (BusServer, PlantModel, BusDriver) {
    let bank = RegisterBank::new([
        RegisterDecl {
            register: registers::DO1,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: registers::DO2,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: registers::DI1,
            initial: Value::Bool(false),
        },
        RegisterDecl {
            register: registers::DI2,
            initial: Value::Bool(false),
        },
    ])
    .unwrap()
    .with_wires([(registers::DO1, registers::DI1)])
    .unwrap();
    let server = BusServer::bind_stationed(
        "127.0.0.1:0",
        bank,
        BTreeMap::from([(
            COUPLER_STATION.to_string(),
            BTreeSet::from([
                registers::DO1,
                registers::DO2,
                registers::DI1,
                registers::DI2,
            ]),
        )]),
    )
    .unwrap();
    let mut model = fixture(CYCLIC_JSON);
    let declared = model.devices[0].parameters.get_mut("address").unwrap();
    assert_eq!(
        declared.as_str().unwrap(),
        CYCLIC_ADDRESS_PLACEHOLDER,
        "the checked-in document must carry the address placeholder"
    );
    *declared = serde_json::json!(server.local_addr().unwrap().to_string());
    let observer = BusDriver::connect(
        server.local_addr().unwrap(),
        &[
            PointRegister {
                point: points::DI1,
                register: registers::DI1,
                kind: ValueKind::Bool,
            },
            PointRegister {
                point: points::DI2,
                register: registers::DI2,
                kind: ValueKind::Bool,
            },
            PointRegister {
                point: points::DO1,
                register: registers::DO1,
                kind: ValueKind::Bool,
            },
            PointRegister {
                point: points::DO2,
                register: registers::DO2,
                kind: ValueKind::Bool,
            },
        ],
    )
    .unwrap();
    (server, model, observer)
}

/// Builds the `sim-cyclic` binding's driver side through the standard
/// registry — the `CyclicBusDriver` attaching to the served register
/// bank, exchanging the whole image once per scan.
fn cyclic_driver(model: &PlantModel) -> FanoutDriver {
    resolve_drivers(model, &DriverRegistry::standard())
        .unwrap()
        .build()
        .unwrap()
}

/// The monitor-backed scripted run — the receipted operator path the
/// commissioning sequence names. `driver` builds the driver side
/// inside the served scope: a cyclic backend's connect-time census
/// needs its device server already answering, so `field`'s serve loop
/// starts first. The driver's `step` paces the simulated plant after
/// each scan (a no-op on the ethercat binding: the field advances
/// itself inside the scan's exchange).
fn scripted_run(
    model: &PlantModel,
    driver: impl FnOnce() -> FanoutDriver,
    field: Option<&BusServer>,
) -> Run {
    let result = thread::scope(|scope| {
        if let Some(server) = field {
            scope.spawn(move || server.serve());
        }
        let driver = driver();
        let executor = assemble(model, &dcs_controller::registry(), &driver).unwrap();
        let monitor = Monitor::bind("127.0.0.1:0", executor, model.signal_index()).unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join: catch
        // the panic so the servers are always shut down first.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut scans = Vec::with_capacity(SCANS as usize);
            for scan in 1..=SCANS {
                let snapshot = client.advance(1).unwrap();
                scans.push(Scan {
                    command: bool_(monitor_sample(&snapshot, points::DO1_COMMAND)),
                    do1: bool_(monitor_sample(&snapshot, points::DO1)),
                    do2: telemetry(&snapshot, points::DO2).sample.map(bool_),
                    di1: bool_(monitor_sample(&snapshot, points::DI1)),
                    di2: bool_(monitor_sample(&snapshot, points::DI2)),
                    di1_witness: bool_(monitor_sample(&snapshot, points::DI1_WITNESS)),
                    di2_witness: bool_(monitor_sample(&snapshot, points::DI2_WITNESS)),
                });
                match scan {
                    // The receipted assert: lands at scan 4's command
                    // boundary, stages DO1 that scan, publishes on
                    // scan 5's exchange.
                    3 => operator_write(&client, points::DO1_COMMAND, true),
                    // The release: lands at scan 9, so DI1 holds one
                    // more exchange before falling at scan 10.
                    8 => operator_write(&client, points::DO1_COMMAND, false),
                    _ => {}
                }
                driver.step(DT).unwrap();
            }
            Run {
                scans,
                receipts: client.receipts().unwrap(),
                snapshot: serde_json::to_string(&client.snapshot().unwrap()).unwrap(),
            }
        }));
        monitor.shutdown();
        if let Some(server) = field {
            server.shutdown();
        }
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// The outcomes a run's receipts carry, in order.
fn outcomes(receipts: &[CommandReceipt]) -> Vec<CommandOutcome> {
    receipts
        .iter()
        .map(|receipt| receipt.outcome.clone())
        .collect()
}

#[test]
fn the_rig_control_path_runs_identically_on_every_binding() {
    let sim_model = fixture(SIM_JSON);
    let ethercat_model = fixture(ETHERCAT_JSON);
    let (server, cyclic_model, _observer) = cyclic_field();

    let published = Arc::new(Mutex::new(Vec::new()));
    let sim_run = scripted_run(&sim_model, || sim_driver(&sim_model), None);
    let ethercat_run = scripted_run(
        &ethercat_model,
        || ethercat_driver(&ethercat_model, Arc::clone(&published)),
        None,
    );
    let cyclic_run = scripted_run(&cyclic_model, || cyclic_driver(&cyclic_model), Some(&server));

    // The WW-FND-002 proof: identical telemetry transition sequences
    // and identical receipted-command outcomes on all three bindings —
    // no component-logic change, one model's control path. The cyclic
    // binding's sequence rides the exchange-per-scan contract the
    // hardware driver implements: the staged `do1` publishes in the
    // next scan's exchange and the field wire's `di1` transition
    // latches in that exchange's own census — the documented one-scan
    // actuation delay between the command-settled tick and the
    // observed transition, the same command-to-observation sequence
    // the commissioning steps expect.
    assert_eq!(
        sim_run.scans, ethercat_run.scans,
        "the sim and ethercat bindings produced different telemetry sequences"
    );
    assert_eq!(
        sim_run.scans, cyclic_run.scans,
        "the cyclic binding produced a different telemetry sequence"
    );
    assert_eq!(sim_run.receipts, ethercat_run.receipts);
    assert_eq!(sim_run.receipts, cyclic_run.receipts);

    // The commissioning sequence itself, scan by scan: three baseline
    // scans, the assert's one-exchange actuation delay, the held
    // high, the release's matching delay, then the low settle. DI2
    // and DO2 stay clear throughout — the independence witness.
    let expected = vec![
        Scan::new(false, false, false),
        Scan::new(false, false, false),
        Scan::new(false, false, false),
        // Scan 4: the command applied; DO1 staged; the exchange still
        // published the previous image, so DI1 holds one more scan.
        Scan::new(true, true, false),
        Scan::new(true, true, true),
        Scan::new(true, true, true),
        Scan::new(true, true, true),
        Scan::new(true, true, true),
        // Scan 9: the release applied; DO1 unstaged; DI1 still reads
        // the image the exchange published before the write landed.
        Scan::new(false, false, true),
        Scan::new(false, false, false),
        Scan::new(false, false, false),
        Scan::new(false, false, false),
    ];
    assert_eq!(sim_run.scans, expected);

    // The receipted command path: both writes applied at the declared
    // scan boundary, attributed to the commissioning operator.
    assert_eq!(
        outcomes(&sim_run.receipts),
        vec![
            CommandOutcome::Applied { tick: Tick(4) },
            CommandOutcome::Applied { tick: Tick(9) },
        ]
    );
    assert!(
        sim_run
            .receipts
            .iter()
            .all(|receipt| receipt.actor.as_deref() == Some(OPERATOR))
    );

    // The ethercat binding's staged-output evidence: what the cyclic
    // exchange actually published, scan by scan — all-zeros until the
    // staged write lands, `{0b01}` while DO1 stands, zeros again after
    // the release crosses one more exchange.
    let published = published.lock().unwrap().clone();
    let mut expected_images = vec![vec![0u8]; 4];
    expected_images.extend(vec![vec![0b01u8]; 5]);
    expected_images.extend(vec![vec![0u8]; 3]);
    assert_eq!(
        published, expected_images,
        "the exchange did not publish the staged sequence"
    );

    // No component failed a step on any binding.
    for run in [&sim_run, &ethercat_run, &cyclic_run] {
        assert!(
            serde_json::from_str::<serde_json::Value>(&run.snapshot).unwrap()["components"]
                .as_array()
                .unwrap()
                .iter()
                .all(|component| component["step_errors"] == 0),
            "a component failed to step"
        );
    }

    // Repeated runs over the cyclic binding produce identical records:
    // the exchange-per-scan contract is as deterministic as the
    // point-wise and fake-transport legs.
    let (rerun_server, rerun_model, _observer) = cyclic_field();
    let rerun = scripted_run(
        &rerun_model,
        || cyclic_driver(&rerun_model),
        Some(&rerun_server),
    );
    assert_eq!(rerun.scans, cyclic_run.scans);
    assert_eq!(rerun.receipts, cyclic_run.receipts);
    assert_eq!(rerun.snapshot, cyclic_run.snapshot);
}

/// The cyclic binding's missed-exchange leg — the contract's failure
/// half on the same rig: scripted missed exchanges hold the held input
/// image below the declared `exchange_miss_threshold`, escalate the
/// device's reads at it, and never abort the scan — while the staged
/// output image a mid-miss command produces is retained until the next
/// completed exchange publishes it.
#[test]
fn a_missed_exchange_holds_the_image_and_escalates_at_the_threshold() {
    let (server, model, observer) = cyclic_field();
    let result = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let driver = cyclic_driver(&model);
        let executor = assemble(&model, &dcs_controller::registry(), &driver).unwrap();
        let monitor = Monitor::bind("127.0.0.1:0", executor, model.signal_index()).unwrap();
        let client = MonitorClient::new(monitor.local_addr());
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            // Baseline: assert DO1 through the receipted path and
            // observe the loopback — the run stands at scan 5 with DI1
            // high.
            for _ in 0..3 {
                client.advance(1).unwrap();
                driver.step(DT).unwrap();
            }
            operator_write(&client, points::DO1_COMMAND, true);
            client.advance(1).unwrap();
            driver.step(DT).unwrap();
            let snapshot = client.advance(1).unwrap();
            driver.step(DT).unwrap();
            assert!(
                bool_(monitor_sample(&snapshot, points::DI1)),
                "the loopback did not observe the published output"
            );

            // The declared threshold's worth of consecutive missed
            // exchanges, scripted on the device server — each consumed
            // by the next scan's exchange.
            match observer.request(&BusRequest::ScriptExchange {
                outcomes: vec![ExchangeOutcome::Miss; EXCHANGE_MISS_THRESHOLD as usize],
            }) {
                Ok(BusResponse::Done) => {}
                other => panic!("script-exchange was refused: {other:?}"),
            }

            // Below the threshold the held input image keeps serving —
            // DI1 reads its last latched value, Good, and the scan
            // completes normally rather than aborting.
            for misses in 1..EXCHANGE_MISS_THRESHOLD {
                let snapshot = client.advance(1).unwrap();
                driver.step(DT).unwrap();
                assert_eq!(snapshot.io_health.failed_exchanges, misses);
                assert_eq!(snapshot.io_health.failed_reads, 0);
                let di1 = monitor_sample(&snapshot, points::DI1);
                assert_eq!(
                    di1.quality,
                    Quality::Good,
                    "miss {misses}: the held image must keep serving"
                );
                assert!(
                    bool_(di1),
                    "miss {misses}: DI1 lost its held value"
                );
            }

            // The retained staged image: the release command submitted
            // inside the miss window applies at the next boundary and
            // stages DO1 low, but the missed exchange publishes
            // nothing.
            operator_write(&client, points::DO1_COMMAND, false);
            let snapshot = client.advance(1).unwrap();
            driver.step(DT).unwrap();
            assert_eq!(
                snapshot.io_health.failed_exchanges, EXCHANGE_MISS_THRESHOLD,
                "the third consecutive miss reaches the declared threshold"
            );
            assert_eq!(
                snapshot.io_health.failed_reads, 2,
                "the threshold escalates the device's field inputs"
            );
            for point in [points::DI1, points::DI2] {
                assert_eq!(
                    monitor_sample(&snapshot, point).quality,
                    Quality::Bad(QualityReason::CommunicationFault),
                    "point {point:?} must escalate at the miss threshold"
                );
            }

            // Recovery: the script exhausted, the next exchange
            // reconnects and completes — the retained release publishes,
            // the field wire lands it, and DI1 falls in the same
            // census, all without the scan ever having aborted.
            let snapshot = client.advance(1).unwrap();
            driver.step(DT).unwrap();
            assert_eq!(
                snapshot.io_health.failed_exchanges, EXCHANGE_MISS_THRESHOLD,
                "a completed exchange ends the miss streak"
            );
            let di1 = monitor_sample(&snapshot, points::DI1);
            assert_eq!(di1.quality, Quality::Good);
            assert!(
                !bool_(di1),
                "the retained staged image must publish on recovery"
            );

            // Both operator writes receipted at their scan boundaries.
            assert_eq!(
                outcomes(&client.receipts().unwrap()),
                vec![
                    CommandOutcome::Applied { tick: Tick(4) },
                    CommandOutcome::Applied { tick: Tick(8) },
                ]
            );
        }));
        monitor.shutdown();
        server.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

#[test]
fn the_ethercat_binding_rejects_a_mismatched_station_before_scanning() {
    // The `fail` startup policy's observable half: a discovered station
    // whose identity disagrees with the declared expectation fails
    // driver resolution — before OP entry and before any scan.
    let model = fixture(ETHERCAT_JSON);
    let mut wrong = discovered_station();
    wrong.product_id += 1;
    let buses = EthercatBuses::with_opener(
        BTreeMap::from([(ECAT_BUS.to_string(), FIELD_INTERFACE.to_string())]),
        Arc::new(move |_| {
            Ok(Box::new(WireTransport {
                discovered: vec![wrong.clone()],
                published: Arc::new(Mutex::new(Vec::new())),
                op_entered: false,
            }) as Box<dyn BusTransport>)
        }),
    );
    let registry = DriverRegistry::standard().with_ethercat_buses(&buses);
    match resolve_drivers(&model, &registry) {
        Err(AssemblyError::DeviceBackend { kind, detail, .. }) => {
            assert_eq!(kind, "ethercat");
            assert!(detail.contains("declared identity"), "{detail}");
        }
        Err(other) => panic!("expected DeviceBackend, got {other:?}"),
        Ok(_) => panic!("a mismatched station identity resolved"),
    }
}

#[test]
fn the_ethercat_binding_rejects_a_layout_mismatch_before_scanning() {
    // The declared mapping places DI2 at input byte 0 bit 1; a
    // discovered station exposing a smaller input area fails the
    // layout check at attach — before any exchange.
    let model = fixture(ETHERCAT_JSON);
    let mut shrunk = discovered_station();
    shrunk.input_bytes = 0;
    let buses = EthercatBuses::with_opener(
        BTreeMap::from([(ECAT_BUS.to_string(), FIELD_INTERFACE.to_string())]),
        Arc::new(move |_| {
            Ok(Box::new(WireTransport {
                discovered: vec![shrunk.clone()],
                published: Arc::new(Mutex::new(Vec::new())),
                op_entered: false,
            }) as Box<dyn BusTransport>)
        }),
    );
    let registry = DriverRegistry::standard().with_ethercat_buses(&buses);
    match resolve_drivers(&model, &registry) {
        Err(AssemblyError::DeviceBackend { kind, detail, .. }) => {
            assert_eq!(kind, "ethercat");
            assert!(detail.contains("beyond the discovered"), "{detail}");
        }
        Err(other) => panic!("expected DeviceBackend, got {other:?}"),
        Ok(_) => panic!("a mapping exceeding the discovered layout resolved"),
    }
}
