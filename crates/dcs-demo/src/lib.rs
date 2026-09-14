//! End-to-end simulated tank level loop: the M1 milestone path in one
//! place, from plant model document to deterministic telemetry.
//!
//! The layers connect exactly as the architecture decisions describe:
//!
//! - `fixtures/tank_level.json` is a versioned
//!   [`PlantModel`](dcs_model::PlantModel) document — the single contract.
//!   It declares the simulated field devices and their channels, the
//!   logical I/O points bound to them, the component instances, and the
//!   connections wiring points to ports.
//! - [`assemble`] resolves that model into driver- and executor-side
//!   inputs: a [`ChannelMap`] for [`SimDriver`], a [`PointMap`] for
//!   [`Executor`], and `dcs-blocks` components built through their
//!   `from_parameters` constructors. A port-to-port connection — the level
//!   signal from `analog-input.out` to `pid.pv` — becomes a synthesized
//!   `Out`/`In` point pair joined by a driver [`Loopback`].
//! - The simulated plant lives in the channel map, not the model: a
//!   [`FirstOrderLag`] drives the raw level point from the valve command
//!   point, standing in for the real tank's response.
//! - [`run`] drives the fixed-step scan. Each iteration runs one
//!   `Executor::scan` (read inputs, step components, write outputs) and one
//!   `SimDriver::step`, which routes loopbacks and advances the lag.
//!   Everything is tick-domain — nothing reads a wall clock — so identical
//!   runs produce identical samples.
//!
//! The `dcs-demo` binary runs [`DEFAULT_SCANS`] scans of this loop and
//! prints the final [`TelemetrySnapshot`] as JSON.
//!
//! The [`showcase`] module runs the broader plant: `fixtures/showcase.json`'s
//! pump-and-tank line exercises every `dcs-blocks` kind, assembled with no
//! manual wiring through the standard driver and component registries —
//! `dcs-assembly`'s [`DriverRegistry::standard`](dcs_assembly::DriverRegistry::standard)
//! plus `dcs-controller`'s deployed [`registry`](dcs_controller::registry) —
//! and run through a documented command-and-fault scenario. This module's
//! hand-rolled [`assemble`] predates that path and stays as the M1 example.

#![warn(missing_docs)]

pub mod showcase;

use dcs_blocks::{AnalogInput, DigitalOutput, ParameterError, Pid, Scaling};
use dcs_core::{IoDriver, IoError, PointId, TelemetrySnapshot, Value, ValueKind};
use dcs_model::{ComponentId, DeviceId, Endpoint, LoadError, PlantModel, PortRef, ValidationError};
use dcs_runtime::{Component, Executor, PointMap, ScanError, WiringError};
use dcs_sim::{
    ChannelId, ChannelMap, ConfigError, FirstOrderLag, Loopback, PointBinding, ProcessElement,
    SimDriver,
};
use std::collections::HashMap;
use std::fmt;

/// The checked-in tank level loop model this crate demonstrates.
pub const MODEL_DOCUMENT: &str = include_str!("../fixtures/tank_level.json");

/// The level setpoint written to the simulated field source before a run,
/// in percent.
pub const SETPOINT_PERCENT: f64 = 60.0;

/// Simulated time each scan advances the process by, in the process's time
/// units — the `dt` passed to [`SimDriver::step`] and the period the PID's
/// `dt` parameter is tuned for.
pub const SCAN_PERIOD: f64 = 0.1;

/// How many scans the binary runs when invoked without arguments.
pub const DEFAULT_SCANS: u64 = 400;

/// Time constant of the first-order lag standing in for the tank, in the
/// same time units as [`SCAN_PERIOD`].
const PROCESS_TIME_CONSTANT: f64 = 2.0;

/// The lag's initial output: an empty tank reads 4 mA, the bottom of the
/// transmitter's raw range.
const EMPTY_TANK_RAW: f64 = 4.0;

/// Simulated device id carrying the internal channels synthesized for
/// port-to-port wiring. Model device ids are small integers, so this
/// cannot collide.
const INTERNAL_DEVICE: u64 = u64::MAX;

