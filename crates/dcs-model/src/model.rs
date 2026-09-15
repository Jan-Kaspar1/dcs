//! Serde types for the versioned plant model document.
//!
//! The schema implements the recorded serialization decision: JSON with an
//! explicit top-level `version` field. [`PlantModel::load`] accepts only
//! [`MODEL_VERSION`]; a future schema bump inserts a migration step between
//! parsing and validation.

use crate::validate::ValidationError;
use dcs_core::{ModelFingerprint, PointId, SignalId, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;

/// The document version this implementation reads and writes.
pub const MODEL_VERSION: u32 = 1;

/// Identifies a field or simulated I/O device in the plant model.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct DeviceId(pub u64);

/// Identifies a component instance in the plant model.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct ComponentId(pub u64);

/// Data-flow direction of a channel, logical I/O point, or component port.
///
/// The model shares [`dcs_core::Direction`] rather than declaring its own —
/// the same vocabulary names direction in component I/O declarations, the
/// driver's point map, and this document. For channels and points, `In`
/// carries a value from the field into the controller and `Out` carries one
/// from the controller to the field. For ports, `In` consumes the value a
/// connection delivers and `Out` produces the value a connection carries
/// away. Within a [`Connection`], an `In` point or an `Out` port can only
/// be a `from` end, and an `Out` point or an `In` port only a `to` end.
pub use dcs_core::Direction;

/// A field or simulated I/O device: `kind` names its driver-side device
/// integration and `channels` the physical endpoints it exposes.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Device {
    /// Unique device identifier.
    pub id: DeviceId,
    /// Device kind, resolved by the driver layer; opaque to the model.
    pub kind: String,
    /// The device's channels, keyed by channel name.
    pub channels: BTreeMap<String, Channel>,
    /// Kind-specific addressing and configuration parameters for the
    /// device's driver integration — e.g. a remote endpoint. The model
    /// treats them as opaque; the registered device-kind factory
    /// validates them at assembly.
    ///
    /// Unlike component parameters these are general JSON values rather
    /// than signal [`Value`]s, so a kind can carry strings and structured
    /// addressing data. Optional like [`Signal::unit`]; see its note on
    /// schema versioning.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub parameters: BTreeMap<String, serde_json::Value>,
}

/// A named device channel: a physical endpoint with a fixed direction and
/// value type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Channel {
    /// Whether the channel reads from (`In`) or writes to (`Out`) the field.
    pub direction: Direction,
    /// The channel's value type.
    pub value_type: ValueKind,
}

/// A reference to one named channel on one device.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ChannelRef {
    /// The device owning the channel.
    pub device: DeviceId,
    /// The channel's name on that device.
    pub name: String,
}

/// `serde` helper for `IoPoint`'s `writable`/`journaled` flags: they
/// follow the optional-field convention — documents that predate them
/// deserialize as `false`, and `false` serializes back without the key.
fn is_false(flag: &bool) -> bool {
    !*flag
}

