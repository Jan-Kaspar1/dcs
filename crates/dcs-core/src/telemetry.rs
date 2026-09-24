//! Monitoring telemetry: the read-side snapshot of a controller run.
//!
//! [`TelemetrySnapshot`] is the monitoring half of the unified contract: a
//! point-in-time, serde-serializable view of what a scan sequence produced —
//! the latest [`Sample`] for every known point and per-component diagnostics.
//! Because the type lives in `dcs-core`, a monitoring UI depends only on the
//! shared contracts, never on the runtime that produced the snapshot.
//!
//! Timestamps are logical [`Tick`]s in the producing run's domain, per the
//! signal model: they are comparable to each other and to the tick stamped
//! on every reported sample, not to a wall clock.

use crate::descriptor::ComponentDescriptor;
use crate::io::{Direction, DriverDiagnostics, IoError};
use crate::signal::{PointId, Sample, Tick, Value};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// One point's active operator force: the point and the value the scan
/// image substitutes for its input read while the force stands.
///
/// A force is applied by [`Command::ForcePoint`](crate::Command::ForcePoint)
/// and released by [`Command::UnforcePoint`](crate::Command::UnforcePoint);
/// while it stands the point's [`PointTelemetry::sample`] reports the
/// forced value stamped
/// [`Quality::Uncertain`](crate::Quality::Uncertain)`(`[`QualityReason::Substituted`](crate::QualityReason::Substituted)`)`,
/// so this list is what a monitoring UI badges from.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ForcedPoint {
    /// The forced point.
    pub point: PointId,
    /// The value the force substitutes.
    pub value: Value,
}

/// One known point's latest observed sample.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct PointTelemetry {
    /// The logical point.
    pub point: PointId,
    /// Whether the controller reads (`In`) or writes (`Out`) the point,
    /// telling input reads and output writes apart.
    pub direction: Direction,
    /// The latest sample the run produced for the point: the scan's input
    /// read for an `In` point, the last staged output write for an `Out`
    /// point. `None` when the point is known but no scan has produced a
    /// sample for it — e.g. an output no component has written yet.
    pub sample: Option<Sample>,
}

/// Runtime diagnostics for one registered component.
///
/// The serializable form of the diagnostics an executor tracks per
/// component: how recently it stepped successfully and how often it failed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentDiagnostics {
    /// The component's name, as it reports it to the executor.
    pub name: String,
    /// The last tick the component stepped without error, if any.
    pub last_tick: Option<Tick>,
    /// How many `step` executions have failed.
    pub step_errors: u64,
    /// The most recent `step` error's message, if any.
    pub last_error: Option<String>,
}

/// One component's current parameter values: the live reading of the
/// tunables its descriptor declares.
///
/// The descriptor's [`ParameterDescriptor`](crate::ParameterDescriptor)s
/// are the editing authority — each tunable's name, [`ValueKind`], and
/// accepted [`ParameterRange`](crate::ParameterRange) — while this
/// section reports what those parameters currently hold, so a faceplate
/// can show the operator the standing tune beside the edit control.
/// `values` carries exactly the descriptor-declared names; a component
/// declaring no parameters reports an empty map. The producer reports
/// from the same fields its checkpoint captures, so the value a
/// faceplate shows is the value a standby inherits.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentParameters {
    /// The component's name — the identity its diagnostics and
    /// descriptor are keyed by in the same snapshot.
    pub name: String,
    /// The current value of each descriptor-declared parameter, keyed by
    /// parameter name.
    pub values: BTreeMap<String, Value>,
}

/// One driver-boundary failure, recorded for the snapshot's I/O health
/// section: the [`IoError`] with the tick, point, and scan boundary it
/// hit.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct IoFault {
    /// The scan tick the failure hit.
    pub tick: Tick,
    /// The point the failure was attributed to — the error's
    /// [`IoError::point`], carried explicitly so a consumer reads the
    /// attribution without decoding the error variant.
    pub point: PointId,
    /// The scan boundary that saw the failure: `In` for the input-read
    /// phase — including a failed cyclic exchange, which opens that
    /// phase — `Out` for the output-write phase.
    pub direction: Direction,
    /// The failure the driver reported.
    pub error: IoError,
}

