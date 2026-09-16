//! The versioned block interface: one schema-driven contract per
//! component `kind`.
//!
//! Decision 82 (`docs/architecture.md`) gives every registered component
//! kind a single machine-readable interface covering five explicit
//! resource collections — `measurements`, `configuration`, `state`,
//! `commands`, and `events` — so a generic consumer reads one declared
//! vocabulary instead of reconstructing the surface from descriptors,
//! the signal index, the generic command variants, and journal
//! transitions separately.
//!
//! The contract is *derived*, not parallel: [`BlockInterface::from_descriptor`]
//! adapts the kind's [`ComponentDescriptor`] — the same `describe()`
//! output the registry path serves in the snapshot — into the five
//! collections, and the
//! `dcs-blocks` drift test pins the derivation against the `dcs-build`
//! spec table and the registered `kind` set, so the build-time spec,
//! the runtime descriptor, and the served interface cannot drift.
//!
//! Mapping rules:
//!
//! - Every [`PortDescriptor`](crate::PortDescriptor) lands in exactly one collection: a port
//!   hinting [`PortRole::Status`] is `state`, every other port is a
//!   `measurement`. Each entry keeps the port's name (its stable
//!   resource identity), [`Direction`], [`ValueKind`], and role, plus
//!   the bound [`PointId`] when the descriptor is instance-annotated.
//! - Every [`ParameterDescriptor`] is a `configuration` entry carrying
//!   its kind, declared [`ParameterRange`], and
//!   [`ConfigCapability::Tunable`] — the descriptor-declared tuning
//!   surface `set_parameter` already enforces.
//! - `commands` adapt the generic [`Command`] variants: every `In`
//!   port gains `write_value`, `force_point`, and `unforce_point`
//!   entries whose availability is the bound point's model-declared
//!   `writable` mark, and every configuration entry gains a
//!   `set_parameter` entry. Command identities are namespaced —
//!   `"write_value:<port>"`, `"set_parameter:<parameter>"` — and the
//!   request schema carries only the payload fields the generic
//!   variant leaves after the command's identity absorbs the
//!   addressing ones.
//! - `events` adapt the journaled transitions: every port's bound point
//!   may emit `quality_changed`, a `Bool`/`Int` port's bound point may
//!   emit `point_changed` where the model marks it `journaled`, every
//!   command settles through `command_settled`, and the component's own
//!   `step` failures surface as `step_failed`.
//! - Kind-declared vocabulary sits beside the adapted entries under
//!   the `Declared` provenance: a [`CommandDecl`](crate::CommandDecl)
//!   becomes a `commands` entry an
//!   [`Invoke`](crate::Command::Invoke) submission addresses, and an
//!   [`EventDecl`](crate::EventDecl) becomes an `events` entry whose
//!   emissions journal as
//!   [`EventEmitted`](crate::JournalEvent::EventEmitted). Names are
//!   unique across each collection.
//!
//! Named-command execution and typed event emission are the runtime
//! tranche: this slice declares the contract surface — the invoke
//! command, the named refusals, the declared-provenance entries, and
//! the emitted-event journal record. Everything here is
//! serde-additive — a document carrying fields this version does not
//! know still deserializes, matching the optional-field convention the
//! rest of the contract follows.

use crate::descriptor::{ComponentDescriptor, ParameterRange, PortRole};
use crate::io::Direction;
use crate::signal::{PointId, ValueKind};
use serde::{Deserialize, Serialize};

/// The block-interface schema version this implementation emits —
/// stamped into [`BlockInterface::version`], mirroring the plant
/// model's `version`/`MODEL_VERSION` convention.
pub const INTERFACE_VERSION: u32 = 1;

/// `serde` helper for optional flags: a field absent from an older
/// document deserializes `false`, and `false` serializes without the
/// key — the optional-field convention `IoPoint::writable` follows.
fn is_false(flag: &bool) -> bool {
    !*flag
}

/// A read-only sampled value a block reports: one entry of
/// [`BlockInterface::measurements`].
///
/// Adapted from a [`PortDescriptor`](crate::PortDescriptor): the port's name is the stable
/// identity, `direction` and `kind` record what the bound point
/// carries, and `role` keeps the semantic hint. The served value is
/// the bound point's [`Sample`](crate::Sample), so every measurement
/// carries the decision-2 quality flag with its value; `unit` is the
/// instance-level annotation the serving layer fills from the bound
/// point's signal — `None` in a kind-level interface.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Measurement {
    /// The resource's stable identity within the interface — the
    /// port's name.
    pub name: String,
    /// The direction of the port the measurement is adapted from.
    pub direction: Direction,
    /// The sampled value's kind.
    pub kind: ValueKind,
    /// The port's semantic role hint, when declared.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<PortRole>,
    /// The logical point the port is bound to — serving-layer
    /// annotation, `None` in a kind-level interface, mirroring
    /// [`PortDescriptor::point`](crate::PortDescriptor::point).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
    /// Engineering unit of the bound point's signal — serving-layer
    /// annotation resolved through the signal index.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
}

