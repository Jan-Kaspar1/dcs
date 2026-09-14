//! Model-driven assembly: turning a validated [`PlantModel`] into a
//! configured [`SimDriver`] and [`Executor`], per the model-driven-assembly
//! decision recorded in `docs/architecture.md`.
//!
//! The plant model declares devices, logical `io_point`s, component
//! instances, and the connections wiring them together. Assembly resolves
//! that document in two steps:
//!
//! - [`sim_channel_map`] translates the model's device/channel mapping
//!   into the simulated backend's [`ChannelMap`], open for callers to add
//!   simulated process elements (the field physics the model does not
//!   describe) before building the [`SimDriver`]; [`sim_driver`] is the
//!   no-elements convenience. Only [`SIM_DEVICE_PREFIX`] kinds (`sim`,
//!   `sim-ai`, …) can be built; any other kind is
//!   [`AssemblyError::UnknownDeviceKind`].
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
//!   an `io_point` — field or internal alike;
//! - `port → port` connections synthesize a pair of internal points — an
//!   `Out` point bound to the producing port and an `In` point bound to the
//!   consuming one — joined by an internal link in the executor's scan
//!   image, so a component-to-component wire delivers the value one scan
//!   later. A declared channel-less `Out`/`In` `io_point` pair wired the
//!   same way serves as the carrier instead;
//! - `point → point` connections describe a wire between two `io_point`s:
//!   field endpoints become a field-side [`Loopback`] in the driver (the
//!   `to` (`Out`) channel drives the `from` (`In`) channel observing it),
//!   internal endpoints an internal link through the scan image — same
//!   one-scan-later boundary. A mixed field/internal pair cannot be
//!   carried and is [`AssemblyError::MixedPointLink`].
//!
//! Every failure is a structured [`AssemblyError`] naming the offending
//! model element.

#![warn(missing_docs)]

mod assembly;
mod error;
mod registry;

pub use assembly::{SIM_DEVICE_PREFIX, assemble, sim_channel_map, sim_driver};
pub use error::{AssemblyError, BuildError, InternalPointError};
pub use registry::{ComponentRegistry, ComponentSpec};
