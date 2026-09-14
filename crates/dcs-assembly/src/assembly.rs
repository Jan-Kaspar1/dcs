//! The model → runtime translation: channel map, point map, port
//! bindings, and component construction.
//!
//! [`resolve`] walks a validated [`PlantModel`] once and derives everything
//! the driver and executor need; [`sim_driver`] and [`assemble`] consume the
//! same resolution, so the point map the executor checks against always
//! matches the channels the driver serves.

use crate::error::{AssemblyError, BuildError, InternalPointError};
use crate::registry::{ComponentRegistry, ComponentSpec};
use dcs_core::{Direction, IoDriver, PointId, Value, ValueKind};
use dcs_model::{ComponentId, Direction as ModelDirection, Endpoint, PlantModel, PortRef};
use dcs_runtime::{Component, Executor, PointMap};
use dcs_sim::{
    ChannelId, ChannelMap, Direction as SimDirection, Loopback, PointBinding, SimDriver,
};
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;

/// The device-kind prefix assembly can build: every `sim*` kind resolves
/// to [`SimDriver`] channels — `sim` itself plus role-flavored kinds like
/// `sim-ai` or `sim-ao`, the convention `dcs-demo` established. A model
/// device of any other kind fails with
/// [`AssemblyError::UnknownDeviceKind`].
pub const SIM_DEVICE_PREFIX: &str = "sim";

/// Shared empty port map for instances with no wired ports.
static NO_PORTS: BTreeMap<String, PointId> = BTreeMap::new();

