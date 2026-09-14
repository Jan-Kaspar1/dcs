//! The model → runtime translation: point map, port bindings, wiring
//! topology, and component construction.
//!
//! [`resolve`] walks a validated [`PlantModel`] once and derives
//! everything the executor needs plus the wiring topology the driver
//! side resolves against a [`DriverRegistry`](crate::DriverRegistry):
//! [`resolve_drivers`](crate::resolve_drivers) turns it into a
//! [`DriverPlan`](crate::DriverPlan), so the point map the executor
//! checks against always matches the channels the drivers serve.

use crate::error::{AssemblyError, BuildError};
use crate::registry::{ComponentRegistry, ComponentSpec};
use dcs_core::{Direction, IoDriver, PointId, Value, ValueKind};
use dcs_model::{ComponentId, Direction as ModelDirection, Endpoint, PlantModel, PortRef};
use dcs_runtime::{Component, Executor, PointMap};
use dcs_sim::{
    ChannelId, ChannelMap, Direction as SimDirection, Loopback, PointBinding, SimDriver,
};
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

/// The device-kind prefix the local simulated factory serves: every
/// `sim*` kind — `sim` itself plus role-flavored kinds like `sim-ai` or
/// `sim-ao`, the convention `dcs-demo` established — resolves to local
/// [`SimDriver`] channels. [`DriverRegistry::standard`](crate::DriverRegistry::standard)
/// installs it as a prefix registration; a device of a kind no
/// registered factory serves fails with
/// [`AssemblyError::UnknownDeviceKind`].
pub const SIM_DEVICE_PREFIX: &str = "sim";

/// The [`ChannelId::device`] synthesized internal points report.
const INTERNAL_DEVICE: u64 = u64::MAX;

/// Shared empty port map for instances with no wired ports.
static NO_PORTS: BTreeMap<String, PointId> = BTreeMap::new();

/// A point's value before the first write or element step: the neutral
/// value of its declared kind.
pub(crate) fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

fn core_direction(direction: ModelDirection) -> Direction {
    match direction {
        ModelDirection::In => Direction::In,
        ModelDirection::Out => Direction::Out,
    }
}

pub(crate) fn sim_direction(direction: ModelDirection) -> SimDirection {
    match direction {
        ModelDirection::In => SimDirection::In,
        ModelDirection::Out => SimDirection::Out,
    }
}

/// The declared value kind of the port `port_ref` names, or `None` when the
/// component or port does not exist — impossible for a validated model.
fn port_kind(model: &PlantModel, port_ref: &PortRef) -> Option<ValueKind> {
    model
        .components
        .iter()
        .find(|component| component.id == port_ref.component)?
        .ports
        .get(&port_ref.name)
        .map(|port| port.value_type)
}

/// Records `port`'s binding to `point`; a port bound twice is recorded as
/// [`AssemblyError::PortBoundTwice`]. Binding failures are deferred rather
/// than returned: the driver topology is unaffected by component wiring
/// problems, and [`assemble`] reports the first recorded error before
/// constructing anything.
fn bind(
    bindings: &mut BTreeMap<ComponentId, BTreeMap<String, PointId>>,
    deferred: &mut Option<AssemblyError>,
    port: &PortRef,
    point: PointId,
) {
    match bindings
        .entry(port.component)
        .or_default()
        .entry(port.name.clone())
    {
        Entry::Occupied(_) => {
            deferred.get_or_insert(AssemblyError::PortBoundTwice {
                component: port.component,
                port: port.name.clone(),
            });
        }
        Entry::Vacant(slot) => {
            slot.insert(point);
        }
    }
}

/// The resolved view of a validated model: everything the driver and the
/// executor need, before any component is constructed.
pub(crate) struct Resolved {
    /// The executor-side authority every component's declared I/O is
    /// checked against.
    pub(crate) point_map: PointMap,
    /// Per component instance: port name → bound point.
    pub(crate) bindings: BTreeMap<ComponentId, BTreeMap<String, PointId>>,
    /// The first port-binding failure, if any — deferred so the driver
    /// side can still be built for a model whose component wiring is
    /// broken. [`assemble`] reports it.
    pub(crate) binding_error: Option<AssemblyError>,
    /// The internal point bindings and loopbacks synthesized for
    /// port-to-port wiring. They always join the local simulated map —
    /// they back component-to-component wires, not any declared device —
    /// reporting the reserved [`INTERNAL_DEVICE`] channel namespace.
    pub(crate) internal: ChannelMap,
    /// The point-to-point field wires, in connection order.
    /// [`resolve_drivers`](crate::resolve_drivers) lands each in the
    /// local simulated map when both ends are sim-served and makes it a
    /// fan-out route across backends otherwise.
    pub(crate) wires: Vec<Loopback>,
}