/// Why loading, assembling, or running the demo loop failed.
#[derive(Debug)]
pub enum DemoError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// A programmatically supplied model failed [`PlantModel::validate`].
    Invalid(Vec<ValidationError>),
    /// A device names a `kind` the simulated driver layer does not serve.
    /// Only `sim*` kinds resolve to [`SimDriver`] channels.
    UnknownDeviceKind {
        /// The device with the unknown kind.
        device: DeviceId,
        /// The kind it declares.
        kind: String,
    },
    /// A component instance names a `kind` the demo does not register.
    UnknownComponentKind {
        /// The component instance with the unknown kind.
        component: ComponentId,
        /// The kind it declares.
        kind: String,
    },
    /// A component port is not wired by any connection.
    UnboundPort {
        /// The component instance owning the port.
        component: ComponentId,
        /// The unbound port's name.
        port: String,
    },
    /// The demo's loop requires one component of this `kind` and the model
    /// declares none.
    MissingLoopComponent {
        /// The required component kind.
        kind: &'static str,
    },
    /// A port's declared value kind is not one the component supports:
    /// `analog-input`'s `raw` port reads `Float` or `Int` points only.
    UnsupportedPortKind {
        /// The component instance owning the port.
        component: ComponentId,
        /// The port's name.
        port: String,
        /// The kind the port's wiring carries.
        kind: ValueKind,
    },
    /// A connection wires a point straight to a point. The schema permits
    /// `In` point to `Out` point wires, but only components move values
    /// between points — the demo has no use for them.
    PointToPoint {
        /// The offending connection's index in `connections`.
        connection: usize,
    },
    /// A component's parameters were rejected by its `from_parameters`.
    Parameter(ParameterError),
    /// The assembled channel map failed [`SimDriver`]'s checks.
    Config(ConfigError),
    /// A component's declared I/O did not match the point map.
    Wiring(WiringError),
    /// A scan's output phase failed.
    Scan(ScanError),
    /// A driver access failed.
    Io(IoError),
}