/// Where a [`StateProperty`]'s authoritative record lives.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StatePersistence {
    /// The state is the bound point's scan-image sample: reported in
    /// the snapshot each scan, and durable only where the model marks
    /// the bound point `journaled` — the transition then also lands in
    /// the journal as a `point_changed` entry.
    BoundPoint,
}

/// A read-only runtime state a block reports: one entry of
/// [`BlockInterface::state`].
///
/// Adapted from a [`PortDescriptor`](crate::PortDescriptor) hinting [`PortRole::Status`] — the
/// alarm, fault, mode, and acknowledgment flags a block reports rather
/// than measures — keeping the port's name, direction, kind, role, and
/// bound-point annotation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StateProperty {
    /// The resource's stable identity within the interface — the
    /// port's name.
    pub name: String,
    /// The direction of the port the state is adapted from.
    pub direction: Direction,
    /// The state's value kind.
    pub kind: ValueKind,
    /// The port's semantic role hint, when declared.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role: Option<PortRole>,
    /// The logical point the port is bound to — serving-layer
    /// annotation, `None` in a kind-level interface.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
    /// Where the state's durable record lives.
    pub persistence: StatePersistence,
}

/// Whether a [`ConfigProperty`] accepts runtime writes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ConfigCapability {
    /// Tunable at runtime through the receipted `set_parameter`
    /// command — the descriptor's declared editing surface, whose
    /// validation (declared name, value kind, declared range) the
    /// command path already enforces.
    Tunable,
    /// Engineering-fixed: the value is set at build time from the
    /// model's parameter map and is not a runtime command target.
    EngineeringFixed,
}

/// A configuration value a block carries: one entry of
/// [`BlockInterface::configuration`].
///
/// Adapted from a [`ParameterDescriptor`](crate::ParameterDescriptor):
/// `name` is the stable identity, `kind` and `range` are the declared
/// constraints the write path enforces, and `capability` records the
/// access direction — every descriptor-declared parameter is
/// [`Tunable`](ConfigCapability::Tunable). The current value is served
/// live in the snapshot's per-component `parameters` section; the
/// engineering default is the model document's parameter map, so a
/// consumer reads "what it holds now" and "what the engineer declared"
/// from those existing surfaces rather than a duplicated copy here.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ConfigProperty {
    /// The resource's stable identity within the interface — the
    /// parameter's name.
    pub name: String,
    /// The value kind the parameter accepts.
    pub kind: ValueKind,
    /// The inclusive bounds the parameter accepts, when bounded — the
    /// declared `allowed values` constraint.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub range: Option<ParameterRange>,
    /// Whether the property accepts runtime writes.
    pub capability: ConfigCapability,
}

/// One typed argument of a [`CommandSpec`]'s request schema.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandArgument {
    /// The argument's name within the request.
    pub name: String,
    /// The value kind the argument accepts — strict, never coercing,
    /// like the command path it adapts.
    pub kind: ValueKind,
}

/// When a [`CommandSpec`] may be submitted at all — the static
/// availability rule; live availability is the served resource state
/// a later tranche adds.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandAvailability {
    /// Submittable whenever the component instance exists: refusal is
    /// only by the command's own named reasons — unknown component or
    /// parameter, type mismatch, out-of-range or invalid value, or the
    /// instance's reported role.
    Always,
    /// Submittable when the command's bound point is a model-declared
    /// writable `In` point — the `SignalIndex`/`io_point` `writable`
    /// mark; a submission against an unmarked point is refused
    /// `not_writable`, and `Out` points are never command targets.
    BoundPointWritable,
    /// Submittable when the kind's own availability predicate permits —
    /// the declared-refusal form a [`CommandDecl`](crate::CommandDecl)
    /// reports: the component's kind decides per submission, and a
    /// refusal is answered
    /// [`CommandRefused`](crate::CommandError::CommandRefused) carrying
    /// the kind's declared refusal reason.
    KindDeclared,
}

/// The generic [`Command`](crate::Command) variant a [`CommandSpec`]
/// entry is adapted from — the provenance pinning the entry to the
/// existing receipted command path rather than a parallel one.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdaptedCommand {
    /// [`Command::WriteValue`](crate::Command::WriteValue) against the
    /// port's bound point.
    WriteValue,
    /// [`Command::ForcePoint`](crate::Command::ForcePoint) against the
    /// port's bound point.
    ForcePoint,
    /// [`Command::UnforcePoint`](crate::Command::UnforcePoint) against
    /// the port's bound point.
    UnforcePoint,
    /// [`Command::SetParameter`](crate::Command::SetParameter) against
    /// the named configuration property.
    SetParameter,
    /// The kind declares the command natively — a
    /// [`CommandDecl`](crate::CommandDecl) entry invoked through
    /// [`Command::Invoke`](crate::Command::Invoke), not adapted from a
    /// generic variant.
    Declared,
}

