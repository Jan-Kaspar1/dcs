//! Model-driven assembly: turning a validated [`PlantModel`] into a
//! configured driver surface and [`Executor`], per the model-driven-assembly
//! decision recorded in `docs/architecture.md`.
//!
//! The plant model declares devices, logical `io_point`s, component
//! instances, and the connections wiring them together. Assembly resolves
//! that document in two steps:
//!
//! - [`resolve_drivers`] builds each device through a
//!   [`DriverRegistry`]: a mapping from `Device.kind` to a factory that
//!   receives the device's declared channels, its kind-specific
//!   addressing `parameters`, and the `io_point`s bound to it. The
//!   resulting [`DriverPlan`] leaves the shared local simulated channel
//!   map open for process elements (the field physics the model does not
//!   describe) before [`DriverPlan::build`] finishes the
//!   [`FanoutDriver`] — the one [`IoDriver`](dcs_core::IoDriver) surface
//!   that routes every point to its owning backend, so a model can mix
//!   local `sim*` devices with remote [`SIM_TCP_KIND`] ones.
//!   [`sim_channel_map`] and [`sim_driver`] remain the single-backend
//!   convenience for all-local-`sim` models. A device kind no factory
//!   serves is [`AssemblyError::UnknownDeviceKind`]; rejected
//!   parameters and unusable backends are
//!   [`AssemblyError::InvalidDeviceParameters`] and
//!   [`AssemblyError::DeviceBackend`].
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
//!   field endpoints a field-side [`Loopback`] — inside the local
//!   simulated map when both ends are sim-served, a [`FanoutDriver`]
//!   route across backends otherwise — internal endpoints an internal
//!   link through the scan image: the `to` (`Out`) point drives the
//!   `from` (`In`) point at the same one-scan-later boundary. A mixed
//!   field/internal pair cannot be carried and is
//!   [`AssemblyError::MixedPointLink`].
//!
//! Every failure is a structured [`AssemblyError`] naming the offending
//! model element.

#![warn(missing_docs)]

mod assembly;
mod drivers;
mod error;
mod registry;

pub use assembly::{SIM_DEVICE_PREFIX, assemble, sim_channel_map, sim_driver};
pub use drivers::{
    DeviceBackend, DeviceDriver, DeviceError, DevicePoint, DeviceSpec, DriverPlan, DriverRegistry,
    FanoutDriver, SIM_TCP_KIND, StepError, StepHook, resolve_drivers,
};
pub use error::{AssemblyError, BuildError, InternalPointError};
pub use registry::{ComponentRegistry, ComponentSpec};