impl fmt::Display for DemoError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Load(error) => write!(f, "{error}"),
            Self::Invalid(errors) => {
                write!(
                    f,
                    "plant model failed validation ({} error(s)):",
                    errors.len()
                )?;
                for error in errors {
                    write!(f, " {error};")?;
                }
                Ok(())
            }
            Self::UnknownDeviceKind { device, kind } => write!(
                f,
                "device {} declares unknown kind {kind:?} (only sim* devices are served)",
                device.0
            ),
            Self::UnknownComponentKind { component, kind } => write!(
                f,
                "component {} declares unknown kind {kind:?}",
                component.0
            ),
            Self::UnboundPort { component, port } => write!(
                f,
                "port {port:?} on component {} is not wired by any connection",
                component.0
            ),
            Self::MissingLoopComponent { kind } => {
                write!(f, "the level loop requires one {kind:?} component")
            }
            Self::UnsupportedPortKind {
                component,
                port,
                kind,
            } => write!(
                f,
                "port {port:?} on component {} carries unsupported kind {kind:?}",
                component.0
            ),
            Self::PointToPoint { connection } => write!(
                f,
                "connection {connection} wires a point straight to a point"
            ),
            Self::Parameter(error) => write!(f, "{error}"),
            Self::Config(error) => write!(f, "channel map rejected: {error}"),
            Self::Wiring(error) => write!(f, "executor wiring rejected: {error}"),
            Self::Scan(error) => write!(f, "{error}"),
            Self::Io(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for DemoError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Load(error) => Some(error),
            Self::Invalid(errors) => errors.first().map(|error| error as _),
            Self::Parameter(error) => Some(error),
            Self::Config(error) => Some(error),
            Self::Wiring(error) => Some(error),
            Self::Scan(error) => Some(error),
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<LoadError> for DemoError {
    fn from(error: LoadError) -> Self {
        Self::Load(error)
    }
}

impl From<ParameterError> for DemoError {
    fn from(error: ParameterError) -> Self {
        Self::Parameter(error)
    }
}

impl From<ConfigError> for DemoError {
    fn from(error: ConfigError) -> Self {
        Self::Config(error)
    }
}

impl From<WiringError> for DemoError {
    fn from(error: WiringError) -> Self {
        Self::Wiring(error)
    }
}

impl From<ScanError> for DemoError {
    fn from(error: ScanError) -> Self {
        Self::Scan(error)
    }
}

impl From<IoError> for DemoError {
    fn from(error: IoError) -> Self {
        Self::Io(error)
    }
}

/// The resolved, runnable form of a validated plant model: everything a
/// caller needs to drive the loop except the pacing loop itself.
pub struct Assembly {
    /// The [`SimDriver`]'s channel map: every I/O point's binding, the
    /// loopbacks synthesized for port-to-port connections, and the tank's
    /// first-order lag process element.
    pub channel_map: ChannelMap,
    /// The [`Executor`]'s point map: every served point with the direction
    /// and value kind component declarations are checked against.
    pub point_map: PointMap,
    /// The components, in scan order, built from the model's component
    /// instances and connections.
    pub components: Vec<Box<dyn Component>>,
    /// The `In` point carrying the level setpoint; [`run`] writes
    /// [`SETPOINT_PERCENT`] to it before scanning.
    pub setpoint: PointId,
    /// The `In` point publishing the simulated tank level, in the
    /// transmitter's raw units (mA).
    pub level: PointId,
    /// The `Out` point carrying the valve command to the simulated
    /// process, in the same units as [`Assembly::level`].
    pub valve: PointId,
    /// The `analog-input` block's raw-to-engineering scaling, reused to
    /// report the simulated level in engineering units.
    pub level_scaling: Scaling,
}

/// The simulated level in engineering units given the [`Assembly::level`]
/// point's raw value.
pub fn level_percent(scaling: &Scaling, raw: f64) -> f64 {
    scaling.eng_min
        + (raw - scaling.raw_min) * (scaling.eng_max - scaling.eng_min)
            / (scaling.raw_max - scaling.raw_min)
}

/// A point's value before the first write or element step; the variant is
/// the point's declared kind.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// The value kind a port declares. Callers run this only after validation
/// has proven the component and port exist.
fn port_value_kind(model: &PlantModel, port: &PortRef) -> ValueKind {
    model
        .components
        .iter()
        .find(|component| component.id == port.component)
        .and_then(|component| component.ports.get(&port.name))
        .map(|port| port.value_type)
        .expect("a validated model resolves every connection's ports")
}

/// Accumulates the resolved driver- and executor-side wiring while a model
/// is assembled.
struct Wiring {
    bindings: Vec<PointBinding>,
    loopbacks: Vec<Loopback>,
    specs: Vec<(PointId, dcs_core::Direction, ValueKind)>,
    internals: Vec<(PointId, dcs_core::Direction, ValueKind, Value)>,
    kinds: HashMap<PointId, ValueKind>,
}

impl Wiring {
    /// Serves `point` through `channel`: a driver binding plus the point
    /// map entry component declarations are checked against.
    fn add_point(
        &mut self,
        point: PointId,
        channel: ChannelId,
        direction: dcs_core::Direction,
        kind: ValueKind,
    ) {
        self.bindings.push(PointBinding {
            point,
            channel,
            direction,
            initial: neutral(kind),
        });
        self.specs.push((point, direction, kind));
        self.kinds.insert(point, kind);
    }

    /// Carries `point` in the scan image at `initial`: a point map entry
    /// with no driver binding — the channel-less internal point.
    fn add_internal(
        &mut self,
        point: PointId,
        direction: dcs_core::Direction,
        kind: ValueKind,
        initial: Value,
    ) {
        self.internals.push((point, direction, kind, initial));
        self.kinds.insert(point, kind);
    }
}

/// The point the `kind` component's `port` resolved to — how the demo
/// locates the loop's setpoint, level, and valve points without hardcoding
/// point ids.
fn loop_point(
    model: &PlantModel,
    port_points: &HashMap<(u64, String), PointId>,
    kind: &'static str,
    port: &str,
) -> Result<PointId, DemoError> {
    let instance = model
        .components
        .iter()
        .find(|component| component.kind == kind)
        .ok_or(DemoError::MissingLoopComponent { kind })?;
    port_points
        .get(&(instance.id.0, port.to_string()))
        .copied()
        .ok_or_else(|| DemoError::UnboundPort {
            component: instance.id,
            port: port.to_string(),
        })
}

/// Resolves a validated plant model into the driver's channel map, the
/// executor's point map, and the component list — see the crate docs for
/// how the layers connect.
///
/// Every I/O point becomes a [`PointBinding`] served by [`SimDriver`] and
/// a [`PointMap`] entry. Connections bind each component port to a point:
/// a port facing a model point binds it directly, and a port-to-port
/// connection synthesizes an `Out`/`In` point pair on a reserved internal
/// device, joined by a [`Loopback`] so the producing component's write
/// reaches the consumer one scan later. Component instances are
/// constructed through the `dcs-blocks` `from_parameters` registry —
/// `analog-input`, `pid`, and `digital-output` are known kinds.
///
/// The demo's process model is added last: a [`FirstOrderLag`] from the
/// `pid`'s `out` point (the valve command) to the `analog-input`'s `raw`
/// point (the level raw signal) closes the loop through the simulated
/// field.
pub fn assemble(model: &PlantModel) -> Result<Assembly, DemoError> {
    let errors = model.validate();
    if !errors.is_empty() {
        return Err(DemoError::Invalid(errors));
    }
    for device in &model.devices {
        if !device.kind.starts_with("sim") {
            return Err(DemoError::UnknownDeviceKind {
                device: device.id,
                kind: device.kind.clone(),
            });
        }
    }

    let mut wiring = Wiring {
        bindings: Vec::with_capacity(model.io_points.len()),
        loopbacks: Vec::new(),
        specs: Vec::with_capacity(model.io_points.len()),
        internals: Vec::new(),
        kinds: HashMap::with_capacity(model.io_points.len()),
    };
    for point in &model.io_points {
        match &point.channel {
            Some(channel) => wiring.add_point(
                point.id,
                ChannelId {
                    device: channel.device.0,
                    name: channel.name.clone(),
                },
                point.direction,
                point.value_type,
            ),
            // A channel-less internal point: the scan image carries its
            // declared initial — validation proved it is present.
            None => wiring.add_internal(
                point.id,
                point.direction,
                point.value_type,
                point
                    .initial
                    .expect("a validated internal point declares initial"),
            ),
        }
    }
    let mut next_internal = model
        .io_points
        .iter()
        .map(|point| point.id.0)
        .max()
        .unwrap_or(0)
        + 1;

    // Bind every wired component port to a point. Port-to-port
    // connections get a synthesized Out/In point pair joined by a driver
    // loopback; the port names stay out of the driver entirely.
    let mut port_points: HashMap<(u64, String), PointId> = HashMap::new();
    for (index, connection) in model.connections.iter().enumerate() {
        match (&connection.from, &connection.to) {
            (Endpoint::Point(point), Endpoint::Port(port))
            | (Endpoint::Port(port), Endpoint::Point(point)) => {
                port_points.insert((port.component.0, port.name.clone()), *point);
            }
            (Endpoint::Port(from), Endpoint::Port(to)) => {
                let kind = port_value_kind(model, from);
                let out_point = PointId(next_internal);
                let in_point = PointId(next_internal + 1);
                next_internal += 2;
                wiring.add_point(
                    out_point,
                    ChannelId {
                        device: INTERNAL_DEVICE,
                        name: format!("link-{index}-out"),
                    },
                    dcs_core::Direction::Out,
                    kind,
                );
                wiring.add_point(
                    in_point,
                    ChannelId {
                        device: INTERNAL_DEVICE,
                        name: format!("link-{index}-in"),
                    },
                    dcs_core::Direction::In,
                    kind,
                );
                wiring.loopbacks.push(Loopback {
                    output: out_point,
                    input: in_point,
                });
                port_points.insert((from.component.0, from.name.clone()), out_point);
                port_points.insert((to.component.0, to.name.clone()), in_point);
            }
            (Endpoint::Point(_), Endpoint::Point(_)) => {
                return Err(DemoError::PointToPoint { connection: index });
            }
        }
    }

    let port_point = |component: ComponentId, port: &str| -> Result<PointId, DemoError> {
        port_points
            .get(&(component.0, port.to_string()))
            .copied()
            .ok_or_else(|| DemoError::UnboundPort {
                component,
                port: port.to_string(),
            })
    };

    let mut components: Vec<Box<dyn Component>> = Vec::with_capacity(model.components.len());
    for instance in &model.components {
        let name = format!("{}-{}", instance.kind, instance.id.0);
        let component: Box<dyn Component> = match instance.kind.as_str() {
            kind if kind == AnalogInput::<f64>::KIND => {
                let raw = port_point(instance.id, "raw")?;
                let out = port_point(instance.id, "out")?;
                match wiring.kinds[&raw] {
                    ValueKind::Float => Box::new(AnalogInput::<f64>::from_parameters(
                        name,
                        raw,
                        out,
                        &instance.parameters,
                    )?),
                    ValueKind::Int => Box::new(AnalogInput::<i64>::from_parameters(
                        name,
                        raw,
                        out,
                        &instance.parameters,
                    )?),
                    kind => {
                        return Err(DemoError::UnsupportedPortKind {
                            component: instance.id,
                            port: "raw".to_string(),
                            kind,
                        });
                    }
                }
            }
            kind if kind == Pid::KIND => Box::new(Pid::from_parameters(
                name,
                port_point(instance.id, "sp")?,
                port_point(instance.id, "pv")?,
                port_point(instance.id, "out")?,
                &instance.parameters,
            )?),
            kind if kind == DigitalOutput::KIND => Box::new(DigitalOutput::from_parameters(
                name,
                port_point(instance.id, "in")?,
                port_point(instance.id, "out")?,
                &instance.parameters,
            )?),
            kind => {
                return Err(DemoError::UnknownComponentKind {
                    component: instance.id,
                    kind: kind.to_string(),
                });
            }
        };
        components.push(component);
    }

    // The loop's process-relevant points: where the setpoint enters, where
    // the valve command leaves, and where the tank level is measured.
    let setpoint = loop_point(model, &port_points, Pid::KIND, "sp")?;
    let level = loop_point(model, &port_points, AnalogInput::<f64>::KIND, "raw")?;
    let valve = loop_point(model, &port_points, Pid::KIND, "out")?;

    // The analog-input's scaling parameters, kept so the level trace can
    // report engineering units. `from_parameters` already validated them.
    let scaling_of = |name: &str| -> Result<f64, DemoError> {
        let instance = model
            .components
            .iter()
            .find(|component| component.kind == AnalogInput::<f64>::KIND)
            .ok_or(DemoError::MissingLoopComponent {
                kind: AnalogInput::<f64>::KIND,
            })?;
        instance
            .parameters
            .get(name)
            .and_then(|value| f64::try_from(*value).ok())
            .ok_or_else(|| {
                DemoError::Parameter(ParameterError::Missing {
                    component: instance.id.0.to_string(),
                    parameter: name.to_string(),
                })
            })
    };
    let level_scaling = Scaling {
        raw_min: scaling_of("raw_min")?,
        raw_max: scaling_of("raw_max")?,
        eng_min: scaling_of("eng_min")?,
        eng_max: scaling_of("eng_max")?,
    };

    // The simulated plant: the valve command drives the tank level through
    // a first-order lag. This is driver-side process emulation — the model
    // documents the control structure, not the physics.
    let elements = vec![ProcessElement::FirstOrderLag(FirstOrderLag {
        input: valve,
        output: level,
        time_constant: PROCESS_TIME_CONSTANT,
        initial: EMPTY_TANK_RAW,
    })];

    let channel_map = ChannelMap {
        points: wiring.bindings,
        loopbacks: wiring.loopbacks,
        elements,
    };
    let mut point_map: PointMap = wiring.specs.into_iter().collect();
    for (point, direction, kind, initial) in wiring.internals {
        point_map = point_map.with_internal(point, direction, kind, initial);
    }

    Ok(Assembly {
        channel_map,
        point_map,
        components,
        setpoint,
        level,
        valve,
        level_scaling,
    })
}

/// A finished demo run: the per-scan level trace and the final telemetry.
#[derive(Debug, Clone, PartialEq)]
pub struct Run {
    /// The simulated tank level after each scan, in engineering units —
    /// one entry per scan, in scan order.
    pub levels: Vec<f64>,
    /// The executor's monitoring snapshot at the final tick: every mapped
    /// point's latest sample and per-component diagnostics.
    pub snapshot: TelemetrySnapshot,
}

/// Loads `source` as a plant model, assembles driver and executor from it,
/// writes [`SETPOINT_PERCENT`] onto the setpoint point, and runs `scans`
/// scans of the fixed-step loop described in the crate docs.
///
/// The run is fully deterministic: driver and executor are tick-domain, so
/// two calls with the same `source` and `scans` return equal [`Run`]s.
pub fn run(source: &str, scans: u64) -> Result<Run, DemoError> {
    let model = PlantModel::load(source)?;
    let Assembly {
        channel_map,
        point_map,
        components,
        setpoint,
        level,
        level_scaling,
        ..
    } = assemble(&model)?;
    let sim = SimDriver::new(channel_map)?;
    // The setpoint source is field-side: forcing a value on an `In` point
    // is how the simulation stands in for an operator-entered setpoint.
    sim.write(setpoint, Value::Float(SETPOINT_PERCENT))?;
    let mut executor = Executor::new(&sim, point_map, components)?;

    let mut levels = Vec::with_capacity(scans as usize);
    for _ in 0..scans {
        executor.scan()?;
        sim.step(SCAN_PERIOD);
        let Value::Float(raw) = sim.read(level)?.value else {
            unreachable!("a validated lag output is always a Float point")
        };
        levels.push(level_percent(&level_scaling, raw));
    }
    Ok(Run {
        levels,
        snapshot: executor.snapshot(),
    })
}