/// A named command a block accepts: one entry of
/// [`BlockInterface::commands`].
///
/// Every submission answers with the existing
/// [`CommandReceipt`](crate::CommandReceipt) — an
/// [`accepted`](crate::CommandOutcome::Accepted) submission applies at
/// the deterministic scan boundary and settles
/// [`applied`](crate::CommandOutcome::Applied) or
/// [`rejected`](crate::CommandOutcome::Rejected) with the named
/// [`CommandError`](crate::CommandError) reason, journaled through
/// `command_settled`. This tranche declares the command surface only;
/// named-command execution beyond the adapted generic variants is a
/// later slice.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommandSpec {
    /// The command's stable identity within the interface —
    /// `"<verb>:<target>"`, e.g. `write_value:sp` or
    /// `set_parameter:kp`.
    pub name: String,
    /// The typed request schema: the arguments a submission carries.
    /// The generic variant's addressing fields — `point` for the point
    /// commands, `component`/`name` for `set_parameter` — are absorbed
    /// into the command's identity, so `write_value` and
    /// `force_point` carry a `value` argument of the port's kind,
    /// `unforce_point` carries none, and `set_parameter` carries a
    /// `value` argument of the property's kind.
    pub request: Vec<CommandArgument>,
    /// The submission condition — a submission outside it is refused
    /// with the named reason.
    pub availability: CommandAvailability,
    /// The generic command variant this entry is adapted from —
    /// [`Declared`](AdaptedCommand::Declared) when the kind declares
    /// the command natively.
    pub adapted: AdaptedCommand,
    /// The logical point a port-adapted command targets — the port's
    /// bound point, `None` in a kind-level interface and for
    /// parameter-adapted commands.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
}

/// The type of one field in an [`EventSpec`]'s payload schema.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventFieldKind {
    /// A [`Value`](crate::Value) of the named kind.
    Value(ValueKind),
    /// A [`Quality`](crate::Quality) flag.
    Quality,
    /// A [`CommandReceipt`](crate::CommandReceipt) — the settled
    /// command's answer.
    Receipt,
    /// Free text — e.g. a step error's message.
    Text,
}

/// One typed field of an [`EventSpec`]'s payload schema.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EventField {
    /// The field's name within the payload.
    pub name: String,
    /// The field's type.
    pub kind: EventFieldKind,
    /// Whether the field may be null — `from` fields on a point's
    /// first observed sample, matching the journal's convention.
    #[serde(default, skip_serializing_if = "is_false")]
    pub optional: bool,
}

/// How an emitted event is retained — the small initial vocabulary
/// aligned with the existing consumers' stores.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventRetention {
    /// The durable, ordered transition journal —
    /// [`JournalEntry`](crate::JournalEntry) stream — where the adapted
    /// transitions already land.
    Journal,
    /// Bounded per-point history — the [`PointHistory`](crate::PointHistory)
    /// ring a trend view reads.
    History,
    /// Latest-value telemetry — the snapshot's points section.
    Latest,
}

/// When an [`EventSpec`] is emitted — the static emission rule.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventEmission {
    /// Emitted for each change the scan observes on the bound point.
    OnObservedChange,
    /// Emitted only where the bound point is model-declared
    /// `journaled` — the durable transition record is opt-in per
    /// point, and valid on `Bool`/`Int` points only.
    WhenJournaled,
    /// Emitted when a command against the block reaches its final
    /// receipted outcome.
    OnCommandSettled,
    /// Emitted when a component `step` reports an error — the scan
    /// continues per the executor's contract.
    OnStepFailure,
    /// Emitted by the component's kind itself during a scan — the
    /// kind-emitted form an [`EventDecl`](crate::EventDecl) reports;
    /// a durable-retention emission journals as
    /// [`EventEmitted`](crate::JournalEvent::EventEmitted) at the
    /// producing scan's tick.
    KindEmitted,
}

/// The [`JournalEvent`](crate::JournalEvent) variant an [`EventSpec`]
/// entry is adapted from — the provenance pinning the event vocabulary
/// to the existing journaled transitions.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AdaptedEvent {
    /// [`JournalEvent::PointChanged`](crate::JournalEvent::PointChanged)
    /// — the bound point's observed value transition.
    PointChanged,
    /// [`JournalEvent::QualityChanged`](crate::JournalEvent::QualityChanged)
    /// — the bound point's observed quality transition.
    QualityChanged,
    /// [`JournalEvent::CommandSettled`](crate::JournalEvent::CommandSettled)
    /// — a command's final receipt.
    CommandSettled,
    /// [`JournalEvent::StepFailed`](crate::JournalEvent::StepFailed) —
    /// the component's `step` returned an error.
    StepFailed,
    /// The kind declares the event natively — an
    /// [`EventDecl`](crate::EventDecl) entry emitted as an
    /// [`EmittedEvent`](crate::EmittedEvent), not adapted from a
    /// journaled transition.
    Declared,
}