/// A logical I/O point: the unit control logic binds to.
///
/// [`PointId`], direction, and value type form the component-facing
/// contract. `channel` binds the point to a physical device channel, which
/// must exist and agree with the point's direction and type. A point
/// declared without `channel` is an *internal point* carried by the
/// controller's scan image rather than field I/O: an internal `In` point
/// holds `initial` until a command writes it through the command path, an
/// internal `Out` point records component writes for monitoring, and a
/// declared internal `In`/`Out` pair can carry a port-to-port or
/// point-to-point wire inside the image.
///
/// `writable` marks the point an operator may write through the receipt-
/// answered command path — the model-declared command surface. Only `In`
/// points can be writable: a writable field `In` point's command write is
/// forwarded to the driver at the scan boundary and the same scan's input
/// phase reads it back — documented operator substitution of the input
/// image, holding until the field side asserts a different value — while a
/// writable internal `In` point holds the written value until the next
/// command and is the common target for operator values like setpoints.
/// `Out` points are never command targets: the command path refuses them
/// outright, so validation rejects `writable` on an `Out` point — operator
/// influence on an output is engineered through components, not raw point
/// writes.
///
/// `stale_after_ticks` declares a freshness budget on a field `In`
/// point: the executor's input phase compares the tick the driver's
/// returned sample carries against the scan tick and lands the image
/// sample `Quality::Uncertain(QualityReason::Stale)` once the lag exceeds
/// the budget. Only field inputs can declare one — the check reads the
/// driver, so validation rejects the field on an `Out` point and on a
/// channel-less internal point, which is never driver-read.
///
/// `journaled` declares the point's observed value transitions part of
/// the durable transition journal: the recorder appends a
/// `point_changed` entry carrying the previous and new values at the
/// producing scan's tick. The flag is opt-in per point and valid on
/// `Bool`/`Int` points of either direction — the lifecycle and
/// managed-state status points (`alarm`, `unacknowledged`, `shelved`,
/// `suppressed`, `out_of_service`), mode changes, and protection-layer
/// states the record exists for — so validation rejects it on a `Float`
/// point: a continuously moving measurement belongs to the volatile
/// history ring, not the low-volume durable record, and an operator's
/// `Float` write is already durable in its attributed settled receipt.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IoPoint {
    /// Unique point identifier.
    pub id: PointId,
    /// `In` points are read by the controller; `Out` points are written.
    pub direction: Direction,
    /// The point's value type; drivers reject mismatched writes.
    pub value_type: ValueKind,
    /// The device channel backing this point, or `None` for an internal
    /// point.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning.
    /// An internal point is declared by omitting `channel` and supplying
    /// `initial`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub channel: Option<ChannelRef>,
    /// The value an internal point is seeded with; its variant must match
    /// `value_type`. Required when `channel` is absent — the scan image
    /// holds it until the point is written — and rejected when `channel`
    /// is present, because the field owns a bound point's value.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub initial: Option<Value>,
    /// Whether the point accepts operator `WriteValue` commands; see the
    /// type docs for the per-kind semantics. Valid only on `In` points —
    /// [`PlantModel::validate`](crate::PlantModel::validate) reports
    /// `writable` on an `Out` point.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning:
    /// documents predating the flag load with `writable` unset.
    #[serde(default, skip_serializing_if = "is_false")]
    pub writable: bool,
    /// The point's freshness budget in ticks, if declared: how far the
    /// driver-stamped tick on a returned sample may lag the scan tick
    /// before the image sample lands `Uncertain(Stale)` — `0` demands a
    /// sample stamped this scan. Valid only on field `In` points —
    /// [`PlantModel::validate`](crate::PlantModel::validate) reports the
    /// field on an `Out` point or a channel-less internal point.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stale_after_ticks: Option<u64>,
    /// Whether the point's observed value transitions join the durable
    /// journal as `point_changed` entries; see the type docs. Valid on
    /// `Bool`/`Int` points of either direction —
    /// [`PlantModel::validate`](crate::PlantModel::validate) reports the
    /// flag on a `Float` point.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning:
    /// documents predating the flag load with `journaled` unset.
    #[serde(default, skip_serializing_if = "is_false")]
    pub journaled: bool,
}

impl IoPoint {
    /// Whether this point is image-carried — declared without a device
    /// channel — rather than bound to field I/O.
    pub fn is_internal(&self) -> bool {
        self.channel.is_none()
    }
}

