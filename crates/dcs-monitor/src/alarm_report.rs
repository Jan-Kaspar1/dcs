//! The alarm flood and performance report — the computing surface the
//! flood-and-performance decision records for `WW-ALM-004`.
//!
//! The metrics are computed data, not contract: this module derives them
//! from the existing monitoring surface — the transition journal
//! (`GET /journal` or the durable journal file
//! [`read_journal_file`](crate::read_journal_file) reads), the retained
//! point history (`GET /history`), and the alarm-surface join the
//! snapshot's descriptors and the signal index carry — with no served
//! metrics endpoint, no in-controller computation, and no new wire
//! type, exactly the plant-overview decision's page/tooling-side
//! precedent. `dcs-alarm-report` is the standalone tool fronting this
//! module.
//!
//! ## The record the metrics read
//!
//! The dataset is the durable ordered transition record: `point_changed`
//! entries on the alarm kinds' declared-`journaled` status points give
//! every activation, return, and managed-state transition in `seq`
//! order, and the settled `WriteValue`/`ForcePoint` receipts on each
//! alarm's `ack` point give the attributed acknowledgments. A burst
//! therefore preserves every underlying transition — a suppressed
//! alarm's `alarm` still asserts and journals, only its
//! `unacknowledged` annunciation demand is withheld — and `seq` order,
//! not tick order, keeps a same-tick burst's initiating cause first.
//!
//! A run boundary never double-counts: the journal file carries a
//! restarted run's first observation of each journaled point as
//! `from: None`, and the fold below diffs against the value the record
//! last carried, so a standing alarm re-observed after restart is not a
//! second activation. Within a served, bounded journal the same rule
//! uses the entry's `from` where the record's earlier entries are
//! evicted. History supplies what the journal cannot: when a point has
//! no surviving journal entries, its retained samples still say whether
//! the alarm stands, and the walk-back over trailing equal samples
//! bounds the standing duration.
//!
//! ## The declared metric set
//!
//! An *activation* is a transition to asserted on an instance's `alarm`
//! point; an *annunciation* is a transition to asserted on its
//! `unacknowledged` point — the annunciation demand — or the activation
//! itself for a kind declaring no `unacknowledged`. From those:
//!
//! - **rates** — total activations and annunciations, the average per
//!   flood window and per 1440-minute day over the record span, and the
//!   peak activations in any window;
//! - **flood periods** — maximal runs of overlapping flood windows, each
//!   carrying its activation and annunciation counts and its first-out
//!   activation (the lowest `seq` in the period); the tumbling-window
//!   view reports the fraction of periods over the bound;
//! - **standing and stale** — alarms asserted at the record's end, each
//!   with its since-tick, standing duration, and stale verdict against
//!   the declared bound, plus the managed states currently asserted;
//! - **chattering and fleeting** — instances reaching the declared
//!   activation count inside the chattering window, and episodes
//!   clearing inside the fleeting bound;
//! - **most frequent** — the activation ranking, ties by component name;
//! - **managed-state accounting** — per bound `shelved`/`suppressed`/
//!   `out_of_service` state, the episode count, the total ticks in the
//!   state, and whether it stands open;
//! - **response times** — FIFO pairs of annunciations to the next
//!   applied `ack` receipt, each checked against the instance's declared
//!   `response_ticks`, with pending annunciations and unpaired acks
//!   counted;
//! - **priority distribution** — annunciations bucketed by the
//!   instance's declared `priority`, the conventional ~80/15/5 shape the
//!   report compares against being site policy.
//!
//! Every threshold is declared report configuration carried in
//! [`ReportConfig`] — the conventional 10-alarms-per-10-minutes flood
//! bound, the 24-hour stale bound, the 3-in-1-minute chattering bound,
//! and the 1-minute fleeting bound expressed through the declared
//! `ticks_per_minute` mapping — never compiled policy. The report echoes
//! the resolved thresholds it computed against.

use crate::journal_file::RunBoundary;
use dcs_core::{
    Command, CommandOutcome, Direction, JournalEntry, JournalEvent, PointHistory, PointId,
    PortRole, TelemetrySnapshot, Tick, Value,
};
use dcs_model::SignalIndex;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, VecDeque};

/// The declared report configuration: the site-policy thresholds the
/// metrics compute against, every field defaulting to the conventional
/// bound the flood-and-performance decision records. Durations are
/// declared in minutes and resolved to ticks through
/// `ticks_per_minute` — the run's tick-domain mapping, which the
/// report's operator declares, since journal stamps are virtual ticks.
///
/// Carried as report input (a JSON document to `dcs-alarm-report
/// --config`), never compiled into the surface: the site's alarm
/// philosophy owns the values.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ReportConfig {
    /// How many ticks make a minute — the report's tick→time mapping.
    /// Default 60 (the one-second scan convention).
    pub ticks_per_minute: u64,
    /// The flood window in minutes. Default 10 — the conventional
    /// 10-minute flood window.
    pub flood_window_minutes: u64,
    /// More than this many activations inside a flood window is a flood
    /// period. Default 10 — the conventional 10-per-10-minutes bound.
    pub flood_alarms: u64,
    /// An alarm standing past this many minutes is stale. Default 1440
    /// — the conventional 24-hour bound.
    pub stale_minutes: u64,
    /// This many activations inside the chattering window mark a
    /// chattering alarm. Default 3.
    pub chattering_activations: u64,
    /// The chattering window in minutes. Default 1 — the conventional
    /// three-activations-in-one-minute bound.
    pub chattering_window_minutes: u64,
    /// An activation returning within this many minutes is fleeting —
    /// the episode clears almost immediately. Default 1.
    pub fleeting_minutes: u64,
}

impl Default for ReportConfig {
    fn default() -> Self {
        Self {
            ticks_per_minute: 60,
            flood_window_minutes: 10,
            flood_alarms: 10,
            stale_minutes: 1440,
            chattering_activations: 3,
            chattering_window_minutes: 1,
            fleeting_minutes: 1,
        }
    }
}

impl ReportConfig {
    /// The declared configuration resolved into the tick domain — the
    /// thresholds [`compute_report`] applies and the report echoes.
    pub fn thresholds(&self) -> Thresholds {
        let ticks_per_minute = self.ticks_per_minute.max(1);
        Thresholds {
            ticks_per_minute,
            flood_window_minutes: self.flood_window_minutes,
            flood_window_ticks: self
                .flood_window_minutes
                .saturating_mul(ticks_per_minute)
                .max(1),
            flood_alarms: self.flood_alarms,
            stale_minutes: self.stale_minutes,
            stale_ticks: self.stale_minutes.saturating_mul(ticks_per_minute),
            chattering_activations: self.chattering_activations,
            chattering_window_minutes: self.chattering_window_minutes,
            chattering_window_ticks: self
                .chattering_window_minutes
                .saturating_mul(ticks_per_minute)
                .max(1),
            fleeting_minutes: self.fleeting_minutes,
            fleeting_ticks: self
                .fleeting_minutes
                .saturating_mul(ticks_per_minute)
                .max(1),
        }
    }
}

/// The declared report configuration resolved into the tick domain,
/// echoed on the report so a reading of the metrics always carries the
/// thresholds they were computed against.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Thresholds {
    /// The declared tick→minute mapping.
    pub ticks_per_minute: u64,
    /// The declared flood window in minutes.
    pub flood_window_minutes: u64,
    /// The flood window in ticks.
    pub flood_window_ticks: u64,
    /// The flood bound: strictly more than this many activations in a
    /// flood window.
    pub flood_alarms: u64,
    /// The declared stale bound in minutes.
    pub stale_minutes: u64,
    /// The stale bound in ticks.
    pub stale_ticks: u64,
    /// The chattering bound: at least this many activations in the
    /// chattering window.
    pub chattering_activations: u64,
    /// The declared chattering window in minutes.
    pub chattering_window_minutes: u64,
    /// The chattering window in ticks.
    pub chattering_window_ticks: u64,
    /// The declared fleeting bound in minutes.
    pub fleeting_minutes: u64,
    /// The fleeting bound in ticks: an episode shorter than this is
    /// fleeting.
    pub fleeting_ticks: u64,
}

/// One alarm instance's lifecycle join: which logical points carry
/// which of the alarm vocabulary's flags, plus the declared
/// rationalization parameters the metrics read — `priority` for the
/// distribution, `response_ticks` for the response-time check.
///
/// Built by [`alarm_instances`] from the served snapshot's descriptors
/// and parameters — the same descriptor-to-point join the page's alarm
/// pane performs — so the report sees exactly the alarm surface the
/// model declares, across kinds, by port name.
#[derive(Debug, Clone, PartialEq)]
pub struct AlarmInstance {
    /// The instance's diagnostic name — the descriptor's `name`.
    pub component: String,
    /// The `alarm` point's signal name — the human-facing alarm label.
    pub signal: String,
    /// The `alarm` status `Out` port's bound point — the standing
    /// process-truth flag whose assertions are the report's
    /// activations.
    pub alarm: PointId,
    /// The `unacknowledged` status port's bound point — the
    /// annunciation demand — when the kind declares one.
    pub unacknowledged: Option<PointId>,
    /// The `ack` input's bound point — the point an applied
    /// `WriteValue`/`ForcePoint` receipt on which counts as the
    /// acknowledgment — when the kind declares one.
    pub ack: Option<PointId>,
    /// The `shelved` status port's bound point, when bound.
    pub shelved: Option<PointId>,
    /// The `suppressed` status port's bound point, when bound.
    pub suppressed: Option<PointId>,
    /// The `out_of_service` status port's bound point, when bound.
    pub out_of_service: Option<PointId>,
    /// The declared `priority` parameter, when the instance carries it.
    pub priority: Option<i64>,
    /// The declared `response_ticks` parameter — the allowable
    /// response time — when the instance carries it.
    pub response_ticks: Option<u64>,
}

