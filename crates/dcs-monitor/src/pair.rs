//! The pair view: two redundant controller monitors presented as one
//! logical controller — the client half of the
//! monitoring-under-redundancy decision.
//!
//! A redundant pair is two `dcs-controller` processes — an active owning
//! field writes and a standby tracking it — each serving the monitoring
//! endpoints of this crate. To an operator the pair is one controller, so
//! [`PairClient`] is configured with *both* peers' monitor addresses,
//! polls `GET /role` on each alongside the normal data cadence, and reads
//! every data endpoint — snapshot, signal index, history, journal — from
//! the peer reporting `active`. The other peer contributes pair health
//! only: its reported role and convergence, or the named
//! [`PeerStatus::Unreachable`] redundancy fault when its poll fails. A
//! peer's loss is never a plant fault: the plant's own health is the
//! active peer's telemetry, which keeps flowing while a standby is down.
//!
//! Commands go only to the peer reporting settled `active` — submitting
//! to a standby or mid-transition peer would be refused with
//! [`CommandError::NotActive`], and the pair view refuses earlier by
//! never sending. Two peers reporting `active` at once is the dual-active
//! split-brain the one-logical-controller contract makes impossible: the
//! view names it as a redundancy fault through [`PairClient::health`]
//! rather than presenting the pair as healthy, holds its data source
//! rather than re-homing between claimants, and finds no unambiguous
//! command target — [`PairError::AmbiguousActive`], nothing sent — while
//! the fault stands. A command landing mid-transition still draws the
//! `NotActive` rejection: the view then re-polls both roles and retries
//! once against the peer now reporting `active`, exactly as the decision
//! prescribes.
//!
//! A standby that cannot demonstrate convergence is likewise named:
//! `unsynchronized` is the legitimate transient a fresh or newly
//! demoted peer occupies while its first tracking pulls land, so
//! [`PairClient::health`] gives it [`CONVERGENCE_GRACE`] — but past the
//! grace the pair has no converged failover peer (the state is
//! permanent for a demoted peer with no checkpoint source), and the
//! verdict says so rather than reporting a healthy pair.
//!
//! Tick continuity across a switchover is a property of the contract,
//! not of this type: checkpoints align the standby's tick with the
//! active's, so the promoted peer's history and journal continue the same
//! tick sequence and a consumer merging streams across a source change —
//! like the page — sees one continuous run.

use crate::MonitorClient;
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandReceipt, JournalEntry, PointHistory, PointId,
    Role, RoleReport, StandbySync, TelemetrySnapshot,
};
use dcs_model::SignalIndex;
use serde::{Deserialize, Serialize};
use std::fmt;
use std::io;
use std::net::SocketAddr;
use std::time::{Duration, Instant};

/// What the last role poll observed of one peer.
#[derive(Debug, Clone, PartialEq)]
pub enum PeerStatus {
    /// No poll has completed yet.
    Unknown,
    /// The peer answered `GET /role`; the payload it reported.
    Reporting(RoleReport),
    /// The role poll failed to reach the peer — the named redundancy
    /// fault of the pair view. Reachability is per-peer pair health:
    /// this indication says nothing about the plant, whose state the
    /// active peer's telemetry keeps reporting.
    Unreachable {
        /// What the failed request reported, for diagnostics.
        detail: String,
    },
}

/// One configured peer of the pair: its monitor address, its
/// [`MonitorClient`], and the status the last role poll observed.
pub struct PeerView {
    addr: SocketAddr,
    client: MonitorClient,
    status: PeerStatus,
    /// The instant the peer's current run of `unsynchronized` reports
    /// began — the start of the convergence grace [`health`] reads.
    /// `None` while the peer reports any converged sync state or is not
    /// reporting at all.
    ///
    /// [`health`]: PairClient::health
    unsynced_since: Option<Instant>,
}

impl PeerView {
    /// The peer's monitor address — its `dcs-controller --listen`
    /// address.
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    /// The last role poll's outcome for this peer.
    pub fn status(&self) -> &PeerStatus {
        &self.status
    }
}