/// An event a block may emit: one entry of
/// [`BlockInterface::events`].
///
/// The entry declares the stable event kind (`name`), the typed
/// payload schema, the retention class, the emission condition, and
/// the journaled transition it is adapted from. Typed event emission
/// beyond the adapted journal vocabulary is a later tranche; these
/// entries describe what already lands in the journal.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventSpec {
    /// The event's stable identity within the interface —
    /// `"<kind>:<port>"` for port-adapted events, e.g.
    /// `point_changed:alarm`; `command_settled` and `step_failed` are
    /// block-level.
    pub name: String,
    /// The typed payload schema.
    pub payload: Vec<EventField>,
    /// How an emitted event is retained.
    pub retention: EventRetention,
    /// The emission condition.
    pub emission: EventEmission,
    /// The journaled transition this entry is adapted from —
    /// [`Declared`](AdaptedEvent::Declared) when the kind declares the
    /// event natively.
    pub adapted: AdaptedEvent,
    /// The logical point a port-adapted event reports on — the port's
    /// bound point, `None` in a kind-level interface and for
    /// block-level events.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub point: Option<PointId>,
}

/// One component kind's schema-driven interface — the decision-82
/// contract a generic consumer renders from.
///
/// `version` is [`INTERFACE_VERSION`]; `kind` is the model `kind`
/// string the registry maps to a constructor, matching the
/// [`ComponentDescriptor::kind`] the interface is derived from. Each
/// collection lists its resources in descriptor-declaration order;
/// names are unique within a collection and, for port- and
/// parameter-adapted resources, match the names the descriptor, the
/// `dcs-build` spec, and the plant model's port keys already use.
///
/// The type is serde-additive: fields a newer minor version adds are
/// ignored by this reader, and optional fields an older document lacks
/// deserialize to their defaults — the convention the rest of the
/// contract follows. Model documents, snapshots, and wire payloads are
/// unchanged by this contract existing; `MODEL_VERSION` is unaffected.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BlockInterface {
    /// The interface schema version — [`INTERFACE_VERSION`].
    pub version: u32,
    /// The component kind this interface describes — the model `kind`
    /// string.
    pub kind: String,
    /// Read-only sampled values, adapted from the descriptor's
    /// non-[`Status`](PortRole::Status) ports.
    pub measurements: Vec<Measurement>,
    /// Writable or engineering-fixed configuration, adapted from the
    /// descriptor's [`ParameterDescriptor`](crate::ParameterDescriptor)s.
    pub configuration: Vec<ConfigProperty>,
    /// Read-only runtime state, adapted from the descriptor's
    /// [`Status`](PortRole::Status)-hinted ports.
    pub state: Vec<StateProperty>,
    /// Named commands, adapted from the generic
    /// [`Command`](crate::Command) variants the block's ports and
    /// parameters expose.
    pub commands: Vec<CommandSpec>,
    /// Typed events, adapted from the journaled transitions the
    /// block's bound points and commands can produce.
    pub events: Vec<EventSpec>,
}

/// The command identity a port-adapted verb carries: `"<verb>:<port>"`.
fn port_command_name(verb: &str, port: &str) -> String {
    format!("{verb}:{port}")
}

/// The `value` argument a point write carries — the port's declared
/// kind.
fn value_argument(kind: ValueKind) -> Vec<CommandArgument> {
    vec![CommandArgument {
        name: "value".to_string(),
        kind,
    }]
}

