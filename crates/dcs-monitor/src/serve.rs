//! The serving-layer derivations behind `GET /schema` and
//! `GET /resources` — decision 82's serving half.
//!
//! Both views read only the [`Publication`]'s materialized snapshot and
//! the store's retained journal tail plus the model's [`SignalIndex`] —
//! never the executor — so they inherit the published-read-model
//! boundary: a read derives from one immutable publication and stamps
//! its `seq`/`tick`, matching a concurrently fetched `/snapshot`.
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
//! reads live — the `writable` mark through the signal index —
//! and each instance the retained journal tail's entries attributed to
//! it.

use crate::store::Publication;
use dcs_core::{
    CommandAvailability, CommandError, CommandSpec, CommandState, ComponentInterface,
    ComponentResources, ConfigValue, Direction, JournalEntry, JournalEvent, PointId,
    ResourceSample, ResourceView, Sample, SchemaView, Value,
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
/// `writable` marks, and `journal`'s retained tail attributed per
/// instance. `journal` is the store's served ring — the same stream
/// `GET /journal` answers — so recently emitted events include the
/// control-plane entries journaled between scans.
pub(crate) fn resource_view(
    publication: &Publication,
    signals: &SignalIndex,
    journal: &[JournalEntry],
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
                    .map(|command| command_state(command, signals))
                    .collect(),
                events: journal
                    .iter()
                    .filter(|entry| attributed(entry, &descriptor.name, &bound))
                    .cloned()
                    .collect(),
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
/// unbound port refused by name. `Always`- and
/// `KindDeclared`-available commands are admissible: a submission
/// validates and dispatches at the scan boundary, where a
/// kind-declared predicate or an `apply_parameter`/`invoke_command`
/// invariant may still refuse — that refusal settles through the
/// journaled `command_settled` receipt the view's `events` carry, so
/// the read model never has to evaluate the kind's predicate.
fn command_state(spec: &CommandSpec, signals: &SignalIndex) -> CommandState {
    let refused = |reason: CommandError| (false, Some(reason.to_string()));
    let (available, refusal) = match spec.availability {
        CommandAvailability::Always | CommandAvailability::KindDeclared => (true, None),
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

/// Whether a retained journal entry is attributed to the component —
/// the attribution rules the resource view's `events` collection
/// follows: point transitions on the instance's bound points, command
/// receipts its commands settle (addressed by name or by bound point),
/// its own step failures, and its kind-emitted events. Run-level
/// entries — role changes, divergence detections, reinitializations —
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
        | JournalEvent::Reinitialized { .. }
        | JournalEvent::SourceRestarted { .. } => false,
    }
}
