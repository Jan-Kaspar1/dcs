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
//! - [`ProcessElement`]s ([`FirstOrderLag`], [`Integrator`], and
//!   [`DeadTime`]) drive `Float` points from other points, advanced by
//!   the caller-supplied `dt` each step — never by wall-clock time;
//! - [`SimDriver::inject_fault`] marks points with non-[`Good`](dcs_core::Quality::Good)
//!   quality or makes accesses fail with an [`IoError`](dcs_core::IoError),
//!   standing in for field-device failures in diagnostics tests.
//!
//! Stepping is fully deterministic: nothing in the driver reads a clock or
//! a random source, so identical write and step sequences always produce
//! identical [`Sample`](dcs_core::Sample)s.
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

pub use driver::{Fault, SimDriver};
pub use map::{
    ChannelId, ChannelMap, ConfigError, DeadTime, Direction, FirstOrderLag, Integrator, Loopback,
    PointBinding, ProcessElement,
};
