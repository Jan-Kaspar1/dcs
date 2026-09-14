//! The transition journal: a bounded, tick-stamped log of run events.
//!
//! Where [`PointHistory`](crate::PointHistory) records each point's sample
//! stream, the journal records the run's transitions — command outcomes,
//! point quality changes, and component step failures — as one ordered
//! event list an operator can audit. Every [`JournalEntry`] is stamped
//! with the [`Tick`] the event is attributed to, keeping journal, history,
//! snapshots, and receipts in one comparable timebase.
//!
//! Entries carry a stream [`seq`](JournalEntry::seq) assigned in append
//! order and never reused, so bounded eviction is visible to consumers as
//! a numbering gap rather than silent loss.

use crate::command::CommandReceipt;
use crate::signal::{PointId, Quality, Tick};
use serde::{Deserialize, Serialize};

/// One kind of event the journal records.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JournalEvent {
    /// A point's observed quality changed. `from` is `None` when the
    /// point's first observed sample already carried the `to` quality —
    /// the point's quality record coming into existence.
    QualityChanged {
        /// The point whose quality changed.
        point: PointId,
        /// The previously observed quality, or `None` on the point's
        /// first observed sample.
        from: Option<Quality>,
        /// The newly observed quality.
        to: Quality,
    },
    /// A submitted [`Command`](crate::Command) reached its final outcome:
    /// [`applied`](crate::CommandOutcome::Applied) at a scan boundary, or
    /// [`rejected`](crate::CommandOutcome::Rejected) with the receipt's
    /// named reason — at submission for a statically invalid command, or
    /// at the scan boundary when the driver refused the write.
    CommandSettled {
        /// The command's final receipt.
        receipt: CommandReceipt,
    },
    /// A component's `step` returned an error during the scan; the scan
    /// continued per the executor's contract.
    StepFailed {
        /// The component's name.
        component: String,
        /// The error the step reported.
        error: String,
    },
}

/// One journaled event: its stream position, the tick it is attributed
/// to, and what happened.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JournalEntry {
    /// The entry's position in the journal stream: assigned in append
    /// order starting at 1 and increasing by one per entry. Numbers are
    /// never reused, so bounded eviction is visible as a gap.
    pub seq: u64,
    /// The run tick the event is attributed to: the producing scan's tick
    /// for events a scan generated, or the run's current tick for a
    /// command refused at submission between scans.
    pub tick: Tick,
    /// What happened.
    pub event: JournalEvent,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::command::{Command, CommandOutcome};
    use crate::signal::{QualityReason, Value, ValueKind};

    #[test]
    fn journal_serde_roundtrip() {
        let entries = vec![
            JournalEntry {
                seq: 1,
                tick: Tick(1),
                event: JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: None,
                    to: Quality::Good,
                },
            },
            JournalEntry {
                seq: 2,
                tick: Tick(4),
                event: JournalEvent::QualityChanged {
                    point: PointId(10),
                    from: Some(Quality::Good),
                    to: Quality::Bad(QualityReason::CommunicationFault),
                },
            },
            JournalEntry {
                seq: 3,
                tick: Tick(4),
                event: JournalEvent::CommandSettled {
                    receipt: CommandReceipt {
                        command: Command::WriteValue {
                            point: PointId(20),
                            kind: ValueKind::Float,
                            value: Value::Float(2.5),
                        },
                        outcome: CommandOutcome::Applied { tick: Tick(4) },
                    },
                },
            },
            JournalEntry {
                seq: 4,
                tick: Tick(5),
                event: JournalEvent::StepFailed {
                    component: "pid".to_string(),
                    error: "computation failed".to_string(),
                },
            },
        ];
        let json = serde_json::to_string(&entries).unwrap();
        assert_eq!(
            serde_json::from_str::<Vec<JournalEntry>>(&json).unwrap(),
            entries
        );
        // Variant names serialize snake_case like the command contract.
        assert!(json.contains("\"quality_changed\""), "{json}");
        assert!(json.contains("\"command_settled\""), "{json}");
        assert!(json.contains("\"step_failed\""), "{json}");
    }
}
