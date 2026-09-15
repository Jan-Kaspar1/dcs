//! The `ethercat` device kind's typed composition: the builder surface
//! emits byte-for-byte the checked-in reference fixture — the same
//! field-bus declaration a hand-written document carries, parsed by the
//! real `dcs-ethercat` contract and resolved by the standard registry
//! identically to it.

use dcs_assembly::{AssemblyError, DriverRegistry, resolve_drivers};
use dcs_build::ethercat::{EthercatSpec, ImageOffset, StationIdentity};
use dcs_build::{PlantBuilder, PointId, SignalId};
use dcs_ethercat::{ChannelDecl, DeviceParameters, StartupPolicy};
use dcs_model::PlantModel;
use std::collections::BTreeMap;

/// The reference document `dcs-assembly`'s ethercat tests exercise.
const ETHERCAT: &str = include_str!("../../dcs-assembly/fixtures/ethercat.json");

/// The coupler composition: 2×DI + 1×AI + 2×DO on logical bus `ecat0`.
fn coupler() -> PlantBuilder {
    let mut plant = PlantBuilder::new();

    let device = plant.ethercat(EthercatSpec::new(
        "ecat0",
        StationIdentity {
            vendor: 21,
            product: 750354,
            revision: 1,
        },
        3,
    ));

    // The channels and their process-image offsets; outputs carry their
    // declared safe states.
    let di0 = plant.ethercat_input::<bool>(device, "di0", ImageOffset::Bit { byte: 0, bit: 0 });
    let di1 = plant.ethercat_input::<bool>(device, "di1", ImageOffset::Bit { byte: 0, bit: 1 });
    let ai0 = plant.ethercat_input::<i64>(device, "ai0", ImageOffset::Field { byte: 1, bits: 16 });
    let do0 =
        plant.ethercat_output::<bool>(device, "do0", ImageOffset::Bit { byte: 0, bit: 0 }, false);
    let do1 =
        plant.ethercat_output::<bool>(device, "do1", ImageOffset::Bit { byte: 0, bit: 1 }, false);

    // Logical I/O points bound to the channels.
    let level = plant.field_input::<i64>(PointId(1), ai0, false);
    let fault = plant.field_input::<bool>(PointId(2), di0, false);
    let closed = plant.field_input::<bool>(PointId(3), di1, false);
    let inlet = plant.field_output::<bool>(PointId(4), do0);
    let drain = plant.field_output::<bool>(PointId(5), do1);

    // The monitoring names for the points.
    plant.signal(SignalId(10), "level-raw", level).unit("mm");
    plant.signal(SignalId(11), "pump-fault", fault);
    plant.signal(SignalId(12), "valve-closed", closed);
    plant.signal(SignalId(13), "inlet-valve", inlet);
    plant.signal(SignalId(14), "drain-valve", drain);

    plant
}

#[test]
fn composition_emits_the_reference_document() {
    let built = coupler().build().unwrap();
    let reference = PlantModel::load(ETHERCAT).unwrap();

    // The same document: a consumer cannot tell the composed model from
    // the hand-written one, and assembly resolves them identically.
    assert_eq!(built, reference);

    let emitted = serde_json::to_value(&built).unwrap();
    let checked_in = serde_json::from_str::<serde_json::Value>(ETHERCAT).unwrap();
    assert_eq!(emitted, checked_in);
}

#[test]
fn emitted_declaration_parses_under_the_real_contract() {
    // The spec is a data mirror: the emitted parameters must satisfy the
    // actual `dcs-ethercat` grammar the factory runs — the drift check
    // keeping the builder surface honest against the contract.
    let model = coupler().build().unwrap();
    let device = &model.devices[0];
    let channels: BTreeMap<String, ChannelDecl> = device
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
    let parsed = DeviceParameters::parse(&device.parameters, &channels).unwrap();
    assert_eq!(parsed.bus, "ecat0");
    assert_eq!(parsed.identity.vendor, 21);
    assert_eq!(parsed.exchange_miss_threshold, 3);
    assert_eq!(parsed.startup, StartupPolicy::FailOnMismatch);
    assert_eq!(parsed.inputs.len(), 3);
    assert_eq!(parsed.outputs.len(), 2);
    assert_eq!(parsed.safe_outputs.len(), 2);
}

#[test]
fn emitted_model_assembles_identically_to_the_fixture() {
    // Both documents resolve to the same named failure: the declaration
    // parses, then startup fails because no EtherCAT master exists.
    for document in [
        coupler().build().unwrap(),
        PlantModel::load(ETHERCAT).unwrap(),
    ] {
        match resolve_drivers(&document, &DriverRegistry::standard()) {
            Err(AssemblyError::DeviceBackend { kind, detail, .. }) => {
                assert_eq!(kind, "ethercat");
                assert!(detail.contains("\"ecat0\""), "{detail}");
            }
            Err(other) => panic!("expected DeviceBackend, got {other:?}"),
            Ok(_) => panic!("expected DeviceBackend, got a resolved driver plan"),
        }
    }
}

#[test]
fn emitted_document_serde_roundtrips_through_dcs_model() {
    let model = coupler().build().unwrap();
    let json = serde_json::to_string_pretty(&model).unwrap();
    let reloaded = PlantModel::load(&json).unwrap();
    assert_eq!(reloaded, model);
    assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
}

#[test]
#[should_panic(expected = "not \"ethercat\"")]
fn ethercat_channels_reject_a_non_ethercat_device() {
    let mut plant = PlantBuilder::new();
    let sim = plant.device("sim").id;
    plant.ethercat_input::<bool>(sim, "di0", ImageOffset::Bit { byte: 0, bit: 0 });
}

#[test]
#[should_panic(expected = "cannot take offset")]
fn a_bool_channel_rejects_a_field_offset() {
    let mut plant = PlantBuilder::new();
    let device = plant.ethercat(EthercatSpec::new(
        "ecat0",
        StationIdentity {
            vendor: 21,
            product: 750354,
            revision: 1,
        },
        3,
    ));
    plant.ethercat_input::<bool>(device, "di0", ImageOffset::Field { byte: 0, bits: 8 });
}