/// Resolves the model's point map and connection wiring — pure, without
/// touching device kinds or building drivers.
///
/// Connections bind ports to points, collect field-side wires for
/// point-to-point connections, and synthesize internal point pairs for
/// port-to-port wires. Finally every port declared on an instance must
/// be bound; binding failures are deferred in
/// [`Resolved::binding_error`] for [`assemble`] to report.
pub(crate) fn resolve(model: &PlantModel) -> Resolved {
    let mut point_map = PointMap::new();
    for point in &model.io_points {
        point_map =
            point_map.with_point(point.id, core_direction(point.direction), point.value_type);
    }

    // Internal point ids are allocated above every declared point id, so
    // they cannot collide with model elements.
    let mut next_internal = model
        .io_points
        .iter()
        .map(|point| point.id.0)
        .max()
        .map_or(0, |max| max.saturating_add(1));

    let mut internal = ChannelMap::new();
    let mut wires = Vec::new();
    let mut bindings: BTreeMap<ComponentId, BTreeMap<String, PointId>> = BTreeMap::new();
    let mut binding_error = None;
    for (index, connection) in model.connections.iter().enumerate() {
        match (&connection.from, &connection.to) {
            (Endpoint::Point(point), Endpoint::Port(port))
            | (Endpoint::Port(port), Endpoint::Point(point)) => {
                bind(&mut bindings, &mut binding_error, port, *point);
            }
            (Endpoint::Point(input), Endpoint::Point(output)) => {
                // A field-side wire: the `to` (Out) channel drives the
                // `from` (In) channel observing it.
                wires.push(Loopback {
                    output: *output,
                    input: *input,
                });
            }
            (Endpoint::Port(from), Endpoint::Port(to)) => {
                // A component-to-component wire needs a field-free path:
                // the producing port writes a synthesized `Out` point the
                // executor flushes to the driver, and a loopback delivers
                // it to the consuming port's `In` point on the next scan.
                let Some(kind) = port_kind(model, from) else {
                    binding_error
                        .get_or_insert(AssemblyError::UnresolvedEndpoint { connection: index });
                    continue;
                };
                let (output, input) = (PointId(next_internal), PointId(next_internal + 1));
                next_internal += 2;
                for (point, direction) in [(output, Direction::Out), (input, Direction::In)] {
                    point_map = point_map.with_point(point, direction, kind);
                    internal = internal.with_point(PointBinding {
                        point,
                        channel: ChannelId {
                            device: INTERNAL_DEVICE,
                            name: format!("internal-{}", point.0),
                        },
                        direction: match direction {
                            Direction::In => SimDirection::In,
                            Direction::Out => SimDirection::Out,
                        },
                        initial: neutral(kind),
                    });
                }
                internal = internal.with_loopback(Loopback { output, input });
                bind(&mut bindings, &mut binding_error, from, output);
                bind(&mut bindings, &mut binding_error, to, input);
            }
        }
    }

    if binding_error.is_none() {
        'instances: for instance in &model.components {
            for port in instance.ports.keys() {
                let bound = bindings
                    .get(&instance.id)
                    .is_some_and(|ports| ports.contains_key(port));
                if !bound {
                    binding_error = Some(AssemblyError::UnboundPort {
                        component: instance.id,
                        port: port.clone(),
                    });
                    break 'instances;
                }
            }
        }
    }

    Resolved {
        point_map,
        bindings,
        binding_error,
        internal,
        wires,
    }
}