/// The snapshot's I/O-health section: the producer's driver-boundary
/// counters plus whatever transport diagnostics the driver volunteers
/// through [`IoDriver::diagnostics`](crate::IoDriver::diagnostics).
///
/// This is the "is the I/O subsystem healthy" answer, distinct from
/// per-point quality: a failed read also marks its point's sample `Bad`,
/// but the counters aggregate every boundary fault, and `driver` names
/// link-level degradation no point owns. The counters describe the
/// producing run's own driver experience — they are not checkpointed;
/// a standby's health is its own driver's.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct IoHealth {
    /// Total input reads that failed at the scan's read boundary — each
    /// also produced a `Bad` input sample.
    pub failed_reads: u64,
    /// Total output writes that failed at the scan's write boundary —
    /// each degraded its scan, which still completed.
    pub failed_writes: u64,
    /// Cyclic process-image exchanges that failed at the scan's read
    /// boundary — one per failed `exchange` call on a driver
    /// implementing the cyclic contract
    /// ([`IoDriver::cyclic`](crate::IoDriver::cyclic)), counted once
    /// however many points the image covers. The held input image still
    /// answers the reads that follow, so a covered point counts nothing
    /// until the driver's miss threshold escalates its read to an
    /// ordinary [`failed_reads`](Self::failed_reads) failure. Always `0`
    /// for a non-cyclic driver; absent from snapshots serialized before
    /// the cyclic contract existed.
    #[serde(default)]
    pub failed_exchanges: u64,
    /// Driver-boundary operations that have failed in a row: every
    /// failed read, write, or cyclic exchange extends the count and
    /// every successful one resets it to zero, so it reads as the
    /// failure streak ending at
    /// [`last_error`](Self::last_error).
    pub consecutive_failures: u64,
    /// The most recent driver-boundary failure, with the tick and point
    /// it hit; `None` when no scan has seen one.
    pub last_error: Option<IoFault>,
    /// Scan cycles the pacing shell reports as having overrun their
    /// wall-clock period — fed through `Executor::record_scan_overrun`,
    /// so the counter is wall-clock data entering from outside the
    /// executor's tick domain: a report, never a scan input.
    pub scan_overruns: u64,
    /// The driver's own transport diagnostics, when it implements the
    /// optional `diagnostics` hook; `None` for drivers with nothing
    /// transport-level to report.
    pub driver: Option<DriverDiagnostics>,
}

/// The monitoring publication store's report — the read-side overload
/// surface of the decision that the controller owns execution while UI
/// delivery is a bounded consumer. A monitor materializes one immutable
/// read model per completed scan into bounded storage outside the
/// executor lock and stamps this section onto the snapshot it carries:
/// consumers read the store's own counters here rather than the
/// executor's.
///
/// `None` on a producer's own `Executor::snapshot` view — the section
/// exists only where a publication store publishes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct PublicationHealth {
    /// Read models published since the monitor bound — also the latest
    /// publication's monotonic sequence: assigned in publish order and
    /// never reused, so a consumer holding a seq cursor knows how far
    /// behind the window it has fallen.
    pub published: u64,
    /// Publications that aged out of the retained window — the seq
    /// stretch a lagging seq-cursor consumer can no longer read back
    /// and so observes as the named gap, coalescing onto the retained
    /// tail or the latest state. This is the overload counter: it
    /// moves when scans out-publish the window, never by backpressure
    /// into execution.
    pub coalesced: u64,
    /// Publications the retained window currently holds.
    pub depth: u64,
    /// The retained window's configured bound.
    pub window: u64,
    /// The durable journal sink's drain report when the monitor
    /// appends the journal to a file — `None` (absent on the wire)
    /// without one. The sink drains on its own writer off the
    /// executor lock: `lagging` reports records still queued for it,
    /// `failed` a sink write that is already failing the run at its
    /// next push. Absent from snapshots serialized before the section
    /// existed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub journal_sink: Option<JournalSinkHealth>,
}

