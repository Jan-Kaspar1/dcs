//! The versioned plant model: the single contract shared by engineering
//! data, controllers, and the monitoring UI.
//!
//! A [`PlantModel`] document — JSON, per the serialization decision recorded
//! in `docs/architecture.md` — declares [`Device`]s with named [`Channel`]s,
//! logical [`IoPoint`]s bound to device channels, [`Signal`]s sourced by
//! points, [`ComponentInstance`]s with named [`Port`]s, and the
//! [`Connection`]s that wire points and ports together.
//! [`PlantModel::load`] parses a document, checks its version, and validates
//! it; [`PlantModel::validate`] applies the same checks to a model built
//! programmatically, returning structured [`ValidationError`]s that name the
//! offending element.

#![warn(missing_docs)]

mod model;
mod validate;

pub use model::{
    Channel, ChannelRef, ComponentId, ComponentInstance, Connection, Device, DeviceId, Direction,
    Endpoint, IoPoint, LoadError, MODEL_VERSION, PlantModel, Port, PortRef, Signal,
};
pub use validate::{End, IdCollection, ValidationError};
