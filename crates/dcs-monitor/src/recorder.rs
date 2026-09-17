//! The bounded history rings and transition journal recorded per scan.
//!
//! [`Recorder`] observes the executor once per completed scan — the
//! documented recording point is after the scan's write phase, never
//! mid-scan — and appends to the bounded streams the monitor's
//! publication [`Store`](crate::store::Store) owns and serves:
//!
//! - a per-point ring of [`HistorySample`]s carrying each point's fresh
//!   image sample, and
//! - the transition journal of [`JournalEntry`]s, appended in the scan's
//!   own phase order: command receipts the scan boundary settled (commands
//!   apply at the scan head), then quality transitions in ascending point
//!   order (the input read and step phases produced them), then value
//!   transitions over the declared-`journaled` points in the same
//!   ascending point order — the durable transition record the
//!   lifecycle-audit decision adds — then the kind-declared events the
//!   step phase emitted, in emission order — then component step
//!   failures in scan order.
//!
//! A scan aborted by a [`ScanError`](dcs_runtime::ScanError) is not
//! recorded: the run ends at it. Both streams evict oldest-first past the
//! configured capacity and number entries with never-reused `seq`s, so
//! consumers detect eviction as a numbering gap. `record_scan` also
//! returns the materialized snapshot — the monitor publishes it into the
//! store as the completed scan's immutable read model rather than
//! rebuilding it per request.

use crate::journal_file::JournalFile;
use crate::store::Store;
use dcs_core::{
    CarryoverReport, CommandOutcome, CommandReceipt, Divergence, EventRetention, JournalEntry,
    JournalEvent, PointId, Quality, Role, TelemetrySnapshot, Tick, Value,
};
use dcs_runtime::{Executor, SourceRestart};
use std::collections::HashMap;
use std::io;
use std::path::PathBuf;

/// Retention bounds for a [`Monitor`](crate::Monitor)'s recorded and
/// published streams, plus the journal's optional durable sink.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MonitorConfig {
    /// Samples retained per point in the history rings; `0` retains none.
    pub history_capacity: usize,
    /// Entries retained in the transition journal; `0` retains none.
    pub journal_capacity: usize,
    /// Publications retained in the read-model window a seq-cursor
    /// consumer pages through; `0` retains only the latest. Bounded
    /// regardless of consumer count — a window that fills evicts
    /// oldest-first and counts the evictions into the `coalesced`
    /// overload counter the served snapshot's `publication` section
    /// reports.
    pub publication_capacity: usize,
    /// When set, every journaled entry is also appended to this
    /// line-delimited JSON file — the journal-persistence decision's
    /// monitor-local sink. Startup replays the file into the in-memory
    /// ring and continues `seq` numbering where it left off, so the
    /// served journal answers continuously across a restart; a file
    /// that cannot be replayed fails the bind naming the file and the
    /// offending record, and a missing file is a cold start. Point
    /// history stays volatile — only the journal persists.
    pub journal_file: Option<PathBuf>,
}

impl Default for MonitorConfig {
    /// Roomy enough for a UI polling seconds apart against scan periods
    /// of tens of milliseconds; still a hard bound.
    fn default() -> Self {
        Self {
            history_capacity: 1024,
            journal_capacity: 1024,
            publication_capacity: 16,
            journal_file: None,
        }
    }
}

/// Records bounded per-point history and the transition journal, one scan
/// at a time. See the module docs for the recording point and ordering.
pub(super) struct Recorder {
    /// The publication store the recorded streams live in — shared with
    /// the monitor, which publishes each completed scan's read model
    /// there and serves every read from it.
    store: Store,
    /// The `seq` the next journaled entry takes.
    next_seq: u64,
    /// The last quality observed per point — what transitions diff
    /// against; absent until the point's first observed sample.
    qualities: HashMap<PointId, Quality>,
    /// The last value observed per declared-`journaled` point — what the
    /// durable value transitions diff against; absent until the point's
    /// first observed sample.
    values: HashMap<PointId, Value>,
    /// The outcome last observed for each receipt in the executor's
    /// log — what the journal diffs against. A locally submitted
    /// command marks its entry at `note_command`; a checkpoint-adopted
    /// log's receipts first appear at the adopting scan's record, so a
    /// command that crossed peers inside the checkpoint journals its
    /// settlement on the observing peer as well — the pair's one
    /// command audit trail.
    receipt_outcomes: Vec<Option<CommandOutcome>>,
    /// Per-component `step_errors` counts at the last record, in scan
    /// order — what step-failure entries diff against.
    step_counts: Vec<u64>,
    /// The durable journal sink, when a path is configured — every
    /// journaled entry is appended there too.
    sink: Option<JournalFile>,
}