/// Resolves the model's device/channel mapping into the [`ChannelMap`] a
/// local [`SimDriver`] serves.
///
/// Every declared `io_point` becomes a point bound to its channel, with a
/// neutral initial value of the point's declared kind; point-to-point and
/// port-to-port wiring contributes the loopbacks and internal points. All
/// devices must be served by the local simulated factory — the
/// [`SIM_DEVICE_PREFIX`] family, which resolves every `sim*` kind
/// locally, `sim-tcp` included; a kind outside it is
/// [`AssemblyError::UnknownDeviceKind`]. The general path —
/// [`resolve_drivers`](crate::resolve_drivers) with a
/// [`DriverRegistry`](crate::DriverRegistry) — is what a model mixing
/// device kinds takes, and where `sim-tcp` routes remotely.
///
/// The returned map is open: the simulated plant lives in the channel
/// map, not the model, so callers may add process elements
/// ([`ProcessElement`](dcs_sim::ProcessElement)) standing in for field
/// physics before constructing the driver.
pub fn sim_channel_map(model: &PlantModel) -> Result<ChannelMap, AssemblyError> {
    let registry = crate::drivers::DriverRegistry::new()
        .with_prefix(SIM_DEVICE_PREFIX, crate::drivers::sim_device);
    Ok(crate::drivers::resolve_drivers(model, &registry)?.sim_map)
}

/// Builds the [`SimDriver`] serving the model's device/channel mapping —
/// [`sim_channel_map`] plus the map's own consistency check, surfaced as
/// [`AssemblyError::InvalidChannelMap`]. Callers needing simulated process
/// elements build the driver from the extended map themselves.
pub fn sim_driver(model: &PlantModel) -> Result<SimDriver, AssemblyError> {
    SimDriver::new(sim_channel_map(model)?)
        .map_err(|detail| AssemblyError::InvalidChannelMap { detail })
}

/// Instantiates every component instance through `registry` and returns
/// the ready [`Executor`] bound to `driver`.
///
/// Each instance's constructor receives its parameter map and its resolved
/// port-to-point bindings through a [`ComponentSpec`]; an unregistered kind
/// is [`AssemblyError::UnknownComponentKind`] and a constructor failure is
/// [`AssemblyError::UnboundPort`] or [`AssemblyError::Component`]. Every
/// constructed component's declared logical I/O is then verified against
/// the driver's point map — unserved point, direction, and value-kind
/// mismatches are named per requirement — before
/// [`Executor::new`](dcs_runtime::Executor::new) performs its own wiring
/// check. Components step in `components` order: the model's declared scan
/// order.
pub fn assemble<'d>(
    model: &PlantModel,
    registry: &ComponentRegistry,
    driver: &'d (dyn IoDriver + Sync),
) -> Result<Executor<'d>, AssemblyError> {
    let resolved = resolve(model);
    if let Some(error) = resolved.binding_error {
        return Err(error);
    }
    let mut components: Vec<Box<dyn Component>> = Vec::with_capacity(model.components.len());
    for instance in &model.components {
        let constructor = registry.constructor(&instance.kind).ok_or_else(|| {
            AssemblyError::UnknownComponentKind {
                component: instance.id,
                kind: instance.kind.clone(),
            }
        })?;
        let spec = ComponentSpec {
            name: format!("{}:{}", instance.kind, instance.id.0),
            id: instance.id,
            parameters: &instance.parameters,
            ports: resolved.bindings.get(&instance.id).unwrap_or(&NO_PORTS),
            points: &resolved.point_map,
        };
        let component = constructor(&spec).map_err(|error| match error {
            BuildError::UnboundPort { port } => AssemblyError::UnboundPort {
                component: instance.id,
                port,
            },
            BuildError::Other(error) => AssemblyError::Component {
                component: instance.id,
                kind: instance.kind.clone(),
                detail: error.to_string(),
            },
        })?;
        for requirement in component.io_requirements() {
            let point = requirement.point;
            let Some(mapped) = resolved.point_map.get(point) else {
                return Err(AssemblyError::UnmappedPoint {
                    component: instance.id,
                    port: requirement.name.clone(),
                    point,
                });
            };
            if mapped.direction != requirement.direction {
                return Err(AssemblyError::DirectionMismatch {
                    component: instance.id,
                    port: requirement.name.clone(),
                    point,
                    declared: requirement.direction,
                    mapped: mapped.direction,
                });
            }
            if mapped.kind != requirement.kind {
                return Err(AssemblyError::TypeMismatch {
                    component: instance.id,
                    port: requirement.name.clone(),
                    point,
                    declared: requirement.kind,
                    mapped: mapped.kind,
                });
            }
        }
        components.push(component);
    }
    Executor::new(driver, resolved.point_map, components)
        .map_err(|detail| AssemblyError::Wiring { detail })
}
