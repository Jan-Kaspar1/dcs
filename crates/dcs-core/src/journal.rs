//! The transition journal: a bounded, tick-stamped log of run events.
//!
//! Where [`PointHistory`](crate::PointHistory) records each point's sample
//! stream, the journal records the run's transitions — command outcomes,
//! point quality changes, the value transitions of declared-`journaled`
//! points, component step failures, and redundancy role
//! changes — as one ordered event list an operator can audit. Every [`JournalEntry`] is stamped
//! with the [`Tick`] the event is attributed to, keeping journal, history,
//! snapshots, and receipts in one comparable timebase.
//!
//! Entries carry a stream [`seq`](JournalEntry::seq) assigned in append
//! order and never reused, so bounded eviction is visible to consumers as
//! a numbering gap rather than silent loss.

use crate::carryover::CarryoverReport;
use crate::command::CommandReceipt;
use crate::interface::EventRetention;
use crate::role::{Divergence, Role};
use crate::signal::{PointId, Quality, Tick, Value};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// One typed value in an [`EmittedEvent`]'s payload — the value half
/// of a declared [`EventField`](crate::EventField): the variant
/// matches the field's declared
/// [`EventFieldKind`](crate::EventFieldKind).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventValue {
    /// A [`Value`] — the payload of a field declared
    /// `EventFieldKind::Value`.
    Value(Value),
    /// A [`Quality`] flag — `EventFieldKind::Quality`.
    Quality(Quality),
    /// A [`CommandReceipt`] — `EventFieldKind::Receipt`.
    Receipt(Box<CommandReceipt>),
    /// Free text — `EventFieldKind::Text`.
    Text(String),
}

/// A kind-declared event a component emitted during the producing scan
/// — the record [`JournalEvent::EventEmitted`] carries.
///
/// `event` is the stable event-kind identity: the declaring
/// [`EventSpec`](crate::EventSpec)'s `name`. `component` is the
/// producing instance's name — the identity its descriptor and
/// diagnostics report. `fields` is the typed payload keyed by the
/// `EventSpec`'s declared [`EventField`](crate::EventField) names; a
/// field the schema marks `optional` is absent when it carried no
/// value, matching the contract's omit-when-none convention.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EmittedEvent {
    /// The event's stable kind identity — the declaring `EventSpec`'s
    /// `name`.
    pub event: String,
    /// The producing component instance's name.
    pub component: String,
    /// The typed payload, keyed by declared field name.
    pub fields: BTreeMap<String, EventValue>,
}

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
    /// A declared-`journaled` point's observed value changed — the
    /// generic durable transition record the lifecycle-audit decision
    /// adds beside the settled receipts, so alarm and managed-state
    /// status transitions (`alarm`, `unacknowledged`, `shelved`,
    /// `suppressed`, `out_of_service`), mode changes, and
    /// protection-layer states are preserved across restart without
    /// dedicated alarm-lifecycle event variants. `from` is `None` when
    /// the point's first observed sample already carried the `to`
    /// value — the `QualityChanged` convention.
    PointChanged {
        /// The point whose value changed.
        point: PointId,
        /// The previously observed value, or `None` on the point's
        /// first observed sample.
        from: Option<Value>,
        /// The newly observed value.
        to: Value,
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
    /// The instance's reported [`Role`] changed — a promotion or
    /// demotion applied at its scan boundary (`standby` → `promoting`,
    /// `active` → `demoting`), or a transition settled on the first scan
    /// under the new mode (`promoting` → `active`, `demoting` →
    /// `standby`). The pair-as-one-controller audit trail.
    RoleChanged {
        /// The previously reported role.
        from: Role,
        /// The newly reported role.
        to: Role,
    },
    /// A tracking peer's staged field outputs were found to mismatch the
    /// field's actual values at the tick this entry is attributed to —
    /// the transition into [`StandbySync::Diverged`](crate::StandbySync),
    /// which blocks promotion until a fresh checkpoint resynchronizes
    /// the peer. `mismatches` carries the diverging points with the
    /// staged and field values side by side.
    DivergenceDetected {
        /// The mismatched field `Out` points.
        mismatches: Vec<Divergence>,
    },
    /// A revision-armed peer consumed a checkpoint captured under a
    /// different model — the transition into
    /// [`StandbySync::Reinitialized`](crate::StandbySync) of the rolling
    /// model-revision decision. `report` carries the carryover audit
    /// record: the fingerprints on each side of the boundary, the
    /// resumed tick this entry is attributed to, and every element
    /// classified — carried, initialized, or named as dropped.
    Reinitialized {
        /// The crossing's carryover record.
        report: CarryoverReport,
    },
    /// A component emitted a kind-declared event — the durable record
    /// of a [`Declared`](crate::AdaptedEvent::Declared)-provenance
    /// [`EventSpec`](crate::EventSpec) whose retention is
    /// [`Journal`](crate::EventRetention::Journal) — and of an
    /// emission the descriptor never declares, which the audit record
    /// still carries. The entry's `tick` is the producing scan's
    /// tick; `event` carries the stable event-kind identity, the
    /// producing component, and the typed payload over the spec's
    /// declared [`EventField`](crate::EventField) schema. Emissions
    /// declared `History`/`Latest` route to the read model's bounded
    /// event-history ring and latest-emission view instead — served
    /// beside this journal in the resource view's `events`
    /// collection, never journaled twice.
    EventEmitted {
        /// The emitted event record.
        event: EmittedEvent,
    },
}

