//! The `ethercat` device kind's assembly contract: a hardware-bound
//! field-bus declaration is validated by the kind's factory — the
//! `hardware` marker, the `dcs-ethercat` parameter grammar — and then
//! fails startup because no EtherCAT master exists in this build.
//! Malformed declarations surface as named [`AssemblyError`] variants
//! before any scan, and a hardware-bound kind is never silently
//! substituted by simulation.

use dcs_assembly::{AssemblyError, DriverRegistry, resolve_drivers};
use dcs_model::{DeviceId, PlantModel};

/// The reference declaration: a coupler carrying 2×DI + 1×AI + 2×DO
/// with the full field-bus contract — logical bus, station identity,
/// image mapping, miss threshold, safe outputs, startup policy.
const ETHERCAT: &str = include_str!("../fixtures/ethercat.json");

/// Parses `fixture` and resolves its drivers through the standard
/// registry — the half of assembly where device-kind factories run.
fn resolve(fixture: &str) -> Result<dcs_assembly::DriverPlan, AssemblyError> {
    let model = PlantModel::load(fixture).unwrap();
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    resolve_drivers(&model, &DriverRegistry::standard())
}

/// `fixture` with `device[0].parameters[key]` replaced by `value`.
fn with_parameter(key: &str, value: serde_json::Value) -> String {
    let mut document: serde_json::Value = serde_json::from_str(ETHERCAT).unwrap();
    document["devices"][0]["parameters"][key.to_string()] = value;
    document.to_string()
}

/// `fixture` with `device[0].parameters` absent and `hardware` set.
fn without_hardware() -> String {
    let mut document: serde_json::Value = serde_json::from_str(ETHERCAT).unwrap();
    document["devices"][0]
        .as_object_mut()
        .unwrap()
        .remove("hardware");
    document.to_string()
}

fn assert_invalid_parameters(fixture: &str, needle: &str) {
    match resolve(fixture) {
        Err(AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        }) => {
            assert_eq!(device, DeviceId(1));
            assert_eq!(kind, "ethercat");
            assert!(detail.contains(needle), "{detail:?} lacks {needle:?}");
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("expected InvalidDeviceParameters, got a resolved driver plan"),
    }
}

#[test]
fn valid_declaration_fails_at_the_backend_not_the_grammar() {
    // The declaration parses; startup then fails because no EtherCAT
    // master can initialize the bus — the hard failure the startup
    // policy requires, never a silent simulated stand-in.
    match resolve(ETHERCAT) {
        Err(AssemblyError::DeviceBackend {
            device,
            kind,
            detail,
        }) => {
            assert_eq!(device, DeviceId(1));
            assert_eq!(kind, "ethercat");
            assert!(detail.contains("\"ecat0\""), "{detail}");
            assert!(detail.contains("never silently substituted"), "{detail}");
        }
        Err(other) => panic!("expected DeviceBackend, got {other:?}"),
        Ok(_) => panic!("expected DeviceBackend, got a resolved driver plan"),
    }
}

#[test]
fn hardware_marker_is_required_for_the_hardware_bound_kind() {
    assert_invalid_parameters(&without_hardware(), "\"hardware\": true");
    // And `hardware: false` is not a substitute for omitting it.
    let mut document: serde_json::Value = serde_json::from_str(ETHERCAT).unwrap();
    document["devices"][0]["hardware"] = serde_json::json!(false);
    assert_invalid_parameters(&document.to_string(), "\"hardware\": true");
}

#[test]
fn malformed_declarations_are_named_parameter_errors() {
    for (fixture, needle) in [
        (
            include_str!("../fixtures/invalid/ethercat_missing_bus.json"),
            "\"bus\" is required",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_bad_identity.json"),
            "\"revision\" must be a non-negative integer",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_offset_collision.json"),
            "overlap",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_wrong_image.json"),
            "places Out channel \"do0\"",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_unmapped_channel.json"),
            "does not place declared channel \"di1\"",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_bad_startup.json"),
            "admits \"fail\" only",
        ),
        (
            include_str!("../fixtures/invalid/ethercat_missing_safe_output.json"),
            "safe state for out channel \"do1\"",
        ),
    ] {
        assert_invalid_parameters(fixture, needle);
    }
}

#[test]
fn a_host_interface_parameter_is_rejected() {
    // The logical `bus` name is the model's only bus field; a host
    // interface name is deployment configuration the document must not
    // carry.
    assert_invalid_parameters(
        &with_parameter("interface", serde_json::json!("eth0")),
        "unknown parameter \"interface\"",
    );
}

#[test]
fn a_simulated_kind_rejects_the_hardware_marker() {
    // The marker stays honest in both directions: `hardware: true` on a
    // `sim` device is a parameter error, so a hardware declaration
    // cannot drift onto a simulated backend silently.
    let fixture = include_str!("../fixtures/invalid/hardware_marked_sim.json");
    match resolve(fixture) {
        Err(AssemblyError::InvalidDeviceParameters {
            device,
            kind,
            detail,
        }) => {
            assert_eq!(device, DeviceId(1));
            assert_eq!(kind, "sim");
            assert!(detail.contains("hardware"), "{detail:?}");
        }
        Err(other) => panic!("expected InvalidDeviceParameters, got {other:?}"),
        Ok(_) => panic!("expected InvalidDeviceParameters, got a resolved driver plan"),
    }
}

#[test]
fn the_ethercat_parameters_never_name_a_host_interface() {
    // The declared vocabulary's only bus-related key is the logical
    // `bus` name — pinning that the parameter set admits no host
    // interface field at all.
    let model = PlantModel::load(ETHERCAT).unwrap();
    let keys: Vec<&String> = model.devices[0].parameters.keys().collect();
    assert_eq!(
        keys,
        [
            "bus",
            "exchange_miss_threshold",
            "identity",
            "mapping",
            "safe_outputs",
            "startup"
        ]
    );
    assert_eq!(
        model.devices[0].parameters["bus"],
        serde_json::json!("ecat0")
    );
}
