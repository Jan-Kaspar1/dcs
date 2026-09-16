//! The Wago EtherCAT QA rig's emitted documents — issue #335's
//! artifact for the HQ-4 "EtherCAT field path" milestone, extended by
//! #412's cyclic simulation binding.
//!
//! The checked-in documents live at `crates/dcs-demo/fixtures/`:
//! `wago_rig.json` is the `ethercat` binding — one hardware-bound
//! device declaring the manifest's expected 750-354 station identity,
//! bus, channel mapping, miss threshold, safe outputs, and startup
//! policy — `wago_rig_sim.json` is the identical control path
//! re-emitted over a local `sim` device plus the declared `di1` ←
//! `do1` loopback wire the simulation plays, and
//! `wago_rig_cyclic.json` is the same control path re-emitted over a
//! `sim-cyclic` device: the coupler station declared as the register
//! bank the manifest's channel layout maps onto, the miss threshold
//! carried as device-parameter data. These tests assert the helper
//! re-emits all three documents byte-for-byte, that they validate,
//! lint clean, and serde-roundtrip deterministically, and that
//! wrong-typed and unmapped channel bindings are rejected by named
//! errors before the first scan on every binding.

use dcs_assembly::{AssemblyError, DriverRegistry, resolve_drivers};
use dcs_build::wago::{
    COUPLER_STATION, CYCLIC_ADDRESS_PLACEHOLDER, CYCLIC_KIND, ECAT_BUS,
    EXCHANGE_MISS_THRESHOLD, RigBinding, WAGO_PRODUCT, WAGO_REVISION, WAGO_VENDOR, channels, points,
    registers, station_identity, wago_rig,
};
use dcs_build::{Direction, PlantBuilder, PointId, ValueKind};
use dcs_core::Value;
use dcs_ethercat::{ChannelDecl, DeviceParameters, StartupPolicy};
use dcs_model::{Endpoint, PlantModel, ValidationError};
use dcs_sim::Loopback;
use dcs_sim_bus::CyclicDeviceParameters;
use std::collections::BTreeMap;

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

fn emit(binding: RigBinding) -> PlantModel {
    wago_rig(binding).unwrap().model
}

/// The checked-in document's exact bytes: `to_string_pretty` plus the
/// trailing newline the emitting example writes.
fn fixture(binding: RigBinding) -> String {
    let path = match binding {
        RigBinding::Ethercat => ETHERCAT_JSON,
        RigBinding::Sim => SIM_JSON,
        RigBinding::Cyclic => CYCLIC_JSON,
    };
    std::fs::read_to_string(path).unwrap()
}

/// Every emitted binding.
const BINDINGS: [RigBinding; 3] = [RigBinding::Ethercat, RigBinding::Sim, RigBinding::Cyclic];

#[test]
fn emission_matches_the_checked_in_documents() {
    for binding in BINDINGS {
        let built = emit(binding);
        // Byte-for-byte, not just semantically equal: the checked-in
        // artifact is the document the QA lane mounts.
        assert_eq!(
            format!("{}\n", serde_json::to_string_pretty(&built).unwrap()),
            fixture(binding),
            "{binding:?} emission differs from the checked-in document"
        );
        // And it loads through the validating reader identically.
        assert_eq!(
            PlantModel::load(&fixture(binding)).unwrap(),
            built,
            "{binding:?} checked-in document does not load to the emitted model"
        );
    }
}

#[test]
fn re_emission_is_byte_identical() {
    for binding in BINDINGS {
        let first = serde_json::to_string_pretty(&emit(binding)).unwrap();
        let second = serde_json::to_string_pretty(&emit(binding)).unwrap();
        assert_eq!(
            first, second,
            "{binding:?} re-emission is not deterministic"
        );
    }
}

#[test]
fn the_sim_binding_declares_the_identical_control_path() {
    let ethercat = emit(RigBinding::Ethercat);
    let sim = emit(RigBinding::Sim);

    // Everything but the device declaration and the field wire is
    // literally identical: same points, signals, components, and
    // control-path connections — the WW-FND-002 hardware-independence
    // shape.
    assert_eq!(sim.io_points, ethercat.io_points);
    assert_eq!(sim.signals, ethercat.signals);
    assert_eq!(sim.components, ethercat.components);
    assert_eq!(
        sim.connections[..ethercat.connections.len()],
        ethercat.connections[..]
    );

    // The sim document's one extra declaration is the loopback wire —
    // `from` the `in` point `to` the `out` point, so DO1's writes feed
    // DI1's reads.
    assert_eq!(sim.connections.len(), ethercat.connections.len() + 1);
    assert_eq!(
        sim.connections.last().unwrap(),
        &dcs_model::Connection {
            from: Endpoint::Point(points::DI1),
            to: Endpoint::Point(points::DO1),
        }
    );

    // And the ethercat document honestly carries no such wire: the
    // manifest marks the physical loopback unverified, so the hardware
    // model declares only the mapping commissioning checks.
    assert!(ethercat.connections.iter().all(|connection| !(matches!(
        connection.from,
        Endpoint::Point(_)
    ) && matches!(
        connection.to,
        Endpoint::Point(_)
    ))));
}