/// A point's value before the first write or element step: the neutral
/// value of its declared kind.
fn neutral(kind: ValueKind) -> Value {
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

fn sim_direction(direction: ModelDirection) -> SimDirection {
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
struct Resolved {
    /// The simulated backend's topology: declared field points and
    /// field-side loopbacks. Internal points are image-carried and never
    /// enter the channel map.
    channel_map: ChannelMap,
    /// The executor-side authority every component's declared I/O is
    /// checked against: field and internal points plus the internal
    /// links carrying port-to-port and internal point-to-point wiring.
    point_map: PointMap,
    /// Per component instance: port name → bound point.
    bindings: BTreeMap<ComponentId, BTreeMap<String, PointId>>,
    /// The first connection-wiring failure, if any — deferred so
    /// [`sim_driver`] can still build the field-side topology of a model
    /// whose component wiring is broken. [`assemble`] reports it.
    binding_error: Option<AssemblyError>,
}

/// Whether `point` resolves to an image-carried internal point.
fn is_internal(point_map: &PointMap, point: PointId) -> bool {
    point_map
        .get(point)
        .is_some_and(|spec| spec.internal.is_some())
}

/// Resolves the model's device/channel mapping and connection wiring.
///
/// Device kinds are checked first — a device whose kind does not start
/// with [`SIM_DEVICE_PREFIX`] is [`AssemblyError::UnknownDeviceKind`] —
/// then each declared `io_point` enters the point map: channel-bound
/// points as field points served by a channel-map binding, channel-less
/// points as internal points the scan image carries at their declared
/// `initial` (a malformed declaration is
/// [`AssemblyError::InvalidInternalPoint`]). Connections then bind ports
/// to points, add field-side loopbacks for field point-to-point wires and
/// internal links for internal ones, and synthesize linked internal point
/// pairs for port-to-port wires. Finally every port declared on an
/// instance must be bound.
fn resolve(model: &PlantModel) -> Result<Resolved, AssemblyError> {
    for device in &model.devices {
        if !device.kind.starts_with(SIM_DEVICE_PREFIX) {
            return Err(AssemblyError::UnknownDeviceKind {
                device: device.id,
                kind: device.kind.clone(),
            });
        }
    }

    let mut channel_map = ChannelMap::new();
    let mut point_map = PointMap::new();
    for point in &model.io_points {
        let direction = core_direction(point.direction);
        match (&point.channel, point.initial) {
            // A field point: the driver serves it through the declared
            // channel, starting at the neutral value of its kind.
            (Some(channel), _) => {
                point_map = point_map.with_point(point.id, direction, point.value_type);
                channel_map = channel_map.with_point(PointBinding {
                    point: point.id,
                    channel: ChannelId {
                        device: channel.device.0,
                        name: channel.name.clone(),
                    },
                    direction: sim_direction(point.direction),
                    initial: neutral(point.value_type),
                });
            }
            // An internal point: image-carried at its declared initial.
            (None, Some(initial)) if initial.kind() == point.value_type => {
                point_map = point_map.with_internal(point.id, direction, point.value_type, initial);
            }
            // Reachable only for a model resolved without validation.
            (None, initial) => {
                return Err(AssemblyError::InvalidInternalPoint {
                    point: point.id,
                    detail: match initial {
                        None => InternalPointError::MissingInitial,
                        Some(initial) => InternalPointError::InitialKindMismatch {
                            declared: point.value_type,
                            initial: initial.kind(),
                        },
                    },
                });
            }
        }
    }

    // Synthesized internal point ids are allocated above every declared
    // point id, so they cannot collide with model elements.
    let mut next_internal = model
        .io_points
        .iter()
        .map(|point| point.id.0)
        .max()
        .map_or(0, |max| max.saturating_add(1));

    let mut bindings: BTreeMap<ComponentId, BTreeMap<String, PointId>> = BTreeMap::new();
    let mut binding_error = None;
    for (index, connection) in model.connections.iter().enumerate() {
        match (&connection.from, &connection.to) {
            (Endpoint::Point(point), Endpoint::Port(port))
            | (Endpoint::Port(port), Endpoint::Point(point)) => {
                bind(&mut bindings, &mut binding_error, port, *point);
            }
            (Endpoint::Point(input), Endpoint::Point(output)) => {
                match (
                    is_internal(&point_map, *input),
                    is_internal(&point_map, *output),
                ) {
                    // A field-side wire: the `to` (Out) channel drives
                    // the `from` (In) channel observing it.
                    (false, false) => {
                        channel_map = channel_map.with_loopback(Loopback {
                            output: *output,
                            input: *input,
                        });
                    }
                    // An internal wire: the `to` (Out) point's image
                    // sample is routed onto the `from` (In) point at each
                    // scan's input phase — the same one-scan-later
                    // boundary a field loopback crosses.
                    (true, true) => {
                        point_map = point_map.with_internal_link(*output, *input);
                    }
                    // A channel-less point has no field channel, and an
                    // internal link cannot carry one.
                    (input_internal, _) => {
                        let (field, internal) = if input_internal {
                            (*output, *input)
                        } else {
                            (*input, *output)
                        };
                        binding_error.get_or_insert(AssemblyError::MixedPointLink {
                            connection: index,
                            field,
                            internal,
                        });
                    }
                }
            }
            (Endpoint::Port(from), Endpoint::Port(to)) => {
                // A component-to-component wire needs a field-free path:
                // the producing port writes a synthesized internal `Out`
                // point and an internal link delivers it to the consuming
                // port's internal `In` point at the next scan's input
                // phase.
                let Some(kind) = port_kind(model, from) else {
                    binding_error
                        .get_or_insert(AssemblyError::UnresolvedEndpoint { connection: index });
                    continue;
                };
                let (output, input) = (PointId(next_internal), PointId(next_internal + 1));
                next_internal += 2;
                point_map = point_map
                    .with_internal(output, Direction::Out, kind, neutral(kind))
                    .with_internal(input, Direction::In, kind, neutral(kind))
                    .with_internal_link(output, input);
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

    Ok(Resolved {
        channel_map,
        point_map,
        bindings,
        binding_error,
    })
}

/// Resolves the model's device/channel mapping into the [`ChannelMap`] a
/// [`SimDriver`] serves.
///
/// Every declared channel-bound `io_point` becomes a point bound to its
/// channel, with a neutral initial value of the point's declared kind;
/// channel-less internal points are carried by the executor's scan image
/// and contribute nothing here. Field point-to-point wiring contributes
/// the loopbacks. All devices must carry a [`SIM_DEVICE_PREFIX`] kind.
///
/// The returned map is open: the simulated plant lives in the channel
/// map, not the model, so callers may add process elements
/// ([`ProcessElement`](dcs_sim::ProcessElement)) standing in for field
/// physics before constructing the driver.
pub fn sim_channel_map(model: &PlantModel) -> Result<ChannelMap, AssemblyError> {
    Ok(resolve(model)?.channel_map)
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
/// the resolved point map — field and internal points alike: unserved
/// point, direction, and value-kind mismatches are named per requirement —
/// before
/// [`Executor::new`](dcs_runtime::Executor::new) performs its own wiring
/// check. Components step in `components` order: the model's declared scan
/// order.
pub fn assemble<'d>(
    model: &PlantModel,
    registry: &ComponentRegistry,
    driver: &'d (dyn IoDriver + Sync),
) -> Result<Executor<'d>, AssemblyError> {
    let resolved = resolve(model)?;
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
