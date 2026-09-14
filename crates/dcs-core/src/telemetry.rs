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
    /// phase, `Out` for the output-write phase.
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
    /// each also failed its scan with `ScanError`.
    pub failed_writes: u64,
    /// Driver-boundary operations that have failed in a row: every
    /// failed read or write extends the count and every successful one
    /// resets it to zero, so it reads as the failure streak ending at
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

/// A point-in-time snapshot of a controller run for monitoring consumers.
///
/// A snapshot reports state, not history: each point and each component
/// appears once, carrying its latest observation. Producers order `points`
/// by ascending [`PointId`] and `components` by execution order so equal
/// runs serialize identically.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
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
                        },
                        PortDescriptor {
                            name: "out".to_string(),
                            direction: Direction::Out,
                            kind: ValueKind::Float,
                            role: Some(PortRole::Output),
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
                },
                ComponentDescriptor {
                    name: "fragile".to_string(),
                    kind: "fragile".to_string(),
                    label: "fragile".to_string(),
                    ports: Vec::new(),
                    parameters: Vec::new(),
                },
            ],
            io_health: IoHealth {
                failed_reads: 4,
                failed_writes: 1,
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
                }),
            },
            forces: vec![ForcedPoint {
                point: PointId(10),
                value: Value::Float(7.0),
            }],
        };
        let json = serde_json::to_string(&snapshot).unwrap();
        assert_eq!(
            serde_json::from_str::<TelemetrySnapshot>(&json).unwrap(),
            snapshot
        );

        // A snapshot serialized before forces existed carries no field
        // and reads back with an empty force set.
        let mut document: serde_json::Value = serde_json::from_str(&json).unwrap();
        document.as_object_mut().unwrap().remove("forces");
        let legacy: TelemetrySnapshot = serde_json::from_value(document).unwrap();
        assert_eq!(legacy.forces, Vec::new());
    }
}
