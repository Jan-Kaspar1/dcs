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
use crate::io::Direction;
use crate::signal::{PointId, Sample, Tick};
use serde::{Deserialize, Serialize};

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
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::descriptor::{ParameterDescriptor, ParameterRange, PortDescriptor, PortRole};
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
        };
        let json = serde_json::to_string(&snapshot).unwrap();
        assert_eq!(
            serde_json::from_str::<TelemetrySnapshot>(&json).unwrap(),
            snapshot
        );
    }
}