/// The named health state of the durable journal sink's drain — the
/// backpressure report the journal-append isolation decision
/// requires. The queue's bound is declared in `capacity`: a sink
/// behind the run's recording rate reports `Lagging`, and a sink
/// write that failed reports `Failed` — the run dies at its next
/// journaled entry naming the file, the fatal-on-append-failure rule
/// moved to the queue's handoff.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JournalSinkState {
    /// The writer is keeping up — no record waits in the drain queue.
    Healthy,
    /// Records wait for the writer — the sink is behind the run's
    /// recording rate. A lag that fills `capacity` is fatal at the
    /// next journaled entry rather than silently dropping one.
    Lagging,
    /// A sink write failed — the run is failing fatally at the
    /// recorded point; `lost` accounts the accepted records the file
    /// never took.
    Failed,
}

/// The durable journal sink's drain accounting — the overload
/// surface beside the publication store's own counters, stamped into
/// the snapshot's `publication` section as of each publish and
/// readable live through the monitor.
///
/// The queue sits between the executor lock's recording point and the
/// writer thread that appends records to the file in `seq` order:
/// `accepted` counts every record handed over, `drained` the ones the
/// file durably took, and `lost` the ones a failed writer consumed
/// without appending — the honest loss accounting for a record the
/// run's audit trail claimed but the file never held.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct JournalSinkHealth {
    /// The sink's standing state.
    pub state: JournalSinkState,
    /// Journaled records handed to the drain queue since bind.
    pub accepted: u64,
    /// Records the writer has appended — `accepted` minus `lost`
    /// minus the in-flight and queued remainder.
    pub drained: u64,
    /// Records the queue admitted but the sink never appended —
    /// nonzero only after a writer failure: the loss the durable file
    /// cannot carry, counted rather than hidden.
    pub lost: u64,
    /// Records waiting in the queue now — what `lagging` reports on.
    pub depth: u64,
    /// The deepest the queue has run.
    pub high_water: u64,
    /// The queue's configured bound — a journaled entry finding it
    /// full fails the run fatally at the push.
    pub capacity: u64,
}

/// The snapshot's command-ingress section: admission metrics for the
/// executor's bounded pending-command queue — the overload visibility
/// the bounded-ingress decision requires beside the bound itself.
///
/// The counters are the run's command-ingress audit like the receipt
/// log they measure: the checkpoint carries them, so a peer that
/// adopted one answers this section identically to the active. The two
/// queue descriptors are local facts, not carried state: `capacity` is
/// construction configuration and `depth` is the adopted pending set —
/// an over-capacity restore reads as `depth >= capacity` until a scan
/// drains it.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct CommandQueueDiagnostics {
    /// Commands presented for admission — every submission the
    /// executor's command path received, whether it settled accepted,
    /// was refused by validation, or was refused by a full queue.
    pub attempts: u64,
    /// Submissions that passed validation but were refused because the
    /// pending queue was already at `capacity` — each answered with a
    /// `queue_full` rejection receipt and queued nothing.
    pub full_rejections: u64,
    /// The declared bound on commands queued awaiting their scan
    /// boundary.
    pub capacity: usize,
    /// Commands currently queued awaiting the next scan boundary.
    pub depth: usize,
    /// The deepest the pending queue has run — the high-water mark.
    pub high_water: usize,
}

