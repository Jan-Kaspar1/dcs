//! The versioned plant model: the single contract shared by engineering
//! data, controllers, and the monitoring UI.
//!
//! A [`PlantModel`] document — JSON, per the serialization decision recorded
//! in `docs/architecture.md` — declares [`Device`]s with named [`Channel`]s,
//! logical [`IoPoint`]s bound to device channels or carried internally by
//! the controller's scan image, [`Signal`]s sourced by points,
//! [`ComponentInstance`]s with named [`Port`]s, and the
//! [`Connection`]s that wire points and ports together.
//! [`PlantModel::load`] parses a document, checks its version, and validates
//! it; [`PlantModel::validate`] applies the same checks to a model built
//! programmatically, returning structured [`ValidationError`]s that name the
//! offending element. For monitoring consumers, [`PlantModel::signal_index`]
//! derives a flat [`SignalIndex`] resolving every point to its signal
//! metadata without walking the model graph. For revision review,
//! [`PlantModel::diff`] reports the elements a revised document adds,
//! removes, or changes as a structured [`ModelDiff`]. For
//! engineering-quality review, [`PlantModel::lint`] reports advisory
//! [`LintFinding`]s — valid but probably unfinished declarations, like an
//! io_point no signal sources. For non-Rust tooling,
//! [`PlantModel::json_schema`] emits the document's JSON Schema — covering
//! the structural and intra-element rules the schema language can express
//! while cross-reference and wiring checks stay with `load`.

#![warn(missing_docs)]

mod deploy_schema;
mod diff;
mod index;
mod lint;
mod model;
mod schema;
mod validate;

pub use deploy_schema::deployment_manifest_schema;
pub use diff::{ChangeKind, ElementChange, FieldChange, ModelDiff};
pub use index::{ComponentRecord, PointSignal, SignalIndex};
pub use lint::{LintFinding, LintRule};
pub use model::{
    Channel, ChannelRef, ComponentId, ComponentInstance, Connection, Device, DeviceId, Direction,
    Endpoint, IoPoint, LoadError, MODEL_VERSION, PlantModel, Port, PortRef, Rationalization,
    Signal,
};
pub use validate::{End, IdCollection, ValidationError};
