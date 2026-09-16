//! The served schema and live-resource views — decision 82's serving
//! half (`docs/architecture.md`).
//!
//! [`interface`](crate::interface) declares each component kind's
//! machine-readable contract; this module carries the documents a
//! serving layer — `dcs-monitor`'s `GET /schema` and `GET /resources`
//! — derives from its published read model, so a generic consumer never
//! reconstructs an instance's surface from scattered endpoints:
//!
//! - [`SchemaView`] is the served block-interface registry: one
//!   instance-level [`BlockInterface`] per component instance, derived
//!   through [`block_interfaces`](crate::block_interfaces) from the
//!   snapshot's bound-point-annotated descriptors, so every kind the
//!   run instantiated is covered once per instance.
//! - [`ResourceView`] is the matching live state: per instance, the
//!   measurement and state values with quality, the current
//!   configuration values, each command's availability or refusal
//!   reason, and the recently emitted events attributed to it.
//!
//! Both views stamp the publication sequence and tick they were derived
//! from, so a consumer can check a fetched view against a concurrently
//! fetched snapshot's `publication` section — the same seq means the
//! same read model. The views are serde-additive like the rest of the
//! contract: a document carrying categories or fields this version does
//! not know still deserializes, and a document predating a category
//! reads it empty.

use crate::interface::BlockInterface;
use crate::journal::JournalEntry;
use crate::signal::{PointId, Sample, Tick, Value};
use serde::{Deserialize, Serialize};

/// `GET /schema`'s answer: the served block-interface registry.
///
/// One [`ComponentInterface`] per served component instance, in the
/// snapshot's descriptor (scan) order — the instance-level form of the
/// decision-82 contract, derived from each descriptor as the serving
/// layer annotated it: port-adapted resources carry their bound
/// [`PointId`]s and the signal index's `unit` annotation. A kind
/// instantiated twice appears once per instance — instance-dependent
/// interfaces (an indexed port set, for example) differ legitimately,
/// so the registry is per instance rather than per kind.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SchemaView {
    /// The publication sequence the view was derived from — the seq the
    /// snapshot's `publication` section reports as `published` for the
    /// same read model.
    pub publication: u64,
    /// The publication's scan tick.
    pub tick: Tick,
    /// One interface per served component instance, in scan order.
    #[serde(default)]
    pub interfaces: Vec<ComponentInterface>,
}

/// One component instance's served interface — a [`SchemaView`] entry.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentInterface {
    /// The instance's name — the [`ComponentDescriptor`](crate::ComponentDescriptor)
    /// `name` the interface was derived from; the join key the
    /// snapshot's descriptors and diagnostics and the
    /// [`ResourceView`]'s components share.
    pub name: String,
    /// The instance-level interface — `kind` keys the kind-level
    /// registry the drift pin checks.
    pub interface: BlockInterface,
}

/// `GET /resources`'s answer: per-instance live resource state joined
/// onto the served schema.
///
/// One [`ComponentResources`] per component instance, in scan order,
/// each collection parallel to the instance's [`BlockInterface`]
/// collection — `measurements[i]` is the live reading of the schema's
/// `measurements[i]`, so a consumer zips the two rather than
/// re-deriving identities. `events` is the serving store's retained
/// journal tail attributed to the instance — bounded like every read
/// side — which between-scans control-plane entries (a refused
/// command, say) can reach ahead of the stamped publication.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResourceView {
    /// The publication sequence the values were read from.
    pub publication: u64,
    /// The publication's scan tick.
    pub tick: Tick,
    /// One entry per component instance, in scan order.
    #[serde(default)]
    pub components: Vec<ComponentResources>,
}

/// One component instance's live resource state — a [`ResourceView`]
/// entry, parallel to the instance's interface collections.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentResources {
    /// The instance's name — the join key.
    pub name: String,
    /// The instance's kind — the `BlockInterface::kind` its schema
    /// entry serves under.
    pub kind: String,
    /// Live readings of the interface's `measurements`, in the same
    /// order.
    #[serde(default)]
    pub measurements: Vec<ResourceSample>,
    /// Current values of the interface's `configuration`, in the same
    /// order — the snapshot's `parameters` section joined by name.
    #[serde(default)]
    pub configuration: Vec<ConfigValue>,
    /// Live readings of the interface's `state`, in the same order.
    #[serde(default)]
    pub state: Vec<ResourceSample>,
    /// Per-command availability of the interface's `commands`, in the
    /// same order.
    #[serde(default)]
    pub commands: Vec<CommandState>,
    /// Recently emitted events attributed to the instance — the
    /// retained journal tail in `seq` order: point transitions on its
    /// bound points, its commands' settled receipts, its step failures,
    /// and its kind-emitted events.
    #[serde(default)]
    pub events: Vec<JournalEntry>,
}

/// A measurement's or state property's live reading — value and
/// quality together, straight from the bound point's latest sample.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResourceSample {
    /// The resource's stable identity — the interface entry's `name`.
    pub name: String,
    /// The bound point the reading comes from — the interface entry's
    /// `point` annotation echoed so a consumer joins to `/history`
    /// without re-reading the schema; absent when the port is unbound.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
    /// The bound point's latest sample — value, quality, and the tick
    /// it was observed at — `None` while the point has produced none
    /// and on an unbound port. Serializes like
    /// [`PointTelemetry::sample`](crate::PointTelemetry): `null`, not
    /// absent.
    #[serde(default)]
    pub sample: Option<Sample>,
}

/// A configuration property's live value — the current tune the
/// snapshot's `parameters` section reports.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfigValue {
    /// The property's stable identity — the interface entry's `name`.
    pub name: String,
    /// The parameter's current value — `None` when the component
    /// reports none for the declared name.
    #[serde(default)]
    pub value: Option<Value>,
}

/// A command's live availability — the resource view's answer to "what
/// a submission would meet now", the [`CommandSpec`](crate::CommandSpec)
/// `availability` rule read against the published plant.
///
/// The redundancy role's gate is reported separately — `GET /role`:
/// the views derive from the published read model, never the executor
/// lock, and a standby's commands refuse
/// [`NotActive`](crate::CommandError::NotActive) on submission
/// regardless of what the instance's own rules report here.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandState {
    /// The command's stable identity — the interface entry's `name`.
    pub name: String,
    /// The bound point a port-adapted command targets — the interface
    /// entry's `point` annotation echoed; absent for parameter- and
    /// kind-declared commands.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
    /// Whether a submission is admissible now — the command's
    /// availability rule read against the published read model.
    pub available: bool,
    /// Why a submission refuses now — the named
    /// [`CommandError`](crate::CommandError)'s text, exactly as the
    /// receipted path would answer it; absent when `available`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refusal: Option<String>,
}