/// A plant signal: the monitoring/UI-facing name for the value carried by
/// one logical I/O point.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Signal {
    /// Unique signal identifier.
    pub id: SignalId,
    /// Human-facing signal name.
    pub name: String,
    /// The I/O point carrying this signal's value.
    pub source: PointId,
    /// Engineering unit of the carried value, e.g. `"degC"`.
    ///
    /// `unit`, `description`, and `group` extend the version-1 schema as
    /// optional fields: documents written before they existed deserialize
    /// them as `None`, so the format evolves without a version bump or
    /// migration.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    /// Human-facing description of the signal.
    ///
    /// Optional like [`Signal::unit`]; see its note on schema versioning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    /// Display group the monitoring UI files this signal under, e.g. the
    /// plant area or unit it belongs to.
    ///
    /// Pure display metadata: validation imposes no wiring rules on it —
    /// any string is a valid group and signals sharing a group name are
    /// simply listed together. Optional like [`Signal::unit`]; see its
    /// note on schema versioning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group: Option<String>,
}

/// An instantiation of a reusable component kind.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentInstance {
    /// Unique instance identifier.
    pub id: ComponentId,
    /// Component kind, resolved by the runtime's component library; the model
    /// treats it as an opaque name.
    pub kind: String,
    /// Kind-specific parameters, keyed by parameter name.
    pub parameters: BTreeMap<String, Value>,
    /// The instance's ports, keyed by port name. These declare the signature
    /// the model wires; the runtime checks it against the component kind's
    /// definition.
    pub ports: BTreeMap<String, Port>,
}

/// A named component port with a fixed direction and value type.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Port {
    /// `In` ports consume a connection's value; `Out` ports produce one.
    pub direction: Direction,
    /// The port's value type.
    pub value_type: ValueKind,
}

/// A reference to one named port on one component instance.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PortRef {
    /// The component instance owning the port.
    pub component: ComponentId,
    /// The port's name on that instance.
    pub name: String,
}

/// One end of a [`Connection`]: a logical I/O point or a named component
/// port.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Endpoint {
    /// A logical I/O point by id. `In` points may only appear as `from` ends
    /// and `Out` points only as `to` ends.
    Point(PointId),
    /// A named port on a component instance. `Out` ports may only appear as
    /// `from` ends and `In` ports only as `to` ends.
    Port(PortRef),
}

/// A wire between two endpoints: `from` must produce a value, `to` must
/// consume one, and both ends must carry the same value type.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Connection {
    /// The producing end: an `In` point or an `Out` port.
    pub from: Endpoint,
    /// The consuming end: an `Out` point or an `In` port.
    pub to: Endpoint,
}

/// A versioned plant model document: the single contract shared by
/// engineering data, controllers, and the monitoring UI.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PlantModel {
    /// Document schema version. [`PlantModel::load`] accepts only
    /// [`MODEL_VERSION`].
    pub version: u32,
    /// Declared field/simulated devices.
    pub devices: Vec<Device>,
    /// Declared logical I/O points.
    pub io_points: Vec<IoPoint>,
    /// Declared plant signals.
    pub signals: Vec<Signal>,
    /// Instantiated components.
    pub components: Vec<ComponentInstance>,
    /// Wires between points and component ports.
    pub connections: Vec<Connection>,
}

/// Failure to [`PlantModel::load`] a document.
#[derive(Debug)]
pub enum LoadError {
    /// The document is not well-formed JSON or does not match the schema.
    Malformed(serde_json::Error),
    /// The document's `version` is not [`MODEL_VERSION`].
    UnsupportedVersion {
        /// The version the document declares.
        found: u32,
        /// The version this implementation accepts.
        supported: u32,
    },
    /// The document parsed but failed validation; carries every error found.
    Invalid(Vec<ValidationError>),
}

impl fmt::Display for LoadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Malformed(error) => write!(f, "malformed model document: {error}"),
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "unsupported model version {found} (this build accepts {supported})"
            ),
            Self::Invalid(errors) => {
                write!(f, "invalid plant model ({} error(s)):", errors.len())?;
                for error in errors {
                    write!(f, " {error};")?;
                }
                Ok(())
            }
        }
    }
}

impl std::error::Error for LoadError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Malformed(error) => Some(error),
            _ => None,
        }
    }
}