/// The descriptor-to-point join producing the report's [`AlarmInstance`]s:
/// every component declaring a status-role `Out` port named `alarm`
/// bound to a point is an alarm instance; the remaining uniform
/// vocabulary joins by port name across kinds, and `priority` /
/// `response_ticks` join from the snapshot's parameters section by
/// component name. `index` supplies each `alarm` point's signal name —
/// the label the report prints.
pub fn alarm_instances(snapshot: &TelemetrySnapshot, index: &SignalIndex) -> Vec<AlarmInstance> {
    let mut instances = Vec::new();
    for descriptor in &snapshot.descriptors {
        let status_point = |name: &str| {
            descriptor
                .ports
                .iter()
                .find(|port| {
                    port.name == name
                        && port.direction == Direction::Out
                        && port.role == Some(PortRole::Status)
                })
                .and_then(|port| port.point)
        };
        let Some(alarm) = status_point("alarm") else {
            continue;
        };
        let ack = descriptor
            .ports
            .iter()
            .find(|port| port.name == "ack" && port.direction == Direction::In)
            .and_then(|port| port.point);
        let values = snapshot
            .parameters
            .iter()
            .find(|entry| entry.name == descriptor.name)
            .map(|entry| &entry.values);
        let int = |name: &str| match values.and_then(|values| values.get(name)) {
            Some(Value::Int(value)) => Some(*value),
            _ => None,
        };
        instances.push(AlarmInstance {
            component: descriptor.name.clone(),
            signal: index
                .get(alarm)
                .map(|entry| entry.name.clone())
                .unwrap_or_else(|| format!("point-{}", alarm.0)),
            alarm,
            unacknowledged: status_point("unacknowledged"),
            ack,
            shelved: status_point("shelved"),
            suppressed: status_point("suppressed"),
            out_of_service: status_point("out_of_service"),
            priority: int("priority"),
            response_ticks: int("response_ticks").and_then(|v| u64::try_from(v).ok()),
        });
    }
    instances
}

/// What the report computed over: the journal stretch and the tick span
/// the rates and windows divide by.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReportSource {
    /// Journal entries the report consumed.
    pub journal_entries: u64,
    /// Process lifetimes the durable file records — `None` when the
    /// journal came from `GET /journal`, which carries no run-boundary
    /// markers.
    pub journal_runs: Option<u64>,
    /// The first and last consumed entries' `seq`s — `None` on an empty
    /// record.
    pub first_seq: Option<u64>,
    /// See [`first_seq`](Self::first_seq).
    pub last_seq: Option<u64>,
    /// The earliest journaled tick in its run's domain — `None` on an
    /// empty record.
    pub first_tick: Option<Tick>,
    /// The latest journaled tick in its run's domain — `None` on an
    /// empty record.
    pub last_tick: Option<Tick>,
    /// The record's end on the duration axis: the latest position the
    /// inputs observe — journal entries, retained history samples, and
    /// the served snapshot's tick — measured in elapsed scans across
    /// every run the record spans.
    pub end: u64,
    /// `end` minus the first entry's axis position; `0` on an empty
    /// record.
    pub span_ticks: u64,
}

/// One alarm instance's computed metrics — the per-alarm detail behind
/// the report's cross-alarm sections.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AlarmMetrics {
    /// The instance's diagnostic name.
    pub component: String,
    /// The `alarm` point's signal name.
    pub signal: String,
    /// The `alarm` status point.
    pub alarm_point: PointId,
    /// The declared `priority` parameter, when carried.
    pub priority: Option<i64>,
    /// The declared `response_ticks` parameter, when carried.
    pub response_ticks: Option<u64>,
    /// Transitions to asserted on `alarm` — every underlying activation
    /// counts, suppressed or not.
    pub activations: u64,
    /// Transitions to asserted on `unacknowledged` — the annunciation
    /// demand — or the activations themselves for a kind declaring no
    /// `unacknowledged` port.
    pub annunciations: u64,
    /// Episodes clearing inside the fleeting bound.
    pub fleeting_episodes: u64,
    /// The most activations the instance produced inside any
    /// chattering window.
    pub max_activations_in_chattering_window: u64,
    /// Whether the instance reaches the declared chattering bound.
    pub chattering: bool,
    /// Acknowledgments the report paired to annunciations.
    pub acknowledgments: u64,
    /// Applied `ack` receipts matching no pending annunciation.
    pub unpaired_acks: u64,
}

/// The rate section: activations and annunciations over the record
/// span, the average per flood window and per 1440-minute day, and the
/// peak activations in any single flood window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RateMetrics {
    /// Every activation in the record — suppressed alarms included.
    pub activations: u64,
    /// Every annunciation in the record.
    pub annunciations: u64,
    /// Activations divided by the record's flood-window count — the
    /// average per 10 minutes at the declared mapping.
    pub average_per_window: f64,
    /// Activations scaled to 1440-minute days — the average the
    /// conventional ~150/day target assesses.
    pub average_per_day: f64,
    /// The most activations inside any one flood window.
    pub peak_per_window: u64,
    /// The peak window's start tick — the earliest achieving the peak.
    pub peak_window_start: Option<Tick>,
}

/// One detected flood period: a maximal run of overlapping flood
/// windows — the sliding-window view of the burst.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FloodPeriod {
    /// The period's first activation's tick.
    pub start_tick: Tick,
    /// The period's last activation's tick.
    pub end_tick: Tick,
    /// Activations inside the period — every underlying transition
    /// counts, suppressed alarms included.
    pub activations: u64,
    /// Annunciations landing inside the period — what the operator
    /// faced while engineered suppression held the rest back.
    pub annunciations: u64,
    /// The period's first-out activation — the lowest-`seq` alarm
    /// assertion, the initiating cause the ordered record preserves.
    pub first_out: FirstOut,
}

/// A flood period's first-out activation: the initiating alarm, by
/// component and `seq`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FirstOut {
    /// The activating alarm's component name.
    pub component: String,
    /// The activation entry's `seq`.
    pub seq: u64,
    /// The activation's tick.
    pub tick: Tick,
}

/// The tumbling-window view of the record: the span partitioned into
/// flood windows, how many exceed the bound, and their fraction — the
/// conventional under-1% target's quantity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FloodWindows {
    /// The window length in ticks.
    pub window_ticks: u64,
    /// Windows spanning the record.
    pub windows: u64,
    /// Windows holding more than `flood_alarms` activations.
    pub over: u64,
    /// `over` divided by `windows`.
    pub fraction_over: f64,
}

/// One alarm standing at the record's end.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StandingAlarm {
    /// The instance's diagnostic name.
    pub component: String,
    /// The `alarm` point's signal name.
    pub signal: String,
    /// The tick the alarm activated at — exact from the journal, or
    /// the earliest retained equal history sample when the activation
    /// is out of the journal's range.
    pub since_tick: Tick,
    /// `since_tick` to the record's end.
    pub standing_ticks: u64,
    /// Whether the standing duration passes the declared stale bound.
    /// With `approximate` set this is proven against a lower bound —
    /// the true duration can only be longer.
    pub stale: bool,
    /// Whether `since_tick` is a history walk-back estimate — a lower
    /// bound — rather than the journal's exact activation tick.
    pub approximate: bool,
    /// The managed states the instance currently asserts, in the
    /// uniform vocabulary's order — a standing alarm's suppression
    /// state.
    pub managed: Vec<String>,
}

/// A chattering finding: an instance reaching the declared activation
/// count inside the chattering window.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatteringAlarm {
    /// The instance's diagnostic name.
    pub component: String,
    /// The `alarm` point's signal name.
    pub signal: String,
    /// The instance's total activations.
    pub activations: u64,
    /// Its most activations inside any one chattering window.
    pub max_activations_in_window: u64,
}

/// A fleeting finding: an instance with episodes clearing inside the
/// fleeting bound.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FleetingAlarm {
    /// The instance's diagnostic name.
    pub component: String,
    /// The `alarm` point's signal name.
    pub signal: String,
    /// Episodes clearing inside the bound.
    pub episodes: u64,
}

/// One row of the most-frequent ranking.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FrequentAlarm {
    /// The instance's diagnostic name.
    pub component: String,
    /// The `alarm` point's signal name.
    pub signal: String,
    /// The instance's activations.
    pub activations: u64,
}

/// One instance's accounting for one managed state: how often it
/// entered, how long it stood, and whether it stands now — the
/// shelving-duration and suppression accounting over the managed-state
/// transitions.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ManagedAccounting {
    /// The instance's diagnostic name.
    pub component: String,
    /// The managed state — `shelved`, `suppressed`, or `out_of_service`.
    pub state: String,
    /// Entries into the state.
    pub episodes: u64,
    /// Total ticks in the state, open episodes counted to the record's
    /// end.
    pub total_ticks: u64,
    /// Whether the state stands asserted at the record's end.
    pub open: bool,
}

