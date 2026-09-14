//! The cyclic component executor: a deterministic fixed-step scan over
//! reusable control components.
//!
//! A [`Component`] is a unit of control logic that declares its logical I/O
//! up front — point ids, value kinds, and directions — and steps once per
//! scan through a scoped [`ComponentIo`] view. It never names a device or
//! bus; per the I/O abstraction decision, binding points to physical
//! channels is the plant model's and the driver's business.
//!
//! [`Executor`] owns the scan. Wiring it checks every component's declared
//! I/O against the driver's [`PointMap`] and refuses to run on a type or
//! direction mismatch. Each scan reads every `In` point into a scan image,
//! steps the components in their configured order, then writes the image's
//! `Out` points back to the driver. Time is a virtual [`Tick`] counter
//! advanced by the executor and stamped onto every sample the scan
//! produces — nothing here or in component code reads a wall clock, so a
//! run of `N` scans is exactly reproducible.
//!
//! A [`PointMap`] entry may be internal — carried by the scan image
//! rather than a driver channel. An internal `In` point holds a declared
//! initial until a command writes it; an internal `Out` point records
//! component writes for monitoring; an internal link routes an `Out`
//! sample onto an `In` point at each scan's input phase, carrying
//! component-to-component wiring one scan later.
//!
//! [`Executor::snapshot`] is the monitoring read-side: it returns the
//! `dcs-core` [`TelemetrySnapshot`](dcs_core::TelemetrySnapshot) contract —
//! the latest sample of every known point, per-component diagnostics, and
//! each component's [`Component::describe`] self-description — serde-
//! serializable so a monitoring UI needs only the shared contracts.
//!
//! [`Executor::submit_command`] is the monitoring write-side: operator
//! [`Command`](dcs_core::Command)s queue between scans and apply at the
//! head of the next scan — before the input read — each producing a
//! [`CommandReceipt`](dcs_core::CommandReceipt) in the
//! [`receipts`](Executor::receipts) log. The command surface is the map's
//! writable `In` points: writes to unmarked points and to every `Out`
//! point are refused at submission with
//! [`CommandError::NotWritable`](dcs_core::CommandError::NotWritable).
//!
//! [`Executor::checkpoint`] and [`Executor::restore`] are the redundancy
//! groundwork: a serde-serializable [`Checkpoint`] carries the tick, each
//! component's [`capture_state`](Component::capture_state), the driver's
//! state when it implements the contract, and the last written outputs,
//! letting a standby rebuild an equivalent executor mid-run.
//!
//! [`Peer`] is the switchover half of redundancy: it wraps an executor
//! assembled behind a [`WriteGate`], reports the instance's
//! [`Role`](dcs_core::Role), and applies promotion and demotion at a scan
//! boundary so exactly one peer of a pair writes the field at a time —
//! a promoted tracking standby continues the checkpointed run bumplessly.

#![warn(missing_docs)]

mod checkpoint;
mod component;
mod executor;
mod gate;
mod peer;
mod standby;

pub use checkpoint::{
    CHECKPOINT_FORMAT_VERSION, Checkpoint, RestoreError, SUPPORTED_FORMAT_VERSIONS,
};
pub use component::{Component, ComponentIo, ComponentIoExt, IoRequirement, StepError};
pub use executor::{
    ComponentStatus, Executor, LinkError, PointMap, PointSpec, ScanError, WiringError,
};
pub use gate::WriteGate;
pub use peer::{ApplyError, Peer, RoleChange};
pub use standby::{Standby, StandbyState};
