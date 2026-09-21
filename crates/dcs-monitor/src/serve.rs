//! The serving-layer derivations behind `GET /schema` and
//! `GET /resources` — decision 82's serving half.
//!
//! Both views read only the [`Publication`]'s materialized snapshot and
//! the store's retained journal tail plus routed event stores and the
//! model's [`SignalIndex`] — never the executor — so they inherit the
//! published-read-model boundary: a read derives from one immutable
//! publication and stamps its `seq`/`tick`, matching a concurrently
//! fetched `/snapshot`.
//!
//! [`schema_view`] is the served block-interface registry: every
//! component instance's [`BlockInterface`](dcs_core::BlockInterface),
//! derived through [`block_interfaces`](dcs_core::block_interfaces)
//! from the snapshot's bound-point-annotated descriptors and completed
//! with the signal index's `unit` annotation — the instance-level
//! annotation [`Measurement`](dcs_core::Measurement) documents.
//!
//! [`resource_view`] joins the same publication's sections onto that
//! schema: each measurement and state resource reports its bound
//! point's latest [`Sample`], each configuration resource the
//! `parameters` section's current value, each command the availability
//! its [`CommandAvailability`](dcs_core::CommandAvailability) rule
//! reads live — the `writable` mark through the signal index, a
//! `KindDeclared` command's standing verdict from the publication's
//! `command_verdicts` section — and each instance its attributed
//! events: the retained journal tail's entries beside the routed
//! `History`/`Latest` emissions it produced.

use crate::store::{Publication, RoutedEvents};
use dcs_core::{
    CommandAvailability, CommandError, CommandSpec, CommandState, CommandVerdict,
    ComponentInterface, ComponentResources, ConfigValue, Direction, EventRecord, EventRetention,
    JournalEntry, JournalEvent, PointId, ResourceEvent, ResourceSample, ResourceView, Sample,
    SchemaView, Value,
};
use dcs_model::SignalIndex;
use std::collections::{BTreeMap, BTreeSet};

/// `GET /schema`'s view over `publication`: every served component
/// instance's [`BlockInterface`](dcs_core::BlockInterface), in scan
/// order, with each measurement's `unit` resolved through the signal
/// index — the serving-layer annotation the kind-level derivation
/// leaves `None`.
pub(crate) fn schema_view(publication: &Publication, signals: &SignalIndex) -> SchemaView {
    let interfaces = dcs_core::block_interfaces(&publication.snapshot.descriptors)
        .into_iter()
        .zip(&publication.snapshot.descriptors)
        .map(|(mut interface, descriptor)| {
            for measurement in &mut interface.measurements {
                measurement.unit = measurement
                    .point
                    .and_then(|point| signals.get(point))
                    .and_then(|signal| signal.unit.clone());
            }
            ComponentInterface {
                name: descriptor.name.clone(),
                interface,
            }
        })
        .collect();
    SchemaView {
        publication: publication.seq,
        tick: publication.tick,
        interfaces,
    }
}

