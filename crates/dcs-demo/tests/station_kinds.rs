//! The station-level WW-FND-002 evidence: the reference pumping
//! station — the same `io_points`, `signals`, `components`, and
//! `connections` — bound once to its emitted `sim-ai`/`sim-di`/`sim-do`
//! devices and once to a `sim-bus` register-mapped overlay, run through
//! the driven scan cycle with the field side fed in lockstep, asserting
//! the two registered kinds produce identical executor snapshots and
//! journals.
//!
//! `src/station_kinds.rs` documents the recorded binding choice, the
//! shared-bank topology, the boundary pacing, and the one normalized
//! snapshot field; these tests pin the document pair's sharing, the
//! register-addressed dynamics merge, standard assembly of both
//! variants, the per-scan and journal equivalence, the closed station
//! loop over registers, misbinding rejection before the first scan, and
//! run-to-run reproducibility.

use dcs_assembly::AssemblyError;
use dcs_controller::check;
use dcs_core::{
    CommandOutcome, Direction, DriverDiagnostics, IoDriver, JournalEvent, LinkState, PointId,
    Quality, QualityReason, Sample, Tick, Value,
};
use dcs_demo::station_kinds::{
    self, BUS_ADDRESS_PLACEHOLDER, BUS_DOCUMENT, BUS_DYNAMICS, LOCAL_DOCUMENT, LOCAL_DYNAMICS,
    TOTAL_SCANS, actions, points,
};
use dcs_model::{DeviceId, PlantModel};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::thread;

/// The shared document's JSON, parsed for shape comparisons.
fn parsed(source: &str) -> serde_json::Value {
    serde_json::from_str(source).expect("the checked-in fixture parses")
}