#[test]
fn the_ethercat_declaration_parses_under_the_real_contract() {
    let model = emit(RigBinding::Ethercat);
    let device = &model.devices[0];
    assert_eq!(device.kind, "ethercat");
    assert!(device.hardware);

    let channel_decls: BTreeMap<String, ChannelDecl> = device
        .channels
        .iter()
        .map(|(name, channel)| {
            (
                name.clone(),
                ChannelDecl {
                    direction: channel.direction,
                    kind: channel.value_type,
                },
            )
        })
        .collect();
    let parsed = DeviceParameters::parse(&device.parameters, &channel_decls).unwrap();

    assert_eq!(parsed.bus, ECAT_BUS);
    assert_eq!(parsed.identity.vendor, WAGO_VENDOR);
    assert_eq!(parsed.identity.product, WAGO_PRODUCT);
    assert_eq!(parsed.identity.revision, WAGO_REVISION);
    assert_eq!(parsed.exchange_miss_threshold, EXCHANGE_MISS_THRESHOLD);
    assert_eq!(parsed.startup, StartupPolicy::FailOnMismatch);

    // The manifest's provisional bit-packed layout, in terminal order:
    // 750-400 DI1/DI2 then 750-501 DO1/DO2 at byte 0 of their images.
    assert_eq!(parsed.inputs[channels::DI1].bit_offset, 0);
    assert_eq!(parsed.inputs[channels::DI2].bit_offset, 1);
    assert_eq!(parsed.outputs[channels::DO1].bit_offset, 0);
    assert_eq!(parsed.outputs[channels::DO2].bit_offset, 1);
    assert_eq!(parsed.inputs.len(), 2);
    assert_eq!(parsed.outputs.len(), 2);
    assert_eq!(
        parsed.safe_outputs[channels::DO1],
        Value::Bool(false),
        "DO1's declared safe state is off"
    );
    assert_eq!(
        parsed.safe_outputs[channels::DO2],
        Value::Bool(false),
        "DO2's declared safe state is off"
    );

    // Every manifest-approved channel exists, with the declared
    // direction and Bool kind.
    for (name, direction) in [
        (channels::DI1, Direction::In),
        (channels::DI2, Direction::In),
        (channels::DO1, Direction::Out),
        (channels::DO2, Direction::Out),
    ] {
        let channel = &device.channels[name];
        assert_eq!(channel.direction, direction, "channel {name:?}");
        assert_eq!(channel.value_type, ValueKind::Bool, "channel {name:?}");
    }
}

#[test]
fn the_cyclic_binding_declares_the_identical_control_path() {
    let ethercat = emit(RigBinding::Ethercat);
    let cyclic = emit(RigBinding::Cyclic);

    // Same points, signals, components — and the hardware document's
    // own connections: the cyclic document declares no point-to-point
    // wire either, because the loopback is the served register bank's
    // field wiring, not a controller-side route.
    assert_eq!(cyclic.io_points, ethercat.io_points);
    assert_eq!(cyclic.signals, ethercat.signals);
    assert_eq!(cyclic.components, ethercat.components);
    assert_eq!(cyclic.connections, ethercat.connections);
}

#[test]
fn the_cyclic_declaration_parses_under_the_real_contract() {
    let model = emit(RigBinding::Cyclic);
    let device = &model.devices[0];
    assert_eq!(device.kind, CYCLIC_KIND);
    assert!(
        !device.hardware,
        "the cyclic binding is a simulated device, never hardware-bound"
    );

    let channel_kinds: BTreeMap<String, ValueKind> = device
        .channels
        .iter()
        .map(|(name, channel)| (name.clone(), channel.value_type))
        .collect();
    let parsed = CyclicDeviceParameters::parse(&device.parameters, &channel_kinds).unwrap();

    // The address is the placeholder a run substitutes its bound
    // device server for; the miss threshold is the declared cyclic
    // contract's, carried as device-parameter data.
    assert_eq!(parsed.address, CYCLIC_ADDRESS_PLACEHOLDER);
    assert_eq!(parsed.exchange_miss_threshold, EXCHANGE_MISS_THRESHOLD);

    // The one station is the manifest's coupler; its register bank
    // lays the channels out in rail order — the 750-501's outputs
    // first, then the 750-400's inputs.
    assert_eq!(parsed.stations.len(), 1);
    let coupler = &parsed.stations[COUPLER_STATION];
    assert_eq!(coupler.len(), 4);
    for (name, register) in [
        (channels::DO1, registers::DO1),
        (channels::DO2, registers::DO2),
        (channels::DI1, registers::DI1),
        (channels::DI2, registers::DI2),
    ] {
        assert_eq!(
            coupler[name].register, register,
            "channel {name:?}'s station placement"
        );
        // And the flat lookup resolves the same placement — the
        // driver-side view the factory binds points through.
        let (station, declaration) = parsed.channel_register(name).unwrap();
        assert_eq!(station, COUPLER_STATION);
        assert_eq!(declaration.register, register);
    }

    // The served-side views of the same declaration: one register
    // decl per channel, and the station's attribution map.
    let decls = parsed.register_decls(&channel_kinds);
    assert_eq!(decls.len(), 4);
    assert_eq!(
        parsed.station_registers(),
        BTreeMap::from([(
            COUPLER_STATION.to_string(),
            [registers::DO1, registers::DO2, registers::DI1, registers::DI2]
                .into_iter()
                .collect()
        )])
    );
}

