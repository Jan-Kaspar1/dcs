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

use crate::signal::{PointId, Sample, Tick};
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

/// The tick-to-civil-time anchor — "one anchor per tick domain: the
/// wall-clock instant of the domain's origin tick, captured by the
/// pacing layer" (durable-history decision record). The domain owns
/// one: a paced run mints it when the domain begins, the state file
/// persists it beside the run's durable artifacts so a resume keeps
/// the domain's anchor, and a tracking peer adopts the tracked
/// line's through checkpoint state rather than minting its own. The
/// anchor is how the platform's durable samples and journal events
/// map onto civil time at the export seam —
/// `anchor + (tick - origin) * period` — without a clock ever
/// entering a component or the executor's step logic. A driven run
/// mints no anchor and its artifacts stay byte-identical under an
/// unchanged script.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct TickAnchor {
    /// Milliseconds since the Unix epoch at the domain's origin tick.
    pub epoch_ms: u64,
}

/// One kind of record the durable process-history stream holds —
/// the payload of a [`DurableEntry`]. The stream is the
/// durable-history decision's bounded served window over the
/// declared-duty record: declared-`record` points' samples captured
/// at their declared cadence, plus the boundary markers a consumer
/// attributes lifetimes and tick domains around.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DurableEvent {
    /// A declared-duty point's post-scan image sample, recorded at the
    /// point's declared cadence — the faithful, quality-stamped
    /// capture the durable duty names. The entry's `tick` is the
    /// recording scan's tick; the [`Sample`] carries its own stamp.
    Sampled {
        /// The point the sample belongs to.
        point: PointId,
        /// The recorded sample, verbatim.
        sample: Sample,
    },
    /// A process lifetime began — the restart's served marker, the
    /// file's `run_boundary` record's served form. `run` counts the
    /// file's lifetimes from 1; `anchor` stamps the tick domain the
    /// run records under, absent on an unanchored domain. Pinned in
    /// the served window: ordinary record volume can never age it
    /// out, so every recorded lifetime stays attributable.
    RunBoundary {
        /// Which lifetime began — the file counts runs from 1.
        run: u64,
        /// The domain's declared civil-time anchor — absent on an
        /// unanchored domain.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        anchor: Option<TickAnchor>,
    },
    /// The run's tick domain changed mid-lifetime — a tracking peer's
    /// checkpoint adoption moved it onto the tracked line's newer
    /// domain, and the records that follow map under `anchor`. `None`
    /// names an unanchored adoption. Pinned like the run boundary:
    /// the domain seam is the attribution record the mapping stands
    /// on.
    Domain {
        /// The adopted domain's declared civil-time anchor — absent
        /// on an unanchored domain.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        anchor: Option<TickAnchor>,
    },
}

/// One record in the durable process-history stream: a
/// [`DurableEvent`] stamped with its position. `seq` is assigned in
/// append order and never reused — across restarts, where the
/// replayed file's numbering continues — so bounded eviction is
/// visible to consumers as a numbering gap rather than silent loss.
/// `tick` attributes the record to the run's tick domain: the
/// recording scan's tick for a sample, the boundary's for a marker.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DurableEntry {
    /// The record's position in the durable stream.
    pub seq: u64,
    /// The run-tick attribution.
    pub tick: Tick,
    /// What the record carries.
    pub event: DurableEvent,
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