/// The point telemetry for `point` in `snapshot`.
fn point_sample(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> &Option<Sample> {
    &snapshot
        .points
        .iter()
        .find(|telemetry| telemetry.point == point)
        .unwrap_or_else(|| panic!("the snapshot serves point {}", point.0))
        .sample
}

/// The float value `point` reports in `snapshot`.
fn float_at(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> f64 {
    match point_sample(snapshot, point) {
        Some(Sample {
            value: Value::Float(value),
            ..
        }) => *value,
        other => panic!("point {} in {:?} is not a float sample", point.0, other),
    }
}

/// The bool value `point` reports in `snapshot`.
fn bool_at(snapshot: &dcs_core::TelemetrySnapshot, point: PointId) -> bool {
    match point_sample(snapshot, point) {
        Some(Sample {
            value: Value::Bool(value),
            ..
        }) => *value,
        other => panic!("point {} in {:?} is not a bool sample", point.0, other),
    }
}

#[test]
fn the_variants_share_the_station_plant() {
    let local = parsed(LOCAL_DOCUMENT);
    let bus = parsed(BUS_DOCUMENT);
    // Every section except `devices` is the same document — the pair is
    // the one station model with a per-kind device overlay.
    for section in [
        "version",
        "io_points",
        "signals",
        "components",
        "connections",
    ] {
        assert_eq!(
            local[section], bus[section],
            "the variants' {section} sections diverged"
        );
    }
    // Each device keeps its id and declared channel set; only the kind —
    // and its kind-specific parameters — differ.
    let local_devices = local["devices"].as_array().unwrap();
    let bus_devices = bus["devices"].as_array().unwrap();
    assert_eq!(local_devices.len(), bus_devices.len());
    assert_eq!(local_devices.len(), 3);
    for (local, bus) in local_devices.iter().zip(bus_devices) {
        assert_eq!(local["id"], bus["id"]);
        assert_eq!(local["channels"], bus["channels"]);
        assert!(
            local["kind"].as_str().unwrap().starts_with("sim-"),
            "{local}"
        );
        assert_eq!(bus["kind"], json!("sim-bus"));
        assert_eq!(bus["parameters"]["address"], json!(BUS_ADDRESS_PLACEHOLDER));
        // The register map covers the device's declared channels
        // exactly, each at its bound point's id — the numbering the
        // register-addressed dynamics document is written against.
        let channels = local["channels"].as_object().unwrap();
        let registers = bus["parameters"]["registers"].as_object().unwrap();
        assert_eq!(
            registers.keys().collect::<BTreeSet<_>>(),
            channels.keys().collect::<BTreeSet<_>>(),
            "the register map must cover the declared channel set exactly"
        );
    }
    // Register = bound point id, station-wide.
    let model = PlantModel::load(LOCAL_DOCUMENT).expect("the primary document loads");
    for (index, bus) in bus_devices.iter().enumerate() {
        let registers = bus["parameters"]["registers"].as_object().unwrap();
        for (name, register) in registers {
            let channel = local_devices[index]["channels"]
                .as_object()
                .unwrap()
                .get(name)
                .unwrap_or_else(|| panic!("device {index} declares no channel {name:?}"));
            let _ = channel;
            let point = model
                .io_points
                .iter()
                .find(|point| {
                    point.channel.as_ref().is_some_and(|channel| {
                        channel.name == *name && channel.device.0 == index as u64 + 1
                    })
                })
                .unwrap_or_else(|| panic!("no point binds {name:?} on device {}", index + 1));
            assert_eq!(
                register.as_u64(),
                Some(point.id.0),
                "channel {name:?} must map to its bound point's id"
            );
        }
    }
    // The register-addressed dynamics document carries the identical
    // declaration list as the point-addressed primary's — the
    // register-equals-point-id numbering makes them coincide.
    assert_eq!(parsed(BUS_DYNAMICS), parsed(LOCAL_DYNAMICS));
}

#[test]
fn both_documents_validate_and_lint_clean() {
    for source in [LOCAL_DOCUMENT, BUS_DOCUMENT] {
        // The overlay's placeholder address never reaches the loader —
        // validation is kind-agnostic — so load it with a stand-in.
        let model = PlantModel::load(&source.replace(BUS_ADDRESS_PLACEHOLDER, "127.0.0.1:1"))
            .expect("the document loads");
        assert_eq!(model.version, dcs_model::MODEL_VERSION);
        assert!(model.validate().is_empty(), "{:?}", model.validate());
        assert!(model.lint().is_empty(), "{:?}", model.lint());
    }
}

#[test]
fn both_variants_assemble_through_the_standard_registry() {
    let (local_model, _driver) =
        station_kinds::local_variant().expect("the primary variant resolves");
    let local_report = check(&local_model).expect("the primary variant assembles");
    assert_eq!(
        local_report.devices,
        BTreeMap::from([
            ("sim-ai".to_string(), 1),
            ("sim-di".to_string(), 1),
            ("sim-do".to_string(), 1),
        ])
    );

    // `check` resolves the `sim-bus` devices — which connect and probe
    // their registers — so the shared bank must be serving.
    let (bus_model, server) = station_kinds::bus_variant().expect("the bus variant binds");
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let bus_report = check(&bus_model).expect("the bus variant assembles");
        assert_eq!(
            bus_report.devices,
            BTreeMap::from([("sim-bus".to_string(), 3)])
        );
        // The control-plane surface is identical — only the device kind
        // counts differ.
        assert_eq!(local_report.declared_points, bus_report.declared_points);
        assert_eq!(local_report.served_points, bus_report.served_points);
        assert_eq!(local_report.components, bus_report.components);
        server.shutdown();
    });
}