impl Recorder {
    /// `tick` is the tick this run starts at — `0` cold, the restored
    /// tick under `--state-file` — recorded in the journal file's
    /// run-boundary marker. Replaying a configured file seeds the
    /// journal ring and continues `seq` numbering; a file that cannot
    /// be replayed fails here naming the file and the offending record.
    pub(super) fn new(config: MonitorConfig, tick: Tick) -> io::Result<Self> {
        let (sink, replay) = match &config.journal_file {
            Some(path) => {
                let (sink, replay) = JournalFile::open(path, config.journal_capacity, tick)?;
                (Some(sink), replay)
            }
            None => (None, crate::journal_file::Replay::default()),
        };
        let store = Store::new(
            config.history_capacity,
            config.journal_capacity,
            config.publication_capacity,
        );
        for entry in replay.entries {
            store.push_journal(entry);
        }
        Ok(Self {
            store,
            next_seq: replay.next_seq,
            qualities: HashMap::new(),
            values: HashMap::new(),
            receipt_outcomes: Vec::new(),
            step_counts: Vec::new(),
            sink,
        })
    }

    /// The publication store the recorded streams live in — the monitor
    /// shares it, publishing each completed scan's read model there and
    /// serving every read from it.
    pub(super) fn store(&self) -> Store {
        self.store.clone()
    }

    /// Notes the receipt a `submit_command` just produced, at
    /// `receipt_index` in the executor's log.
    ///
    /// A command refused at submission is already final and is journaled
    /// at the run's current tick; an accepted one is marked observed and
    /// the next record journals the outcome its boundary settled.
    pub(super) fn note_command(
        &mut self,
        receipt_index: usize,
        receipt: CommandReceipt,
        tick: Tick,
    ) {
        match receipt.outcome {
            CommandOutcome::Accepted { .. } => {}
            CommandOutcome::Applied { .. } | CommandOutcome::Rejected { .. } => {
                self.push(
                    tick,
                    JournalEvent::CommandSettled {
                        receipt: receipt.clone(),
                    },
                );
            }
        }
        self.observe(receipt_index, receipt.outcome);
    }

    /// Journals a receipt that never entered the executor's log — a
    /// command refused before it could queue, e.g. at the role boundary.
    pub(super) fn note_settled(&mut self, receipt: CommandReceipt, tick: Tick) {
        self.push(tick, JournalEvent::CommandSettled { receipt });
    }

    /// Marks `outcome` as the last observed at `index` in the receipt
    /// log, extending the observed vector on first sight of an index —
    /// a checkpoint-adopted log's receipts surface here before ever
    /// passing `note_command`.
    fn observe(&mut self, index: usize, outcome: CommandOutcome) {
        if self.receipt_outcomes.len() <= index {
            self.receipt_outcomes.resize(index + 1, None);
        }
        self.receipt_outcomes[index] = Some(outcome);
    }

    /// Journals a reported-role transition at `tick` — a promotion or
    /// demotion applied at its boundary, or a transition settling on the
    /// first scan under the new mode.
    pub(super) fn note_role_change(&mut self, tick: Tick, from: Role, to: Role) {
        self.push(tick, JournalEvent::RoleChanged { from, to });
    }

    /// Journals a standby-divergence transition at `tick` — the tick the
    /// compared staged image belonged to — with the mismatched field
    /// `Out` points and both sides' values.
    pub(super) fn note_divergence(&mut self, tick: Tick, mismatches: Vec<Divergence>) {
        self.push(tick, JournalEvent::DivergenceDetected { mismatches });
    }

    /// Journals a model-boundary crossing — a revision-armed peer's
    /// transition into `reinitialized` — carrying its
    /// [`CarryoverReport`]; the entry is attributed to the tick the run
    /// resumed at.
    pub(super) fn note_reinitialized(&mut self, report: CarryoverReport) {
        self.push(report.resumed_at, JournalEvent::Reinitialized { report });
    }

    /// Journals a field-claim loss — the shared field fenced a write of
    /// this instance's, meaning another attachment preempted the
    /// single-writer claim — attributed to the tick the fenced scan was
    /// observed at.
    pub(super) fn note_field_claim_lost(&mut self, tick: Tick, point: PointId) {
        self.push(tick, JournalEvent::FieldClaimLost { point });
    }

    /// Journals a tracked-source restart — the checkpoint stream
    /// regressed, the signature of a cold-restarted or replaced source
    /// — attributed to the run tick the resync landed at, the apply
    /// having adopted the regressed state without rewinding the run's
    /// clock.
    pub(super) fn note_source_restart(&mut self, restart: SourceRestart) {
        self.push(
            restart.tick,
            JournalEvent::SourceRestarted {
                was_aligned: restart.was_aligned,
                resumed_at: restart.resumed_at,
            },
        );
    }