impl PlantModel {
    /// Parses, version-checks, and validates a JSON model document.
    ///
    /// Returns [`LoadError::Malformed`] for documents that are not well-formed
    /// JSON or do not match the schema, [`LoadError::UnsupportedVersion`] for
    /// a `version` other than [`MODEL_VERSION`], and
    /// [`LoadError::Invalid`] carrying every validation error found.
    pub fn load(source: &str) -> Result<Self, LoadError> {
        let model: Self = serde_json::from_str(source).map_err(LoadError::Malformed)?;
        if model.version != MODEL_VERSION {
            return Err(LoadError::UnsupportedVersion {
                found: model.version,
                supported: MODEL_VERSION,
            });
        }
        let errors = model.validate();
        if errors.is_empty() {
            Ok(model)
        } else {
            Err(LoadError::Invalid(errors))
        }
    }

    /// The model's [`ModelFingerprint`]: a deterministic hash over the
    /// canonical document.
    ///
    /// The canonical document is this value's own serialization —
    /// reserializing the parsed model, not hashing the source text, is
    /// what makes the fingerprint stable across inessential document
    /// differences: struct fields serialize in declaration order and
    /// every map is ordered, so two documents that parse to the same
    /// model fingerprint identically regardless of their key order or
    /// whitespace, while any semantic difference — including one the
    /// component set does not see, like a renamed signal — fingerprints
    /// differently. The assembling layer stamps it into checkpoints so a
    /// standby can verify it tracks a run of the same model.
    pub fn fingerprint(&self) -> ModelFingerprint {
        ModelFingerprint::of(
            &serde_json::to_vec(self).expect("serializing a parsed model cannot fail"),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = include_str!("../fixtures/minimal.json");
    const SIGNAL_GROUPS: &str = include_str!("../fixtures/signal_groups.json");

    #[test]
    fn minimal_fixture_loads() {
        let model = PlantModel::load(MINIMAL).unwrap();
        assert_eq!(model.version, MODEL_VERSION);
        assert_eq!(model.devices.len(), 2);
        assert_eq!(model.io_points.len(), 2);
        assert_eq!(model.signals.len(), 1);
        assert_eq!(model.components.len(), 1);
        assert_eq!(model.connections.len(), 2);
    }

    #[test]
    fn loaded_model_serde_roundtrips() {
        let model = PlantModel::load(MINIMAL).unwrap();
        let json = serde_json::to_string_pretty(&model).unwrap();
        // The shared `dcs_core::Direction` keeps the snake_case wire shape
        // model documents have always carried.
        assert!(json.contains("\"direction\": \"in\""), "{json}");
        assert!(json.contains("\"direction\": \"out\""), "{json}");
        let reloaded = PlantModel::load(&json).unwrap();
        assert_eq!(model, reloaded);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }

    #[test]
    fn grouped_fixture_loads_validates_and_roundtrips() {
        let model = PlantModel::load(SIGNAL_GROUPS).unwrap();
        // Grouped signals carry their display group; the ungrouped one
        // parses to `None`.
        assert_eq!(model.signals[0].group.as_deref(), Some("reactor"));
        assert_eq!(model.signals[1].group.as_deref(), Some("reactor"));
        assert_eq!(model.signals[2].group, None);
        assert_eq!(model.signals[3].group.as_deref(), Some("utilities"));

        let json = serde_json::to_string_pretty(&model).unwrap();
        assert!(json.contains("\"group\": \"reactor\""), "{json}");
        let reloaded = PlantModel::load(&json).unwrap();
        assert_eq!(model, reloaded);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }

    #[test]
    fn documents_predating_group_load_unchanged() {
        // Signals without the optional field deserialize `group` as
        // `None`, and `None` serializes back without the key.
        let model = PlantModel::load(MINIMAL).unwrap();
        assert_eq!(model.signals[0].group, None);
        let json = serde_json::to_string(&model).unwrap();
        assert!(!json.contains("\"group\""), "{json}");
    }

    #[test]
    fn documents_predating_writable_default_to_unwritable() {
        // Points without the optional field deserialize `writable` as
        // `false`, and `false` serializes back without the key.
        let model = PlantModel::load(MINIMAL).unwrap();
        assert!(model.io_points.iter().all(|point| !point.writable));
        let json = serde_json::to_string(&model).unwrap();
        assert!(!json.contains("\"writable\""), "{json}");
    }

    #[test]
    fn writable_flag_parses_and_roundtrips() {
        // A writable internal point: declare one by dropping the channel
        // and carrying an initial value.
        let mut model = PlantModel::load(MINIMAL).unwrap();
        model.io_points[0].channel = None;
        model.io_points[0].initial = Some(Value::Float(25.0));
        model.io_points[0].writable = true;
        let json = serde_json::to_string_pretty(&model).unwrap();
        assert!(json.contains("\"writable\": true"), "{json}");

        let reloaded = PlantModel::load(&json).unwrap();
        assert!(reloaded.io_points[0].writable);
        assert!(!reloaded.io_points[1].writable);
        assert_eq!(reloaded, model);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }

    #[test]
    fn writable_out_point_is_rejected_by_load() {
        let mut model = PlantModel::load(MINIMAL).unwrap();
        model.io_points[1].writable = true;
        let json = serde_json::to_string(&model).unwrap();
        match PlantModel::load(&json) {
            Err(LoadError::Invalid(errors)) => assert!(
                errors.contains(&ValidationError::WritableOut { point: PointId(11) }),
                "{errors:?}"
            ),
            other => panic!("expected invalid model, got {other:?}"),
        }
    }

    #[test]
    fn documents_predating_stale_after_ticks_load_unchanged() {
        // Points without the optional field deserialize
        // `stale_after_ticks` as `None`, and `None` serializes back
        // without the key.
        let model = PlantModel::load(MINIMAL).unwrap();
        assert!(
            model
                .io_points
                .iter()
                .all(|point| point.stale_after_ticks.is_none())
        );
        let json = serde_json::to_string(&model).unwrap();
        assert!(!json.contains("\"stale_after_ticks\""), "{json}");
    }

    #[test]
    fn stale_after_ticks_parses_and_roundtrips() {
        let mut model = PlantModel::load(MINIMAL).unwrap();
        model.io_points[0].stale_after_ticks = Some(3);
        let json = serde_json::to_string_pretty(&model).unwrap();
        assert!(json.contains("\"stale_after_ticks\": 3"), "{json}");

        let reloaded = PlantModel::load(&json).unwrap();
        assert_eq!(reloaded.io_points[0].stale_after_ticks, Some(3));
        assert_eq!(reloaded.io_points[1].stale_after_ticks, None);
        assert_eq!(reloaded, model);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }

    #[test]
    fn documents_predating_journaled_load_unchanged() {
        // Points without the optional field deserialize `journaled` as
        // `false`, and `false` serializes back without the key.
        let model = PlantModel::load(MINIMAL).unwrap();
        assert!(model.io_points.iter().all(|point| !point.journaled));
        let json = serde_json::to_string(&model).unwrap();
        assert!(!json.contains("\"journaled\""), "{json}");
    }

    #[test]
    fn journaled_flag_parses_and_roundtrips() {
        // `journaled` marks `Bool`/`Int` points of either direction: a
        // journaled internal `Out` status point is the lifecycle shape —
        // declare one by dropping the channel, carrying an initial, and
        // matching the wired port's kind.
        let mut model = PlantModel::load(MINIMAL).unwrap();
        model.io_points[1].channel = None;
        model.io_points[1].initial = Some(Value::Bool(false));
        model.io_points[1].value_type = ValueKind::Bool;
        model.io_points[1].journaled = true;
        model.components[0].ports.get_mut("out").unwrap().value_type = ValueKind::Bool;
        let json = serde_json::to_string_pretty(&model).unwrap();
        assert!(json.contains("\"journaled\": true"), "{json}");

        let reloaded = PlantModel::load(&json).unwrap();
        assert!(!reloaded.io_points[0].journaled);
        assert!(reloaded.io_points[1].journaled);
        assert_eq!(reloaded, model);
        assert_eq!(serde_json::to_string_pretty(&reloaded).unwrap(), json);
    }

    #[test]
    fn journaled_float_point_is_rejected_by_load() {
        let mut model = PlantModel::load(MINIMAL).unwrap();
        model.io_points[0].journaled = true;
        let json = serde_json::to_string(&model).unwrap();
        match PlantModel::load(&json) {
            Err(LoadError::Invalid(errors)) => assert!(
                errors.contains(&ValidationError::JournaledFloat { point: PointId(10) }),
                "{errors:?}"
            ),
            other => panic!("expected invalid model, got {other:?}"),
        }
    }

    #[test]
    fn unsupported_version_is_rejected() {
        let document = MINIMAL.replace("\"version\": 1", "\"version\": 2");
        match PlantModel::load(&document) {
            Err(LoadError::UnsupportedVersion { found, supported }) => {
                assert_eq!(found, 2);
                assert_eq!(supported, MODEL_VERSION);
            }
            other => panic!("expected unsupported version, got {other:?}"),
        }
    }

    #[test]
    fn malformed_and_unversioned_documents_are_rejected() {
        assert!(matches!(
            PlantModel::load("{ not json"),
            Err(LoadError::Malformed(_))
        ));
        // A document without a version field is malformed, not version 0.
        assert!(matches!(
            PlantModel::load("{}"),
            Err(LoadError::Malformed(_))
        ));
    }

    #[test]
    fn fingerprint_is_stable_across_serialization_ordering() {
        let model = PlantModel::load(MINIMAL).unwrap();
        let fingerprint = model.fingerprint();

        // The same model round-tripped through differently ordered or
        // laid-out text fingerprints identically: the hash covers the
        // canonical reserialization, not the source bytes.
        let pretty = serde_json::to_string_pretty(&model).unwrap();
        let compact = serde_json::to_string(&model).unwrap();
        assert_ne!(pretty, compact);
        assert_eq!(
            PlantModel::load(&pretty).unwrap().fingerprint(),
            fingerprint
        );

        // Reordered object keys — top-level and nested — parse to the
        // same model and so carry the same fingerprint.
        let reordered: serde_json::Value = serde_json::from_str::<serde_json::Value>(MINIMAL)
            .map(|mut document| {
                let object = document.as_object_mut().unwrap();
                let mut reversed = serde_json::Map::new();
                for (key, value) in object.iter().rev() {
                    reversed.insert(key.clone(), value.clone());
                }
                *object = reversed;
                for device in document["devices"].as_array_mut().unwrap() {
                    let channels = device["channels"].as_object().unwrap().clone();
                    let mut swapped = serde_json::Map::new();
                    swapped.insert("channels".to_string(), channels.into());
                    swapped.insert("kind".to_string(), device["kind"].clone());
                    swapped.insert("id".to_string(), device["id"].clone());
                    *device.as_object_mut().unwrap() = swapped;
                }
                document
            })
            .unwrap();
        let reparsed = PlantModel::load(&reordered.to_string()).unwrap();
        assert_eq!(reparsed, model);
        assert_eq!(reparsed.fingerprint(), fingerprint);
    }

    #[test]
    fn fingerprint_distinguishes_models_sharing_a_component_set() {
        // Two models differing only where the component set does not
        // look — a renamed signal — still fingerprint differently: the
        // hash covers the whole canonical document, so a standby
        // rejects a checkpoint from the other model rather than
        // converging on a structurally matching component set.
        let mut other = PlantModel::load(MINIMAL).unwrap();
        other.signals[0].name = "renamed".to_string();
        assert_eq!(
            other.components,
            PlantModel::load(MINIMAL).unwrap().components
        );
        assert_ne!(
            other.fingerprint(),
            PlantModel::load(MINIMAL).unwrap().fingerprint()
        );
    }
}