#[test]
fn driven_runs_across_kinds_produce_identical_snapshots_and_journals() {
    let local = station_kinds::run_local().expect("the primary run completes");
    let bus = station_kinds::run_bus().expect("the bus run completes");
    assert_eq!(local.snapshots.len() as u64, TOTAL_SCANS);
    assert_eq!(bus.snapshots.len() as u64, TOTAL_SCANS);

    for (index, (local, bus)) in local.snapshots.iter().zip(&bus.snapshots).enumerate() {
        let tick = Tick(index as u64 + 1);
        assert_eq!(local.tick, tick, "primary snapshot ordering");
        assert_eq!(bus.tick, tick, "bus snapshot ordering");
        // `io_health.driver` is the driver's own volunteered transport
        // diagnostics — legitimately kind-specific (the module documents
        // this): the bus link reports itself, the local sim has no
        // transport. Normalize it; everything the control plane observed
        // — every point's value, quality, and tick, component
        // diagnostics, descriptors, live parameters, forces, and the
        // executor's I/O-health counters — must be identical.
        let mut local = local.clone();
        let mut bus = bus.clone();
        local.io_health.driver = None;
        bus.io_health.driver = None;
        assert_eq!(
            serde_json::to_value(&local).unwrap(),
            serde_json::to_value(&bus).unwrap(),
            "scan {} snapshots diverged across transports",
            tick.0
        );
    }

    // Pin what was normalized away: the local run reports no transport
    // diagnostics; the bus run reports a connected link with no recorded
    // failure on every scan.
    for snapshot in &local.snapshots {
        assert_eq!(snapshot.io_health.driver, None);
    }
    for snapshot in &bus.snapshots {
        assert_eq!(
            snapshot.io_health.driver,
            Some(DriverDiagnostics {
                link: LinkState::Connected,
                last_error: None,
                exchange: None,
            })
        );
    }

    // The transition journal — sequence numbers, attributed ticks, and
    // events — is identical across the two kinds.
    assert_eq!(
        local.journal, bus.journal,
        "the journal sequences diverged across transports"
    );
    assert_eq!(local.receipts, bus.receipts);

    // The journal carries the scenario the module documents: every
    // operator action settled Applied at its scheduled tick, and the
    // quality injections journaled their transitions.
    let settled: Vec<Tick> = local
        .journal
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::CommandSettled { receipt } => match receipt.outcome {
                CommandOutcome::Applied { tick } => Some(tick),
                ref outcome => panic!("a command settled as {outcome:?}"),
            },
            _ => None,
        })
        .collect();
    assert_eq!(
        settled,
        actions()
            .iter()
            .map(|action| Tick(action.tick))
            .collect::<Vec<_>>(),
        "the journal's settled commands"
    );
    let bad = Quality::Bad(QualityReason::CommunicationFault);
    assert!(local.journal.iter().any(|entry| matches!(
        &entry.event,
        JournalEvent::QualityChanged {
            point,
            to,
            ..
        } if *point == points::LEVEL_PRIMARY && *to == bad
    )));

    // Scenario pins, checked on one variant — equality carries them to
    // the other.
    let at = |scan: u64| &local.snapshots[scan as usize - 1];

    // The declared inflow lifted the seeded level: scan 1 reads the
    // dynamics' 0.8 m start plus one step of 0.6 inflow.
    assert!(
        (float_at(at(1), points::LEVEL_PRIMARY) - 1.4).abs() < 1e-9,
        "scan 1 level: {:?}",
        at(1).tick
    );

    // The chain staged real pumps and the station cycled: demand
    // reached two and the group's duty rotated between pumps.
    assert!(local.snapshots.iter().any(|snapshot| matches!(
        point_sample(snapshot, points::DEMAND),
        Some(Sample {
            value: Value::Int(2),
            ..
        })
    )));
    let duties: BTreeSet<i64> = local
        .snapshots
        .iter()
        .filter_map(|snapshot| match point_sample(snapshot, points::DUTY) {
            Some(Sample {
                value: Value::Int(duty),
                ..
            }) if *duty > 0 => Some(*duty),
            _ => None,
        })
        .collect();
    assert_eq!(duties, BTreeSet::from([1, 2]), "duty never rotated");

    // The failover selected the backup measurement while the primary
    // stood Bad.
    assert!(
        (39..=47).all(|scan| bool_at(at(scan), points::BACKUP_ACTIVE)),
        "backup_active must stand while the primary is Bad"
    );
    assert_eq!(
        point_sample(at(40), points::LEVEL_PRIMARY)
            .as_ref()
            .map(|s| s.quality),
        Some(bad)
    );

    // The issue-#502 leg: the backup stood Bad on its own while the
    // primary kept serving — `backup_unhealthy` asserted for the
    // fault's whole standing without `backup_active` ever rising, the
    // standby-loss annunciation the composition alarms.
    assert!(
        (49..=56).all(|scan| bool_at(at(scan), points::BACKUP_UNHEALTHY)),
        "backup_unhealthy must stand while the unused backup is Bad"
    );
    assert!(
        (48..=57).all(|scan| !bool_at(at(scan), points::BACKUP_ACTIVE)),
        "the primary keeps serving — backup_active must stay down"
    );
}

