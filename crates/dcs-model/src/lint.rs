//! Advisory engineering-quality lint for validated
//! [`PlantModel`](crate::PlantModel) documents.
//!
//! [`PlantModel::validate`](crate::PlantModel::validate) checks correctness
//! — unique ids, resolved references, agreeing directions and value types —
//! while [`PlantModel::lint`] checks the completeness of the engineering
//! metadata the monitoring surface consumes. Findings are advisory: they
//! name declarations that are valid but probably unfinished, and a linted
//! document still assembles and runs. A document that fails validation is
//! reported with its validation errors and is never linted.
//!
//! The rule set, in the order [`PlantModel::lint`] reports it:
//!
//! - [`LintRule::PointWithoutSignal`] — an [`IoPoint`](crate::IoPoint) no
//!   [`Signal`](crate::Signal) sources. The point still reaches the
//!   monitoring page, but under its `point-<id>` fallback label.
//! - [`LintRule::SignalMissingUnit`],
//!   [`LintRule::SignalMissingDescription`], and
//!   [`LintRule::SignalMissingGroup`] — a [`Signal`](crate::Signal) leaving
//!   unset the metadata the point list and group rendering display.
//! - [`LintRule::WritableFieldPoint`] — a channel-bound
//!   [`IoPoint`](crate::IoPoint) marked `writable`: part of the operator
//!   surface, listed for review. Writable *internal* points are the
//!   ordinary setpoint mechanism and are not flagged.
//! - [`LintRule::UnboundChannel`] — a device [`Channel`](crate::Channel) no
//!   io_point binds: a dead field declaration.
//!
//! Each rule pass walks its collection in declaration order — device
//! channels in their map's name order — so the report is deterministic and
//! follows the document.

use crate::model::{IoPoint, PlantModel, Signal};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::fmt;

/// The advisory rule that produced a [`LintFinding`]; see the
/// [module documentation](self) for what each rule means.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LintRule {
    /// An `io_point` no `signal` sources.
    PointWithoutSignal,
    /// A `signal` declaring no `unit`.
    SignalMissingUnit,
    /// A `signal` declaring no `description`.
    SignalMissingDescription,
    /// A `signal` declaring no `group`.
    SignalMissingGroup,
    /// A channel-bound `io_point` marked `writable` — the operator surface.
    WritableFieldPoint,
    /// A device channel no `io_point` binds.
    UnboundChannel,
}

impl fmt::Display for LintRule {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            LintRule::PointWithoutSignal => "point_without_signal",
            LintRule::SignalMissingUnit => "signal_missing_unit",
            LintRule::SignalMissingDescription => "signal_missing_description",
            LintRule::SignalMissingGroup => "signal_missing_group",
            LintRule::WritableFieldPoint => "writable_field_point",
            LintRule::UnboundChannel => "unbound_channel",
        })
    }
}

/// One advisory lint finding, naming the element it applies to.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LintFinding {
    /// The rule that produced the finding.
    pub rule: LintRule,
    /// The element the finding names: `io_point 11`,
    /// `signal 101 "reactor-level-switch"`, or `device 1 channel "ch2"`.
    pub element: String,
    /// What the rule found, e.g. `declares no unit`.
    pub message: String,
}

impl fmt::Display for LintFinding {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}: {}", self.rule, self.element, self.message)
    }
}

