//! Shared DCS contracts, to be developed against simulated plant I/O.
//!
//! The signal model (`Value`, `Quality`, `Sample`, `Tick`) and the logical
//! I/O identifiers (`SignalId`, `PointId`) are the contract shared by control
//! components, the plant model, and the monitoring UI.

#![warn(missing_docs)]

mod signal;

pub use signal::{
    CoercionError, CoercionTarget, PointId, Quality, QualityReason, Sample, SignalId, Tick, Value,
};
