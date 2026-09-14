//! Serde types for the versioned plant model document.
//!
//! The schema implements the recorded serialization decision: JSON with an
//! explicit top-level `version` field. [`PlantModel::load`] accepts only
//! [`MODEL_VERSION`]; a future schema bump inserts a migration step between
//! parsing and validation.

use crate::validate::ValidationError;
use dcs_core::{PointId, SignalId, Value, ValueKind};
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
/// For channels and points, `In` carries a value from the field into the
/// controller and `Out` carries one from the controller to the field. For
/// ports, `In` consumes the value a connection delivers and `Out` produces
/// the value a connection carries away. Within a [`Connection`], an `In`
/// point or an `Out` port can only be a `from` end, and an `Out` point or an
/// `In` port only a `to` end.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Field-to-controller for channels and points; connection-to-component
    /// for ports.
    In,
    /// Controller-to-field for channels and points; component-to-connection
    /// for ports.
    Out,
}

impl fmt::Display for Direction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Direction::In => "in",
            Direction::Out => "out",
        })
    }
}

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
}