/// One routed emission record — the element the read model's bounded
/// event-history ring and latest-emission view serve for emissions
/// declared [`History`](crate::EventRetention::History) or
/// [`Latest`](crate::EventRetention::Latest).
///
/// Where [`JournalEntry`] is the durable record's element, the
/// `EventRecord` is the diagnostic stream's: `seq` numbers the
/// routed-event stream in append order and is never reused, so the
/// ring's bounded eviction is visible to consumers as a numbering
/// gap — the same honest-gap convention the journal and the
/// per-point history rings follow. `tick` is the producing scan's
/// tick; `retention` names the declared class the emission routed
/// under; `event` carries the [`EmittedEvent`] itself.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct EventRecord {
    /// The record's position in the routed-event stream — assigned in
    /// append order starting at 1 and increasing by one per routed
    /// emission, never reused.
    pub seq: u64,
    /// The producing scan's tick.
    pub tick: Tick,
    /// The declared retention class the emission routed under —
    /// `History` for the bounded ring's records, `Latest` for the
    /// latest-emission view's.
    pub retention: EventRetention,
    /// The emitted event: stable event-kind identity, producing
    /// component, and typed payload.
    pub event: EmittedEvent,
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
    use crate::carryover::{CarriedPoint, DroppedElement};
    use crate::command::{Command, CommandOutcome};
    use crate::fingerprint::ModelFingerprint;
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
                event: JournalEvent::PointChanged {
                    point: PointId(21),
                    from: None,
                    to: Value::Bool(false),
                },
            },
            JournalEntry {
                seq: 4,
                tick: Tick(4),
                event: JournalEvent::PointChanged {
                    point: PointId(21),
                    from: Some(Value::Bool(false)),
                    to: Value::Bool(true),
                },
            },
            JournalEntry {
                seq: 5,
                tick: Tick(4),
                event: JournalEvent::CommandSettled {
                    receipt: CommandReceipt {
                        command: Command::WriteValue {
                            point: PointId(20),
                            kind: ValueKind::Float,
                            value: Value::Float(2.5),
                        },
                        outcome: CommandOutcome::Applied { tick: Tick(4) },
                        actor: None,
                    },
                },
            },
            JournalEntry {
                seq: 6,
                tick: Tick(5),
                event: JournalEvent::StepFailed {
                    component: "pid".to_string(),
                    error: "computation failed".to_string(),
                },
            },
            JournalEntry {
                seq: 7,
                tick: Tick(8),
                event: JournalEvent::RoleChanged {
                    from: Role::Standby,
                    to: Role::Promoting,
                },
            },
            JournalEntry {
                seq: 8,
                tick: Tick(9),
                event: JournalEvent::DivergenceDetected {
                    mismatches: vec![Divergence {
                        point: PointId(20),
                        staged: Value::Float(4.5),
                        field: Value::Float(6.0),
                    }],
                },
            },
            JournalEntry {
                seq: 9,
                tick: Tick(12),
                event: JournalEvent::Reinitialized {
                    report: CarryoverReport {
                        from: Some(ModelFingerprint(0x0123_4567_89ab_cdef)),
                        to: Some(ModelFingerprint(0xfedc_ba98_7654_3210)),
                        resumed_at: Tick(12),
                        carried: vec![CarriedPoint {
                            point: PointId(11),
                            value: Value::Float(33.5),
                        }],
                        carried_outputs: vec![CarriedPoint {
                            point: PointId(20),
                            value: Value::Float(4.5),
                        }],
                        carried_forces: vec![],
                        dropped: vec![DroppedElement::InternalPoint { point: PointId(31) }],
                        reinitialized: vec!["level_ctrl".to_string()],
                        initialized: vec![PointId(32)],
                    },
                },
            },
            JournalEntry {
                seq: 10,
                tick: Tick(14),
                event: JournalEvent::EventEmitted {
                    event: EmittedEvent {
                        event: "stroke_complete".to_string(),
                        component: "vlv:1".to_string(),
                        fields: [
                            ("ticks".to_string(), EventValue::Value(Value::Int(30))),
                            ("detail".to_string(), EventValue::Text("seated".to_string())),
                        ]
                        .into_iter()
                        .collect(),
                    },
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
        assert!(json.contains("\"point_changed\""), "{json}");
        assert!(json.contains("\"command_settled\""), "{json}");
        assert!(json.contains("\"step_failed\""), "{json}");
        assert!(json.contains("\"role_changed\""), "{json}");
        assert!(json.contains("\"divergence_detected\""), "{json}");
        assert!(json.contains("\"reinitialized\""), "{json}");
        assert!(json.contains("\"event_emitted\""), "{json}");
    }

    #[test]
    fn event_emitted_uses_the_documented_wire_shape() {
        // The emitted-event journal entry: the stable event-kind
        // identity, the producing component, and the typed payload
        // keyed by declared field name — each field value tagged by
        // its `EventFieldKind` counterpart. The entry's `tick` is the
        // producing scan's tick.
        let receipt = CommandReceipt {
            command: Command::Invoke {
                component: "vlv:1".to_string(),
                command: "stroke_test".to_string(),
                arguments: BTreeMap::new(),
            },
            outcome: CommandOutcome::Applied { tick: Tick(14) },
            actor: Some("operator-3".to_string()),
        };
        let entry = JournalEntry {
            seq: 10,
            tick: Tick(14),
            event: JournalEvent::EventEmitted {
                event: EmittedEvent {
                    event: "stroke_complete".to_string(),
                    component: "vlv:1".to_string(),
                    fields: [
                        ("ticks".to_string(), EventValue::Value(Value::Int(30))),
                        ("quality".to_string(), EventValue::Quality(Quality::Good)),
                        (
                            "receipt".to_string(),
                            EventValue::Receipt(Box::new(receipt)),
                        ),
                        ("detail".to_string(), EventValue::Text("seated".to_string())),
                    ]
                    .into_iter()
                    .collect(),
                },
            },
        };
        let json = serde_json::to_string(&entry).unwrap();
        assert_eq!(
            json,
            r#"{"seq":10,"tick":14,"event":{"event_emitted":{"event":{"event":"stroke_complete","component":"vlv:1","fields":{"detail":{"text":"seated"},"quality":{"quality":"good"},"receipt":{"receipt":{"command":{"invoke":{"component":"vlv:1","command":"stroke_test","arguments":{}}},"outcome":{"applied":{"tick":14}},"actor":"operator-3"}},"ticks":{"value":{"int":30}}}}}}}"#
        );
        assert_eq!(serde_json::from_str::<JournalEntry>(&json).unwrap(), entry);
    }

    #[test]
    fn journal_entries_predating_event_emitted_deserialize() {
        // A journal file recorded before the variant exists carries
        // none of it; each entry deserializes unchanged.
        let entries: Vec<JournalEntry> = serde_json::from_str(
            r#"[{"seq":1,"tick":4,"event":{"step_failed":{"component":"pid","error":"computation failed"}}}]"#,
        )
        .unwrap();
        assert_eq!(
            entries,
            [JournalEntry {
                seq: 1,
                tick: Tick(4),
                event: JournalEvent::StepFailed {
                    component: "pid".to_string(),
                    error: "computation failed".to_string(),
                },
            }]
        );
    }
}