impl BlockInterface {
    /// Derives a kind's [`BlockInterface`] from its
    /// [`ComponentDescriptor`] — the single adaptation the whole
    /// interface comes from, per the module's mapping rules.
    ///
    /// A descriptor the serving layer annotated with bound points —
    /// the form `TelemetrySnapshot.descriptors` serves — carries those
    /// points through onto the port-adapted resources, so the same
    /// derivation produces the instance-level served schema.
    pub fn from_descriptor(descriptor: &ComponentDescriptor) -> Self {
        let mut measurements = Vec::new();
        let mut state = Vec::new();
        let mut commands = Vec::new();
        let mut events = Vec::new();

        for port in &descriptor.ports {
            if port.role == Some(PortRole::Status) {
                state.push(StateProperty {
                    name: port.name.clone(),
                    direction: port.direction,
                    kind: port.kind,
                    role: port.role,
                    point: port.point,
                    persistence: StatePersistence::BoundPoint,
                });
            } else {
                measurements.push(Measurement {
                    name: port.name.clone(),
                    direction: port.direction,
                    kind: port.kind,
                    role: port.role,
                    point: port.point,
                    unit: None,
                });
            }

            // The durable value-transition record is opt-in per bound
            // point and valid on `Bool`/`Int` points only — the same
            // bound the model schema writes down for `journaled`.
            if matches!(port.kind, ValueKind::Bool | ValueKind::Int) {
                events.push(EventSpec {
                    name: format!("point_changed:{}", port.name),
                    payload: vec![
                        EventField {
                            name: "from".to_string(),
                            kind: EventFieldKind::Value(port.kind),
                            optional: true,
                        },
                        EventField {
                            name: "to".to_string(),
                            kind: EventFieldKind::Value(port.kind),
                            optional: false,
                        },
                    ],
                    retention: EventRetention::Journal,
                    emission: EventEmission::WhenJournaled,
                    adapted: AdaptedEvent::PointChanged,
                    point: port.point,
                });
            }
            events.push(EventSpec {
                name: format!("quality_changed:{}", port.name),
                payload: vec![
                    EventField {
                        name: "from".to_string(),
                        kind: EventFieldKind::Quality,
                        optional: true,
                    },
                    EventField {
                        name: "to".to_string(),
                        kind: EventFieldKind::Quality,
                        optional: false,
                    },
                ],
                retention: EventRetention::Journal,
                emission: EventEmission::OnObservedChange,
                adapted: AdaptedEvent::QualityChanged,
                point: port.point,
            });

            // The point-command surface adapts onto `In` ports only:
            // `Out` points are never command targets.
            if port.direction == Direction::In {
                for (verb, adapted, request) in [
                    (
                        "write_value",
                        AdaptedCommand::WriteValue,
                        value_argument(port.kind),
                    ),
                    (
                        "force_point",
                        AdaptedCommand::ForcePoint,
                        value_argument(port.kind),
                    ),
                    ("unforce_point", AdaptedCommand::UnforcePoint, Vec::new()),
                ] {
                    commands.push(CommandSpec {
                        name: port_command_name(verb, &port.name),
                        request,
                        availability: CommandAvailability::BoundPointWritable,
                        adapted,
                        point: port.point,
                    });
                }
            }
        }

        let configuration = descriptor
            .parameters
            .iter()
            .map(|parameter| ConfigProperty {
                name: parameter.name.clone(),
                kind: parameter.kind,
                range: parameter.range,
                capability: ConfigCapability::Tunable,
            })
            .collect::<Vec<_>>();
        for property in &configuration {
            commands.push(CommandSpec {
                name: format!("set_parameter:{}", property.name),
                request: value_argument(property.kind),
                availability: CommandAvailability::Always,
                adapted: AdaptedCommand::SetParameter,
                point: None,
            });
        }

        // Kind-declared commands sit beside the adapted entries under
        // the `Declared` provenance, carrying the declaration's request
        // schema and availability verbatim.
        for declared in &descriptor.commands {
            commands.push(CommandSpec {
                name: declared.name.clone(),
                request: declared.request.clone(),
                availability: declared.availability,
                adapted: AdaptedCommand::Declared,
                point: None,
            });
        }

        // Block-level events: every command's settlement and the
        // component's own step failures already journal through the
        // existing transitions.
        events.push(EventSpec {
            name: "command_settled".to_string(),
            payload: vec![EventField {
                name: "receipt".to_string(),
                kind: EventFieldKind::Receipt,
                optional: false,
            }],
            retention: EventRetention::Journal,
            emission: EventEmission::OnCommandSettled,
            adapted: AdaptedEvent::CommandSettled,
            point: None,
        });
        events.push(EventSpec {
            name: "step_failed".to_string(),
            payload: vec![EventField {
                name: "error".to_string(),
                kind: EventFieldKind::Text,
                optional: false,
            }],
            retention: EventRetention::Journal,
            emission: EventEmission::OnStepFailure,
            adapted: AdaptedEvent::StepFailed,
            point: None,
        });

        // Kind-declared events sit beside the adapted entries under
        // the `Declared` provenance — the kind emits them itself, so
        // the emission condition is `KindEmitted` and the record lands
        // in the journal as `event_emitted`.
        for declared in &descriptor.events {
            events.push(EventSpec {
                name: declared.name.clone(),
                payload: declared.payload.clone(),
                retention: declared.retention,
                emission: EventEmission::KindEmitted,
                adapted: AdaptedEvent::Declared,
                point: None,
            });
        }

        // Names are each collection's stable resource identity — a
        // declaration colliding with an adapted entry or a sibling
        // declaration is a kind-authoring error.
        debug_assert!(
            commands
                .iter()
                .map(|command| command.name.as_str())
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                == commands.len(),
            "interface command names must be unique"
        );
        debug_assert!(
            events
                .iter()
                .map(|event| event.name.as_str())
                .collect::<std::collections::BTreeSet<_>>()
                .len()
                == events.len(),
            "interface event names must be unique"
        );

        BlockInterface {
            version: INTERFACE_VERSION,
            kind: descriptor.kind.clone(),
            measurements,
            configuration,
            state,
            commands,
            events,
        }
    }
}

impl ComponentDescriptor {
    /// This descriptor's derived [`BlockInterface`] — shorthand for
    /// [`BlockInterface::from_descriptor`].
    pub fn interface(&self) -> BlockInterface {
        BlockInterface::from_descriptor(self)
    }
}

