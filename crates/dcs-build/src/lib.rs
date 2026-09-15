//! Typed composition of plant-model documents.
//!
//! `dcs-build` is the engineering seam the vision asks for: plant
//! software composed in Rust with the contracts enforced at compile
//! time where the types can reach, and at
//! [`build`](PlantBuilder::build) — with named errors — where they
//! cannot. A composition emits exactly the versioned
//! [`PlantModel`] document `dcs-model` loads and validates; the builder
//! adds no fields and no schema of its own.
//!
//! ```rust
//! use dcs_build::specs::{AnalogInputSpec, PidSpec};
//! use dcs_build::{Direction, PlantBuilder, PointId, SignalId, Value, parameters};
//!
//! let mut plant = PlantBuilder::new();
//!
//! // A simulated device and its channels — `f64` analog channels.
//! let sim = plant.device("sim").id;
//! let setpoint = plant.channel::<f64>(sim, "setpoint", Direction::In);
//! let level = plant.channel::<f64>(sim, "level-raw", Direction::In);
//! let valve = plant.channel::<f64>(sim, "valve", Direction::Out);
//!
//! // Logical I/O points bound to the channels; the setpoint is an
//! // operator-writable point.
//! let sp = plant.field_input::<f64>(PointId(10), setpoint, true);
//! let pv = plant.field_input::<f64>(PointId(11), level, false);
//! let cmd = plant.field_output::<f64>(PointId(12), valve);
//!
//! plant
//!     .signal(SignalId(100), "level-setpoint", sp)
//!     .unit("m");
//!
//! // Component instances from their kind specs; `add` returns the
//! // instance's ports as typed handles.
//! let ai = plant.add(AnalogInputSpec::<f64>::new(parameters([
//!     ("raw_min", Value::Float(4.0)),
//!     ("raw_max", Value::Float(20.0)),
//!     ("eng_min", Value::Float(0.0)),
//!     ("eng_max", Value::Float(100.0)),
//! ])));
//! let pid = plant.add(PidSpec::new(parameters([
//!     ("kp", Value::Float(0.1)),
//!     ("dt", Value::Float(0.1)),
//!     ("out_min", Value::Float(4.0)),
//!     ("out_max", Value::Float(20.0)),
//! ])));
//!
//! plant.connect(sp, pid.sp);
//! plant.connect(pv, ai.raw);
//! plant.connect(ai.out, pid.pv);
//! plant.connect(&pid.out, cmd);
//! plant.connect(pv, cmd);
//!
//! let model = plant.build().unwrap();
//! assert_eq!(model.components.len(), 2);
//! ```
//!
//! `connect` accepts only a producing [`Source<T>`] feeding a consuming
//! [`Sink<T>`], so a wrong-direction connection fails to compile —
//! `pid.out` produces; it cannot consume what another `Out` port
//! produces:
//!
//! ```compile_fail
//! use dcs_build::specs::TimerSpec;
//! use dcs_build::{PlantBuilder, Value, parameters};
//!
//! let mut plant = PlantBuilder::new();
//! let on = plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
//! let off = plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
//! // Both ends produce; `Source` is not a `Sink`.
//! plant.connect(on.out, off.out);
//! ```
//!
//! and so does a wrong-kind one — `count` carries `i64`, `input`
//! consumes `bool`:
//!
//! ```compile_fail
//! use dcs_build::specs::{CounterSpec, TimerSpec};
//! use dcs_build::{PlantBuilder, Value, parameters};
//!
//! let mut plant = PlantBuilder::new();
//! let counter = plant.add(CounterSpec::new(parameters([("preset", Value::Int(4))])));
//! let timer = plant.add(TimerSpec::new(parameters([("delay_ticks", Value::Int(3))])));
//! plant.connect(counter.count, timer.input);
//! ```
//!
//! ## Kinds without a spec
//!
//! [`DynamicSpec`] registers a component kind whose interface is known
//! only from data: its ports connect through [`Source<Dynamic>`] /
//! [`Sink<Dynamic>`] ends, which occupy only their producing or
//! consuming position but defer name, direction, and value-kind checks
//! to [`build`](PlantBuilder::build) as named
//! [`ValidationError`](dcs_model::ValidationError)s. Mixing a statically
//! typed end with a dynamic one requires an explicit
//! [`erase`](Source::erase) so the dynamic path never silently swallows
//! a static check.
//!
//! ## Specs versus descriptors
//!
//! `dcs-build` depends on `dcs-core` and `dcs-model` only, so the kind
//! specs in [`specs`] are data mirrors of the registered `dcs-blocks`
//! kinds' descriptors, not imports of them. The documented convention
//! keeping them honest: a spec declares the same kind string, the same
//! ports in `io_requirements` order, and the same parameters in
//! `describe` order — and `dcs-blocks/tests/spec_drift.rs` compares each
//! spec against its kind's `describe()` output and pins the spec table
//! against the registered-kind list, so a spec that drifts or a
//! registered kind with no spec fails CI beside the kind it mirrors.

#![warn(missing_docs)]

mod builder;
pub mod dosing;
mod endpoint;
mod spec;
pub mod specs;
pub mod station;

pub use builder::{BuildError, PlantBuilder, SignalBuilder};
pub use endpoint::{Dynamic, InPoint, OutPoint, Sink, Source};
pub use spec::{
    DynamicInstance, DynamicSpec, FINITE_F64, FRACTION_F64, NONNEGATIVE_F64, NONNEGATIVE_INT,
    POSITIVE_F64, ParamDecl, Parameters, PortDecl, Spec, optional, parameters, port, required,
};

// The contract vocabulary a composition speaks: re-exported so a
// `dcs-build` consumer needs no other crate's imports.
pub use dcs_core::{Direction, PointId, PointType, SignalId, Value, ValueKind};
pub use dcs_model::{ChannelRef, ComponentId, DeviceId, Endpoint, PortRef};
