//! End-to-end simulated tank level loop: the M1 milestone path in one
//! place, from plant model document to deterministic telemetry.
//!
//! The layers connect exactly as the architecture decisions describe:
//!
//! - `fixtures/tank_level.json` is a versioned
//!   [`PlantModel`](dcs_model::PlantModel) document — the single contract.
//!   It declares the simulated field devices and their channels, the
//!   logical I/O points bound to them, the component instances, and the
//!   connections wiring points to ports. Architecture decision 106's
//!   declared-unit metadata rides on it: the level transmitter's raw
//!   reading and the valve command are `mA`, the operator setpoint is
//!   `%`, the `analog-input`'s `raw`/`out` ports and its
//!   `raw_min`/`raw_max`/`eng_min`/`eng_max` bounds follow the same
//!   split, and the `pid`'s `sp`/`pv` are the level's `%` while its
//!   manipulated variable — written straight to the command channel,
//!   this M1 sheet has no `analog-output` scaling stage — is the
//!   channel's `mA`, its `out_min`/`out_max` bounds beside it.
//! - [`assemble`] resolves the model's devices through
//!   [`DriverRegistry::standard`](dcs_assembly::DriverRegistry::standard),
//!   merges the tank's [`FirstOrderLag`] — the simulated physics the
//!   model does not describe — into the still-open local channel map, and
//!   builds the [`FanoutDriver`]. [`run`] then assembles the executor
//!   through [`dcs_assembly::assemble`] with the standard component
//!   registry — `dcs_controller::registry()`, the same set
//!   `dcs-controller` deploys — so port bindings, point-map contract
//!   fields (`writable`, `stale_after_ticks`, `journaled`), the declared
//!   signal index, and port-to-port wiring all come from the one
//!   model-driven path a customer plant's controller takes. A
//!   port-to-port connection — the level signal from `analog-input.out`
//!   to `pid.pv` — becomes a synthesized internal `Out`/`In` point pair
//!   joined by an internal link in the executor's scan image, delivering
//!   the value one scan later.
//! - The simulated plant lives in the channel map, not the model: the
//!   lag drives the raw level point from the valve command point,
//!   standing in for the real tank's response.
//! - [`run`] drives the fixed-step scan. Each iteration runs one
//!   `Executor::scan` (read inputs, step components, write outputs) and
//!   one [`FanoutDriver::step`], which advances the local simulated
//!   plant's lag. Everything is tick-domain — nothing reads a wall
//!   clock — so identical runs produce identical samples.
//!
//! The `dcs-demo` binary runs [`DEFAULT_SCANS`] scans of this loop and
//! prints the final [`TelemetrySnapshot`] as JSON.
//!
//! The [`showcase`] module runs the broader plant: `fixtures/showcase.json`'s
//! pump-and-tank line exercises every `dcs-blocks` kind through a
//! documented command-and-fault scenario. It is the same composition
//! path this module takes — `resolve_drivers` plus
//! [`dcs_assembly::assemble`] on the standard registries — so the M1
//! demo and the customer-facing example cannot drift apart: the demo
//! keeps only what the platform cannot supply, the process physics
//! standing in for the field and the level-trace reporting.
//!
//! The [`equivalence`] module carries the driven-cycle orchestration
//! both equivalence runners share — the driven-`Monitor` bind and
//! `Driven` wiring, the per-tick command-and-advance loop, receipt and
//! journal collection, and the `sim-bus` overlay's bank
//! bind/substitute/resolve pattern — so a runner supplies only its
//! scenario content: fixture documents, the field program, operator
//! actions, and the scenario pins.
//!
//! The [`two_kinds`] module carries the interim WW-FND-002 evidence: one
//! logical plant bound once to `sim-scripted` and once to `sim-bus` — two
//! registered driver kinds with different transport semantics — run through
//! the same driven scan cycle with the field side fed in lockstep, so the
//! tests can assert both transports produce identical executor snapshots
//! and journals.
//!
//! The [`station_kinds`] module lands the requirement's real evidence: the
//! reference pumping station's checked-in model — `io_points`, signals,
//! components, and connections — bound once to its emitted local `sim-*`
//! devices and once to a `sim-bus` register-mapped overlay, run through the
//! same driven scan cycle with identical snapshots and journals.