    /// Records one completed scan attributed to `scan_tick`; see the
    /// module docs for the event ordering. Returns the materialized
    /// snapshot — built exactly once here — for the monitor to publish
    /// as the scan's read model.
    pub(super) fn record_scan(
        &mut self,
        executor: &Executor<'_>,
        scan_tick: Tick,
    ) -> TelemetrySnapshot {
        // Commands settle at the scan head, before the input read. A
        // receipt journals on the outcome transition this record
        // observes — whether the command was submitted here or arrived
        // adopted inside a checkpoint, so the run's command audit reads
        // the same on either peer. An applied receipt reports the tick
        // it applied at; a boundary rejection is attributed to this
        // scan.
        for index in 0..executor.receipts().len() {
            let receipt = &executor.receipts()[index];
            let observed = self
                .receipt_outcomes
                .get(index)
                .and_then(|outcome| outcome.as_ref());
            if observed == Some(&receipt.outcome) {
                continue;
            }
            match receipt.outcome {
                CommandOutcome::Accepted { .. } => {}
                CommandOutcome::Applied { tick } => self.push(
                    tick,
                    JournalEvent::CommandSettled {
                        receipt: receipt.clone(),
                    },
                ),
                CommandOutcome::Rejected { .. } => self.push(
                    scan_tick,
                    JournalEvent::CommandSettled {
                        receipt: receipt.clone(),
                    },
                ),
            }
            self.observe(index, receipt.outcome.clone());
        }

        let snapshot = executor.snapshot();
        for telemetry in &snapshot.points {
            let Some(sample) = telemetry.sample else {
                continue;
            };
            self.store.push_sample(telemetry.point, sample);
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

        // A declared-`journaled` point's value transition journals at the
        // producing scan's tick, in the same ascending point order — the
        // durable transition record for the status, lifecycle, mode, and
        // protection points the flag marks. Undeclared points journal no
        // value entries: the record stays low-volume. The first observed
        // sample of a journaled point is itself the record's `from:
        // None` convention, matching `QualityChanged`.
        let point_map = executor.point_map();
        for telemetry in &snapshot.points {
            let Some(sample) = telemetry.sample else {
                continue;
            };
            let journaled = point_map
                .get(telemetry.point)
                .is_some_and(|spec| spec.journaled);
            if !journaled {
                continue;
            }
            let from = self.values.insert(telemetry.point, sample.value);
            if from != Some(sample.value) {
                self.push(
                    scan_tick,
                    JournalEvent::PointChanged {
                        point: telemetry.point,
                        from,
                        to: sample.value,
                    },
                );
            }
        }

        // The kind-declared events the scan's components emitted — the
        // executor drained each after its `step` — journal here in
        // emission order at the producing scan's tick. Retention is the
        // serving-side read of the declared `EventRetention`: `Journal`
        // events — and an emission the descriptor never declares, which
        // the audit record still carries — land as `event_emitted`;
        // `History`/`Latest` emissions follow their declared channel and
        // are not duplicated into the durable journal.
        for event in executor.emitted_events() {
            let retention = snapshot
                .descriptors
                .iter()
                .find(|descriptor| descriptor.name == event.component)
                .and_then(|descriptor| {
                    descriptor
                        .events
                        .iter()
                        .find(|decl| decl.name == event.event)
                })
                .map(|decl| decl.retention);
            if matches!(retention, None | Some(EventRetention::Journal)) {
                self.push(
                    scan_tick,
                    JournalEvent::EventEmitted {
                        event: event.clone(),
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
        snapshot
    }

    /// Retained journal entries with a `seq` above `since`, oldest first
    /// — the store's served ring. Test-only: the served `GET /journal`
    /// answer reads the store directly.
    #[cfg(test)]
    pub(super) fn journal(&self, since: u64) -> Vec<JournalEntry> {
        self.store.journal(since)
    }

    /// Appends one journal entry — to the configured file sink first,
    /// then the store's served ring and pending publication delta. An
    /// append the file cannot take is fatal: the run dies naming the
    /// file rather than running on while its audit trail silently
    /// stops, and the partial record a crash can leave is what the next
    /// startup's replay rejects by name.
    pub(super) fn push(&mut self, tick: Tick, event: JournalEvent) {
        let entry = JournalEntry {
            seq: self.next_seq,
            tick,
            event,
        };
        if let Some(sink) = &mut self.sink {
            sink.append(&entry)
                .unwrap_or_else(|error| panic!("{error}"));
        }
        self.next_seq += 1;
        self.store.push_journal(entry);
    }
}
