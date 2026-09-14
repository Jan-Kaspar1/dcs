//! The bounded history rings and transition journal recorded per scan.
//!
//! [`Recorder`] observes the executor once per completed scan — the
//! documented recording point is after the scan's write phase, never
//! mid-scan — and appends to two bounded streams:
//!
//! - a per-point ring of [`HistorySample`]s carrying each point's fresh
//!   image sample, and
//! - the transition journal of [`JournalEntry`]s, appended in the scan's
//!   own phase order: command receipts the scan boundary settled (commands
//!   apply at the scan head), then quality transitions in ascending point
//!   order (the input read and step phases produced them), then component
//!   step failures in scan order.
//!
//! A scan aborted by a [`ScanError`](dcs_runtime::ScanError) is not
//! recorded: the run ends at it. Both streams evict oldest-first past the
//! configured capacity and number entries with never-reused `seq`s, so
//! consumers detect eviction as a numbering gap.

use dcs_core::{
    CommandOutcome, CommandReceipt, HistorySample, JournalEntry, JournalEvent, PointHistory,
    PointId, Quality, Role, Sample, Tick,
};
use dcs_runtime::Executor;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

/// Retention bounds for a [`Monitor`](crate::Monitor)'s recorded streams.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MonitorConfig {
    /// Samples retained per point in the history rings; `0` retains none.
    pub history_capacity: usize,
    /// Entries retained in the transition journal; `0` retains none.
    pub journal_capacity: usize,
}

impl Default for MonitorConfig {
    /// Roomy enough for a UI polling seconds apart against scan periods
    /// of tens of milliseconds; still a hard bound.
    fn default() -> Self {
        Self {
            history_capacity: 1024,
            journal_capacity: 1024,
        }
    }
}

/// One point's ring of recent samples.
struct Ring {
    /// The `seq` the next appended sample takes.
    next_seq: u64,
    samples: VecDeque<HistorySample>,
}

impl Ring {
    fn new() -> Self {
        Self {
            next_seq: 1,
            samples: VecDeque::new(),
        }
    }

    fn push(&mut self, sample: Sample, capacity: usize) {
        self.samples.push_back(HistorySample {
            seq: self.next_seq,
            sample,
        });
        self.next_seq += 1;
        while self.samples.len() > capacity {
            self.samples.pop_front();
        }
    }
}

/// Records bounded per-point history and the transition journal, one scan
/// at a time. See the module docs for the recording point and ordering.
pub(super) struct Recorder {
    config: MonitorConfig,
    /// Rings keyed by point id; the `BTreeMap` keeps `/history` output in
    /// ascending point order.
    rings: BTreeMap<PointId, Ring>,
    /// Retained journal entries, oldest first.
    journal: VecDeque<JournalEntry>,
    /// The `seq` the next journaled entry takes.
    next_seq: u64,
    /// The last quality observed per point — what transitions diff
    /// against; absent until the point's first observed sample.
    qualities: HashMap<PointId, Quality>,
    /// Receipt indices of commands accepted but not yet settled.
    open_commands: BTreeSet<usize>,
    /// Per-component `step_errors` counts at the last record, in scan
    /// order — what step-failure entries diff against.
    step_counts: Vec<u64>,
}

impl Recorder {
    pub(super) fn new(config: MonitorConfig) -> Self {
        Self {
            config,
            rings: BTreeMap::new(),
            journal: VecDeque::new(),
            next_seq: 1,
            qualities: HashMap::new(),
            open_commands: BTreeSet::new(),
            step_counts: Vec::new(),
        }
    }

    /// Notes the receipt a `submit_command` just produced, at
    /// `receipt_index` in the executor's log.
    ///
    /// A command refused at submission is already final and is journaled
    /// at the run's current tick; an accepted one is tracked until a scan
    /// boundary settles it.
    pub(super) fn note_command(
        &mut self,
        receipt_index: usize,
        receipt: CommandReceipt,
        tick: Tick,
    ) {
        match receipt.outcome {
            CommandOutcome::Accepted { .. } => {
                self.open_commands.insert(receipt_index);
            }
            CommandOutcome::Applied { .. } | CommandOutcome::Rejected { .. } => {
                self.push(tick, JournalEvent::CommandSettled { receipt });
            }
        }
    }

