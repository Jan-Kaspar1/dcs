//! Shared DCS contracts, to be developed against simulated plant I/O.
//!
//! The signal model (`Value`, `Quality`, `Sample`, `Tick`) and the logical
//! I/O identifiers (`SignalId`, `PointId`) are the contract shared by control
//! components, the plant model, and the monitoring UI. The I/O contracts
//! (`IoDriver`, `IoError`, `Input`, `Output`) let components declare typed
//! logical I/O without naming a bus technology; the plant model and the
//! driver supply the physical mapping. The telemetry contract
//! (`TelemetrySnapshot`) is the monitoring read-side: a serializable view
//! of a run's latest point samples and per-component diagnostics. The
//! command contract (`Command`, `CommandReceipt`, `CommandError`) is the
//! monitoring write-side: operator writes applied by the executor at a
//! deterministic scan boundary, each answered by a receipt. The state
//! contract (`StateMap`, `StateError`) is the redundancy groundwork: a
//! serializable map of named values components and drivers checkpoint
//! their internal state into and restore from. The descriptor contract
//! (`ComponentDescriptor`) is the UI-facing self-description: each
//! component's kind, label, named I/O ports, and tunable parameters —
//! enough for a monitoring UI to render a faceplate without per-kind
//! engineering. The history contract (`PointHistory`, `HistorySample`)
//! and the journal contract (`JournalEntry`, `JournalEvent`) are the
//! monitoring stream types: bounded per-point sample retention for trend
//! views and the tick-stamped transition log for audit views. The role
//! contract (`Role`, `StandbySync`, `RoleReport`, `SwitchError`) is the
//! redundancy contract: which instance of a controller pair owns field
//! writes, the transition states between, and the named switchover
//! refusals — so a monitoring UI treats the pair as one logical
//! controller.

#![warn(missing_docs)]

mod command;
mod descriptor;
mod history;
mod io;
mod journal;
mod role;
mod signal;
mod state;
mod telemetry;

pub use command::{Command, CommandError, CommandOutcome, CommandReceipt};
pub use descriptor::{
    ComponentDescriptor, ParameterDescriptor, ParameterRange, PortDescriptor, PortRole,
};
pub use history::{HistorySample, PointHistory};
pub use io::{Direction, Input, IoDriver, IoError, Output, PointType, TypedSample};
pub use journal::{JournalEntry, JournalEvent};
pub use role::{Divergence, Role, RoleReport, StandbySync, SwitchError};
pub use signal::{
    CoercionError, PointId, Quality, QualityReason, Sample, SignalId, Tick, Value, ValueKind,
};
pub use state::{StateError, StateMap};
pub use telemetry::{ComponentDiagnostics, PointTelemetry, TelemetrySnapshot};
