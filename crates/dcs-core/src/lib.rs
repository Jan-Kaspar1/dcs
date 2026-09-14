//! Shared DCS contracts, to be developed against simulated plant I/O.
//!
//! The signal model (`Value`, `Quality`, `Sample`, `Tick`) and the logical
//! I/O identifiers (`SignalId`, `PointId`) are the contract shared by control
//! components, the plant model, and the monitoring UI. The I/O contracts
//! (`IoDriver`, `IoError`, `Input`, `Output`) let components declare typed
//! logical I/O without naming a bus technology; the plant model and the
//! driver supply the physical mapping.

#![warn(missing_docs)]

mod io;
mod signal;

pub use io::{Input, IoDriver, IoError, Output, PointType, TypedSample};
pub use signal::{
    CoercionError, PointId, Quality, QualityReason, Sample, SignalId, Tick, Value, ValueKind,
};
