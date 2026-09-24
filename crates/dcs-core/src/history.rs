//! Bounded per-point sample history for monitoring trend views.
//!
//! A [`TelemetrySnapshot`](crate::TelemetrySnapshot) reports each point's
//! latest [`Sample`]; a history retains the recent sequence of them so a
//! trend view can draw the run's past, not just its present. Producers
//! append one [`HistorySample`] per point per completed scan, bounded by a
//! configured capacity with oldest-first eviction. Every appended sample
//! carries a stream [`seq`](HistorySample::seq) drawn from the run's tick
//! domain and never reused, so an evicted stretch is visible to consumers
//! as a numbering gap rather than silent loss — including across a
//! restart that continues the tick domain, where the axis simply carries
//! on. Each served [`PointHistory`] also stamps the producing process
//! lifetime's [`run`](PointHistory::run) ordinal, so a restart that
//! begins a new tick domain is detectable even when the restarted axis
//! hides behind a `since` cursor.
//!
//! The types are serde-serializable monitoring contracts like the rest of
//! `dcs-core`: the transport serves them and a UI consumes them without
//! depending on the producing runtime.

use crate::signal::{PointId, Sample};
use serde::{Deserialize, Serialize};

/// One recorded sample in a point's history stream.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct HistorySample {
    /// The sample's position in its point's stream: the producing
    /// scan's tick, strictly increasing and never reused within the
    /// stream, so bounded eviction — or any other stretch the serving
    /// run did not serve — is visible to consumers as a numbering gap.
    /// Numbering rides the run's tick domain rather than a per-process
    /// append count, so a restart that continues the tick domain (a
    /// checkpoint-adopted standby or a state-restored run) keeps the
    /// axis continuous instead of silently restarting it; a restart
    /// beginning a new tick domain restarts the axis, distinguishable
    /// through the envelope's [`run`](PointHistory::run) marker.
    pub seq: u64,
    /// The recorded sample, stamped with the scan tick that produced it.
    pub sample: Sample,
}

/// One point's retained history.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointHistory {
    /// The point this history belongs to.
    pub point: PointId,
    /// The serving process's lifetime ordinal — the same counter the
    /// journal's `run_boundary` markers carry: the configured journal
    /// file's run count, or `1` on a monitor without one. Served on the
    /// envelope rather than per sample so a `since`-filtered answer —
    /// an empty `samples` included — still carries it: a cursor consumer
    /// comparing across polls reads a changed `run` as the seq axis
    /// having restarted, never as the stream silently continuing.
    /// `0` on a payload a producer predating the marker served.
    #[serde(default)]
    pub run: u64,
    /// The retained samples in append order — oldest first, so in
    /// ascending tick order — bounded by the producer's configured
    /// capacity. A point that has produced no samples yet reports an
    /// empty list.
    pub samples: Vec<HistorySample>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signal::{Quality, Tick, Value};

    #[test]
    fn history_serde_roundtrip() {
        let history = PointHistory {
            point: PointId(10),
            run: 1,
            samples: vec![
                HistorySample {
                    seq: 1,
                    sample: Sample::good(Value::Float(3.0), Tick(1)),
                },
                HistorySample {
                    seq: 2,
                    sample: Sample::new(
                        Value::Float(3.5),
                        Quality::Uncertain(crate::QualityReason::Stale),
                        Tick(2),
                    ),
                },
            ],
        };
        let json = serde_json::to_string(&history).unwrap();
        assert_eq!(
            serde_json::from_str::<PointHistory>(&json).unwrap(),
            history
        );
        let empty = PointHistory {
            point: PointId(30),
            run: 1,
            samples: Vec::new(),
        };
        let json = serde_json::to_string(&empty).unwrap();
        assert_eq!(serde_json::from_str::<PointHistory>(&json).unwrap(), empty);
    }
}