    /// Journals a receipt that never entered the executor's log — a
    /// command refused before it could queue, e.g. at the role boundary.
    pub(super) fn note_settled(&mut self, receipt: CommandReceipt, tick: Tick) {
        self.push(tick, JournalEvent::CommandSettled { receipt });
    }

    /// Journals a reported-role transition at `tick` — a promotion or
    /// demotion applied at its boundary, or a transition settling on the
    /// first scan under the new mode.
    pub(super) fn note_role_change(&mut self, tick: Tick, from: Role, to: Role) {
        self.push(tick, JournalEvent::RoleChanged { from, to });
    }

    /// Records one completed scan attributed to `scan_tick`; see the
    /// module docs for the event ordering.
    pub(super) fn record_scan(&mut self, executor: &Executor<'_>, scan_tick: Tick) {
        // Commands settle at the scan head, before the input read.
        let settled: Vec<(usize, Tick)> = self
            .open_commands
            .iter()
            .filter_map(|&index| {
                match executor.receipts()[index].outcome {
                    CommandOutcome::Accepted { .. } => None,
                    // An applied command reports the tick it applied at; a
                    // boundary rejection is attributed to this scan.
                    CommandOutcome::Applied { tick } => Some((index, tick)),
                    CommandOutcome::Rejected { .. } => Some((index, scan_tick)),
                }
            })
            .collect();
        for (index, event_tick) in settled {
            self.open_commands.remove(&index);
            self.push(
                event_tick,
                JournalEvent::CommandSettled {
                    receipt: executor.receipts()[index].clone(),
                },
            );
        }

        let snapshot = executor.snapshot();
        for telemetry in &snapshot.points {
            let Some(sample) = telemetry.sample else {
                continue;
            };
            self.rings
                .entry(telemetry.point)
                .or_insert_with(Ring::new)
                .push(sample, self.config.history_capacity);
            let from = self.qualities.insert(telemetry.point, sample.quality);
            if from != Some(sample.quality) {
                self.push(
                    scan_tick,
                    JournalEvent::QualityChanged {
                        point: telemetry.point,
                        from,
                        to: sample.quality,
                    },
                );
            }
        }

        // A component's step error count can grow by at most one per scan.
        let failures: Vec<JournalEvent> = snapshot
            .components
            .iter()
            .enumerate()
            .filter_map(|(index, diagnostics)| {
                let seen = self.step_counts.get(index).copied().unwrap_or(0);
                (diagnostics.step_errors > seen).then(|| JournalEvent::StepFailed {
                    component: diagnostics.name.clone(),
                    error: diagnostics.last_error.clone().unwrap_or_default(),
                })
            })
            .collect();
        self.step_counts = snapshot
            .components
            .iter()
            .map(|diagnostics| diagnostics.step_errors)
            .collect();
        for event in failures {
            self.push(scan_tick, event);
        }
    }

    /// The retained history of `points` — every mapped point when empty —
    /// keeping only samples with a `seq` above `since`. Points are
    /// returned in ascending id order regardless of request order, so
    /// equal runs answer identically.
    pub(super) fn history(
        &self,
        executor: &Executor<'_>,
        points: &[PointId],
        since: u64,
    ) -> Vec<PointHistory> {
        let selected: BTreeSet<PointId> = if points.is_empty() {
            executor
                .snapshot()
                .points
                .iter()
                .map(|telemetry| telemetry.point)
                .collect()
        } else {
            points.iter().copied().collect()
        };
        selected
            .into_iter()
            .map(|point| PointHistory {
                point,
                samples: self
                    .rings
                    .get(&point)
                    .map(|ring| {
                        ring.samples
                            .iter()
                            .filter(|sample| sample.seq > since)
                            .copied()
                            .collect()
                    })
                    .unwrap_or_default(),
            })
            .collect()
    }

    /// Retained journal entries with a `seq` above `since`, oldest first.
    pub(super) fn journal(&self, since: u64) -> Vec<JournalEntry> {
        self.journal
            .iter()
            .filter(|entry| entry.seq > since)
            .cloned()
            .collect()
    }

    /// Appends one journal entry, evicting the oldest past capacity.
    fn push(&mut self, tick: Tick, event: JournalEvent) {
        self.journal.push_back(JournalEntry {
            seq: self.next_seq,
            tick,
            event,
        });
        self.next_seq += 1;
        while self.journal.len() > self.config.journal_capacity {
            self.journal.pop_front();
        }
    }
}