#[test]
fn the_command_register_drains_the_level_only_while_it_stands() {
    // The closed loop over registers: the manual-takeover phase holds
    // pump 1's command asserted by the operator's `hand` request while
    // the group stands down — the well refills on the declared inflow
    // alone, so the hand-driven command is the field's only draw. Each
    // explicit field step then drains the level by the pump's draw less
    // the inflow, and releasing the request stops the drain on the next
    // step.
    let bus = station_kinds::run_bus().expect("the bus run completes");
    let level = |scan: u64| float_at(&bus.snapshots[scan as usize - 1], points::LEVEL_PRIMARY);
    let p101_cmd = |scan: u64| bool_at(&bus.snapshots[scan as usize - 1], points::cmd(0));
    let p102_cmd = |scan: u64| bool_at(&bus.snapshots[scan as usize - 1], points::cmd(1));

    // `hand` applies at scan 29 and releases at 33; the three
    // port-to-port gate hops between the request point and the motor
    // turn that into the command register standing at scans 32–35 —
    // and only it: the group has no demand, so p102's register stays
    // down. Every step in the window drains the level, the draw (−1.0)
    // outweighing the declared inflow (0.6).
    for scan in 32..=35 {
        assert!(p101_cmd(scan), "p101-cmd must stand at scan {scan}");
        assert!(!p102_cmd(scan), "p102-cmd must be down at scan {scan}");
        assert!(
            level(scan + 1) < level(scan),
            "the level must drain while the command stands: scan {scan}: {} -> {}",
            level(scan),
            level(scan + 1)
        );
    }
    // With the request released the register drops at scan 36 and the
    // same explicit steps let the inflow refill the well — the level
    // stops draining and climbs.
    for scan in 36..=37 {
        assert!(!p101_cmd(scan), "p101-cmd must be down at scan {scan}");
        assert!(
            level(scan + 1) > level(scan),
            "the level must climb once the command releases: scan {scan}: {} -> {}",
            level(scan),
            level(scan + 1)
        );
    }
    // And the staged pump-down did the same through the group's own
    // requests: at some scan a command stands and the level is falling,
    // while early scans — no command standing — only ever climb.
    assert!(bus.snapshots.iter().enumerate().any(|(index, snapshot)| {
        let level_fell = index > 0
            && float_at(snapshot, points::LEVEL_PRIMARY)
                < float_at(&bus.snapshots[index - 1], points::LEVEL_PRIMARY);
        level_fell && (bool_at(snapshot, points::cmd(0)) || bool_at(snapshot, points::cmd(1)))
    }));
    for scan in 1..=4 {
        assert!(!p101_cmd(scan) && !bool_at(&bus.snapshots[scan as usize - 1], points::cmd(1)));
        if scan > 1 {
            assert!(
                level(scan) > level(scan - 1),
                "no command stands; the level only climbs: scan {scan}"
            );
        }
    }
}

