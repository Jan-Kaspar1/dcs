//! The interim WW-FND-002 evidence: one logical plant — the same
//! `io_points`, `signals`, `components`, and `connections` — bound once
//! to `sim-scripted` and once to `sim-bus`, run through the driven scan
//! cycle with the field side fed in lockstep, asserting the two
//! transports produce identical executor snapshots and journals.
//!
//! `src/two_kinds.rs` documents the scenario, the tick mapping, and the
//! one normalized snapshot field; these tests pin the fixture pair's
//! sharing, the script's encoding of the shared field program, standard
//! assembly of both variants, the per-scan and journal equivalence, and
//! run-to-run reproducibility.

use dcs_controller::check;
use dcs_core::{
    CommandOutcome, DriverDiagnostics, JournalEvent, LinkState, PointId, Quality, QualityReason,
    Sample, Tick, Value,
};
use dcs_demo::two_kinds::{
    self, BUS_ADDRESS_PLACEHOLDER, BUS_DOCUMENT, FIELD_PROGRAM, SCRIPTED_DOCUMENT, TOTAL_SCANS,
    actions, points,
};
use dcs_model::PlantModel;
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};
use std::thread;

/// The shared fixture's JSON, parsed for shape comparisons.
fn document(source: &str) -> serde_json::Value {
    serde_json::from_str(source).expect("the checked-in fixture parses")
}

/// The channel name `point` binds in the shared model — the fixtures
/// share their channel declarations, so either model answers.
fn channel_of(model: &PlantModel, point: PointId) -> String {
    model
        .io_points
        .iter()
        .find(|io_point| io_point.id == point)
        .unwrap_or_else(|| panic!("the fixture declares point {}", point.0))
        .channel
        .as_ref()
        .unwrap_or_else(|| panic!("point {} is field-bound", point.0))
        .name
        .clone()
}