impl fmt::Debug for PeerView {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PeerView")
            .field("addr", &self.addr)
            .field("status", &self.status)
            .finish()
    }
}

/// Why a pair-view operation could not be carried out — failures the UI
/// surfaces distinctly from per-command rejection receipts.
#[derive(Debug)]
pub enum PairError {
    /// No reachable peer reports the settled `active` role: the pair is
    /// mid-transition or its active is lost. Nothing was sent — a
    /// command must never land on a peer not reporting `active`.
    NoActivePeer,
    /// More than one peer reports the settled `active` role — the
    /// dual-active split-brain the one-logical-controller contract makes
    /// impossible. Nothing was sent: no claimant is an unambiguous
    /// target, and picking one would risk commanding a fenced peer.
    AmbiguousActive,
    /// No peer can currently source the logical view — every poll has
    /// failed.
    NoSourcePeer,
    /// The request to the selected peer failed at the transport layer.
    Io(io::Error),
}

impl fmt::Display for PairError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NoActivePeer => f.write_str(
                "no peer reports role active; the pair is mid-transition or its active is lost",
            ),
            Self::AmbiguousActive => f.write_str(
                "multiple peers report role active; the dual-active fault leaves \
                 no unique command target",
            ),
            Self::NoSourcePeer => {
                f.write_str("no peer is reachable to source the logical controller view")
            }
            Self::Io(error) => write!(f, "request to the selected peer failed: {error}"),
        }
    }
}

impl std::error::Error for PairError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for PairError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// The wall-clock grace a peer reporting `unsynchronized` gets before
/// [`PairClient::health`] names it a redundancy fault — the transient a
/// fresh or newly demoted standby legitimately occupies while its
/// first tracking pulls land at the documented pull-per-scan cadence
/// (a handful of scan periods at the controller's default 100 ms pace,
/// plus a pull bound). Past it, a peer that still cannot demonstrate
/// convergence — above all a demoted peer with no checkpoint source,
/// which stays `unsynchronized` forever and can never be promoted back
/// — leaves the pair with zero failover coverage, and the verdict
/// says so rather than rendering "redundant pair healthy".
pub const CONVERGENCE_GRACE: Duration = Duration::from_secs(5);

/// The pair's redundancy health as the view summarizes it — the unique
/// settled-`active` peer plus the named faults of the last role poll;
/// the page's `pairHealth` verdict for in-process consumers. See
/// [`PairClient::health`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PairHealth {
    /// The peer reporting settled `active` when exactly one does —
    /// `None` while no peer reports it or the dual-active fault stands.
    pub active: Option<SocketAddr>,
    /// The named redundancy faults the last poll observed: an
    /// unreachable peer, a degraded or diverged standby convergence, a
    /// peer still `unsynchronized` past [`CONVERGENCE_GRACE`], no peer
    /// reporting `active`, or more than one — the dual-active
    /// split-brain. Empty is the healthy pair.
    pub faults: Vec<String>,
    #[doc = "Vocabulary version; absent on legacy prose-only verdicts."]
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fault_kinds_version: Option<u32>,
    #[doc = "Stable kinds, one per human-readable fault at the same index."]
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub fault_kinds: Vec<PairFaultKind>,
}

#[doc = "Version of the serialized pair-fault kind vocabulary, pinned by contract drift tests."]
pub const PAIR_FAULT_KINDS_VERSION: u32 = 2;

#[doc = "Stable redundancy fault names shared by the pair view and operator consumers."]
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PairFaultKind {
    #[doc = "A configured peer's role poll failed."]
    PeerUnreachable,
    #[doc = "A peer continuously reported unsynchronized beyond the convergence grace."]
    StandbyUnsynchronizedPastGrace,
    #[doc = "No peer reported the settled active role."]
    NoActivePeer,
    #[doc = "More than one peer reported the settled active role."]
    DualActive,
    #[doc = "A standby reported degraded checkpoint synchronization."]
    StandbyDegraded,
    #[doc = "A standby reported staged outputs diverging from the field."]
    StandbyDiverged,
    #[doc = "A standby reported the tracked line has no field owner."]
    StandbyOrphaned,
}