#[test]
fn a_misbound_overlay_is_rejected_before_the_first_scan() {
    // The honest bank: built from the checked-in overlay, so the
    // controller's binding is the only thing under test. The outcomes
    // are collected and the server shut down before any assertion, so
    // a failed expectation cannot deadlock the serve thread's join.
    let server = station_kinds::serve_bus_bank().expect("the bank binds");
    let addr = server.local_addr().unwrap();
    let (wrong_kind, uncovered, mismatched) = thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let load = |mutated: serde_json::Value| {
            PlantModel::load(
                &mutated
                    .to_string()
                    .replace(BUS_ADDRESS_PLACEHOLDER, &addr.to_string()),
            )
            .map(|model| check(&model))
        };

        // A wrong kind: a kind no registered factory serves.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["kind"] = json!("modbus");
        let wrong_kind = load(document);

        // A missing register mapping: the device's channel is
        // uncovered — the parameter contract names it.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["parameters"]["registers"]
            .as_object_mut()
            .unwrap()
            .remove("p101-run");
        let uncovered = load(document);

        // A wrong-kind register mapping: the run channel binds the
        // level register — the probe finds a Float where the model
        // declares a Bool point.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["parameters"]["registers"]["p101-run"] = json!(10);
        let mismatched = load(document);

        server.shutdown();
        (wrong_kind, uncovered, mismatched)
    });

    // The standard registry names the device and its unserved kind.
    let error = wrong_kind
        .expect("the mutated document loads")
        .expect_err("a wrong-kind device must not assemble");
    assert_eq!(
        error,
        AssemblyError::UnknownDeviceKind {
            device: DeviceId(2),
            kind: "modbus".to_string(),
        },
        "{error}"
    );

    // The parameter contract names the uncovered channel.
    let error = uncovered
        .expect("the mutated document loads")
        .expect_err("an uncovered channel must not assemble");
    match &error {
        AssemblyError::InvalidDeviceParameters { device, detail, .. } => {
            assert_eq!(*device, DeviceId(2), "{error}");
            assert!(
                detail.contains("p101-run"),
                "the rejection must name the uncovered channel: {detail}"
            );
        }
        other => panic!("expected InvalidDeviceParameters, got {other}"),
    }

    // The probe names the misbound point.
    let error = mismatched
        .expect("the mutated document loads")
        .expect_err("a kind-mismatched register must not assemble");
    match &error {
        AssemblyError::DeviceBackend { device, detail, .. } => {
            assert_eq!(*device, DeviceId(2), "{error}");
            assert!(
                detail.contains("io point 40"),
                "the rejection must name the misbound point: {detail}"
            );
        }
        other => panic!("expected DeviceBackend, got {other}"),
    }

    // A wrong-direction channel: the command channel rebound as an
    // input — validation names the point, channel, and directions
    // before any assembly runs.
    let mut document = parsed(BUS_DOCUMENT);
    document["devices"][2]["channels"]["p101-cmd"]["direction"] = json!("in");
    let error = PlantModel::load(
        &document
            .to_string()
            .replace(BUS_ADDRESS_PLACEHOLDER, "127.0.0.1:1"),
    )
    .expect_err("a wrong-direction channel must not load");
    let text = error.to_string();
    assert!(
        text.contains("p101-cmd") && text.contains("100"),
        "the rejection must name the channel and its point: {text}"
    );
}

#[test]
fn repeated_runs_are_identical() {
    for run in [station_kinds::run_local, station_kinds::run_bus] {
        let first = run().expect("the first run completes");
        let second = run().expect("the second run completes");
        assert_eq!(first.snapshots, second.snapshots);
        assert_eq!(first.journal, second.journal);
        assert_eq!(first.receipts, second.receipts);
    }
}

/// The backup level leg reads the shared physical quantity, never the
/// primary instrument's sample: the checked-in dynamics drive point 11
/// from a second integrator on the flow-sum point 13 — the
/// already-decoupled `reference-plant/model/dynamics.json` pattern (a
/// second integrator on 13 with its own slightly-offset initial, 3.45
/// against the primary's 3.5) — so no element consumes point 10.
/// `reference-plant/model/dynamics.json` already uses that form and
/// needs no change.
fn assert_backup_decoupled(elements: &[dcs_sim::ProcessElement]) {
    let backup = elements
        .iter()
        .find(|element| element.output() == points::LEVEL_BACKUP)
        .expect("the dynamics drive the backup level point");
    let dcs_sim::ProcessElement::Integrator(integrator) = backup else {
        panic!("the backup leg must be a decoupled integrator, got {backup:?}");
    };
    assert_eq!(
        integrator.input, points::FLOW_SUM,
        "the backup integrator must read the flow sum, not the primary instrument"
    );
    assert!(
        elements
            .iter()
            .flat_map(|element| element.inputs())
            .all(|input| *input != points::LEVEL_PRIMARY),
        "no dynamics element may consume the primary instrument's sample"
    );
}