impl PlantModel {
    /// Reports the model's advisory engineering-quality findings; see the
    /// [module documentation](self) for the rule set.
    ///
    /// The model is expected to be validated first — [`PlantModel::load`]
    /// guarantees it — so lint findings never repeat what validation
    /// already reports. The result is deterministic: rules are checked in
    /// the documented order, each walking its collection in declaration
    /// order (device channels in name order).
    pub fn lint(&self) -> Vec<LintFinding> {
        let mut findings = Vec::new();

        // Points the monitoring page renders under their fallback
        // `point-<id>` label because no signal sources them.
        let sourced: BTreeSet<u64> = self.signals.iter().map(|signal| signal.source.0).collect();
        for point in &self.io_points {
            if !sourced.contains(&point.id.0) {
                findings.push(LintFinding {
                    rule: LintRule::PointWithoutSignal,
                    element: describe_point(point),
                    message: "no signal sources this point; it renders unlabeled on the \
                              monitoring page"
                        .to_owned(),
                });
            }
        }

        // Signal metadata the point list and group rendering display.
        for signal in &self.signals {
            let element = describe_signal(signal);
            for (rule, field) in [
                (LintRule::SignalMissingUnit, "unit"),
                (LintRule::SignalMissingDescription, "description"),
                (LintRule::SignalMissingGroup, "group"),
            ] {
                let missing = match rule {
                    LintRule::SignalMissingUnit => signal.unit.is_none(),
                    LintRule::SignalMissingDescription => signal.description.is_none(),
                    LintRule::SignalMissingGroup => signal.group.is_none(),
                    _ => unreachable!("the pass lists only signal-metadata rules"),
                };
                if missing {
                    findings.push(LintFinding {
                        rule,
                        element: element.clone(),
                        message: format!("declares no {field}"),
                    });
                }
            }
        }

        // The operator surface: a writable channel-bound point accepts
        // command writes forwarded to the driver — listed for review.
        // Writable internal points are the ordinary setpoint mechanism.
        for point in &self.io_points {
            let Some(channel) = &point.channel else {
                continue;
            };
            if point.writable {
                findings.push(LintFinding {
                    rule: LintRule::WritableFieldPoint,
                    element: describe_point(point),
                    message: format!(
                        "writable field point bound to channel {:?} on device {} \
                         (operator surface)",
                        channel.name, channel.device.0
                    ),
                });
            }
        }

        // Dead field declarations: channels no io_point binds.
        let bound: BTreeSet<(u64, &str)> = self
            .io_points
            .iter()
            .filter_map(|point| point.channel.as_ref())
            .map(|channel| (channel.device.0, channel.name.as_str()))
            .collect();
        for device in &self.devices {
            for name in device.channels.keys() {
                if !bound.contains(&(device.id.0, name.as_str())) {
                    findings.push(LintFinding {
                        rule: LintRule::UnboundChannel,
                        element: format!("device {} channel {name:?}", device.id.0),
                        message: "is bound by no io_point".to_owned(),
                    });
                }
            }
        }

        findings
    }
}

fn describe_point(point: &IoPoint) -> String {
    format!("io_point {}", point.id.0)
}

fn describe_signal(signal: &Signal) -> String {
    format!("signal {} {:?}", signal.id.0, signal.name)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FINDINGS: &str = include_str!("../fixtures/lint/findings.json");
    const CLEAN: &str = include_str!("../fixtures/lint/clean.json");

    #[test]
    fn findings_fixture_reports_every_rule_in_model_order() {
        let model = PlantModel::load(FINDINGS).unwrap();
        let findings = model.lint();
        let rules: Vec<LintRule> = findings.iter().map(|finding| finding.rule).collect();
        assert_eq!(
            rules,
            vec![
                LintRule::PointWithoutSignal,
                LintRule::SignalMissingUnit,
                LintRule::SignalMissingGroup,
                LintRule::SignalMissingDescription,
                LintRule::WritableFieldPoint,
                LintRule::UnboundChannel,
                LintRule::UnboundChannel,
            ],
            "{findings:?}"
        );
        assert_eq!(findings[0].element, "io_point 11");
        assert_eq!(findings[1].element, "signal 101 \"reactor-level-switch\"");
        assert_eq!(findings[5].element, "device 1 channel \"ch7\"");
        assert_eq!(findings[6].element, "device 2 channel \"ch3\"");
    }

    #[test]
    fn clean_fixture_reports_no_findings() {
        assert!(PlantModel::load(CLEAN).unwrap().lint().is_empty());
    }

    #[test]
    fn a_writable_internal_point_is_not_flagged() {
        // Internal writable points are the ordinary setpoint mechanism;
        // only channel-bound writable points are operator-surface review.
        let model = PlantModel::load(FINDINGS).unwrap();
        assert!(
            model
                .lint()
                .iter()
                .all(|finding| !(finding.rule == LintRule::WritableFieldPoint
                    && finding.element == "io_point 13")),
            "{:?}",
            model.lint()
        );
    }

    #[test]
    fn lint_is_deterministic() {
        let model = PlantModel::load(FINDINGS).unwrap();
        assert_eq!(model.lint(), model.lint());
    }
}