/// One paired annunciation→acknowledgment measurement.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResponsePair {
    /// The instance's diagnostic name.
    pub component: String,
    /// The annunciation's journal `seq`.
    pub annunciation_seq: u64,
    /// The annunciation's tick.
    pub annunciation_tick: Tick,
    /// The settled `ack` receipt's journal `seq`.
    pub receipt_seq: u64,
    /// The tick the acknowledgment applied at.
    pub applied_tick: Tick,
    /// `applied_tick` minus `annunciation_tick` — the operator's
    /// response time in ticks.
    pub ticks: u64,
    /// Whether the response sits inside the instance's declared
    /// `response_ticks`; `None` when the instance declares none.
    pub within_declared: Option<bool>,
    /// The actor the receipt attributes the acknowledgment to.
    pub actor: Option<String>,
}

/// The response-time section: the annunciation→ack pairs and their
/// aggregate, the annunciations still pending an acknowledgment, and
/// applied `ack` receipts that paired nothing.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ResponseMetrics {
    /// The measured pairs, in journal `seq` order.
    pub pairs: Vec<ResponsePair>,
    /// Applied `ack` receipts matching no pending annunciation.
    pub unpaired_acks: u64,
    /// Annunciations still unacknowledged at the record's end.
    pub pending: u64,
    /// The fastest measured response.
    pub min_ticks: Option<u64>,
    /// The slowest measured response.
    pub max_ticks: Option<u64>,
    /// The mean measured response.
    pub mean_ticks: Option<f64>,
    /// Pairs exceeding their instance's declared `response_ticks`.
    pub over_declared: u64,
}

/// One row of the annunciated-priority distribution: annunciations
/// bucketed by the instance's declared `priority` code.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PriorityShare {
    /// The priority code; `None` buckets annunciations of instances
    /// declaring no `priority`.
    pub priority: Option<i64>,
    /// Annunciations carrying the code.
    pub annunciations: u64,
    /// The bucket's share of all annunciations.
    pub share: f64,
}

/// The computed report: the declared metric set over the durable
/// record, deterministic for an identical record.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AlarmReport {
    /// What the report computed over.
    pub source: ReportSource,
    /// The declared thresholds, resolved to ticks.
    pub thresholds: Thresholds,
    /// Rate and peak-rate metrics.
    pub rates: RateMetrics,
    /// The tumbling-window flood view.
    pub flood_windows: FloodWindows,
    /// The detected flood periods, earliest first.
    pub flood_periods: Vec<FloodPeriod>,
    /// Alarms standing at the record's end.
    pub standing: Vec<StandingAlarm>,
    /// Instances reaching the chattering bound.
    pub chattering: Vec<ChatteringAlarm>,
    /// Instances with fleeting episodes.
    pub fleeting: Vec<FleetingAlarm>,
    /// The activation ranking, most frequent first.
    pub most_frequent: Vec<FrequentAlarm>,
    /// Per-instance managed-state accounting.
    pub managed_states: Vec<ManagedAccounting>,
    /// Acknowledgment/response-time metrics.
    pub responses: ResponseMetrics,
    /// The annunciated-priority distribution.
    pub priority_distribution: Vec<PriorityShare>,
    /// The per-instance detail behind the sections.
    pub alarms: Vec<AlarmMetrics>,
}

/// Whether a journaled value asserts — the pane's `isAsserted` rule:
/// `Bool` true or a nonzero number. Journal `point_changed` values are
/// `Bool`/`Int` by the model's `journaled` declaration rule; `Float` is
/// covered for completeness.
fn asserted(value: &Value) -> bool {
    match value {
        Value::Bool(value) => *value,
        Value::Int(value) => *value != 0,
        Value::Float(value) => *value != 0.0,
    }
}

/// One folded lifecycle transition on a watched point: the journaled
/// `point_changed` entries reduced to the effective changes — the
/// restarted run's `from: None` re-observation of a still-standing
/// value folds away against the record's last carried value. `tick` is
/// the entry's attributed tick in its run's own domain — what the
/// report prints — and `at` its position on the record's duration
/// axis, what durations and windows compute on.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Transition {
    seq: u64,
    tick: Tick,
    /// The transition's position on the elapsed-scans axis — the
    /// duration domain the report computes in.
    at: u64,
    asserted: bool,
}

/// Which lifecycle flag a watched point carries for its instance.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Watch {
    Alarm,
    Unacknowledged,
    Shelved,
    Suppressed,
    OutOfService,
}

/// The managed-state accounting accumulator for one instance's one
/// state point.
#[derive(Debug, Default)]
struct ManagedAcc {
    episodes: u64,
    total_ticks: u64,
    /// The asserting transition an open episode started at.
    open: Option<Transition>,
}

/// One instance's accumulated run state through the journal pass.
#[derive(Debug, Default)]
struct Acc {
    activations: Vec<Transition>,
    annunciations: Vec<Transition>,
    /// Closed `alarm` episodes, `(activated, returned)` pairs.
    episodes: Vec<(Transition, Transition)>,
    /// The still-open activation, if the alarm stands.
    open: Option<Transition>,
    /// Annunciations awaiting their ack — the pairing queue.
    pending: VecDeque<Transition>,
    responses: Vec<ResponsePair>,
    unpaired_acks: u64,
    managed: [ManagedAcc; 3],
}

/// The value `point` was last known to carry: the fold's tracked value,
/// else the entry's declared `from` — the within-run previous value the
/// served journal reports even when our retained window starts
/// mid-stream.
fn effective_previous(last: Option<bool>, from: &Option<Value>) -> Option<bool> {
    last.or(from.as_ref().map(asserted))
}