impl PairFaultKind {
    #[doc = "The complete vocabulary for this version, in drift-pin order."]
    pub const ALL: [Self; 7] = [
        Self::PeerUnreachable,
        Self::StandbyUnsynchronizedPastGrace,
        Self::NoActivePeer,
        Self::DualActive,
        Self::StandbyDegraded,
        Self::StandbyDiverged,
        Self::StandbyOrphaned,
    ];
}

/// A client-side view presenting an active/standby controller pair as
/// one logical controller, per the monitoring-under-redundancy decision.
///
/// Construct with both peers' monitor addresses — order does not matter;
/// the view follows whichever peer reports `active` — then call
/// [`poll_roles`](Self::poll_roles) on the page's normal cadence. Data
/// reads ([`snapshot`](Self::snapshot), [`signals`](Self::signals),
/// [`history`](Self::history), [`journal`](Self::journal)) are answered
/// by the currently selected source; [`command`](Self::command) submits
/// only to the peer reporting settled `active`.
#[derive(Debug)]
pub struct PairClient {
    peers: Vec<PeerView>,
    /// The peer index currently sourcing the logical view.
    source: Option<usize>,
    /// The grace [`health`](Self::health) allows a peer's
    /// `unsynchronized` report before naming it — see
    /// [`CONVERGENCE_GRACE`].
    convergence_grace: Duration,
}

impl PairClient {
    /// A pair view over the peers' monitor addresses — typically the two
    /// `dcs-controller --listen` addresses of an active/standby pair.
    pub fn new(addrs: impl IntoIterator<Item = SocketAddr>) -> Self {
        Self {
            peers: addrs
                .into_iter()
                .map(|addr| PeerView {
                    addr,
                    client: MonitorClient::new(addr),
                    status: PeerStatus::Unknown,
                    unsynced_since: None,
                })
                .collect(),
            source: None,
            convergence_grace: CONVERGENCE_GRACE,
        }
    }

    /// Overrides [`CONVERGENCE_GRACE`] — the wall-clock grace a peer
    /// reporting `unsynchronized` gets before [`health`](Self::health)
    /// names it a redundancy fault. A deployment whose tracking cadence
    /// is slower than the default 100 ms scan period widens it;
    /// `Duration::ZERO` names the first observed `unsynchronized`
    /// report.
    pub fn with_convergence_grace(mut self, grace: Duration) -> Self {
        self.convergence_grace = grace;
        self
    }

    /// The configured peers with their last-polled statuses — the pair
    /// health the view renders beside the logical controller.
    pub fn peers(&self) -> &[PeerView] {
        &self.peers
    }

    /// The monitor address currently sourcing the logical view — the
    /// last poll's settled-`active` peer, a `promoting` peer while a
    /// switchover settles, or the previously selected peer while it
    /// stays reachable. `None` before the first successful poll.
    pub fn source(&self) -> Option<SocketAddr> {
        self.source.map(|index| self.peers[index].addr)
    }

    /// Polls `GET /role` on every peer — the role cadence the pair view
    /// adds to the normal data polling — and re-selects the source.
    ///
    /// A peer whose poll fails becomes [`PeerStatus::Unreachable`] — the
    /// named redundancy fault — rather than an error of the view itself:
    /// one peer's loss must never interrupt the other peer's data.
    /// Selection prefers a peer reporting settled `active`, then a
    /// `promoting` peer (it already owns the field and is becoming
    /// active), then the previously selected peer while it stays
    /// reachable, then any reachable peer. Under the dual-active fault —
    /// more than one peer reporting `active` — the current source is
    /// held while it is one of the claimants: [`health`](Self::health)
    /// names the fault, and re-homing the view between two reported
    /// actives would only churn the displays without resolving which is
    /// real.
    pub fn poll_roles(&mut self) {
        for peer in &mut self.peers {
            peer.status = match peer.client.role() {
                Ok(report) => PeerStatus::Reporting(report),
                Err(error) => PeerStatus::Unreachable {
                    detail: error.to_string(),
                },
            };
            // The convergence grace [`health`](Self::health) reads: an
            // `unsynchronized` report starts or continues the peer's
            // run; any converged sync state — and the unreachable
            // fault, which `health` already names on its own — ends
            // it.
            peer.unsynced_since = match &peer.status {
                PeerStatus::Reporting(report)
                    if matches!(report.sync, Some(StandbySync::Unsynchronized)) =>
                {
                    Some(peer.unsynced_since.unwrap_or_else(Instant::now))
                }
                _ => None,
            };
        }
        let actives: Vec<usize> = self
            .peers
            .iter()
            .enumerate()
            .filter(|(_, peer)| {
                matches!(&peer.status, PeerStatus::Reporting(report) if report.role == Role::Active)
            })
            .map(|(index, _)| index)
            .collect();
        self.source = self
            .source
            .filter(|index| actives.contains(index))
            .or_else(|| actives.first().copied())
            .or_else(|| self.reporting(Role::Promoting))
            .or_else(|| {
                self.source
                    .filter(|&index| matches!(self.peers[index].status, PeerStatus::Reporting(_)))
            })
            .or_else(|| {
                self.peers
                    .iter()
                    .position(|peer| matches!(peer.status, PeerStatus::Reporting(_)))
            });
    }