/// `GET /resources`'s view over `publication`: per-instance live
/// resource state — values from the snapshot's points and parameters
/// sections, command availability read against the signal index's
/// `writable` marks and the `command_verdicts` section's published
/// `KindDeclared` verdicts, and the store's event streams attributed
/// per instance. `journal` is the store's served ring — the same stream
/// `GET /journal` answers — so recent events include the control-plane
/// entries journaled between scans; `routed` is the bounded
/// event-history ring and the latest-emission view the declared
/// `History`/`Latest` emissions landed in, each entry carrying the
/// `retention` mark that tells it from the durable record.
pub(crate) fn resource_view(
    publication: &Publication,
    signals: &SignalIndex,
    journal: &[JournalEntry],
    routed: &RoutedEvents,
) -> ResourceView {
    let snapshot = &publication.snapshot;
    let samples: BTreeMap<PointId, Option<Sample>> = snapshot
        .points
        .iter()
        .map(|telemetry| (telemetry.point, telemetry.sample))
        .collect();
    let parameters: BTreeMap<&str, &BTreeMap<String, Value>> = snapshot
        .parameters
        .iter()
        .map(|entry| (entry.name.as_str(), &entry.values))
        .collect();
    let verdicts: BTreeMap<(&str, &str), &CommandVerdict> = snapshot
        .command_verdicts
        .iter()
        .flat_map(|component| {
            component
                .verdicts
                .iter()
                .map(move |verdict| ((component.name.as_str(), verdict.name.as_str()), verdict))
        })
        .collect();
    let components = snapshot
        .descriptors
        .iter()
        .map(|descriptor| {
            let interface = descriptor.interface();
            let bound: BTreeSet<PointId> = descriptor
                .ports
                .iter()
                .filter_map(|port| port.point)
                .collect();
            let sample = |name: &str, point: Option<PointId>| ResourceSample {
                name: name.to_string(),
                point,
                sample: point.and_then(|point| samples.get(&point).copied().flatten()),
            };
            let values = parameters.get(descriptor.name.as_str()).copied();
            ComponentResources {
                name: descriptor.name.clone(),
                kind: descriptor.kind.clone(),
                measurements: interface
                    .measurements
                    .iter()
                    .map(|measurement| sample(&measurement.name, measurement.point))
                    .collect(),
                configuration: interface
                    .configuration
                    .iter()
                    .map(|property| ConfigValue {
                        name: property.name.clone(),
                        value: values.and_then(|values| values.get(&property.name).copied()),
                    })
                    .collect(),
                state: interface
                    .state
                    .iter()
                    .map(|property| sample(&property.name, property.point))
                    .collect(),
                commands: interface
                    .commands
                    .iter()
                    .map(|command| {
                        command_state(
                            command,
                            signals,
                            verdicts
                                .get(&(descriptor.name.as_str(), command.name.as_str()))
                                .copied(),
                        )
                    })
                    .collect(),
                events: attributed_events(journal, routed, &descriptor.name, &bound),
            }
        })
        .collect();
    ResourceView {
        publication: publication.seq,
        tick: publication.tick,
        components,
    }
}

/// One command's live availability — the [`CommandSpec`]'s
/// `availability` rule read against the published plant.
///
/// A port-adapted command submits only while its bound point is a
/// model-declared writable `In` point — the same surface
/// `check_command_point` enforces, read here through the signal index,
/// so the reported refusal is the named reason the receipted path would
/// answer: `not_writable` on a served but unmarked point,
/// `unknown_point` on a bound point the model never declared, an
/// unbound port refused by name. `Always`-available commands are
/// admissible unconditionally. A `KindDeclared` command joins the
/// publication's `command_verdicts` verdict — the standing-availability
/// probe the producer evaluates once per declared command at each
/// scan's step end — so the view serves the kind's named refusal in
/// the scan the predicate refuses without the read model ever
/// evaluating it. A publication carrying no verdict for the command —
/// one materialized before the first scan, or a snapshot predating the
/// section — keeps the unconditional `available` the read model
/// reported before the section existed.
///
/// Every answer is advisory: a submission still validates, queues, and
/// dispatches at the scan boundary, where an `apply_parameter`/
/// `invoke_command` invariant — or a predicate the verdict predates —
/// may still refuse, that refusal settling through the journaled
/// `command_settled` receipt the view's `events` carry. The receipted
/// path stays the sole authority.
fn command_state(
    spec: &CommandSpec,
    signals: &SignalIndex,
    verdict: Option<&CommandVerdict>,
) -> CommandState {
    let refused = |reason: CommandError| (false, Some(reason.to_string()));
    let (available, refusal) = match spec.availability {
        CommandAvailability::Always => (true, None),
        CommandAvailability::KindDeclared => match verdict {
            Some(verdict) if !verdict.available => (false, verdict.refusal.clone()),
            _ => (true, None),
        },
        CommandAvailability::BoundPointWritable => match spec.point {
            None => (
                false,
                Some(format!("command {:?} targets an unbound port", spec.name)),
            ),
            Some(point) => match signals.get(point) {
                Some(signal) if signal.direction == Direction::In && signal.writable => {
                    (true, None)
                }
                Some(_) => refused(CommandError::NotWritable { point }),
                None => refused(CommandError::UnknownPoint { point }),
            },
        },
    };
    CommandState {
        name: spec.name.clone(),
        point: spec.point,
        available,
        refusal,
    }
}