/// Computes the declared alarm metric set over `journal` — the durable
/// transition record, entries taken in `seq` order — with `history`
/// supplying the retained per-point samples for alarms whose record is
/// out of journal range, `now` the serving run's current tick, and
/// `boundaries` the journal file's recorded run-boundary markers
/// (empty for a served journal).
///
/// Every reported `*_tick` field carries the tick its entry was
/// attributed to in that entry's own run domain — the audit truth.
/// Durations and windows compute on the record's duration axis: each
/// run boundary starts a fresh tick domain, so a run's entries map to
/// the elapsed-scans line the previous run's last entry ended, the
/// run's marker declaring where its ticks begin. On a single-run
/// record the axis is the tick domain itself; across a restart it is
/// the honest elapsed-scans measure — a standing alarm's duration
/// keeps growing through the restart instead of subtracting across
/// domains. `now` and `history` — both live in the serving run's
/// domain — map under the last boundary.
///
/// The computation is a pure function of its inputs: identical records
/// produce identical reports.
pub fn compute_report(
    alarms: &[AlarmInstance],
    journal: &[JournalEntry],
    boundaries: &[RunBoundary],
    history: &[PointHistory],
    now: Tick,
    config: &ReportConfig,
) -> AlarmReport {
    let thresholds = config.thresholds();

    // The watched lifecycle points and the ack points, keyed to their
    // instance's position in `alarms`.
    let mut watched: HashMap<PointId, (usize, Watch)> = HashMap::new();
    let mut acks: HashMap<PointId, usize> = HashMap::new();
    for (index, alarm) in alarms.iter().enumerate() {
        watched.insert(alarm.alarm, (index, Watch::Alarm));
        for (point, watch) in [
            (alarm.unacknowledged, Watch::Unacknowledged),
            (alarm.shelved, Watch::Shelved),
            (alarm.suppressed, Watch::Suppressed),
            (alarm.out_of_service, Watch::OutOfService),
        ] {
            if let Some(point) = point {
                watched.insert(point, (index, watch));
            }
        }
        if let Some(ack) = alarm.ack {
            acks.insert(ack, index);
        }
    }

    // One pass in durable seq order: point_changed transitions fold
    // against each point's last carried value, settled ack receipts
    // pair FIFO against pending annunciations. `axis` maps each entry
    // to the record's duration axis.
    let mut entries: Vec<&JournalEntry> = journal.iter().collect();
    entries.sort_by_key(|entry| entry.seq);
    let (axis, last_offset, last_start) = duration_axis(&entries, boundaries);
    let bounded = !boundaries.is_empty();
    let current = |tick: Tick| axis_position_at(tick, last_offset, last_start, bounded);

    // The end of the record: the latest axis position any input
    // observes.
    let mut end = current(now);
    for at in &axis {
        end = end.max(*at);
    }
    for point in history {
        for sample in &point.samples {
            end = end.max(current(sample.sample.tick));
        }
    }

    let mut accs: Vec<Acc> = alarms.iter().map(|_| Acc::default()).collect();
    let mut lasts: HashMap<PointId, bool> = HashMap::new();
    for (position, entry) in entries.iter().copied().enumerate() {
        let at = axis[position];
        match &entry.event {
            JournalEvent::PointChanged { point, from, to } => {
                let Some(&(index, watch)) = watched.get(point) else {
                    continue;
                };
                let to = asserted(to);
                let last = lasts.get(point).copied();
                let previous = effective_previous(last, from);
                lasts.insert(*point, to);
                if previous == Some(to) {
                    continue;
                }
                let transition = Transition {
                    seq: entry.seq,
                    tick: entry.tick,
                    at,
                    asserted: to,
                };
                let acc = &mut accs[index];
                match watch {
                    Watch::Alarm => {
                        if transition.asserted {
                            acc.activations.push(transition);
                            acc.open = Some(transition);
                            // A kind without `unacknowledged` annunciates
                            // every activation — queue it for ack pairing
                            // when it declares an `ack` at all.
                            if alarms[index].unacknowledged.is_none() && alarms[index].ack.is_some()
                            {
                                acc.pending.push_back(transition);
                            }
                        } else if let Some(open) = acc.open.take() {
                            acc.episodes.push((open, transition));
                        }
                    }
                    Watch::Unacknowledged => {
                        if transition.asserted {
                            acc.annunciations.push(transition);
                            acc.pending.push_back(transition);
                        } else {
                            // The latch dropped without an ack — a
                            // suppression taking hold, or the run
                            // boundary's fresh observation: the episode's
                            // annunciation can never pair now.
                            acc.pending.pop_back();
                        }
                    }
                    Watch::Shelved | Watch::Suppressed | Watch::OutOfService => {
                        let managed = match watch {
                            Watch::Shelved => &mut acc.managed[0],
                            Watch::Suppressed => &mut acc.managed[1],
                            Watch::OutOfService => &mut acc.managed[2],
                            _ => unreachable!(),
                        };
                        if transition.asserted {
                            managed.episodes += 1;
                            managed.open = Some(transition);
                        } else if let Some(open) = managed.open.take() {
                            managed.total_ticks += transition.at.saturating_sub(open.at);
                        }
                    }
                }
            }
            JournalEvent::CommandSettled { receipt } => {
                let (point, value) = match &receipt.command {
                    Command::WriteValue { point, value, .. }
                    | Command::ForcePoint { point, value, .. } => (*point, *value),
                    _ => continue,
                };
                let Some(&index) = acks.get(&point) else {
                    continue;
                };
                if !asserted(&value) {
                    continue;
                }
                let CommandOutcome::Applied { tick } = receipt.outcome else {
                    continue;
                };
                let acc = &mut accs[index];
                let Some(annunciation) = acc.pending.pop_front() else {
                    acc.unpaired_acks += 1;
                    continue;
                };
                // The applied tick lands at the scan that settled the
                // command — this entry's own attribution — so the pair's
                // response time is the entries' axis distance.
                let ticks = at.saturating_sub(annunciation.at);
                acc.responses.push(ResponsePair {
                    component: alarms[index].component.clone(),
                    annunciation_seq: annunciation.seq,
                    annunciation_tick: annunciation.tick,
                    receipt_seq: entry.seq,
                    applied_tick: tick,
                    ticks,
                    within_declared: alarms[index].response_ticks.map(|bound| ticks <= bound),
                    actor: receipt.actor.clone(),
                });
            }
            _ => {}
        }
    }

    // Kinds without `unacknowledged` annunciate every activation.
    for (index, alarm) in alarms.iter().enumerate() {
        if alarm.unacknowledged.is_none() {
            accs[index].annunciations = accs[index].activations.clone();
        }
    }

    // Per-instance metrics: episodes, fleeting, chattering, standing.
    let mut alarm_metrics = Vec::with_capacity(alarms.len());
    let mut standing = Vec::new();
    let mut chattering = Vec::new();
    let mut fleeting = Vec::new();
    let mut managed_states = Vec::new();
    for (index, alarm) in alarms.iter().enumerate() {
        let acc = &mut accs[index];
        let activation_axis: Vec<u64> = acc
            .activations
            .iter()
            .map(|transition| transition.at)
            .collect();
        let max_in_window = max_in_window(&activation_axis, thresholds.chattering_window_ticks);
        let is_chattering = max_in_window >= thresholds.chattering_activations;
        let fleeting_episodes = acc
            .episodes
            .iter()
            .filter(|(activated, returned)| {
                returned.at.saturating_sub(activated.at) < thresholds.fleeting_ticks
            })
            .count() as u64;
        alarm_metrics.push(AlarmMetrics {
            component: alarm.component.clone(),
            signal: alarm.signal.clone(),
            alarm_point: alarm.alarm,
            priority: alarm.priority,
            response_ticks: alarm.response_ticks,
            activations: acc.activations.len() as u64,
            annunciations: acc.annunciations.len() as u64,
            fleeting_episodes,
            max_activations_in_chattering_window: max_in_window,
            chattering: is_chattering,
            acknowledgments: acc.responses.len() as u64,
            unpaired_acks: acc.unpaired_acks,
        });
        if is_chattering {
            chattering.push(ChatteringAlarm {
                component: alarm.component.clone(),
                signal: alarm.signal.clone(),
                activations: acc.activations.len() as u64,
                max_activations_in_window: max_in_window,
            });
        }
        if fleeting_episodes > 0 {
            fleeting.push(FleetingAlarm {
                component: alarm.component.clone(),
                signal: alarm.signal.clone(),
                episodes: fleeting_episodes,
            });
        }

        // Standing: the journal's last carried value answers first —
        // an open episode's activation dates it exactly. Otherwise the
        // point was never journaled (or its record is out of window):
        // retained history's last sample says whether it stands, and
        // the walk-back over trailing equal samples bounds the
        // duration — a lower bound, the activation may predate the
        // ring. History lives in the serving run's domain, so its
        // estimate maps under the last boundary like `now`.
        let last = lasts.get(&alarm.alarm).copied();
        let mut standing_since: Option<(Tick, u64, bool)> = match last {
            Some(true) => acc.open.map(|open| (open.tick, open.at, false)),
            _ => None,
        };
        if standing_since.is_none() && last != Some(false) {
            standing_since =
                history_since(history, alarm.alarm).map(|since| (since, current(since), true));
        }
        if let Some((since, since_at, approximate)) = standing_since {
            let ticks = end.saturating_sub(since_at);
            standing.push(StandingAlarm {
                component: alarm.component.clone(),
                signal: alarm.signal.clone(),
                since_tick: since,
                standing_ticks: ticks,
                stale: ticks > thresholds.stale_ticks,
                approximate,
                managed: managed_now(alarm, acc),
            });
        }

        // Managed-state accounting in the uniform vocabulary's order.
        for (name, point, slot) in [
            ("shelved", alarm.shelved, 0_usize),
            ("suppressed", alarm.suppressed, 1_usize),
            ("out_of_service", alarm.out_of_service, 2_usize),
        ] {
            if point.is_none() {
                continue;
            }
            let managed = &accs[index].managed[slot];
            let mut total = managed.total_ticks;
            if let Some(open) = managed.open {
                total += end.saturating_sub(open.at);
            }
            managed_states.push(ManagedAccounting {
                component: alarm.component.clone(),
                state: name.to_string(),
                episodes: managed.episodes,
                total_ticks: total,
                open: managed.open.is_some(),
            });
        }
    }

    // The standing list keeps the journal's order of proof: earliest
    // activation first, name breaking ties.
    standing.sort_by(|a, b| {
        a.since_tick
            .cmp(&b.since_tick)
            .then_with(|| a.component.cmp(&b.component))
    });

    // Rates, flood windows, and flood periods over every activation on
    // the duration axis — `seq` order preserved for first-out.
    let mut activations: Vec<(usize, Transition)> = Vec::new();
    for (index, acc) in accs.iter().enumerate() {
        activations.extend(
            acc.activations
                .iter()
                .map(|transition| (index, *transition)),
        );
    }
    activations.sort_by(|a, b| a.1.at.cmp(&b.1.at).then_with(|| a.1.seq.cmp(&b.1.seq)));
    let axis_ticks: Vec<u64> = activations.iter().map(|(_, t)| t.at).collect();
    let annunciations: Vec<(usize, Transition)> = accs
        .iter()
        .enumerate()
        .flat_map(|(index, acc)| acc.annunciations.iter().map(move |t| (index, *t)))
        .collect();

    let first_at = axis.first().copied();
    let span_ticks = first_at.map(|first| end.saturating_sub(first)).unwrap_or(0);
    let windows_total = match first_at {
        Some(first) => end.saturating_sub(first) / thresholds.flood_window_ticks + 1,
        None => 1,
    };
    let (peak, peak_start) = peak_in_window(&activations, thresholds.flood_window_ticks);
    let rates = RateMetrics {
        activations: axis_ticks.len() as u64,
        annunciations: annunciations.len() as u64,
        average_per_window: axis_ticks.len() as f64 / windows_total as f64,
        average_per_day: if span_ticks > 0 {
            axis_ticks.len() as f64 * (1440.0 * thresholds.ticks_per_minute as f64)
                / span_ticks as f64
        } else {
            axis_ticks.len() as f64
        },
        peak_per_window: peak,
        peak_window_start: peak_start,
    };

    // Tumbling windows over the record's duration axis.
    let mut over = 0_u64;
    if let Some(first) = first_at {
        for window in 0..windows_total {
            let lo = first + window * thresholds.flood_window_ticks;
            let hi = lo + thresholds.flood_window_ticks;
            let count = axis_ticks
                .iter()
                .filter(|tick| **tick >= lo && **tick < hi)
                .count() as u64;
            if count > thresholds.flood_alarms {
                over += 1;
            }
        }
    }
    let flood_windows = FloodWindows {
        window_ticks: thresholds.flood_window_ticks,
        windows: windows_total,
        over,
        fraction_over: over as f64 / windows_total as f64,
    };

    // Sliding flood windows merged into maximal periods: a flooding
    // window whose start still falls inside the previous flooding
    // window's axis span extends the running period; a gap of quiet
    // ends it.
    let mut flood_periods = Vec::new();
    let mut period: Option<(usize, usize, u64)> = None;
    for (i, (_, transition)) in activations.iter().enumerate() {
        // The window's activations are the contiguous run from `i` —
        // same-`at` activations sit on both sides of `i`, so counting
        // over the whole axis would overrun the covered index.
        let count = axis_ticks[i..]
            .iter()
            .take_while(|tick| **tick < transition.at.saturating_add(thresholds.flood_window_ticks))
            .count();
        if count as u64 <= thresholds.flood_alarms {
            continue;
        }
        let last = i + count - 1;
        let window_end = transition.at.saturating_add(thresholds.flood_window_ticks);
        match &mut period {
            Some((_, end_i, end_at)) if transition.at <= *end_at => {
                *end_i = (*end_i).max(last);
                *end_at = (*end_at).max(window_end);
            }
            _ => {
                if let Some((first, covered, _)) = period.take() {
                    flood_periods.push((first, covered));
                }
                period = Some((i, last, window_end));
            }
        }
    }
    if let Some((first, covered, _)) = period.take() {
        flood_periods.push((first, covered));
    }
    let flood_periods: Vec<FloodPeriod> = flood_periods
        .into_iter()
        .map(|(start, last)| {
            let start_at = activations[start].1.at;
            let end_at = activations[last].1.at;
            let first_out = activations[start..=last]
                .iter()
                .min_by_key(|(_, transition)| transition.seq)
                .map(|(index, transition)| FirstOut {
                    component: alarms[*index].component.clone(),
                    seq: transition.seq,
                    tick: transition.tick,
                })
                .expect("a period covers at least one activation");
            FloodPeriod {
                start_tick: activations[start].1.tick,
                end_tick: activations[last].1.tick,
                activations: (last - start + 1) as u64,
                annunciations: annunciations
                    .iter()
                    .filter(|(_, transition)| transition.at >= start_at && transition.at <= end_at)
                    .count() as u64,
                first_out,
            }
        })
        .collect();

    // Most-frequent: every activating instance, ranked.
    let mut most_frequent: Vec<FrequentAlarm> = alarms
        .iter()
        .zip(accs.iter())
        .filter(|(_, acc)| !acc.activations.is_empty())
        .map(|(alarm, acc)| FrequentAlarm {
            component: alarm.component.clone(),
            signal: alarm.signal.clone(),
            activations: acc.activations.len() as u64,
        })
        .collect();
    most_frequent.sort_by(|a, b| {
        b.activations
            .cmp(&a.activations)
            .then_with(|| a.component.cmp(&b.component))
    });

    // Response metrics aggregate the per-instance pairs in seq order.
    let mut responses = ResponseMetrics::default();
    for acc in &accs {
        responses.pairs.extend(acc.responses.iter().cloned());
        responses.unpaired_acks += acc.unpaired_acks;
        responses.pending += acc.pending.len() as u64;
    }
    responses.pairs.sort_by_key(|pair| pair.receipt_seq);
    if !responses.pairs.is_empty() {
        let measured: Vec<u64> = responses.pairs.iter().map(|pair| pair.ticks).collect();
        responses.min_ticks = measured.iter().copied().min();
        responses.max_ticks = measured.iter().copied().max();
        responses.mean_ticks = Some(measured.iter().sum::<u64>() as f64 / measured.len() as f64);
        responses.over_declared = responses
            .pairs
            .iter()
            .filter(|pair| pair.within_declared == Some(false))
            .count() as u64;
    }

    // The annunciated-priority distribution: annunciations bucketed by
    // the instance's declared priority code, undeclared last.
    let mut buckets: BTreeMap<Option<i64>, u64> = BTreeMap::new();
    for (index, _) in &annunciations {
        *buckets.entry(alarms[*index].priority).or_default() += 1;
    }
    let mut priority_distribution: Vec<PriorityShare> = buckets
        .into_iter()
        .map(|(priority, count)| PriorityShare {
            priority,
            annunciations: count,
            share: count as f64 / annunciations.len() as f64,
        })
        .collect();
    priority_distribution.sort_by_key(|row| row.priority.unwrap_or(i64::MAX));

    AlarmReport {
        source: ReportSource {
            journal_entries: journal.len() as u64,
            journal_runs: (!boundaries.is_empty()).then_some(boundaries.len() as u64),
            first_seq: entries.first().map(|entry| entry.seq),
            last_seq: entries.last().map(|entry| entry.seq),
            first_tick: entries.first().map(|entry| entry.tick),
            last_tick: entries.last().map(|entry| entry.tick),
            end,
            span_ticks,
        },
        thresholds,
        rates,
        flood_windows,
        flood_periods,
        standing,
        chattering,
        fleeting,
        most_frequent,
        managed_states,
        responses,
        priority_distribution,
        alarms: alarm_metrics,
    }
}