/// Derives the [`BlockInterface`] set a serving layer publishes from a
/// snapshot's descriptor list — one interface per component, each
/// keyed by its descriptor's `kind`. The descriptors a
/// [`TelemetrySnapshot`](crate::TelemetrySnapshot) serves carry the
/// bound-point annotation, so the derived interfaces are the
/// instance-level served schema.
pub fn block_interfaces(descriptors: &[ComponentDescriptor]) -> Vec<BlockInterface> {
    descriptors
        .iter()
        .map(ComponentDescriptor::interface)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::descriptor::{ParameterDescriptor, PortDescriptor};
    use crate::signal::Value;

    fn descriptor() -> ComponentDescriptor {
        ComponentDescriptor {
            name: "vlv:1".to_string(),
            kind: "valve".to_string(),
            label: "vlv:1".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "cmd".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Setpoint),
                    point: None,
                },
                PortDescriptor {
                    name: "out".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Output),
                    point: None,
                },
                PortDescriptor {
                    name: "fb".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::ProcessValue),
                    point: None,
                },
                PortDescriptor {
                    name: "discrepancy".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: None,
                },
            ],
            parameters: vec![
                ParameterDescriptor {
                    name: "travel_ticks".to_string(),
                    kind: ValueKind::Float,
                    range: Some(ParameterRange {
                        min: Value::Float(0.0),
                        max: Value::Float(f64::MAX),
                    }),
                },
                ParameterDescriptor {
                    name: "discrepancy_ticks".to_string(),
                    kind: ValueKind::Int,
                    range: None,
                },
            ],
            commands: Vec::new(),
            events: Vec::new(),
        }
    }

    #[test]
    fn interface_serde_roundtrip() {
        let interface = BlockInterface::from_descriptor(&descriptor());
        let json = serde_json::to_string(&interface).unwrap();
        assert_eq!(
            serde_json::from_str::<BlockInterface>(&json).unwrap(),
            interface
        );
    }

    #[test]
    fn interface_tolerates_unknown_fields() {
        // Forward compatibility: a document a newer minor version
        // wrote — carrying fields this version does not know — still
        // deserializes, at the top level and inside a resource entry.
        let document = r#"{
            "version": 1,
            "kind": "valve",
            "future_section": {"anything": true},
            "measurements": [{
                "name": "fb",
                "direction": "in",
                "kind": "bool",
                "role": "process_value",
                "widget_hint": "trend"
            }],
            "configuration": [],
            "state": [],
            "commands": [],
            "events": []
        }"#;
        let interface: BlockInterface = serde_json::from_str(document).unwrap();
        assert_eq!(interface.version, INTERFACE_VERSION);
        assert_eq!(interface.kind, "valve");
        assert_eq!(interface.measurements.len(), 1);
        assert_eq!(interface.measurements[0].name, "fb");
        assert_eq!(interface.measurements[0].unit, None);
    }

    #[test]
    fn interface_reads_documents_predating_optional_fields() {
        // A minimal resource entry — no role, point, unit, or optional
        // payload flags — deserializes with the documented defaults,
        // matching the optional-field convention.
        let measurement: Measurement =
            serde_json::from_str(r#"{"name":"fb","direction":"in","kind":"bool"}"#).unwrap();
        assert_eq!(measurement.role, None);
        assert_eq!(measurement.point, None);
        assert_eq!(measurement.unit, None);
        let json = serde_json::to_string(&measurement).unwrap();
        assert!(!json.contains("role"));
        assert!(!json.contains("point"));
        assert!(!json.contains("unit"));

        let field: EventField =
            serde_json::from_str(r#"{"name":"to","kind":{"value":"bool"}}"#).unwrap();
        assert!(!field.optional);
        assert!(!serde_json::to_string(&field).unwrap().contains("optional"));
    }

    #[test]
    fn ports_split_into_measurements_and_state_by_status_role() {
        let interface = BlockInterface::from_descriptor(&descriptor());
        let measurement_names: Vec<&str> = interface
            .measurements
            .iter()
            .map(|m| m.name.as_str())
            .collect();
        assert_eq!(measurement_names, ["cmd", "out", "fb"]);
        let state_names: Vec<&str> = interface.state.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(state_names, ["discrepancy"]);
        assert_eq!(interface.state[0].persistence, StatePersistence::BoundPoint);
    }

    #[test]
    fn parameters_become_tunable_configuration() {
        let interface = BlockInterface::from_descriptor(&descriptor());
        assert_eq!(interface.configuration.len(), 2);
        let travel = &interface.configuration[0];
        assert_eq!(travel.name, "travel_ticks");
        assert_eq!(travel.kind, ValueKind::Float);
        assert_eq!(
            travel.range,
            Some(ParameterRange {
                min: Value::Float(0.0),
                max: Value::Float(f64::MAX),
            })
        );
        assert_eq!(travel.capability, ConfigCapability::Tunable);
        assert_eq!(interface.configuration[1].range, None);
    }

    #[test]
    fn commands_adapt_the_generic_variants() {
        let interface = BlockInterface::from_descriptor(&descriptor());
        // Two `In` ports × the point-command triple, plus one
        // `set_parameter` per configuration entry.
        let names: Vec<&str> = interface.commands.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(
            names,
            [
                "write_value:cmd",
                "force_point:cmd",
                "unforce_point:cmd",
                "write_value:fb",
                "force_point:fb",
                "unforce_point:fb",
                "set_parameter:travel_ticks",
                "set_parameter:discrepancy_ticks",
            ]
        );
        let write = &interface.commands[0];
        assert_eq!(write.adapted, AdaptedCommand::WriteValue);
        assert_eq!(write.availability, CommandAvailability::BoundPointWritable);
        assert_eq!(
            write.request,
            [CommandArgument {
                name: "value".to_string(),
                kind: ValueKind::Bool,
            }]
        );
        let tune = interface
            .commands
            .iter()
            .find(|c| c.name == "set_parameter:discrepancy_ticks")
            .unwrap();
        assert_eq!(tune.adapted, AdaptedCommand::SetParameter);
        assert_eq!(tune.availability, CommandAvailability::Always);
        assert_eq!(
            tune.request,
            [CommandArgument {
                name: "value".to_string(),
                kind: ValueKind::Int,
            }]
        );
    }

    #[test]
    fn events_adapt_the_journaled_transitions() {
        let interface = BlockInterface::from_descriptor(&descriptor());
        let names: Vec<&str> = interface.events.iter().map(|e| e.name.as_str()).collect();
        // Every Bool port may emit both adapted transitions.
        assert_eq!(
            names,
            [
                "point_changed:cmd",
                "quality_changed:cmd",
                "point_changed:out",
                "quality_changed:out",
                "point_changed:fb",
                "quality_changed:fb",
                "point_changed:discrepancy",
                "quality_changed:discrepancy",
                "command_settled",
                "step_failed",
            ]
        );
        let point_changed = &interface.events[0];
        assert_eq!(point_changed.adapted, AdaptedEvent::PointChanged);
        assert_eq!(point_changed.retention, EventRetention::Journal);
        assert_eq!(point_changed.emission, EventEmission::WhenJournaled);
        assert_eq!(
            point_changed.payload,
            [
                EventField {
                    name: "from".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Bool),
                    optional: true,
                },
                EventField {
                    name: "to".to_string(),
                    kind: EventFieldKind::Value(ValueKind::Bool),
                    optional: false,
                },
            ]
        );
        assert!(
            interface
                .events
                .iter()
                .all(|e| e.retention == EventRetention::Journal)
        );
    }

    #[test]
    fn float_ports_emit_no_point_changed_event() {
        // The model schema bounds `journaled` to `Bool`/`Int` points —
        // a `Float` port's interface carries `quality_changed` only.
        let mut descriptor = descriptor();
        descriptor.ports[2].kind = ValueKind::Float;
        let interface = BlockInterface::from_descriptor(&descriptor);
        assert!(
            interface
                .events
                .iter()
                .any(|e| e.name == "quality_changed:fb")
        );
        assert!(
            !interface
                .events
                .iter()
                .any(|e| e.name == "point_changed:fb")
        );
    }

    #[test]
    fn bound_points_carry_through_onto_adapted_resources() {
        // The serving layer annotates descriptors with bound points;
        // the derived interface is then the instance-level schema.
        let mut descriptor = descriptor();
        descriptor.ports[0].point = Some(PointId(10));
        descriptor.ports[3].point = Some(PointId(13));
        let interface = BlockInterface::from_descriptor(&descriptor);
        assert_eq!(interface.measurements[0].point, Some(PointId(10)),);
        assert_eq!(interface.state[0].point, Some(PointId(13)));
        let write = interface
            .commands
            .iter()
            .find(|c| c.name == "write_value:cmd")
            .unwrap();
        assert_eq!(write.point, Some(PointId(10)));
        let event = interface
            .events
            .iter()
            .find(|e| e.name == "point_changed:discrepancy")
            .unwrap();
        assert_eq!(event.point, Some(PointId(13)));
        // Block-level and parameter-adapted resources carry no point.
        let settled = interface
            .events
            .iter()
            .find(|e| e.name == "command_settled")
            .unwrap();
        assert_eq!(settled.point, None);
    }

    #[test]
    fn declared_commands_and_events_sit_beside_adapted_entries() {
        // A kind-declared command and event land in the same
        // collections as the adapted entries under the `Declared`
        // provenance — distinct and machine-readable.
        let mut descriptor = descriptor();
        descriptor.commands.push(crate::CommandDecl {
            name: "stroke_test".to_string(),
            request: vec![CommandArgument {
                name: "ticks".to_string(),
                kind: ValueKind::Int,
            }],
            availability: CommandAvailability::KindDeclared,
        });
        descriptor.events.push(crate::EventDecl {
            name: "stroke_complete".to_string(),
            payload: vec![EventField {
                name: "ticks".to_string(),
                kind: EventFieldKind::Value(ValueKind::Int),
                optional: false,
            }],
            retention: EventRetention::Journal,
        });
        let interface = BlockInterface::from_descriptor(&descriptor);

        let command = interface
            .commands
            .iter()
            .find(|c| c.name == "stroke_test")
            .unwrap();
        assert_eq!(command.adapted, AdaptedCommand::Declared);
        assert_eq!(command.availability, CommandAvailability::KindDeclared);
        assert_eq!(
            command.request,
            [CommandArgument {
                name: "ticks".to_string(),
                kind: ValueKind::Int,
            }]
        );
        assert_eq!(command.point, None);
        // The adapted entries are untouched beside it.
        assert!(
            interface
                .commands
                .iter()
                .any(|c| c.name == "write_value:cmd" && c.adapted == AdaptedCommand::WriteValue)
        );

        let event = interface
            .events
            .iter()
            .find(|e| e.name == "stroke_complete")
            .unwrap();
        assert_eq!(event.adapted, AdaptedEvent::Declared);
        assert_eq!(event.emission, EventEmission::KindEmitted);
        assert_eq!(event.retention, EventRetention::Journal);
        assert_eq!(event.point, None);
        assert!(
            interface
                .events
                .iter()
                .any(|e| e.name == "command_settled" && e.adapted == AdaptedEvent::CommandSettled)
        );
    }

    #[test]
    fn declared_provenance_uses_the_documented_wire_shapes() {
        // The declared-provenance forms: `adapted` spells `declared`,
        // availability `kind_declared`, emission `kind_emitted` —
        // beside the unchanged adapted spellings.
        let mut descriptor = descriptor();
        descriptor.commands.push(crate::CommandDecl {
            name: "stroke_test".to_string(),
            request: vec![CommandArgument {
                name: "ticks".to_string(),
                kind: ValueKind::Int,
            }],
            availability: CommandAvailability::KindDeclared,
        });
        descriptor.events.push(crate::EventDecl {
            name: "stroke_complete".to_string(),
            payload: vec![EventField {
                name: "ticks".to_string(),
                kind: EventFieldKind::Value(ValueKind::Int),
                optional: false,
            }],
            retention: EventRetention::Journal,
        });
        let interface = BlockInterface::from_descriptor(&descriptor);
        let command = interface
            .commands
            .iter()
            .find(|c| c.name == "stroke_test")
            .unwrap();
        assert_eq!(
            serde_json::to_string(command).unwrap(),
            r#"{"name":"stroke_test","request":[{"name":"ticks","kind":"int"}],"availability":"kind_declared","adapted":"declared"}"#
        );
        let event = interface
            .events
            .iter()
            .find(|e| e.name == "stroke_complete")
            .unwrap();
        assert_eq!(
            serde_json::to_string(event).unwrap(),
            r#"{"name":"stroke_complete","payload":[{"name":"ticks","kind":{"value":"int"}}],"retention":"journal","emission":"kind_emitted","adapted":"declared"}"#
        );
        // The adapted spellings are unchanged beside them.
        let write = interface
            .commands
            .iter()
            .find(|c| c.name == "write_value:cmd")
            .unwrap();
        assert_eq!(
            serde_json::to_string(write).unwrap(),
            r#"{"name":"write_value:cmd","request":[{"name":"value","kind":"bool"}],"availability":"bound_point_writable","adapted":"write_value"}"#
        );
    }

    #[test]
    #[cfg_attr(debug_assertions, should_panic)]
    fn declared_names_must_not_collide_with_adapted_entries() {
        let mut descriptor = descriptor();
        descriptor.commands.push(crate::CommandDecl {
            name: "write_value:cmd".to_string(),
            request: Vec::new(),
            availability: CommandAvailability::KindDeclared,
        });
        let _ = BlockInterface::from_descriptor(&descriptor);
    }

    #[test]
    fn interface_documents_predating_declared_provenance_deserialize() {
        // A `commands`/`events` collection written before the
        // `Declared` provenance existed carries only adapted entries;
        // it deserializes unchanged.
        let command: CommandSpec = serde_json::from_str(
            r#"{"name":"write_value:cmd","request":[{"name":"value","kind":"bool"}],"availability":"bound_point_writable","adapted":"write_value","point":10}"#,
        )
        .unwrap();
        assert_eq!(command.adapted, AdaptedCommand::WriteValue);
        assert_eq!(command.point, Some(PointId(10)));
        let event: EventSpec = serde_json::from_str(
            r#"{"name":"command_settled","payload":[{"name":"receipt","kind":"receipt"}],"retention":"journal","emission":"on_command_settled","adapted":"command_settled"}"#,
        )
        .unwrap();
        assert_eq!(event.adapted, AdaptedEvent::CommandSettled);
    }

    #[test]
    fn block_interfaces_derives_one_per_descriptor() {
        let interfaces = block_interfaces(&[descriptor(), {
            let mut other = descriptor();
            other.kind = "motor".to_string();
            other
        }]);
        assert_eq!(interfaces.len(), 2);
        assert_eq!(interfaces[0].kind, "valve");
        assert_eq!(interfaces[1].kind, "motor");
    }
}
