//! The structured failures model-driven assembly reports.
//!
//! Every [`AssemblyError`] variant names the offending model element — a
//! device, component instance, port, or point — so diagnostics can point at
//! what to fix rather than at a byte offset.

use dcs_core::{Direction, PointId, ValueKind};
use dcs_model::{ComponentId, DeviceId};
use dcs_runtime::WiringError;
use dcs_sim::ConfigError;
use std::fmt;

/// Why a registered component constructor could not build its instance.
///
/// Constructors receive the resolved [`ComponentSpec`](crate::ComponentSpec)
/// and report [`BuildError::UnboundPort`] for a port the model does not wire
/// and [`BuildError::Other`] for kind-local failures such as a missing or
/// invalid parameter.
#[derive(Debug)]
pub enum BuildError {
    /// The kind requires a port the model leaves unbound.
    UnboundPort {
        /// The unbound port's name.
        port: String,
    },
    /// Construction failed for a kind-local reason, e.g. a missing or
    /// invalid parameter.
    Other(Box<dyn std::error::Error>),
}

impl BuildError {
    /// Wraps an arbitrary construction failure.
    pub fn other(error: impl std::error::Error + 'static) -> Self {
        Self::Other(Box::new(error))
    }
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnboundPort { port } => write!(f, "port {port:?} is not bound"),
            Self::Other(error) => error.fmt(f),
        }
    }
}

impl std::error::Error for BuildError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::UnboundPort { .. } => None,
            Self::Other(error) => Some(error.as_ref()),
        }
    }
}

/// Why a validated [`PlantModel`](dcs_model::PlantModel) could not be
/// assembled into the driver surface and the executor.
#[derive(Debug, Clone, PartialEq)]
pub enum AssemblyError {
    /// A device declares a kind no registered driver factory serves.
    UnknownDeviceKind {
        /// The offending device.
        device: DeviceId,
        /// The kind it declares.
        kind: String,
    },
    /// A device's registered factory rejected its kind-specific
    /// parameters — e.g. a `sim-tcp` device whose `address` is missing
    /// or malformed.
    InvalidDeviceParameters {
        /// The offending device.
        device: DeviceId,
        /// The kind it declares.
        kind: String,
        /// What the parameters violate.
        detail: String,
    },
    /// The backend behind a device could not be built — e.g. a `sim-tcp`
    /// endpoint that refused the connection or does not serve a declared
    /// point.
    DeviceBackend {
        /// The offending device.
        device: DeviceId,
        /// The kind it declares.
        kind: String,
        /// The backend failure, formatted.
        detail: String,
    },
    /// The resolved local simulated channel map is internally inconsistent.
    InvalidChannelMap {
        /// The underlying channel-map error.
        detail: ConfigError,
    },
    /// A component instance's `kind` has no registered constructor.
    UnknownComponentKind {
        /// The offending instance.
        component: ComponentId,
        /// The unregistered kind.
        kind: String,
    },
    /// Two connections bind the same port.
    PortBoundTwice {
        /// The instance owning the port.
        component: ComponentId,
        /// The contested port.
        port: String,
    },
    /// A port has no binding: declared on the instance but left unwired, or
    /// required by the kind but absent from the resolved bindings.
    UnboundPort {
        /// The instance owning the port.
        component: ComponentId,
        /// The unbound port.
        port: String,
    },
    /// A component's declared logical I/O names a point the driver map does
    /// not serve — neither an `io_point` nor a synthesized internal point.
    UnmappedPoint {
        /// The instance owning the requirement.
        component: ComponentId,
        /// The requirement's name within the component.
        port: String,
        /// The point the map does not serve.
        point: PointId,
    },
    /// A bound point's direction differs from the requirement's declared
    /// direction.
    DirectionMismatch {
        /// The instance owning the requirement.
        component: ComponentId,
        /// The requirement's name within the component.
        port: String,
        /// The mismatched point.
        point: PointId,
        /// The direction the component declared.
        declared: Direction,
        /// The direction the bound point carries.
        mapped: Direction,
    },
    /// A bound point's value kind differs from the requirement's declared
    /// value kind.
    TypeMismatch {
        /// The instance owning the requirement.
        component: ComponentId,
        /// The requirement's name within the component.
        port: String,
        /// The mismatched point.
        point: PointId,
        /// The value kind the component declared.
        declared: ValueKind,
        /// The value kind the bound point carries.
        mapped: ValueKind,
    },
    /// A registered constructor rejected the instance.
    Component {
        /// The instance that failed to build.
        component: ComponentId,
        /// Its component kind.
        kind: String,
        /// The constructor's failure, formatted.
        detail: String,
    },
    /// A port-to-port connection references a component or port the model
    /// does not declare; reachable only for a model assembled without
    /// validation.
    UnresolvedEndpoint {
        /// The offending connection's index in `connections`.
        connection: usize,
    },
    /// An `io_point` binds a device the model does not declare, so no
    /// backend can serve it — reachable only for a model assembled
    /// without validation.
    UnroutedPoint {
        /// The offending point.
        point: PointId,
        /// The undeclared device it names.
        device: DeviceId,
    },
    /// The executor's own wiring check rejected the assembled component set.
    Wiring {
        /// The executor's wiring error.
        detail: WiringError,
    },
}

impl fmt::Display for AssemblyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownDeviceKind { device, kind } => write!(
                f,
                "device {} has kind {kind:?}, which no registered driver factory serves",
                device.0
            ),
            Self::InvalidDeviceParameters {
                device,
                kind,
                detail,
            } => write!(
                f,
                "device {} of kind {kind:?} has invalid parameters: {detail}",
                device.0
            ),
            Self::DeviceBackend {
                device,
                kind,
                detail,
            } => write!(
                f,
                "device {} of kind {kind:?} has an unusable backend: {detail}",
                device.0
            ),
            Self::InvalidChannelMap { detail } => {
                write!(f, "simulated channel map is inconsistent: {detail}")
            }
            Self::UnknownComponentKind { component, kind } => write!(
                f,
                "component {} has kind {kind:?}, which no registered constructor serves",
                component.0
            ),
            Self::PortBoundTwice { component, port } => write!(
                f,
                "component {} port {port:?} is bound by more than one connection",
                component.0
            ),
            Self::UnboundPort { component, port } => write!(
                f,
                "component {} port {port:?} is not bound to an io point",
                component.0
            ),
            Self::UnmappedPoint {
                component,
                port,
                point,
            } => write!(
                f,
                "component {} requirement {port:?} binds io point {} the driver map does not serve",
                component.0, point.0
            ),
            Self::DirectionMismatch {
                component,
                port,
                point,
                declared,
                mapped,
            } => write!(
                f,
                "component {} requirement {port:?} declares io point {} as {declared} but the point is {mapped}",
                component.0, point.0
            ),
            Self::TypeMismatch {
                component,
                port,
                point,
                declared,
                mapped,
            } => write!(
                f,
                "component {} requirement {port:?} declares io point {} as {declared:?} but the point is {mapped:?}",
                component.0, point.0
            ),
            Self::Component {
                component,
                kind,
                detail,
            } => write!(
                f,
                "component {} of kind {kind:?} failed to build: {detail}",
                component.0
            ),
            Self::UnresolvedEndpoint { connection } => write!(
                f,
                "connection {connection} references a port the model does not declare"
            ),
            Self::UnroutedPoint { point, device } => write!(
                f,
                "io point {} binds a channel on device {} the model does not declare",
                point.0, device.0
            ),
            Self::Wiring { detail } => write!(f, "executor wiring failed: {detail}"),
        }
    }
}

impl std::error::Error for AssemblyError {}
