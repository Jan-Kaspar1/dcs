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
    CommandOutcome, DriverDiagnostics, JournalEvent, LinkState, PointId, Quality, QualityReason,
    Sample, Tick, Value,
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
        assert_eq!(
            bus["parameters"]["address"],
            json!(BUS_ADDRESS_PLACEHOLDER)
        );
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
    assert!(local
        .snapshots
        .iter()
        .any(|snapshot| matches!(
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
        point_sample(at(40), points::LEVEL_PRIMARY).as_ref().map(|s| s.quality),
        Some(bad)
    );
}

#[test]
fn the_command_register_drains_the_level_only_while_it_stands() {
    // The closed loop over registers: the manual-takeover phase holds
    // pump 1's command asserted by the operator's `hand` request while
    // the group's own request stands down — each explicit field step
    // then drains the level by the pump's draw less the inflow, and
    // releasing the command stops the drain on the next step.
    let bus = station_kinds::run_bus().expect("the bus run completes");
    let level = |scan: u64| float_at(&bus.snapshots[scan as usize - 1], points::LEVEL_PRIMARY);
    let p101_cmd = |scan: u64| bool_at(&bus.snapshots[scan as usize - 1], points::cmd(0));

    // Hand stands from scan 53's application through its release at 58:
    // every scan in the window observes the asserted command register
    // and a level lower than the previous scan's — the draw (−1.0)
    // outweighing the declared inflow (0.6).
    for scan in 54..=57 {
        assert!(p101_cmd(scan), "p101-cmd must stand at scan {scan}");
        assert!(
            level(scan) < level(scan - 1),
            "the level must drain while the command stands: scan {scan}: {} -> {}",
            level(scan - 1),
            level(scan)
        );
    }
    // With the command released, the same explicit steps let the inflow
    // refill the well — the level stops draining and climbs.
    for scan in 60..=62 {
        assert!(!p101_cmd(scan), "p101-cmd must be down at scan {scan}");
        assert!(
            level(scan) > level(scan - 1),
            "the level must climb once the command releases: scan {scan}: {} -> {}",
            level(scan - 1),
            level(scan)
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
    // controller's binding is the only thing under test.
    let server = station_kinds::serve_bus_bank().expect("the bank binds");
    let addr = server.local_addr().unwrap();
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let load = |mutated: serde_json::Value| {
            PlantModel::load(
                &mutated
                    .to_string()
                    .replace(BUS_ADDRESS_PLACEHOLDER, &addr.to_string()),
            )
        };

        // A wrong kind: the standard registry names the device and its
        // unserved kind.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["kind"] = json!("sim-nope");
        let error = check(&load(document).expect("the mutated document loads"))
            .expect_err("a wrong-kind device must not assemble");
        assert_eq!(
            error,
            AssemblyError::UnknownDeviceKind {
                device: DeviceId(2),
                kind: "sim-nope".to_string(),
            },
            "{error}"
        );

        // A missing register mapping: the device's channel is
        // uncovered — the parameter contract names it.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["parameters"]["registers"]
            .as_object_mut()
            .unwrap()
            .remove("p101-run");
        let error = check(&load(document).expect("the mutated document loads"))
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

        // A wrong-kind register mapping: the run channel binds the
        // level register — the probe finds a Float where the model
        // declares a Bool point.
        let mut document = parsed(BUS_DOCUMENT);
        document["devices"][1]["parameters"]["registers"]["p101-run"] = json!(10);
        let error = check(&load(document).expect("the mutated document loads"))
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
        server.shutdown();
    });

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