/// The standalone sim behind the point-addressed dynamics document —
/// the same `with_element` merge `dcs-plant-server --dynamics`
/// performs — over the points the document names.
fn dynamics_test_driver(source: &str) -> dcs_sim::SimDriver {
    use dcs_sim::{ChannelId, ChannelMap, PointBinding, ProcessElement, SimDriver};
    let elements: Vec<ProcessElement> =
        serde_json::from_str(source).expect("the dynamics document parses");
    assert_backup_decoupled(&elements);
    let float_in = |point: PointId| PointBinding {
        point,
        channel: ChannelId {
            device: 1,
            name: format!("p{}", point.0),
        },
        direction: Direction::In,
        initial: Value::Float(0.0),
    };
    let mut map = ChannelMap::new()
        .with_point(float_in(points::LEVEL_PRIMARY))
        .with_point(float_in(points::LEVEL_BACKUP))
        .with_point(float_in(points::INFLOW))
        .with_point(float_in(points::FLOW_SUM))
        .with_point(float_in(points::draw(0)))
        .with_point(float_in(points::draw(1)));
    for gate in [points::cmd(0), points::cmd(1)] {
        map = map.with_point(PointBinding {
            point: gate,
            channel: ChannelId {
                device: 3,
                name: format!("p{}", gate.0),
            },
            direction: Direction::Out,
            initial: Value::Bool(false),
        });
    }
    for element in elements {
        map = map.with_element(element);
    }
    SimDriver::new(map).expect("the checked-in dynamics validate")
}

/// The shared register bank behind the register-addressed dynamics
/// document — the same `RegisterBank::with_dynamics` construction
/// `dcs-sim-bus-device --dynamics` performs.
fn dynamics_test_bank(source: &str) -> dcs_sim_bus::RegisterBank {
    use dcs_sim::ProcessElement;
    use dcs_sim_bus::{RegisterBank, RegisterDecl};
    let elements: Vec<ProcessElement> =
        serde_json::from_str(source).expect("the dynamics document parses");
    assert_backup_decoupled(&elements);
    let decls = [
        points::LEVEL_PRIMARY.0,
        points::LEVEL_BACKUP.0,
        points::INFLOW.0,
        13,
        points::draw(0).0,
        points::draw(1).0,
    ]
    .map(|register| RegisterDecl {
        register: register as u16,
        initial: Value::Float(0.0),
    });
    let gates = [points::cmd(0).0, points::cmd(1).0].map(|register| RegisterDecl {
        register: register as u16,
        initial: Value::Bool(false),
    });
    RegisterBank::with_dynamics(decls.into_iter().chain(gates), elements)
        .expect("the checked-in bus dynamics validate")
}

/// The float value a served sample carries.
fn served_float(sample: Sample) -> f64 {
    match sample.value {
        Value::Float(value) => value,
        other => panic!("the level sample must be Float, got {other:?}"),
    }
}