/// The managed states the instance currently asserts, in the uniform
/// vocabulary's order — a standing alarm row's suppression state.
fn managed_now(alarm: &AlarmInstance, acc: &Acc) -> Vec<String> {
    let mut states = Vec::new();
    for (name, point, slot) in [
        ("shelved", alarm.shelved, 0_usize),
        ("suppressed", alarm.suppressed, 1_usize),
        ("out_of_service", alarm.out_of_service, 2_usize),
    ] {
        if point.is_some() && acc.managed[slot].open.is_some() {
            states.push(name.to_string());
        }
    }
    states
}

/// The record's duration axis: one position per `entries` element, in
/// file order — plus the offset and start tick the last-declared run
/// stands at, the mapping the serving run's live ticks (`now`, retained
/// history) join under.
///
/// Each run boundary starts a fresh tick domain; a run's entries map
/// to the elapsed-scans line the previous run's last entry ended —
/// its span is its last entry's distance from the marker's declared
/// start, plus one tick — so a duration measured across a restart
/// counts every run's observed scans and never subtracts across
/// domains. A run the file records no entries for contributes no
/// span. Without boundaries the axis is the tick domain itself.
fn duration_axis(entries: &[&JournalEntry], boundaries: &[RunBoundary]) -> (Vec<u64>, u64, Tick) {
    let mut positions = Vec::with_capacity(entries.len());
    let mut offset = 0_u64;
    let mut run = 0_usize;
    let mut run_last_tick: Option<u64> = None;
    for entry in entries {
        while run + 1 < boundaries.len() && entry.seq >= boundaries[run + 1].first_seq {
            offset += run_last_tick
                .map(|tick| tick.saturating_sub(boundaries[run].start_tick.0) + 1)
                .unwrap_or(0);
            run += 1;
            run_last_tick = None;
        }
        let start = boundaries
            .get(run)
            .map(|boundary| boundary.start_tick.0)
            .unwrap_or(0);
        positions.push(offset + entry.tick.0.saturating_sub(start));
        run_last_tick = Some(entry.tick.0);
    }
    // Boundaries trailing the last entry's run — the current run may
    // have journaled nothing yet — carry no span but place its start.
    while run + 1 < boundaries.len() {
        offset += run_last_tick
            .map(|tick| tick.saturating_sub(boundaries[run].start_tick.0) + 1)
            .unwrap_or(0);
        run += 1;
        run_last_tick = None;
    }
    let last_start = boundaries
        .last()
        .map(|boundary| boundary.start_tick)
        .unwrap_or(Tick::ZERO);
    (positions, offset, last_start)
}

/// `tick` — in the serving run's domain — mapped onto the duration
/// axis: the live inputs join under the last-declared run's boundary,
/// at the `(offset, last_start)` [`duration_axis`] leaves. Without
/// boundaries the axis is the tick domain itself.
fn axis_position_at(tick: Tick, offset: u64, last_start: Tick, bounded: bool) -> u64 {
    if bounded {
        offset + tick.0.saturating_sub(last_start.0)
    } else {
        tick.0
    }
}

/// Whether retained history says `point` stands asserted at its end,
/// and the walk-back estimate of since when: the earliest tick of the
/// trailing run of equal-valued samples — the same `lastChangedTick`
/// walk the page's alarm pane performs. The estimate is a lower bound:
/// the true activation may predate the ring. `None` when the point has
/// no retained samples or its last sample is clear.
fn history_since(history: &[PointHistory], point: PointId) -> Option<Tick> {
    let point = history.iter().find(|history| history.point == point)?;
    let last = point.samples.last()?;
    if !asserted(&last.sample.value) {
        return None;
    }
    let value = last.sample.value;
    Some(
        point
            .samples
            .iter()
            .rev()
            .take_while(|entry| entry.sample.value == value)
            .last()?
            .sample
            .tick,
    )
}

