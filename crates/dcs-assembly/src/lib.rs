//! Model-driven assembly: turning a validated [`PlantModel`] into a
//! configured [`SimDriver`] and [`Executor`], per the model-driven-assembly
//! decision recorded in `docs/architecture.md`.
//!
//! The plant model declares devices, logical `io_point`s, component
//! instances, and the connections wiring them together. Assembly resolves
//! that document in two steps:
//!
//! - [`sim_driver`] translates the model's device/channel mapping into the
//!   simulated backend's [`ChannelMap`] and builds the [`SimDriver`] serving
//!   every declared point. Only the [`SIM_DEVICE_KIND`] device kind can be
//!   built; any other kind is [`AssemblyError::UnknownDeviceKind`].
//! - [`assemble`] constructs every component instance through a
//!   [`ComponentRegistry`], binds each declared logical I/O requirement to
//!   the `io_point` the model wires to its port — synthesizing internal
//!   points for port-to-port wiring — verifies direction and value kind
//!   against the driver's point map, and returns the ready [`Executor`].
//!
//! Because the returned [`Executor`] borrows the driver, the two calls split
//! along the borrow: build the driver first, assemble against it, then step
//! the driver between scans to advance the simulated plant.
//!
//! Wiring vocabulary, resolved from [`Connection`](dcs_model::Connection)
//! ends:
//!
//! - `point → port` and `port → point` connections bind a component port to
//!   an `io_point`;
//! - `port → port` connections synthesize a pair of internal points — an
//!   `Out` point bound to the producing port and an `In` point bound to the
//!   consuming one — joined by a [`Loopback`] in the driver, so a
//!   component-to-component wire delivers the value one scan later;
//! - `point → point` connections describe a field-side wire on simulated
//!   devices: the `to` (`Out`) channel drives the `from` (`In`) channel
//!   observing it, also as a [`Loopback`].
//!
//! Every failure is a structured [`AssemblyError`] naming the offending
//! model element.

#![warn(missing_docs)]

mod assembly;
mod error;
mod registry;

pub use assembly::{SIM_DEVICE_KIND, assemble, sim_driver};
pub use error::{AssemblyError, BuildError};
pub use registry::{ComponentRegistry, ComponentSpec};
