//! The redundancy role contract: which instance of a controller pair owns
//! field writes, and how far a standby has converged.
//!
//! Per the switchover-semantics and monitoring-under-redundancy
//! decisions, an active/standby pair is one logical controller to the
//! monitoring UI: each instance reports a [`Role`] — `active`,
//! `standby`, or one of the transition states — plus the standby's
//! checkpoint convergence, together as the serde [`RoleReport`] the
//! monitoring transport serves and promotion requests answer. Refusals of
//! promotion, demotion, and checkpoint application are the named
//! [`SwitchError`]s.
//!
//! The transition states are observable by construction: a promotion or
//! demotion request moves the field-write gate at its scan boundary —
//! between scans, never mid-scan — and the instance reports `promoting`
//! or `demoting` until the first scan under the new mode completes, when
//! the reported role settles to `active` or `standby`.

use crate::signal::Tick;
use serde::{Deserialize, Serialize};
use std::fmt;

/// The role a controller instance reports in a redundant pair.
///
/// `active`, `standby`, and the transition states `promoting` and
/// `demoting`, per the switchover-semantics decision. A UI treats the
/// pair as one logical controller by displaying and commanding the peer
/// reporting [`Active`](Role::Active); every other role is pair health,
/// not a second controller.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    /// Owns field writes.
    Active,
    /// Field writes quiesced behind the write gate; the instance scans
    /// and tracks the active's checkpoints.
    Standby,
    /// A promotion applied at its scan boundary — the write gate is
    /// lifted and scans already write the field — reported until the
    /// first scan under the lifted gate completes, then settling to
    /// [`Active`](Role::Active).
    Promoting,
    /// A demotion applied at its scan boundary — the write gate is
    /// closed and scans no longer reach the field — reported until the
    /// next scan completes, then settling to
    /// [`Standby`](Role::Standby).
    Demoting,
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Active => "active",
            Self::Standby => "standby",
            Self::Promoting => "promoting",
            Self::Demoting => "demoting",
        })
    }
}

/// How far a tracking peer has converged to the active's run — the
/// standby half of a [`RoleReport`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StandbySync {
    /// Assembled and scanning, but no checkpoint has landed yet: the run
    /// reflects only this instance's initial state.
    Unsynchronized,
    /// The last checkpoint applied cleanly: the run is aligned with the
    /// active's at the checkpointed tick and tracking.
    Tracking {
        /// The last applied checkpoint's tick — how far the run is known
        /// to be aligned.
        aligned: Tick,
    },
    /// The last transfer failed — an unreachable active or a rejected
    /// checkpoint — so the run continues on its last-known state.
    /// Recoverable: the next successful apply realigns it.
    Degraded {
        /// What the failed transfer reported, for diagnostics.
        detail: String,
    },
}

impl fmt::Display for StandbySync {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unsynchronized => f.write_str("unsynchronized"),
            Self::Tracking { aligned } => write!(f, "tracking (aligned at tick {})", aligned.0),
            Self::Degraded { detail } => write!(f, "degraded: {detail}"),
        }
    }
}

/// The instance's reported role: the `GET /role` payload and the answer
/// body of `POST /promote` and `POST /demote`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RoleReport {
    /// The reported role.
    pub role: Role,
    /// The instance's current run tick — the same virtual-tick domain
    /// snapshots, history, and journal use, so a consumer can order a
    /// switchover against the run. Tick continuity across a promotion is
    /// the divergence check: the promoted peer continues the sequence.
    pub tick: Tick,
    /// Standby convergence — `Some` whenever the instance is or is
    /// becoming a tracking peer (`standby`, `promoting`, `demoting`);
    /// `None` for a settled `active`.
    pub sync: Option<StandbySync>,
}

/// Why a switchover request was refused — the named errors of the
/// promotion contract, carried on the wire so a monitoring consumer can
/// tell "try again after convergence" from "wrong peer".
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SwitchError {
    /// Promotion was requested on an instance already owning the field —
    /// `active`, or `promoting` (a repeated promotion).
    AlreadyActive,
    /// Promotion was requested before the standby converged: its last
    /// checkpoint has not applied cleanly, so taking over would diverge
    /// the run. Carries the convergence the request found.
    NotConverged {
        /// The standby's reported convergence at refusal.
        sync: StandbySync,
    },
    /// Demotion was requested on an instance that does not own the field
    /// — `standby` or `demoting`.
    NotActive,
    /// A checkpoint was offered to a field-owning instance — `active` or
    /// `promoting`; only a tracking peer applies checkpoints.
    OwnsField,
}

impl fmt::Display for SwitchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyActive => {
                f.write_str("instance already owns field writes (role active or promoting)")
            }
            Self::NotConverged { sync } => {
                write!(f, "standby has not converged: {sync}")
            }
            Self::NotActive => {
                f.write_str("instance does not own field writes (role standby or demoting)")
            }
            Self::OwnsField => {
                f.write_str("instance owns field writes; checkpoints apply to a tracking peer")
            }
        }
    }
}

impl std::error::Error for SwitchError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn role_contract_serde_roundtrip() {
        let reports = [
            RoleReport {
                role: Role::Active,
                tick: Tick(12),
                sync: None,
            },
            RoleReport {
                role: Role::Standby,
                tick: Tick(9),
                sync: Some(StandbySync::Unsynchronized),
            },
            RoleReport {
                role: Role::Promoting,
                tick: Tick(30),
                sync: Some(StandbySync::Tracking { aligned: Tick(25) }),
            },
            RoleReport {
                role: Role::Demoting,
                tick: Tick(31),
                sync: Some(StandbySync::Degraded {
                    detail: "fetch failed".to_string(),
                }),
            },
        ];
        for report in reports {
            let json = serde_json::to_string(&report).unwrap();
            assert_eq!(serde_json::from_str::<RoleReport>(&json).unwrap(), report);
        }
    }

    #[test]
    fn role_json_uses_snake_case_names() {
        assert_eq!(
            serde_json::to_string(&Role::Promoting).unwrap(),
            "\"promoting\""
        );
        assert_eq!(
            serde_json::to_string(&RoleReport {
                role: Role::Standby,
                tick: Tick(30),
                sync: Some(StandbySync::Tracking { aligned: Tick(25) }),
            })
            .unwrap(),
            r#"{"role":"standby","tick":30,"sync":{"tracking":{"aligned":25}}}"#
        );
    }

    #[test]
    fn switch_error_serde_roundtrip_and_names() {
        for error in [
            SwitchError::AlreadyActive,
            SwitchError::NotConverged {
                sync: StandbySync::Unsynchronized,
            },
            SwitchError::NotActive,
            SwitchError::OwnsField,
        ] {
            let json = serde_json::to_string(&error).unwrap();
            assert_eq!(serde_json::from_str::<SwitchError>(&json).unwrap(), error);
            assert!(!error.to_string().is_empty());
        }
        assert_eq!(
            serde_json::to_string(&SwitchError::AlreadyActive).unwrap(),
            "\"already_active\""
        );
        assert!(
            serde_json::to_string(&SwitchError::NotConverged {
                sync: StandbySync::Unsynchronized,
            })
            .unwrap()
            .contains("\"not_converged\"")
        );
    }
}