/// One command's standing availability verdict — the probe's answer to
/// "is this declared `KindDeclared` command invocable at all now".
///
/// The verdicts are produced inside the scan boundary — the producer
/// evaluates each component's availability probe once per declared
/// [`KindDeclared`](crate::CommandAvailability::KindDeclared) command
/// after each completed scan — never under a consumer read. They are
/// advisory only: a submission still validates, queues, and settles
/// through the receipted path, which remains the sole authority — a
/// verdict the dispatch disagrees with settles honestly on the receipt
/// rather than failing the scan or altering the command.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CommandVerdict {
    /// The command's name — a [`CommandDecl`](crate::CommandDecl) name
    /// the component's descriptor declares.
    pub name: String,
    /// Whether a submission dispatches to the kind's implementation
    /// now — the probe's standing answer. `true` reports the command
    /// invocable; dispatch may still refuse argument-dependent or
    /// kind-invariant reasons, which settle on the receipt.
    pub available: bool,
    /// The kind's standing refusal reason when `available` is `false` —
    /// the same text a refused invocation's
    /// [`CommandError::CommandRefused`](crate::CommandError) receipt
    /// carries; absent when `available`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refusal: Option<String>,
}

/// One component's `KindDeclared`-command availability — a
/// [`TelemetrySnapshot`] `command_verdicts` entry.
///
/// `verdicts` covers exactly the commands the component's descriptor
/// declares [`KindDeclared`](crate::CommandAvailability::KindDeclared):
/// `Always`-available commands are admissible by construction and
/// `BoundPointWritable` ones read against the signal index, so neither
/// takes a probe verdict. A component declaring no `KindDeclared`
/// commands reports an empty `verdicts`; a kind that does not
/// implement the probe reports each one `available` — the unconditional
/// reporting the read model already produced before the section
/// existed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ComponentCommands {
    /// The component's name — the join key `components`, `descriptors`,
    /// and `parameters` share.
    pub name: String,
    /// The standing verdict for each `KindDeclared`-declared command,
    /// in the descriptor's declaration order.
    #[serde(default)]
    pub verdicts: Vec<CommandVerdict>,
}

/// A point-in-time snapshot of a controller run for monitoring consumers.
///
/// A snapshot reports state, not history: each point and each component
/// appears once, carrying its latest observation. Producers order `points`
/// by ascending [`PointId`] and `components` by execution order so equal
/// runs serialize identically.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetrySnapshot {
    /// The producer's tick when the snapshot was taken; [`Tick::ZERO`]
    /// before the first scan.
    pub tick: Tick,
    /// The latest sample of every point the producer knows, ordered by
    /// point id.
    pub points: Vec<PointTelemetry>,
    /// Diagnostics of every registered component, in execution order.
    pub components: Vec<ComponentDiagnostics>,
    /// One self-description per registered component, in the same
    /// execution order as `components`: `descriptors[i]` describes the
    /// component `components[i]` diagnoses. This is where the executor
    /// surfaces each component's `describe()` result — the static
    /// metadata a UI renders faceplates from.
    pub descriptors: Vec<ComponentDescriptor>,
    /// The I/O-health section: driver-boundary failure counters the
    /// executor collects, the scan-overrun count the pacing shell feeds,
    /// and the driver's volunteered transport diagnostics.
    pub io_health: IoHealth,
    /// The active force set — every point currently pinned to an
    /// operator-forced value — ordered by ascending point id, so equal
    /// runs serialize identically. Empty when nothing is forced;
    /// absent from snapshots serialized before forces existed.
    #[serde(default)]
    pub forces: Vec<ForcedPoint>,
    /// Per-component current parameter values in the same execution
    /// order as `components` and `descriptors`: `parameters[i]` reports
    /// the live values of the parameters `descriptors[i]` declares for
    /// the component `components[i]` diagnoses — exactly the
    /// descriptor-declared names, so the descriptor stays the editing
    /// surface's sole authority. A component declaring no parameters
    /// reports an empty `values` map; absent from snapshots serialized
    /// before the section existed.
    #[serde(default)]
    pub parameters: Vec<ComponentParameters>,
    /// The command-ingress section: the bounded pending-command queue's
    /// admission metrics — submissions attempted, full-queue rejections,
    /// the declared capacity, and the queue's current depth and
    /// high-water mark — so a monitoring consumer sees command-path
    /// overload as telemetry rather than as timing failure. Absent from
    /// snapshots serialized before the bound existed; such a snapshot
    /// reads back with a zeroed section.
    #[serde(default)]
    pub command_queue: CommandQueueDiagnostics,
    /// The `KindDeclared`-command availability section: one
    /// [`ComponentCommands`] per registered component in the same
    /// execution order as `components` and `descriptors`, carrying the
    /// standing verdicts the producer's post-scan availability probe
    /// evaluated for each declared
    /// [`KindDeclared`](crate::CommandAvailability::KindDeclared)
    /// command. The verdicts are advisory — the receipted command path
    /// stays the sole authority — and are evaluated at the scan
    /// boundary, never under a consumer read. Absent from snapshots
    /// serialized before the section existed; such a snapshot reads
    /// back with an empty section.
    #[serde(default)]
    pub command_verdicts: Vec<ComponentCommands>,
    /// The serving monitor's publication-store report — the overload
    /// counters of the bounded read-model storage this snapshot was
    /// published into. `None` — and absent on the wire — on a
    /// producer's own snapshot; a monitor stamps it as of the publish
    /// the snapshot rides. Absent from snapshots serialized before the
    /// section existed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub publication: Option<PublicationHealth>,
}

