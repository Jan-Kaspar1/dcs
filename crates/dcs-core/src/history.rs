//! Bounded per-point sample history for monitoring trend views.
//!
//! A [`TelemetrySnapshot`](crate::TelemetrySnapshot) reports each point's
//! latest [`Sample`]; a history retains the recent sequence of them so a
//! trend view can draw the run's past, not just its present. Producers
//! append one [`HistorySample`] per point per completed scan, bounded by a
//! configured capacity with oldest-first eviction. Every appended sample
//! carries a stream [`seq`](HistorySample::seq) assigned in append order
//! and never reused *within a run*, so an evicted stretch is visible to
//! consumers as a numbering gap rather than silent loss.
//!
//! The ring itself is volatile — only the journal persists across a
//! restart — so a new process lifetime renumbers its samples from 1.
//! [`PointHistory::run`] is the served mark that keeps that reset honest:
//! it names the producing lifetime on every answer, empty pages included,
//! so a `since`-cursor consumer holding a dead lifetime's cursor sees the
//! run change rather than a silent stall and resyncs from `since=0`.
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
    /// never reused *within a run*, so bounded eviction is visible as a
    /// gap; a new run — the producer's restart — renumbers from 1, which
    /// [`PointHistory::run`] names.
    pub seq: u64,
    /// The recorded sample, stamped with the scan tick that produced it.
    pub sample: Sample,
}

/// One point's retained history.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PointHistory {
    /// The point this history belongs to.
    pub point: PointId,
    /// The process lifetime the serving producer belongs to — the run
    /// number the monitor's journal file counts, the same numbering a
    /// `run_boundary` journal entry marks, so a consumer can attribute a
    /// sample stretch to the lifetime the journal names. The volatile
    /// ring's `seq`s are scoped to a run and restart at 1 on the next, so
    /// this field is what makes that reset explicit: a `since` cursor
    /// captured under an earlier run names a dead seq domain, and the
    /// changed `run` on the answer — which is always present, even with
    /// an empty `samples` page — tells the consumer to resync from
    /// `since=0` rather than wait on samples that can never pass the
    /// stale cursor. A monitor without a journal file records no
    /// lifetimes and serves `run: 1` on every one: restart attribution
    /// needs the durable record, exactly as the journal's own
    /// `run_boundary` markers do. Answers from a producer that predates
    /// the field deserialize as `0` — the unattributed run.
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