    /// The pair's redundancy health for the summary the view renders —
    /// the same verdict the page's `pairHealth` computes over a `?peer`
    /// pair: the uniquely reporting settled-`active` peer's address,
    /// and the named redundancy faults the last poll observed — an
    /// unreachable peer, a degraded or diverged standby convergence, a
    /// peer still reporting `unsynchronized` past
    /// [`CONVERGENCE_GRACE`] (the lost failover coverage a stranded
    /// standby leaves, where `unsynchronized` may be permanent), no
    /// peer reporting `active`, or more than one reporting it (the
    /// dual-active split-brain the one-logical-controller contract makes
    /// impossible). Pair health, never plant faults.
    pub fn health(&self) -> PairHealth {
        let mut faults = Vec::new();
        let mut fault_kinds = Vec::new();
        let mut actives = Vec::new();
        for peer in &self.peers {
            match &peer.status {
                PeerStatus::Unknown => {}
                PeerStatus::Unreachable { .. } => {
                    fault_kinds.push(PairFaultKind::PeerUnreachable);
                    faults.push(format!("{} unreachable", peer.addr));
                }
                PeerStatus::Reporting(report) => {
                    if report.role == Role::Active {
                        actives.push(peer.addr);
                    }
                    match &report.sync {
                        Some(StandbySync::Unsynchronized) => {
                            if peer
                                .unsynced_since
                                .is_some_and(|since| since.elapsed() >= self.convergence_grace)
                            {
                                fault_kinds.push(PairFaultKind::StandbyUnsynchronizedPastGrace);
                                faults.push(format!(
                                    "{} has not converged: unsynchronized past the \
                                     convergence grace",
                                    peer.addr
                                ));
                            }
                        }
                        Some(StandbySync::Degraded { detail }) => {
                            fault_kinds.push(PairFaultKind::StandbyDegraded);
                            faults.push(format!("{} sync degraded: {detail}", peer.addr));
                        }
                        Some(StandbySync::Diverged { mismatches }) => {
                            fault_kinds.push(PairFaultKind::StandbyDiverged);
                            faults.push(format!(
                                "{} standby diverged: staged outputs mismatch the field at {}",
                                peer.addr,
                                mismatches
                                    .iter()
                                    .map(|mismatch| mismatch.point.0.to_string())
                                    .collect::<Vec<_>>()
                                    .join(", ")
                            ));
                        }
                        Some(StandbySync::Orphaned { aligned }) => {
                            fault_kinds.push(PairFaultKind::StandbyOrphaned);
                            faults.push(format!(
                                "{} reports the tracked line has no field owner \
                                 (aligned at tick {})",
                                peer.addr, aligned.0
                            ));
                        }
                        _ => {}
                    }
                }
            }
        }
        if actives.is_empty() {
            fault_kinds.push(PairFaultKind::NoActivePeer);
            faults.push("no peer reports role active".to_string());
        } else if actives.len() > 1 {
            fault_kinds.push(PairFaultKind::DualActive);
            faults.push(format!(
                "dual-active: {} report role active",
                actives
                    .iter()
                    .map(SocketAddr::to_string)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        PairHealth {
            active: match actives.as_slice() {
                [only] => Some(*only),
                _ => None,
            },
            faults,
            fault_kinds_version: Some(PAIR_FAULT_KINDS_VERSION),
            fault_kinds,
        }
    }

    /// `GET /signals` from the current source — both peers serve the same
    /// model's index.
    pub fn signals(&self) -> Result<SignalIndex, PairError> {
        self.selected()?.signals().map_err(PairError::from)
    }

    /// `GET /snapshot` from the current source — the logical
    /// controller's telemetry, which keeps flowing while the standby is
    /// unreachable.
    pub fn snapshot(&self) -> Result<TelemetrySnapshot, PairError> {
        self.selected()?.snapshot().map_err(PairError::from)
    }

    /// `GET /history` from the current source; see
    /// [`MonitorClient::history`]. Across a source change the pair's
    /// shared tick domain keeps the merged series continuous.
    pub fn history(&self, points: &[PointId], since: u64) -> Result<Vec<PointHistory>, PairError> {
        self.selected()?
            .history(points, since)
            .map_err(PairError::from)
    }

    /// `GET /journal` from the current source; see
    /// [`MonitorClient::journal`].
    pub fn journal(&self, since: u64) -> Result<Vec<JournalEntry>, PairError> {
        self.selected()?.journal(since).map_err(PairError::from)
    }

    /// Submits `command` to the peer reporting settled `active` — never
    /// to a standby — and answers its receipt.
    ///
    /// A [`CommandError::NotActive`] rejection means the roles moved
    /// mid-flight: the view re-polls both peers and retries once against
    /// the peer now reporting `active`. A second refusal, or a poll
    /// finding no unique settled-active peer, answers honestly: the
    /// rejection receipt, [`PairError::NoActivePeer`] when none reports
    /// `active`, or [`PairError::AmbiguousActive`] under the dual-active
    /// fault — nothing is sent while no unique target exists.
    pub fn command(&mut self, command: &Command) -> Result<CommandReceipt, PairError> {
        if self.unique_active().is_none() {
            // Nothing polled yet, or the last poll saw no unique active:
            // refresh before concluding there is no legitimate target —
            // a mid-settle dual report can resolve either way.
            self.poll_roles();
        }
        let mut retried = false;
        loop {
            let Some(target) = self.unique_active() else {
                return Err(if self.reporting(Role::Active).is_some() {
                    // An active reports but is not unique — the
                    // dual-active fault leaves no unambiguous target.
                    PairError::AmbiguousActive
                } else {
                    PairError::NoActivePeer
                });
            };
            let receipt = self.peers[target].client.command(command)?;
            let landed_mid_transition = matches!(
                receipt.outcome,
                CommandOutcome::Rejected {
                    reason: CommandError::NotActive { .. }
                }
            );
            if !landed_mid_transition || retried {
                return Ok(receipt);
            }
            retried = true;
            self.poll_roles();
        }
    }

    /// The peer index reporting settled `active` when exactly one does —
    /// the only legitimate command target. No reported active, or the
    /// dual-active fault's several, leaves no unambiguous target.
    fn unique_active(&self) -> Option<usize> {
        let mut actives = self.peers.iter().enumerate().filter(|(_, peer)| {
            matches!(&peer.status, PeerStatus::Reporting(report) if report.role == Role::Active)
        });
        match (actives.next(), actives.next()) {
            (Some((index, _)), None) => Some(index),
            _ => None,
        }
    }

    /// The peer index whose last report carries `role`, if any.
    fn reporting(&self, role: Role) -> Option<usize> {
        self.peers.iter().position(
            |peer| matches!(&peer.status, PeerStatus::Reporting(report) if report.role == role),
        )
    }

    /// The currently selected peer's client.
    fn selected(&self) -> Result<&MonitorClient, PairError> {
        self.source
            .map(|index| &self.peers[index].client)
            .ok_or(PairError::NoSourcePeer)
    }
}