/// The component's `events` collection: the journal tail's attributed
/// entries beside the routed `History`/`Latest` records the instance
/// emitted — `record.event.component == name` is the routed record's
/// attribution, the emission-side counterpart of the journal rule —
/// ordered by attributed tick, each stream's own order kept within a
/// tick: the durable record first, then the event-history ring, then
/// the latest view. Every routed emission carries its
/// [`EmittedEvent`](dcs_core::EmittedEvent) under
/// [`JournalEvent::EventEmitted`] so the collection keeps one `event`
/// vocabulary.
fn attributed_events(
    journal: &[JournalEntry],
    routed: &RoutedEvents,
    name: &str,
    points: &BTreeSet<PointId>,
) -> Vec<ResourceEvent> {
    let emitted = |record: &EventRecord| ResourceEvent {
        seq: record.seq,
        tick: record.tick,
        retention: record.retention,
        event: JournalEvent::EventEmitted {
            event: record.event.clone(),
        },
    };
    let mut events: Vec<ResourceEvent> = journal
        .iter()
        .filter(|entry| attributed(entry, name, points))
        .map(|entry| ResourceEvent {
            seq: entry.seq,
            tick: entry.tick,
            retention: EventRetention::Journal,
            event: entry.event.clone(),
        })
        .chain(
            routed
                .history
                .iter()
                .filter(|record| record.event.component == name)
                .map(emitted),
        )
        .chain(
            routed
                .latest
                .iter()
                .filter(|record| record.event.component == name)
                .map(emitted),
        )
        .collect();
    // The journal tail is seq-ordered and each routed stream append-
    // ordered; a stable merge by attributed tick keeps the collection
    // newest-last like the journal pane reads it.
    events.sort_by_key(|entry| entry.tick);
    events
}

/// Whether a retained journal entry is attributed to the component —
/// the attribution rules the resource view's `events` collection
/// follows: point transitions on the instance's bound points, command
/// receipts its commands settle (addressed by name or by bound point),
/// its own step failures, and its kind-emitted events. Run-level
/// entries — role changes, divergence detections and resolutions,
/// reinitializations, tracking-source adoptions, run boundaries —
/// belong to no instance.
fn attributed(entry: &JournalEntry, name: &str, points: &BTreeSet<PointId>) -> bool {
    match &entry.event {
        JournalEvent::QualityChanged { point, .. }
        | JournalEvent::PointChanged { point, .. }
        | JournalEvent::FieldClaimLost { point } => points.contains(point),
        JournalEvent::CommandSettled { receipt } => {
            receipt.command.component() == Some(name)
                || receipt
                    .command
                    .point()
                    .is_some_and(|point| points.contains(&point))
        }
        JournalEvent::StepFailed { component, .. } => component == name,
        JournalEvent::EventEmitted { event } => event.component == name,
        JournalEvent::RoleChanged { .. }
        | JournalEvent::DivergenceDetected { .. }
        | JournalEvent::DivergenceResolved { .. }
        | JournalEvent::Reinitialized { .. }
        | JournalEvent::SourceRestarted { .. }
        | JournalEvent::TrackingSourceAdopted { .. }
        | JournalEvent::RunBoundary { .. } => false,
    }
}