/// A script entry's bare JSON `value` — the `sim-scripted` contract's
/// untyped literal — compared against a typed [`Value`].
fn entry_value_matches(json: &serde_json::Value, value: &Value) -> bool {
    match value {
        Value::Bool(expected) => json.as_bool() == Some(*expected),
        Value::Int(expected) => json.as_i64() == Some(*expected),
        Value::Float(expected) => json.as_f64() == Some(*expected),
    }
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

#[test]
fn the_variants_share_the_logical_plant() {
    let scripted = document(SCRIPTED_DOCUMENT);
    let bus = document(BUS_DOCUMENT);
    // Every section except `devices` is the same document — the pair is
    // one logical fixture with a per-kind device overlay.
    for section in [
        "version",
        "io_points",
        "signals",
        "components",
        "connections",
    ] {
        assert_eq!(
            scripted[section], bus[section],
            "the variants' {section} sections diverged"
        );
    }
    // The device keeps its id and declared channel set; only the kind —
    // and its kind-specific parameters — differ.
    let scripted_device = &scripted["devices"][0];
    let bus_device = &bus["devices"][0];
    assert_eq!(scripted_device["id"], bus_device["id"]);
    assert_eq!(scripted_device["channels"], bus_device["channels"]);
    assert_eq!(scripted_device["kind"], json!("sim-scripted"));
    assert_eq!(bus_device["kind"], json!("sim-bus"));
    // The only runtime-dependent parameter is the server address a run
    // substitutes; the register map covers the declared channels
    // exactly, one register each.
    assert_eq!(
        bus_device["parameters"]["address"],
        json!(BUS_ADDRESS_PLACEHOLDER)
    );
    let channels: BTreeSet<&String> = bus_device["channels"]
        .as_object()
        .expect("channels is an object")
        .keys()
        .collect();
    let registers = bus_device["parameters"]["registers"]
        .as_object()
        .expect("registers is an object");
    assert_eq!(
        registers.keys().collect::<BTreeSet<_>>(),
        channels,
        "the register map must cover the declared channel set exactly"
    );
    let indices: BTreeSet<u64> = registers
        .values()
        .map(|entry| entry.as_u64().expect("a bare register index"))
        .collect();
    assert_eq!(indices.len(), channels.len(), "register indices collide");
    // The scripted script covers exactly the field `In` channels the
    // program feeds — the feedback channel is driven by the plant's own
    // point-to-point wire, not the program.
    let scripted_channels: BTreeSet<&String> = scripted_device["parameters"]["script"]
        .as_object()
        .expect("script is an object")
        .keys()
        .collect();
    let model = PlantModel::load(SCRIPTED_DOCUMENT).expect("scripted fixture loads");
    let fed: BTreeSet<String> = FIELD_PROGRAM
        .iter()
        .map(|change| channel_of(&model, change.point))
        .collect();
    assert_eq!(
        scripted_channels,
        fed.iter().collect::<BTreeSet<_>>(),
        "the scripted fixture scripts exactly the fed channels"
    );
}

#[test]
fn the_scripted_fixture_encodes_the_field_program() {
    let model = PlantModel::load(SCRIPTED_DOCUMENT).expect("scripted fixture loads");
    let script = model.devices[0]
        .parameters
        .get("script")
        .and_then(|script| script.as_object())
        .expect("the scripted fixture declares a script map");
    // Group the program per channel; a FieldChange at scan tick t is the
    // script entry at driver tick t - 1, applied by the step after scan
    // t - 1 and first observed by scan t (see the module docs).
    let mut expected: BTreeMap<String, Vec<(u64, Value)>> = BTreeMap::new();
    for change in FIELD_PROGRAM {
        expected
            .entry(channel_of(&model, change.point))
            .or_default()
            .push((change.tick - 1, change.value));
    }
    for (channel, expected) in &expected {
        let entries = script
            .get(channel.as_str())
            .and_then(|entries| entries.as_array())
            .unwrap_or_else(|| panic!("the script declares channel {channel:?}"));
        assert_eq!(
            entries.len(),
            expected.len(),
            "script channel {channel:?} entry count"
        );
        for (entry, (tick, value)) in entries.iter().zip(expected) {
            assert_eq!(
                entry["tick"].as_u64(),
                Some(*tick),
                "script channel {channel:?} tick"
            );
            assert!(
                entry_value_matches(&entry["value"], value),
                "script channel {channel:?} entry {entry} carries {value:?}"
            );
            // Entries carry no quality override: the program is all-Good.
            assert!(entry.get("quality").is_none(), "{entry}");
        }
    }
}

#[test]
fn both_variants_validate_and_assemble_through_the_standard_registry() {
    let scripted = PlantModel::load(SCRIPTED_DOCUMENT).expect("scripted fixture loads");
    let scripted_report = check(&scripted).expect("the scripted variant assembles");
    assert_eq!(
        scripted_report.devices,
        BTreeMap::from([("sim-scripted".to_string(), 1)])
    );

    // `check` resolves the `sim-bus` device — which connects and probes
    // its registers — so the device's server must be serving.
    let (bus_model, server) = two_kinds::bus_variant().expect("the bus variant binds");
    thread::scope(|scope| {
        scope.spawn(|| server.serve());
        let bus_report = check(&bus_model).expect("the bus variant assembles");
        assert_eq!(
            bus_report.devices,
            BTreeMap::from([("sim-bus".to_string(), 1)])
        );
        // The control-plane surface is identical — only the device kind
        // count differs.
        assert_eq!(scripted_report.declared_points, bus_report.declared_points);
        assert_eq!(scripted_report.served_points, bus_report.served_points);
        assert_eq!(scripted_report.components, bus_report.components);
        server.shutdown();
    });
}

#[test]
fn driven_runs_across_kinds_produce_identical_snapshots_and_journals() {
    let scripted = two_kinds::run_scripted().expect("the scripted run completes");
    let bus = two_kinds::run_bus().expect("the bus run completes");
    assert_eq!(scripted.snapshots.len() as u64, TOTAL_SCANS);
    assert_eq!(bus.snapshots.len() as u64, TOTAL_SCANS);

    for (index, (scripted, bus)) in scripted.snapshots.iter().zip(&bus.snapshots).enumerate() {
        let tick = Tick(index as u64 + 1);
        assert_eq!(scripted.tick, tick, "scripted snapshot ordering");
        assert_eq!(bus.tick, tick, "bus snapshot ordering");
        // `io_health.driver` is the driver's own volunteered transport
        // diagnostics — legitimately kind-specific (the module documents
        // this): the bus link reports itself, the scripted backend has
        // no transport. Normalize it; everything the control plane
        // observed — every point's value, quality, and tick, component
        // diagnostics, descriptors, live parameters, forces, and the
        // executor's I/O-health counters — must be identical.
        let mut scripted = scripted.clone();
        let mut bus = bus.clone();
        scripted.io_health.driver = None;
        bus.io_health.driver = None;
        assert_eq!(
            serde_json::to_value(&scripted).unwrap(),
            serde_json::to_value(&bus).unwrap(),
            "scan {} snapshots diverged across transports",
            tick.0
        );
    }

    // Pin what was normalized away: the scripted run never reports
    // transport diagnostics; the bus run reports a connected link with
    // no recorded failure on every scan.
    for snapshot in &scripted.snapshots {
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
        scripted.journal, bus.journal,
        "the journal sequences diverged across transports"
    );
    assert_eq!(scripted.receipts, bus.receipts);

    // The journal carries the scenario the module documents, not just
    // first observations: every operator action settled Applied at its
    // scheduled tick, and the force journaled its substitution quality.
    let settled: Vec<Tick> = scripted
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
    let force_quality = Quality::Uncertain(QualityReason::Substituted);
    assert!(scripted.journal.iter().any(|entry| matches!(
        &entry.event,
        JournalEvent::QualityChanged {
            point,
            to,
            ..
        } if *point == points::LEVEL_RAW && *to == force_quality
    )));

    // Scenario sanity pins, checked on one variant — equality already
    // carries them to the other. Every image sample carries its scan
    // tick — the executor restamps driver reads — so the forced value
    // scans 31..=38 (applied at 31, released at 39) stamps each scan.
    for index in 30..38 {
        assert_eq!(
            point_sample(&scripted.snapshots[index], points::LEVEL_RAW),
            &Some(Sample::new(
                Value::Float(15.0),
                force_quality,
                Tick(index as u64 + 1)
            )),
            "forced level_raw at scan {}",
            index + 1
        );
    }
    // - The mid-run feedback loss (from scan 45 against a run command,
    //   observed by the motor from scan 46 through the digital-input's
    //   internal link) trips the fault on the fifth consecutive
    //   disagreeing scan; the feedback returning at scan 53 clears it
    //   once the motor observes it at scan 54.
    for (index, expected) in [
        (46usize, false),
        (49, true),
        (52, true),
        (53, false),
        (TOTAL_SCANS as usize - 1, false),
    ] {
        assert_eq!(
            point_sample(&scripted.snapshots[index], points::PUMP_FAULT),
            &Some(Sample::new(
                Value::Bool(expected),
                Quality::Good,
                Tick(index as u64 + 1)
            )),
            "pump fault at scan {}",
            index + 1
        );
    }
    // - The force's Substituted quality propagates through the
    //   analog-input → pid → valve links onto `valve.cmd`; the valve's
    //   fail-safe reading counts the non-Good command as deviating, so
    //   the discrepancy asserts on the third deviating scan and clears
    //   once the released command is Good and tracked again — the
    //   quality-propagation path is transport-independent.
    for (index, expected, quality) in [
        (33usize, false, force_quality),
        (34, true, force_quality),
        (40, true, Quality::Good),
        (41, false, Quality::Good),
        (TOTAL_SCANS as usize - 1, false, Quality::Good),
    ] {
        assert_eq!(
            point_sample(&scripted.snapshots[index], points::VALVE_DISCREPANCY),
            &Some(Sample::new(
                Value::Bool(expected),
                quality,
                Tick(index as u64 + 1)
            )),
            "valve discrepancy at scan {}",
            index + 1
        );
    }
    // The command path exercised the field write: the pump command
    // output carried the start request to the field.
    assert_eq!(
        point_sample(&scripted.snapshots[10], points::PUMP_CMD),
        &Some(Sample::new(Value::Bool(true), Quality::Good, Tick(11)))
    );
}

#[test]
fn repeated_runs_are_identical() {
    for run in [two_kinds::run_scripted, two_kinds::run_bus] {
        let first = run().expect("the first run completes");
        let second = run().expect("the second run completes");
        assert_eq!(first.snapshots, second.snapshots);
        assert_eq!(first.journal, second.journal);
        assert_eq!(first.receipts, second.receipts);
    }
}