#![warn(missing_docs)]

pub mod equivalence;
pub mod showcase;
pub mod station_kinds;
pub mod two_kinds;

use dcs_assembly::{AssemblyError, DriverRegistry, FanoutDriver, StepError, resolve_drivers};
use dcs_blocks::{AnalogInput, ParameterError, Pid, Scaling};
use dcs_core::{IoDriver, IoError, PointId, TelemetrySnapshot, Value};
use dcs_model::{ComponentId, Endpoint, LoadError, PlantModel, ValidationError};
use dcs_sim::{ConfigError, FirstOrderLag, ProcessElement};
use std::collections::HashMap;
use std::fmt;

/// The checked-in tank level loop model this crate demonstrates.
pub const MODEL_DOCUMENT: &str = include_str!("../fixtures/tank_level.json");

/// The level setpoint written to the simulated field source before a run,
/// in percent.
pub const SETPOINT_PERCENT: f64 = 60.0;

/// Simulated time each scan advances the process by, in the process's time
/// units — the `dt` passed to [`FanoutDriver::step`] and the period the PID's
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

/// Why loading, assembling, or running the demo loop failed.
#[derive(Debug)]
pub enum DemoError {
    /// The model document failed [`PlantModel::load`].
    Load(LoadError),
    /// A programmatically supplied model failed [`PlantModel::validate`].
    Invalid(Vec<ValidationError>),
    /// A component port the demo's loop needs is not wired straight to an
    /// `io_point`. The level loop's setpoint, level, and valve points must
    /// be field-bound points — a port-to-port wire would land on a
    /// synthesized internal point the demo's field-side setpoint write and
    /// process-element legs cannot serve.
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
    /// The `analog-input` scaling parameters the level trace reports were
    /// absent or of the wrong kind.
    Parameter(ParameterError),
    /// Device resolution, component construction, or executor wiring
    /// failed on the settled assembly path.
    Assembly(AssemblyError),
    /// The simulated channel map — the resolved local map plus the demo's
    /// process element — failed [`ChannelMap::validate`].
    ///
    /// [`ChannelMap::validate`]: dcs_sim::ChannelMap::validate
    Config(ConfigError),
    /// The driver failed to step the simulated plant.
    Step(StepError),
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
            Self::UnboundPort { component, port } => write!(
                f,
                "port {port:?} on component {} is not wired straight to an io point",
                component.0
            ),
            Self::MissingLoopComponent { kind } => {
                write!(f, "the level loop requires one {kind:?} component")
            }
            Self::Parameter(error) => write!(f, "{error}"),
            Self::Assembly(error) => write!(f, "{error}"),
            Self::Config(error) => write!(f, "channel map rejected: {error}"),
            Self::Step(error) => write!(f, "plant step failed: {error}"),
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
            Self::Assembly(error) => Some(error),
            Self::Config(error) => Some(error),
            Self::Step(error) => Some(error),
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

impl From<AssemblyError> for DemoError {
    fn from(error: AssemblyError) -> Self {
        Self::Assembly(error)
    }
}

impl From<ConfigError> for DemoError {
    fn from(error: ConfigError) -> Self {
        Self::Config(error)
    }
}

impl From<StepError> for DemoError {
    fn from(error: StepError) -> Self {
        Self::Step(error)
    }
}

impl From<IoError> for DemoError {
    fn from(error: IoError) -> Self {
        Self::Io(error)
    }
}

/// The resolved, runnable form of a validated plant model: everything a
/// caller needs to build the executor and drive the loop except the
/// pacing loop itself. Assemble the executor through
/// [`dcs_assembly::assemble`] with the standard component registry —
/// `dcs_controller::registry()`, exactly as [`run`] does; the executor
/// borrows the driver, so it cannot live here.
pub struct Assembly {
    /// The field driver the model resolves to through
    /// [`DriverRegistry::standard`], with the tank's [`FirstOrderLag`]
    /// merged into the still-open local simulated channel map before
    /// [`DriverPlan::build`](dcs_assembly::DriverPlan::build) finished
    /// it.
    pub driver: FanoutDriver,
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

/// The point the `kind` component's `port` resolves to — how the demo
/// locates the loop's setpoint, level, and valve points without hardcoding
/// point ids. `port_points` carries only the connections binding a port
/// straight to an `io_point`; a port-to-port wire resolves to an internal
/// point pair the executor's scan image owns, which the demo's field-side
/// seeding and lag legs cannot serve — so it reads as unbound here.
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

/// Resolves a validated plant model into the runnable pieces the demo
/// drives — see the crate docs for how the layers connect.
///
/// Driver resolution is the settled path: [`resolve_drivers`] against
/// [`DriverRegistry::standard`], the returned [`DriverPlan`](dcs_assembly::DriverPlan)'s
/// `sim_map` still open for the demo's process element — a
/// [`FirstOrderLag`] from the `pid`'s `out` point (the valve command) to
/// the `analog-input`'s `raw` point (the level raw signal) closing the
/// loop through the simulated field — revalidated and finished by
/// [`DriverPlan::build`](dcs_assembly::DriverPlan::build) into the
/// [`FanoutDriver`].
///
/// The genuinely demo-specific remainder is the loop's field geometry:
/// the `io_point`s the `pid` `sp`/`out` and `analog-input` `raw` ports
/// bind, and the `analog-input` block's scaling parameters the level
/// trace reports engineering units through.
pub fn assemble(model: &PlantModel) -> Result<Assembly, DemoError> {
    let errors = model.validate();
    if !errors.is_empty() {
        return Err(DemoError::Invalid(errors));
    }
    let mut plan = resolve_drivers(model, &DriverRegistry::standard())?;

    // The loop's points bind straight to io_points; `loop_point` reads
    // only the point-to-port connections — a port-to-port wire lands on
    // an internal point pair the executor's scan image serves, not the
    // field the demo writes and drives.
    let mut port_points: HashMap<(u64, String), PointId> = HashMap::new();
    for connection in &model.connections {
        if let (Endpoint::Point(point), Endpoint::Port(port))
        | (Endpoint::Port(port), Endpoint::Point(point)) = (&connection.from, &connection.to)
        {
            port_points.insert((port.component.0, port.name.clone()), *point);
        }
    }
    let setpoint = loop_point(model, &port_points, Pid::KIND, "sp")?;
    let level = loop_point(model, &port_points, AnalogInput::<f64>::KIND, "raw")?;
    let valve = loop_point(model, &port_points, Pid::KIND, "out")?;

    // The analog-input's scaling parameters, kept so the level trace can
    // report engineering units — the same declaration the block's own
    // constructor validates.
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
    plan.sim_map = plan
        .sim_map
        .with_element(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: valve,
            output: level,
            time_constant: PROCESS_TIME_CONSTANT,
            initial: EMPTY_TANK_RAW,
        }));
    plan.sim_map.validate()?;

    Ok(Assembly {
        driver: plan.build()?,
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
    let assembly = assemble(&model)?;
    let mut executor =
        dcs_assembly::assemble(&model, &dcs_controller::registry(), &assembly.driver)?;
    // The setpoint source is field-side: forcing a value on an `In` point
    // is how the simulation stands in for an operator-entered setpoint.
    assembly
        .driver
        .write(assembly.setpoint, Value::Float(SETPOINT_PERCENT))?;

    let mut levels = Vec::with_capacity(scans as usize);
    for _ in 0..scans {
        executor.scan();
        assembly.driver.step(SCAN_PERIOD)?;
        let Value::Float(raw) = assembly.driver.read(assembly.level)?.value else {
            unreachable!("a validated lag output is always a Float point")
        };
        levels.push(level_percent(&assembly.level_scaling, raw));
    }
    Ok(Run {
        levels,
        snapshot: executor.snapshot(),
    })
}