#[test]
fn local_backup_level_is_decoupled_from_primary_quality() {
    use dcs_sim::Fault;
    let sim = dynamics_test_driver(LOCAL_DYNAMICS);
    let bad = Quality::Bad(QualityReason::CommunicationFault);

    // The declared inflow; the backup tracks the well level through
    // its own integrator — a constant 0.01 below the primary, the
    // slightly-offset initial (0.79 against 0.8).
    sim.write(points::INFLOW, Value::Float(0.6)).unwrap();
    sim.step(station_kinds::SCAN_PERIOD);
    sim.step(station_kinds::SCAN_PERIOD);
    let primary = served_float(sim.read(points::LEVEL_PRIMARY).unwrap());
    let backup = served_float(sim.read(points::LEVEL_BACKUP).unwrap());
    assert!((primary - 2.0).abs() < 1e-9, "primary tracks 0.8 + 2·0.6");
    assert!((backup - 1.99).abs() < 1e-9, "backup tracks 0.79 + 2·0.6");
    assert!((primary - backup - 0.01).abs() < 1e-9);

    // A quality fault on the primary leaves the backup Good and still
    // tracking: the producible primary-faulted/backup-healthy state.
    sim.inject_fault(points::LEVEL_PRIMARY, Fault::Quality(bad))
        .unwrap();
    sim.step(station_kinds::SCAN_PERIOD);
    assert_eq!(sim.read(points::LEVEL_PRIMARY).unwrap().quality, bad);
    let served = sim.read(points::LEVEL_BACKUP).unwrap();
    assert!(served.quality.is_good(), "backup must stay Good: {served:?}");
    assert!(
        served_float(served) > backup,
        "the backup keeps integrating the flow sum while the primary stands Bad"
    );
    sim.clear_fault(points::LEVEL_PRIMARY).unwrap();

    // And the reverse: a fault on the backup leaves the primary Good —
    // the backup-faulted/primary-healthy state.
    sim.inject_fault(points::LEVEL_BACKUP, Fault::Quality(bad))
        .unwrap();
    sim.step(station_kinds::SCAN_PERIOD);
    assert_eq!(sim.read(points::LEVEL_BACKUP).unwrap().quality, bad);
    assert!(
        sim.read(points::LEVEL_PRIMARY).unwrap().quality.is_good(),
        "the primary must stay Good under a backup fault"
    );
}

#[test]
fn bus_backup_level_is_decoupled_from_primary_quality() {
    let bank = dynamics_test_bank(BUS_DYNAMICS);
    let bad = Quality::Bad(QualityReason::CommunicationFault);

    bank.write(points::INFLOW.0 as u16, Value::Float(0.6))
        .unwrap();
    bank.step(station_kinds::SCAN_PERIOD);
    bank.step(station_kinds::SCAN_PERIOD);
    let primary = served_float(bank.read(points::LEVEL_PRIMARY.0 as u16).unwrap());
    let backup = served_float(bank.read(points::LEVEL_BACKUP.0 as u16).unwrap());
    assert!((primary - 2.0).abs() < 1e-9, "primary tracks 0.8 + 2·0.6");
    assert!((backup - 1.99).abs() < 1e-9, "backup tracks 0.79 + 2·0.6");
    assert!((primary - backup - 0.01).abs() < 1e-9);

    // A quality fault on the primary leaves the backup Good and still
    // tracking: the producible primary-faulted/backup-healthy state.
    bank.inject_quality(points::LEVEL_PRIMARY.0 as u16, bad)
        .unwrap();
    bank.step(station_kinds::SCAN_PERIOD);
    assert_eq!(
        bank.read(points::LEVEL_PRIMARY.0 as u16).unwrap().quality,
        bad
    );
    let served = bank.read(points::LEVEL_BACKUP.0 as u16).unwrap();
    assert!(served.quality.is_good(), "backup must stay Good: {served:?}");
    assert!(
        served_float(served) > backup,
        "the backup keeps integrating the flow sum while the primary stands Bad"
    );
    bank.clear_quality(points::LEVEL_PRIMARY.0 as u16).unwrap();

    // And the reverse: a fault on the backup leaves the primary Good —
    // the backup-faulted/primary-healthy state.
    bank.inject_quality(points::LEVEL_BACKUP.0 as u16, bad)
        .unwrap();
    bank.step(station_kinds::SCAN_PERIOD);
    assert_eq!(
        bank.read(points::LEVEL_BACKUP.0 as u16).unwrap().quality,
        bad
    );
    assert!(
        bank.read(points::LEVEL_PRIMARY.0 as u16)
            .unwrap()
            .quality
            .is_good(),
        "the primary must stay Good under a backup fault"
    );
}