impl PartialEq for TelemetrySnapshot {
    /// The `publication` section is the serving monitor's own
    /// bookkeeping — its store's counters, whatever instance answered —
    /// not run state: two peers of a redundant pair legitimately
    /// publish different counts, and a restarted peer restarts them.
    /// Equality is therefore the run's state — every field but that
    /// section; the destructure names each compared field so a future
    /// field forces the decision here.
    fn eq(&self, other: &Self) -> bool {
        let Self {
            tick,
            points,
            components,
            descriptors,
            io_health,
            forces,
            parameters,
            command_queue,
            command_verdicts,
            publication: _,
        } = self;
        tick == &other.tick
            && points == &other.points
            && components == &other.components
            && descriptors == &other.descriptors
            && io_health == &other.io_health
            && forces == &other.forces
            && parameters == &other.parameters
            && command_queue == &other.command_queue
            && command_verdicts == &other.command_verdicts
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::descriptor::{ParameterDescriptor, ParameterRange, PortDescriptor, PortRole};
    use crate::io::LinkState;
    use crate::signal::{Quality, QualityReason, Value, ValueKind};

    #[test]
    fn snapshot_serde_roundtrip() {
        let snapshot = TelemetrySnapshot {
            tick: Tick(3),
            points: vec![
                PointTelemetry {
                    point: PointId(10),
                    direction: Direction::In,
                    sample: Some(Sample::new(
                        Value::Float(7.0),
                        Quality::Bad(QualityReason::CommunicationFault),
                        Tick(3),
                    )),
                },
                PointTelemetry {
                    point: PointId(20),
                    direction: Direction::Out,
                    sample: None,
                },
            ],
            components: vec![
                ComponentDiagnostics {
                    name: "scale".to_string(),
                    last_tick: Some(Tick(3)),
                    step_errors: 0,
                    last_error: None,
                },
                ComponentDiagnostics {
                    name: "fragile".to_string(),
                    last_tick: None,
                    step_errors: 2,
                    last_error: Some("computation failed".to_string()),
                },
            ],
            descriptors: vec![
                ComponentDescriptor {
                    name: "scale".to_string(),
                    kind: "scale".to_string(),
                    label: "scale".to_string(),
                    ports: vec![
                        PortDescriptor {
                            name: "in".to_string(),
                            direction: Direction::In,
                            kind: ValueKind::Float,
                            role: Some(PortRole::ProcessValue),
                            point: Some(PointId(10)),
                        },
                        PortDescriptor {
                            name: "out".to_string(),
                            direction: Direction::Out,
                            kind: ValueKind::Float,
                            role: Some(PortRole::Output),
                            point: Some(PointId(20)),
                        },
                    ],
                    parameters: vec![ParameterDescriptor {
                        name: "gain".to_string(),
                        kind: ValueKind::Float,
                        range: Some(ParameterRange {
                            min: Value::Float(0.0),
                            max: Value::Float(10.0),
                        }),
                    }],
                    commands: Vec::new(),
                    events: Vec::new(),
                },
                ComponentDescriptor {
                    name: "fragile".to_string(),
                    kind: "fragile".to_string(),
                    label: "fragile".to_string(),
                    ports: Vec::new(),
                    parameters: Vec::new(),
                    commands: Vec::new(),
                    events: Vec::new(),
                },
            ],
            io_health: IoHealth {
                failed_reads: 4,
                failed_writes: 1,
                failed_exchanges: 2,
                consecutive_failures: 2,
                last_error: Some(IoFault {
                    tick: Tick(3),
                    point: PointId(10),
                    direction: Direction::In,
                    error: IoError::Disconnected(PointId(10)),
                }),
                scan_overruns: 1,
                driver: Some(DriverDiagnostics {
                    link: LinkState::Disconnected,
                    last_error: Some("no live connection to the plant server".to_string()),
                    exchange: Some(crate::ExchangeDiagnostics {
                        attempted: 5,
                        succeeded: 3,
                        working_counter_mismatches: 1,
                        last_exchange_tick: Some(Tick(2)),
                        missed_deadlines: 1,
                    }),
                }),
            },
            forces: vec![ForcedPoint {
                point: PointId(10),
                value: Value::Float(7.0),
            }],
            parameters: vec![
                ComponentParameters {
                    name: "scale".to_string(),
                    values: [("gain".to_string(), Value::Float(7.0))]
                        .into_iter()
                        .collect(),
                },
                ComponentParameters {
                    name: "fragile".to_string(),
                    values: BTreeMap::new(),
                },
            ],
            command_queue: CommandQueueDiagnostics {
                attempts: 9,
                full_rejections: 2,
                capacity: 64,
                depth: 3,
                high_water: 6,
            },
            command_verdicts: vec![
                ComponentCommands {
                    name: "scale".to_string(),
                    verdicts: vec![CommandVerdict {
                        name: "advance".to_string(),
                        available: false,
                        refusal: Some("the run is complete".to_string()),
                    }],
                },
                ComponentCommands {
                    name: "fragile".to_string(),
                    verdicts: Vec::new(),
                },
            ],
            publication: Some(PublicationHealth {
                published: 7,
                coalesced: 3,
                depth: 4,
                window: 8,
                journal_sink: None,
            }),
        };
        let json = serde_json::to_string(&snapshot).unwrap();
        assert_eq!(
            serde_json::from_str::<TelemetrySnapshot>(&json).unwrap(),
            snapshot
        );

        // A snapshot serialized before forces, parameter reporting, the
        // publication, command-queue and command-verdict sections, and
        // the cyclic exchange counters existed carries none of those
        // fields and reads back with empty sections.
        let mut document: serde_json::Value = serde_json::from_str(&json).unwrap();
        let object = document.as_object_mut().unwrap();
        object.remove("forces");
        object.remove("parameters");
        object.remove("publication");
        object.remove("command_queue");
        object.remove("command_verdicts");
        object
            .get_mut("io_health")
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove("failed_exchanges");
        let legacy: TelemetrySnapshot = serde_json::from_value(document).unwrap();
        assert_eq!(legacy.forces, Vec::new());
        assert_eq!(legacy.parameters, Vec::new());
        assert_eq!(legacy.publication, None);
        assert_eq!(legacy.io_health.failed_exchanges, 0);
        assert_eq!(legacy.command_queue, CommandQueueDiagnostics::default());
        assert_eq!(legacy.command_verdicts, Vec::new());
    }
}
