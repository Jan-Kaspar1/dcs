//! Deterministic simulated I/O backend for the `dcs-core`
//! [`IoDriver`](dcs_core::IoDriver) boundary.
//!
//! [`SimDriver`] is the driver all control code is developed and tested
//! against before field hardware exists. It is built from a [`ChannelMap`]
//! — the resolved, driver-side form of the plant model's I/O mapping — and
//! implements [`IoDriver`](dcs_core::IoDriver) so components, typed
//! [`Input`](dcs_core::Input)/[`Output`](dcs_core::Output) handles, and the
//! executor can use it through `&dyn IoDriver` alone.
//!
//! Three simulation features sit alongside plain point storage:
//!
//! - [`Loopback`] routing copies values written to an `Out` point onto its
//!   paired `In` point at the next [`SimDriver::step`], closing simple
//!   control loops without a process model;
//! - [`ProcessElement`]s ([`FirstOrderLag`], [`SecondOrderLag`],
//!   [`Integrator`], [`DeadTime`], [`Noise`], [`BoolFlow`],
//!   [`FlowSum`], [`ScaledFlow`], and [`Threshold`]) drive points from
//!   other points — a [`BoolFlow`] reads a `Bool` gate, a [`FlowSum`] a
//!   declared list, a [`ScaledFlow`] a `Float` demand, a [`Threshold`]
//!   a `Float` input whose bound crossing asserts its `Bool` contact —
//!   advanced by the caller-supplied `dt` each step, never by
//!   wall-clock time;
//! - [`SimDriver::inject_fault`] marks points with non-[`Good`](dcs_core::Quality::Good)
//!   quality or makes accesses fail with an [`IoError`](dcs_core::IoError),
//!   standing in for field-device failures in diagnostics tests.
//!
//! [`ScriptedDriver`] is the sibling backend for scripted scenarios: its
//! `In` points replay a tick-indexed script of values and qualities
//! declared in the plant model's device parameters, and its `Out` points
//! record every accepted write for inspection via
//! [`ScriptedDriver::writes`]. Where `SimDriver` simulates a plant,
//! `ScriptedDriver` replays one — which is what a second device kind
//! through the assembly registry needs.
//!
//! Stepping is fully deterministic: nothing in the driver reads a clock or
//! a random source, so identical write and step sequences always produce
//! identical [`Sample`](dcs_core::Sample)s.
//!
//! Every stored sample stays representable: a `Float` point never holds
//! NaN or an infinity, because JSON — the wire and checkpoint spelling —
//! has no literal for them (serde emits `null`, which no `Value` decode
//! reads back). Writes carrying a non-finite `Float` are refused with
//! [`IoError::InvalidValue`](dcs_core::IoError::InvalidValue), map and
//! script validation reject non-finite seeds, checkpoint restores refuse
//! them, and a step whose element arithmetic overflows commits nothing:
//! the element holds its last finite state and reports it
//! `Bad`/`out_of_range`, recovering on the first step whose arithmetic
//! lands finite — where a committed non-finite result would have stayed
//! corrupt until the field restarted.
//!
//! `SimDriver` also implements the driver half of the state-capture
//! contract — [`IoDriver::capture_state`](dcs_core::IoDriver::capture_state)
//! and [`IoDriver::restore_state`](dcs_core::IoDriver::restore_state) — so
//! an executor checkpoint carries the simulated field state (point
//! samples, injected faults, element accumulators) across to a standby.
//! Real drivers leave the contract unimplemented and observe the actual
//! process instead.

#![warn(missing_docs)]

mod driver;
mod map;
mod scripted;
mod state;

pub use driver::{Fault, PointInfo, SimDriver};
pub use map::{
    BoolFlow, ChannelId, ChannelMap, ConfigError, DeadTime, Direction, FirstOrderLag, FlowSum,
    Integrator, Loopback, Noise, PointBinding, ProcessElement, ScaledFlow, SecondOrderLag,
    Threshold,
};
pub use scripted::{RecordedWrite, ScriptEntry, ScriptError, ScriptedDriver};