#[test]
fn the_cyclic_binding_fails_honestly_without_a_device_server() {
    // The checked-in address is the placeholder: it parses, then the
    // connect-time resolution fails — a `sim-cyclic` device is never
    // silently substituted by a local simulation.
    match resolve_drivers(&emit(RigBinding::Cyclic), &DriverRegistry::standard()) {
        Err(AssemblyError::InvalidDeviceParameters { kind, detail, .. }) => {
            assert_eq!(kind, CYCLIC_KIND);
            assert!(detail.contains(CYCLIC_ADDRESS_PLACEHOLDER), "{detail}");
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("expected InvalidDeviceParameters, got a resolved driver plan"),
    }

    // The grammar's own rules apply to the emitted declaration: a
    // station entry naming a channel the device does not declare is a
    // named parameter error at resolution — before any scan.
    let mut model = emit(RigBinding::Cyclic);
    model.devices[0]
        .parameters
        .get_mut("stations")
        .unwrap()
        .as_object_mut()
        .unwrap()
        .get_mut(COUPLER_STATION)
        .unwrap()
        .as_object_mut()
        .unwrap()
        .insert("di9".to_string(), serde_json::json!(9));
    match resolve_drivers(&model, &DriverRegistry::standard()) {
        Err(AssemblyError::InvalidDeviceParameters { kind, detail, .. }) => {
            assert_eq!(kind, CYCLIC_KIND);
            assert!(detail.contains("di9"), "{detail}");
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("a station placing an undeclared channel resolved"),
    }
}

#[test]
fn emitted_documents_validate_lint_and_serde_roundtrip() {
    for binding in BINDINGS {
        let model = emit(binding);
        assert!(
            model.validate().is_empty(),
            "{binding:?} emitted document fails validation: {:?}",
            model.validate()
        );
        assert!(
            model.lint().is_empty(),
            "{binding:?} emitted document has lint findings: {:?}",
            model.lint()
        );
        let json = serde_json::to_string_pretty(&model).unwrap();
        let reloaded = PlantModel::load(&json).unwrap();
        assert_eq!(reloaded, model);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }
}

#[test]
fn the_sim_binding_resolves_the_declared_loopback() {
    let plan = resolve_drivers(&emit(RigBinding::Sim), &DriverRegistry::standard()).unwrap();
    // The field wire lands in the shared simulated map exactly as the
    // model mapping's pairing of an `Out` channel with the `In` point
    // observing it — DI2 is left without a driver, so it stays clear.
    assert_eq!(
        plan.sim_map.loopbacks,
        vec![Loopback {
            output: points::DO1,
            input: points::DI1,
        }]
    );
}

#[test]
fn the_ethercat_binding_fails_honestly_without_a_bus_deployment() {
    // The standard registry's validating stub: the declaration parses,
    // then startup fails because no EtherCAT master exists — a
    // hardware-bound kind is never silently substituted by simulation.
    match resolve_drivers(&emit(RigBinding::Ethercat), &DriverRegistry::standard()) {
        Err(AssemblyError::DeviceBackend { kind, detail, .. }) => {
            assert_eq!(kind, "ethercat");
            assert!(detail.contains("\"ecat0\""), "{detail}");
        }
        Err(other) => panic!("expected DeviceBackend, got {other:?}"),
        Ok(_) => panic!("expected DeviceBackend, got a resolved driver plan"),
    }
}

/// Mutates one of the emitted document's channel declarations to `Int`
/// — a point-to-channel kind disagreement the model validation must
/// name before any scan.
fn wrong_typed_channel(model: &mut PlantModel, channel: &str) {
    model.devices[0]
        .channels
        .get_mut(channel)
        .unwrap()
        .value_type = ValueKind::Int;
}

/// Rebinds the point to a channel the device does not declare.
fn unmapped_channel(model: &mut PlantModel, point: PointId) {
    model
        .io_points
        .iter_mut()
        .find(|declared| declared.id == point)
        .unwrap()
        .channel
        .as_mut()
        .unwrap()
        .name = "di9".to_string();
}

#[test]
fn wrong_typed_channel_bindings_fail_before_the_first_scan() {
    for binding in BINDINGS {
        let mut model = emit(binding);
        wrong_typed_channel(&mut model, channels::DI1);
        let errors = model.validate();
        assert!(
            errors.contains(&ValidationError::ChannelTypeMismatch {
                point: points::DI1,
                device: model.devices[0].id,
                channel: channels::DI1.to_string(),
                point_type: ValueKind::Bool,
                channel_type: ValueKind::Int,
            }),
            "{binding:?}: expected ChannelTypeMismatch, got {errors:?}"
        );
    }

    // The ethercat grammar's own wrong-type rule: a `bool` channel's
    // mapping entry must be a `{"byte","bit"}` placement, not a field —
    // a named parameter error at driver resolution, still before any
    // scan.
    let mut model = emit(RigBinding::Ethercat);
    *model.devices[0]
        .parameters
        .get_mut("mapping")
        .unwrap()
        .pointer_mut("/inputs/di1")
        .unwrap() = serde_json::json!({"byte": 0, "bits": 8});
    match resolve_drivers(&model, &DriverRegistry::standard()) {
        Err(AssemblyError::InvalidDeviceParameters { kind, detail, .. }) => {
            assert_eq!(kind, "ethercat");
            assert!(detail.contains(channels::DI1), "{detail}");
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("a mistyped mapping entry resolved"),
    }
}

#[test]
fn unmapped_channel_bindings_fail_before_the_first_scan() {
    for binding in BINDINGS {
        let mut model = emit(binding);
        unmapped_channel(&mut model, points::DI1);
        let errors = model.validate();
        assert!(
            errors.contains(&ValidationError::UnknownChannel {
                point: points::DI1,
                device: model.devices[0].id,
                channel: "di9".to_string(),
            }),
            "{binding:?}: expected UnknownChannel, got {errors:?}"
        );
    }

    // The ethercat document's mapping table is the binding: a declared
    // channel the mapping does not place is a named parameter error at
    // driver resolution — before the first exchange could run.
    let mut model = emit(RigBinding::Ethercat);
    let removed = model.devices[0]
        .parameters
        .get_mut("mapping")
        .unwrap()
        .pointer_mut("/inputs")
        .unwrap()
        .as_object_mut()
        .unwrap()
        .remove(channels::DI2);
    assert!(removed.is_some());
    match resolve_drivers(&model, &DriverRegistry::standard()) {
        Err(AssemblyError::InvalidDeviceParameters { kind, detail, .. }) => {
            assert_eq!(kind, "ethercat");
            assert!(
                detail.contains("mapping does not place declared channel \"di2\""),
                "{detail}"
            );
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("an unmapped declared channel resolved"),
    }
}

#[test]
fn the_hardware_marker_is_enforced_on_every_binding() {
    // An `ethercat` device missing `"hardware": true` cannot be served
    // by a simulated backend.
    let mut model = emit(RigBinding::Ethercat);
    model.devices[0].hardware = false;
    match resolve_drivers(&model, &DriverRegistry::standard()) {
        Err(AssemblyError::InvalidDeviceParameters { kind, .. }) => {
            assert_eq!(kind, "ethercat")
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("an ethercat device without the hardware marker resolved"),
    }

    // A simulated device carrying `"hardware": true` is the
    // mirror-image dishonesty: a simulated factory cannot serve a
    // hardware-bound declaration.
    for (binding, kind) in [
        (RigBinding::Sim, "sim"),
        (RigBinding::Cyclic, CYCLIC_KIND),
    ] {
        let mut model = emit(binding);
        model.devices[0].hardware = true;
        match resolve_drivers(&model, &DriverRegistry::standard()) {
            Err(AssemblyError::InvalidDeviceParameters { kind: rejected, .. }) => {
                assert_eq!(rejected, kind)
            }
            Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
            Ok(_) => panic!("a {kind} device with the hardware marker resolved"),
        }
    }
}

#[test]
#[should_panic(expected = "cannot take offset")]
fn a_bool_rig_channel_rejects_a_field_offset() {
    let mut plant = PlantBuilder::new();
    let device = plant.ethercat(dcs_build::ethercat::EthercatSpec::new(
        ECAT_BUS,
        station_identity(),
        EXCHANGE_MISS_THRESHOLD,
    ));
    plant.ethercat_input::<bool>(
        device,
        channels::DI1,
        dcs_build::ethercat::ImageOffset::Field { byte: 0, bits: 8 },
    );
}