/// The most activations inside any one window of `window` ticks, over
/// `positions` on the duration axis — sorted or not; each activation's
/// own position starts a candidate window so the peak is exact.
fn max_in_window(positions: &[u64], window: u64) -> u64 {
    let mut sorted = positions.to_vec();
    sorted.sort_unstable();
    let mut max = 0;
    for (i, &start) in sorted.iter().enumerate() {
        let count = sorted[i..]
            .iter()
            .take_while(|&&tick| tick < start.saturating_add(window))
            .count() as u64;
        max = max.max(count);
    }
    max
}

/// The peak activations inside any flood window and the earliest raw
/// tick achieving it. `activations` must be sorted on the duration
/// axis already.
fn peak_in_window(activations: &[(usize, Transition)], window: u64) -> (u64, Option<Tick>) {
    let mut peak = 0;
    let mut start = None;
    for (i, (_, transition)) in activations.iter().enumerate() {
        let count = activations[i..]
            .iter()
            .take_while(|(_, other)| other.at < transition.at.saturating_add(window))
            .count() as u64;
        if count > peak {
            peak = count;
            start = Some(transition.tick);
        }
    }
    (peak, start)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{
        CommandReceipt, ComponentDescriptor, ComponentDiagnostics, ComponentParameters,
        HistorySample, IoHealth, PortDescriptor, Sample, ValueKind,
    };

    /// A `point_changed` journal entry — the durable transition record
    /// the metrics fold.
    fn changed(seq: u64, tick: u64, point: u64, from: Option<bool>, to: bool) -> JournalEntry {
        JournalEntry {
            seq,
            tick: Tick(tick),
            event: JournalEvent::PointChanged {
                point: PointId(point),
                from: from.map(Value::Bool),
                to: Value::Bool(to),
            },
        }
    }

    /// A settled `WriteValue` receipt asserting `point` — the
    /// acknowledgment record the response metric pairs.
    fn acked(seq: u64, applied: u64, point: u64) -> JournalEntry {
        JournalEntry {
            seq,
            tick: Tick(applied),
            event: JournalEvent::CommandSettled {
                receipt: CommandReceipt {
                    command: Command::WriteValue {
                        point: PointId(point),
                        kind: ValueKind::Bool,
                        value: Value::Bool(true),
                    },
                    outcome: CommandOutcome::Applied {
                        tick: Tick(applied),
                    },
                    actor: None,
                },
            },
        }
    }

    /// One managed-vocabulary instance over consecutive point ids —
    /// the six-point layout the kinds' descriptors declare:
    /// `alarm`, `unacknowledged`, `ack`, `shelved`, `suppressed`,
    /// `out_of_service` at `base`..`base + 5`.
    fn managed(name: &str, base: u64) -> AlarmInstance {
        AlarmInstance {
            component: name.to_string(),
            signal: format!("{name}-signal"),
            alarm: PointId(base),
            unacknowledged: Some(PointId(base + 1)),
            ack: Some(PointId(base + 2)),
            shelved: Some(PointId(base + 3)),
            suppressed: Some(PointId(base + 4)),
            out_of_service: Some(PointId(base + 5)),
            priority: None,
            response_ticks: None,
        }
    }

    /// One full managed lifecycle, journaled in `seq` order:
    /// activation and annunciation at `on`, the ack receipt at `ack`,
    /// the latch dropping with it, the return at `off`.
    fn lifecycle(seq: &mut u64, base: u64, on: u64, ack: u64, off: u64) -> Vec<JournalEntry> {
        let entries = vec![
            changed(*seq, on, base, Some(false), true),
            changed(*seq + 1, on, base + 1, Some(false), true),
            acked(*seq + 2, ack, base + 2),
            changed(*seq + 3, ack, base + 1, Some(true), false),
            changed(*seq + 4, off, base, Some(true), false),
        ];
        *seq += 5;
        entries
    }

    fn report(
        alarms: &[AlarmInstance],
        journal: &[JournalEntry],
        config: &ReportConfig,
        now: u64,
    ) -> AlarmReport {
        compute_report(alarms, journal, &[], &[], Tick(now), config)
    }

    #[test]
    fn the_config_resolves_the_declared_minutes_to_ticks() {
        let thresholds = ReportConfig::default().thresholds();
        assert_eq!(thresholds.flood_window_ticks, 600);
        assert_eq!(thresholds.flood_alarms, 10);
        assert_eq!(thresholds.stale_ticks, 86400);
        assert_eq!(thresholds.chattering_activations, 3);
        assert_eq!(thresholds.chattering_window_ticks, 60);
        assert_eq!(thresholds.fleeting_ticks, 60);

        // A declared override resolves through the declared mapping.
        let config = ReportConfig {
            ticks_per_minute: 10,
            flood_window_minutes: 5,
            flood_alarms: 4,
            ..ReportConfig::default()
        };
        let thresholds = config.thresholds();
        assert_eq!(thresholds.flood_window_ticks, 50);
        assert_eq!(thresholds.flood_alarms, 4);
        // The document form carries only the fields a site overrides.
        let parsed: ReportConfig =
            serde_json::from_str("{\"flood_alarms\": 7, \"ticks_per_minute\": 100}").unwrap();
        assert_eq!(parsed.flood_alarms, 7);
        assert_eq!(parsed.ticks_per_minute, 100);
        assert_eq!(parsed.flood_window_minutes, 10);
    }

    #[test]
    fn activations_and_annunciations_count_every_transition() {
        let alarm = managed("a", 100);
        let mut seq = 1;
        let journal = lifecycle(&mut seq, 100, 10, 25, 40);
        let report = report(&[alarm], &journal, &ReportConfig::default(), 100);

        assert_eq!(report.rates.activations, 1);
        assert_eq!(report.rates.annunciations, 1);
        let metrics = &report.alarms[0];
        assert_eq!(metrics.component, "a");
        assert_eq!(metrics.activations, 1);
        assert_eq!(metrics.annunciations, 1);
        assert_eq!(metrics.acknowledgments, 1);
        assert_eq!(report.responses.pairs.len(), 1);
        let pair = &report.responses.pairs[0];
        assert_eq!(pair.annunciation_tick, Tick(10));
        assert_eq!(pair.applied_tick, Tick(25));
        assert_eq!(pair.ticks, 15);
        assert_eq!(report.responses.min_ticks, Some(15));
        assert_eq!(report.responses.max_ticks, Some(15));
        assert_eq!(report.responses.mean_ticks, Some(15.0));
        assert_eq!(report.responses.pending, 0);
        assert_eq!(report.responses.unpaired_acks, 0);
        assert!(report.standing.is_empty());
    }

    #[test]
    fn rates_and_peak_report_against_the_declared_window() {
        // A three-activation burst against a 2-per-10-tick declared
        // bound: the window slides to the activations, so the peak is
        // three and the period covers the burst.
        let a = managed("a", 100);
        let b = managed("b", 200);
        let config = ReportConfig {
            ticks_per_minute: 1,
            flood_window_minutes: 10,
            flood_alarms: 2,
            ..ReportConfig::default()
        };
        let journal = vec![
            changed(1, 0, 100, Some(false), true),
            changed(2, 1, 200, Some(false), true),
            changed(3, 2, 100, Some(true), false),
            changed(4, 3, 100, Some(false), true),
        ];
        let report = report(&[a, b], &journal, &config, 100);

        assert_eq!(report.rates.activations, 3);
        assert_eq!(report.rates.peak_per_window, 3);
        assert_eq!(report.rates.peak_window_start, Some(Tick(0)));
        // The record spans 100 ticks = 10 windows.
        assert_eq!(report.flood_windows.windows, 11);
        assert_eq!(report.flood_windows.over, 1);
        assert_eq!(report.rates.average_per_window, 3.0 / 11.0);
        // The burst's merged period runs tick 0..3 holding all three.
        assert_eq!(report.flood_periods.len(), 1);
        let period = &report.flood_periods[0];
        assert_eq!(period.start_tick, Tick(0));
        assert_eq!(period.end_tick, Tick(3));
        assert_eq!(period.activations, 3);
        // First-out is seq order, not tick order: the seq-1 activation
        // initiates the period.
        assert_eq!(period.first_out.component, "a");
        assert_eq!(period.first_out.seq, 1);
    }

    #[test]
    fn separated_bursts_report_separate_flood_periods() {
        let a = managed("a", 100);
        let b = managed("b", 200);
        let config = ReportConfig {
            ticks_per_minute: 1,
            flood_window_minutes: 10,
            flood_alarms: 1,
            ..ReportConfig::default()
        };
        let journal = vec![
            changed(1, 0, 100, Some(false), true),
            changed(2, 2, 200, Some(false), true),
            changed(3, 50, 200, Some(true), false),
            changed(4, 60, 100, Some(true), false),
            changed(5, 100, 200, Some(false), true),
            changed(6, 105, 100, Some(false), true),
        ];
        let report = report(&[a, b], &journal, &config, 200);
        assert_eq!(report.flood_periods.len(), 2);
        assert_eq!(report.flood_periods[0].start_tick, Tick(0));
        assert_eq!(report.flood_periods[0].end_tick, Tick(2));
        assert_eq!(report.flood_periods[0].first_out.seq, 1);
        assert_eq!(report.flood_periods[1].start_tick, Tick(100));
        assert_eq!(report.flood_periods[1].first_out.component, "b");
        assert_eq!(report.flood_periods[1].first_out.seq, 5);
        assert_eq!(report.flood_windows.over, 2);
    }

    #[test]
    fn standing_and_stale_report_the_open_episodes() {
        let a = managed("a", 100);
        let b = managed("b", 200);
        let journal = vec![
            // `a` activates and never returns — standing.
            changed(1, 5, 100, None, true),
            // `b` activates and returns — closed, never standing.
            changed(2, 10, 200, None, true),
            changed(3, 20, 200, Some(true), false),
        ];
        // `a` stands 95 ticks — fresh under the 100-tick bound.
        let config = ReportConfig {
            ticks_per_minute: 1,
            stale_minutes: 100,
            ..ReportConfig::default()
        };
        let fresh = report(&[a.clone(), b.clone()], &journal, &config, 100);
        assert_eq!(fresh.standing.len(), 1);
        assert_eq!(fresh.standing[0].component, "a");
        assert_eq!(fresh.standing[0].since_tick, Tick(5));
        assert_eq!(fresh.standing[0].standing_ticks, 95);
        assert!(!fresh.standing[0].stale);
        assert!(!fresh.standing[0].approximate);

        // The same alarm at 200 ticks is stale.
        let stale = report(&[a, b], &journal, &config, 200);
        assert_eq!(stale.standing[0].standing_ticks, 195);
        assert!(stale.standing[0].stale);
    }

    #[test]
    fn history_dates_a_standing_alarm_the_journal_does_not_hold() {
        let a = managed("a", 100);
        // No journal entry touches the point — its activation predates
        // the retained record. History's trailing asserted samples date
        // it approximately.
        let history = vec![PointHistory {
            point: PointId(100),
            samples: vec![
                HistorySample {
                    seq: 1,
                    sample: Sample::good(Value::Bool(false), Tick(10)),
                },
                HistorySample {
                    seq: 2,
                    sample: Sample::good(Value::Bool(true), Tick(50)),
                },
                HistorySample {
                    seq: 3,
                    sample: Sample::good(Value::Bool(true), Tick(60)),
                },
            ],
        }];
        let report = compute_report(
            &[a],
            &[],
            &[],
            &history,
            Tick(100),
            &ReportConfig::default(),
        );
        assert_eq!(report.standing.len(), 1);
        let standing = &report.standing[0];
        assert_eq!(standing.since_tick, Tick(50));
        assert_eq!(standing.standing_ticks, 50);
        assert!(standing.approximate);
        // A history ending clear reports nothing standing.
        let mut cleared = history.clone();
        cleared[0].samples.push(HistorySample {
            seq: 4,
            sample: Sample::good(Value::Bool(false), Tick(70)),
        });
        let report = compute_report(
            &[managed("a", 100)],
            &[],
            &[],
            &cleared,
            Tick(100),
            &ReportConfig::default(),
        );
        assert!(report.standing.is_empty());
    }

    #[test]
    fn a_restart_reobservation_is_not_a_second_activation() {
        let a = managed("a", 100);
        // Run 1 activates at tick 10; run 2's first observation of the
        // still-standing alarm arrives `from: None` at the fresh run's
        // own tick 0 — the fold keeps it one episode, and the duration
        // counts elapsed scans across the boundary, never subtracting
        // between the two runs' tick domains.
        let boundaries = [
            RunBoundary {
                run: 1,
                start_tick: Tick(0),
                first_seq: 1,
            },
            RunBoundary {
                run: 2,
                start_tick: Tick(0),
                first_seq: 2,
            },
        ];
        let journal = vec![
            changed(1, 10, 100, None, true),
            changed(2, 0, 100, None, true),
        ];
        let report = compute_report(
            &[a],
            &journal,
            &boundaries,
            &[],
            Tick(20),
            &ReportConfig::default(),
        );
        assert_eq!(report.rates.activations, 1);
        assert_eq!(report.standing.len(), 1);
        assert_eq!(report.standing[0].since_tick, Tick(10));
        // Run 1's observed span is 11 scans (0..=10); run 2's tick 20
        // lands at axis 31 — the alarm stands 21 elapsed scans.
        assert_eq!(report.standing[0].standing_ticks, 21);
        assert_eq!(report.source.journal_runs, Some(2));
    }

    #[test]
    fn chattering_and_fleeting_report_the_declared_bounds() {
        let a = managed("a", 100);
        let b = managed("b", 200);
        let config = ReportConfig::default();
        let journal = vec![
            // `a` chatters: three activations inside one minute.
            changed(1, 10, 100, Some(false), true),
            changed(2, 20, 100, Some(true), false),
            changed(3, 30, 100, Some(false), true),
            changed(4, 40, 100, Some(true), false),
            changed(5, 50, 100, Some(false), true),
            changed(6, 400, 100, Some(true), false),
            // `b` activates once and holds — neither metric fires.
            changed(7, 10, 200, Some(false), true),
            changed(8, 300, 200, Some(true), false),
        ];
        let report = report(&[a, b], &journal, &config, 500);

        assert_eq!(report.chattering.len(), 1);
        assert_eq!(report.chattering[0].component, "a");
        assert_eq!(report.chattering[0].activations, 3);
        assert_eq!(report.chattering[0].max_activations_in_window, 3);
        assert!(!report.alarms[1].chattering);

        // `a`'s first two episodes clear inside the minute; its third
        // (30→400) does not. `b` is not fleeting.
        assert_eq!(report.fleeting.len(), 1);
        assert_eq!(report.fleeting[0].component, "a");
        assert_eq!(report.fleeting[0].episodes, 2);
        assert_eq!(report.alarms[0].fleeting_episodes, 2);
        assert_eq!(report.alarms[1].fleeting_episodes, 0);
    }

    #[test]
    fn most_frequent_ranks_activations_then_name() {
        let a = managed("a", 100);
        let b = managed("b", 200);
        let c = managed("c", 300);
        let journal = vec![
            changed(1, 0, 300, Some(false), true),
            changed(2, 0, 200, Some(false), true),
            changed(3, 5, 300, Some(true), false),
            changed(4, 5, 100, Some(false), true),
            changed(5, 10, 300, Some(false), true),
            changed(6, 15, 100, Some(true), false),
            changed(7, 20, 100, Some(false), true),
        ];
        let report = report(&[a, b, c], &journal, &ReportConfig::default(), 30);
        assert_eq!(
            report
                .most_frequent
                .iter()
                .map(|row| (row.component.as_str(), row.activations))
                .collect::<Vec<_>>(),
            vec![("a", 2), ("c", 2), ("b", 1)]
        );
    }

    #[test]
    fn managed_accounting_counts_episodes_durations_and_open_states() {
        let a = managed("a", 100);
        let journal = vec![
            changed(1, 10, 100, Some(false), true),
            // Shelved for 100 ticks — one closed episode.
            changed(2, 100, 103, Some(false), true),
            changed(3, 200, 103, Some(true), false),
            // Suppressed at 300 and still standing at the end.
            changed(4, 300, 104, Some(false), true),
            // Out-of-service twice for 10 ticks each.
            changed(5, 50, 105, Some(false), true),
            changed(6, 60, 105, Some(true), false),
            changed(7, 400, 105, Some(false), true),
            changed(8, 410, 105, Some(true), false),
        ];
        let report = report(&[a], &journal, &ReportConfig::default(), 500);
        assert_eq!(
            report
                .managed_states
                .iter()
                .map(|row| (row.state.as_str(), row.episodes, row.total_ticks, row.open))
                .collect::<Vec<_>>(),
            vec![
                ("shelved", 1, 100, false),
                ("suppressed", 1, 200, true),
                ("out_of_service", 2, 20, false),
            ]
        );
        // The standing alarm's row carries its asserted states.
        let standing = &report.standing[0];
        assert_eq!(standing.managed, vec!["suppressed".to_string()]);
    }

    #[test]
    fn response_pairs_follow_annunciations_and_check_the_declared_bound() {
        let mut a = managed("a", 100);
        a.response_ticks = Some(30);
        let journal = vec![
            // First episode: annunciation at 10, acked at 20 — inside 30.
            changed(1, 10, 100, Some(false), true),
            changed(2, 10, 101, Some(false), true),
            acked(3, 20, 102),
            changed(4, 20, 101, Some(true), false),
            changed(5, 30, 100, Some(true), false),
            // Second episode: annunciation at 40, acked at 80 — over 30.
            changed(6, 40, 100, Some(false), true),
            changed(7, 40, 101, Some(false), true),
            acked(8, 80, 102),
            changed(9, 80, 101, Some(true), false),
            changed(10, 90, 100, Some(true), false),
            // An ack with nothing pending counts unpaired.
            acked(11, 95, 102),
        ];
        let report = report(&[a], &journal, &ReportConfig::default(), 100);
        let responses = &report.responses;
        assert_eq!(responses.pairs.len(), 2);
        assert_eq!(responses.pairs[0].ticks, 10);
        assert_eq!(responses.pairs[0].within_declared, Some(true));
        assert_eq!(responses.pairs[1].ticks, 40);
        assert_eq!(responses.pairs[1].within_declared, Some(false));
        assert_eq!(responses.over_declared, 1);
        assert_eq!(responses.unpaired_acks, 1);
        assert_eq!(responses.pending, 0);
        assert_eq!(responses.min_ticks, Some(10));
        assert_eq!(responses.max_ticks, Some(40));
        assert_eq!(responses.mean_ticks, Some(25.0));
    }

    #[test]
    fn a_suppressed_annunciation_never_pairs() {
        let a = managed("a", 100);
        let journal = vec![
            changed(1, 10, 100, Some(false), true),
            changed(2, 10, 101, Some(false), true),
            // Suppression takes the latch down without an ack — the
            // annunciation can never pair, and the pending queue drops it.
            changed(3, 20, 104, Some(false), true),
            changed(4, 20, 101, Some(true), false),
        ];
        let report = report(&[a], &journal, &ReportConfig::default(), 100);
        assert_eq!(report.responses.pairs.len(), 0);
        assert_eq!(report.responses.pending, 0);
        assert_eq!(report.responses.unpaired_acks, 0);
    }

    #[test]
    fn the_priority_distribution_buckets_annunciations() {
        let mut a = managed("a", 100);
        a.priority = Some(1);
        let mut b = managed("b", 200);
        b.priority = Some(3);
        let c = managed("c", 300);
        let journal = vec![
            changed(1, 0, 100, Some(false), true),
            changed(2, 0, 101, Some(false), true),
            changed(3, 5, 200, Some(false), true),
            changed(4, 5, 201, Some(false), true),
            changed(5, 10, 300, Some(false), true),
            changed(6, 10, 301, Some(false), true),
            changed(7, 20, 100, Some(true), false),
            changed(8, 20, 101, Some(true), false),
            changed(9, 30, 100, Some(false), true),
            changed(10, 30, 101, Some(false), true),
        ];
        let report = report(&[a, b, c], &journal, &ReportConfig::default(), 40);
        assert_eq!(
            report
                .priority_distribution
                .iter()
                .map(|row| (row.priority, row.annunciations))
                .collect::<Vec<_>>(),
            vec![(Some(1), 2), (Some(3), 1), (None, 1)]
        );
        assert_eq!(report.priority_distribution[0].share, 0.5);
    }

    #[test]
    fn a_burst_keeps_every_transition_countable_in_seq_order() {
        // An equipment-trip burst: five alarms assert at one tick —
        // every underlying transition counts, and seq order keeps the
        // trip's initiating alarm first.
        let alarms: Vec<AlarmInstance> = (0..5)
            .map(|i| managed(&format!("burst-{i}"), 100 + i * 10))
            .collect();
        let journal: Vec<JournalEntry> = (0..5)
            .map(|i| changed(i + 1, 60, 100 + i * 10, Some(false), true))
            .collect();
        let report = report(&alarms, &journal, &ReportConfig::default(), 120);
        assert_eq!(report.rates.activations, 5);
        assert_eq!(
            report
                .alarms
                .iter()
                .map(|metrics| metrics.activations)
                .collect::<Vec<_>>(),
            vec![1, 1, 1, 1, 1]
        );
        assert_eq!(report.flood_periods.len(), 0);
        // All five stand, ordered by their activation.
        assert_eq!(report.standing.len(), 5);
        assert_eq!(report.standing[0].component, "burst-0");
    }

    #[test]
    fn a_suppressed_alarm_still_counts_its_activation() {
        // The consequential-burst rule: suppression withholds the
        // annunciation, never the durable `alarm` transition — a
        // suppressed burst member still counts.
        let a = managed("a", 100);
        let b = managed("b", 200);
        let journal = vec![
            // `a` annunciates normally.
            changed(1, 10, 100, Some(false), true),
            changed(2, 10, 101, Some(false), true),
            // `b`'s suppression pre-empts: suppressed asserts and the
            // unacknowledged demand never appears — the activation
            // still journals and counts.
            changed(3, 12, 204, Some(false), true),
            changed(4, 12, 200, Some(false), true),
        ];
        let report = report(&[a, b], &journal, &ReportConfig::default(), 100);
        assert_eq!(report.rates.activations, 2);
        assert_eq!(report.rates.annunciations, 1);
        assert_eq!(report.alarms[1].activations, 1);
        assert_eq!(report.alarms[1].annunciations, 0);
        assert_eq!(report.standing.len(), 2);
        assert_eq!(report.standing[1].managed, vec!["suppressed".to_string()]);
    }

    #[test]
    fn a_kind_without_unacknowledged_annunciates_every_activation() {
        let mut a = managed("a", 100);
        a.unacknowledged = None;
        let journal = vec![
            changed(1, 10, 100, Some(false), true),
            changed(2, 20, 100, Some(true), false),
            changed(3, 30, 100, Some(false), true),
            // The single ack pairs the oldest pending activation.
            acked(4, 45, 102),
        ];
        let report = report(&[a], &journal, &ReportConfig::default(), 50);
        assert_eq!(report.rates.activations, 2);
        assert_eq!(report.rates.annunciations, 2);
        assert_eq!(report.responses.pairs.len(), 1);
        assert_eq!(report.responses.pairs[0].annunciation_tick, Tick(10));
        assert_eq!(report.responses.pending, 1);
    }

    #[test]
    fn identical_records_produce_identical_reports() {
        let a = managed("a", 100);
        let mut seq = 1;
        let journal = lifecycle(&mut seq, 100, 10, 25, 40);
        let config = ReportConfig::default();
        let first = report(std::slice::from_ref(&a), &journal, &config, 100);
        let second = report(&[a], &journal, &config, 100);
        assert_eq!(first, second);
        assert_eq!(
            serde_json::to_string(&first).unwrap(),
            serde_json::to_string(&second).unwrap()
        );
    }

    #[test]
    fn the_instance_join_reads_the_served_descriptor_surface() {
        // The same join the alarm pane performs: the status-role `Out`
        // ports name the lifecycle points by the uniform vocabulary,
        // the `ack` input binds the ack point, and the parameters
        // section carries `priority` and `response_ticks`.
        let descriptor = ComponentDescriptor {
            name: "managed-latching-alarm:1".to_string(),
            kind: "managed-latching-alarm".to_string(),
            label: "wet well high".to_string(),
            ports: vec![
                PortDescriptor {
                    name: "in".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Float,
                    role: Some(PortRole::ProcessValue),
                    point: Some(PointId(10)),
                },
                PortDescriptor {
                    name: "ack".to_string(),
                    direction: Direction::In,
                    kind: ValueKind::Bool,
                    role: None,
                    point: Some(PointId(11)),
                },
                PortDescriptor {
                    name: "alarm".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: Some(PointId(20)),
                },
                PortDescriptor {
                    name: "unacknowledged".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: Some(PointId(21)),
                },
                PortDescriptor {
                    name: "shelved".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: Some(PointId(22)),
                },
                PortDescriptor {
                    name: "out_of_service".to_string(),
                    direction: Direction::Out,
                    kind: ValueKind::Bool,
                    role: Some(PortRole::Status),
                    point: Some(PointId(24)),
                },
            ],
            parameters: Vec::new(),
        };
        let snapshot = TelemetrySnapshot {
            tick: Tick(5),
            points: Vec::new(),
            components: vec![ComponentDiagnostics {
                name: "managed-latching-alarm:1".to_string(),
                last_tick: Some(Tick(5)),
                step_errors: 0,
                last_error: None,
            }],
            descriptors: vec![descriptor],
            io_health: IoHealth::default(),
            forces: Vec::new(),
            parameters: vec![ComponentParameters {
                name: "managed-latching-alarm:1".to_string(),
                values: [
                    ("priority".to_string(), Value::Int(2)),
                    ("response_ticks".to_string(), Value::Int(30)),
                ]
                .into_iter()
                .collect(),
            }],
        };
        let model =
            dcs_model::PlantModel::load(include_str!("../fixtures/managed_alarms.json")).unwrap();
        let instances = alarm_instances(&snapshot, &model.signal_index());
        assert_eq!(instances.len(), 1);
        let instance = &instances[0];
        assert_eq!(instance.component, "managed-latching-alarm:1");
        assert_eq!(instance.alarm, PointId(20));
        assert_eq!(instance.unacknowledged, Some(PointId(21)));
        assert_eq!(instance.ack, Some(PointId(11)));
        assert_eq!(instance.shelved, Some(PointId(22)));
        assert_eq!(instance.suppressed, None);
        assert_eq!(instance.out_of_service, Some(PointId(24)));
        assert_eq!(instance.priority, Some(2));
        assert_eq!(instance.response_ticks, Some(30));
        // A component without a status `alarm` port is not an instance.
        assert!(
            alarm_instances(
                &TelemetrySnapshot {
                    tick: Tick(0),
                    points: Vec::new(),
                    components: Vec::new(),
                    descriptors: vec![ComponentDescriptor {
                        name: "gain:1".to_string(),
                        kind: "gain".to_string(),
                        label: "gain".to_string(),
                        ports: Vec::new(),
                        parameters: Vec::new(),
                    }],
                    io_health: IoHealth::default(),
                    forces: Vec::new(),
                    parameters: Vec::new(),
                },
                &model.signal_index(),
            )
            .is_empty()
        );
    }
}
