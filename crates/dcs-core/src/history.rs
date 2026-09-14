//! Bounded per-point sample history for monitoring trend views.
//!
//! A [`TelemetrySnapshot`](crate::TelemetrySnapshot) reports each point's
//! latest [`Sample`]; a history retains the recent sequence of them so a
//! trend view can draw the run's past, not just its present. Producers
//! append one [`HistorySample`] per point per completed scan, bounded by a
//! configured capacity with oldest-first eviction. Every appended sample
//! carries a stream [`seq`](HistorySample::seq) assigned in append order
//! and never reused, so an evicted stretch is visible to consumers as a
//! numbering gap rather than silent loss.
//!
//! The types are serde-serializable monitoring contracts like the rest of
//! `dcs-core`: the transport serves them and a UI consumes them without
//! depending on the producing runtime.

use crate::signal::{PointId, Sample};
use serde::{Deserialize, Serialize};

/// One recorded sample in a point's history stream.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct HistorySample {
    /// The sample's position in its point's stream: assigned in append
    /// order starting at 1 and increasing by one per append. Numbers are
    /// never reused, so bounded eviction is visible as a gap.
    pub seq: u64,
    /// The recorded sample, stamped with the scan tick that produced it.
    pub sample: Sample,
}

/// One point's retained history.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointHistory {
    /// The point this history belongs to.
    pub point: PointId,
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
            samples: Vec::new(),
        };
        let json = serde_json::to_string(&empty).unwrap();
        assert_eq!(serde_json::from_str::<PointHistory>(&json).unwrap(), empty);
    }
}
