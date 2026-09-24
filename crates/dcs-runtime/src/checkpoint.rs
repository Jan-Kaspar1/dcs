//! Deterministic checkpoint/restore: the redundant hot-swap groundwork.
//!
//! [`Executor::checkpoint`](crate::Executor::checkpoint) captures a run's
//! transferable state as a serde-serializable [`Checkpoint`];
//! [`Executor::restore`](crate::Executor::restore) rebuilds an equivalent
//! executor — fresh components wired against the same point map — with
//! that state applied. Determinism is what makes the transfer meaningful:
//! same model, same state, same inputs produce the same outputs, so a
//! restored run's subsequent scans are identical to the uninterrupted
//! run's.
//!
//! This maps to real redundancy as follows. Active and standby peers run
//! the same plant model; the active periodically ships a [`Checkpoint`]
//! to the standby, which restores it into its own executor so that on
//! switchover its next scan produces what the active would have produced.
//! The `components`, `outputs`, and `internal` sections transfer verbatim —
//! they are controller-side state: `outputs` carries the image's `Out`
//! samples and `internal` the image-carried `In` samples. So does the
//! `receipts` log — the bounded tail of the run's command audit, so
//! `GET /receipts` answers identically on a peer that adopted the
//! checkpoint — along with the `command_admission` counters measuring
//! that audit, so the pair's command-ingress telemetry agrees too and
//! the adopted window's place in the submission sequence stays known:
//! `attempts` minus the retained length is the count the source already
//! evicted. The `generation` stamp is the stream's tick-domain
//! identity: minted by the process that begins a run and adopted
//! verbatim by every run that applies one of its checkpoints, so the
//! whole tracked line — across a switchover, across a demoted peer's
//! reconvergence — names one generation, while a cold-restarted or
//! replaced source begins a new one. A tracking peer reads it to tell
//! the source's new generation — the journaled
//! `JournalEvent::SourceRestarted` — from its own tracking-state reset:
//! a demoted peer's first pull on its uninterrupted successor regresses
//! in tick but names the generation the demoted run's own captures
//! stamped, which is no restart. The `source_owns_field` stamp — set by
//! the serving peer, absent on a bare executor's capture — lets a
//! tracking peer name the mutual-standby wedge: a checkpoint applied
//! cleanly from a run owning no field writes means the tracked line
//! has no field owner, and the puller reports `orphaned` rather than a
//! healthy `tracking`. The `driver`
//! section is simulation-specific:
//! on live hardware the standby's driver observes the actual process
//! through its own channels rather than reconstructing captured field
//! state, so a driver that does not implement the contract simply leaves
//! `driver` empty and the restore skips it.

use crate::executor::WiringError;
use dcs_core::{
    CommandReceipt, ModelFingerprint, PointId, Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;
use std::net::SocketAddr;

/// The checkpoint format version this build writes.
///
/// The version negotiates checkpoint compatibility explicitly, the way
/// `MODEL_VERSION` does for the model document: a bump marks a breaking
/// format change, while additive changes — a new optional section or a
/// new optional field inside an element's [`StateMap`] vocabulary — ride
/// the existing version. Restore accepts every version in
/// [`SUPPORTED_FORMAT_VERSIONS`]; anything else is
/// [`RestoreError::UnsupportedVersion`].
pub const CHECKPOINT_FORMAT_VERSION: u32 = 1;

/// The checkpoint format versions a restore accepts.
///
/// Version 0 is the shape checkpoints had before the format was
/// versioned — serde reads an absent `format_version` as 0 — so a
/// checkpoint captured by a pre-versioning build still restores where
/// its fingerprint also matches, i.e. onto an executor assembled without
/// one. A compatible version may differ from
/// [`CHECKPOINT_FORMAT_VERSION`] only in what serde tolerates on this
/// schema: sections or fields it does not name may be absent (they
/// default) or extra (they are ignored), and an element's [`StateMap`]
/// may carry optional fields its `optional_*` accessors read. Everything
/// else remains strict: component-set equality, each element's required
/// fields and known-field check, and the point/kind checks on the
/// `outputs` and `internal` sections all still apply after the version
/// is accepted.
pub const SUPPORTED_FORMAT_VERSIONS: &[u32] = &[0, CHECKPOINT_FORMAT_VERSION];

/// The pending-command queue's admission counters — the bounded
/// command-ingress metrics the snapshot's `command_queue` section
/// reports.
///
/// The executor counts into this set on every
/// [`Executor::submit_command`](crate::Executor::submit_command), and the
/// checkpoint carries it beside the `receipts` log it measures: a peer
/// adopting the checkpoint converges to the same admission history, so
/// the pair's command-ingress telemetry answers identically across a
/// switchover. The snapshot section's `capacity` and `depth` are not
/// here — the bound is construction configuration, and the depth is the
/// adopted pending set itself.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct CommandAdmissionCounts {
    /// Commands presented for admission — every submission the
    /// executor's command path received, whether it settled accepted,
    /// was refused by validation, or was refused by a full queue.
    pub attempts: u64,
    /// Validated submissions refused because the pending queue was at
    /// capacity — each answered with a `queue_full` rejection receipt.
    pub full_rejections: u64,
    /// The deepest the pending queue has run — the high-water mark.
    pub high_water: usize,
}

/// A serializable snapshot of a run's transferable state.
///
/// Produced by [`Executor::checkpoint`](crate::Executor::checkpoint) at
/// the current tick and consumed by
/// [`Executor::restore`](crate::Executor::restore) on a standby — or by a
/// test reconstructing a run. The whole value round-trips through serde
/// like the rest of the contract types.
///
/// Restore enforces a two-part negotiation before any state applies: the
/// checkpoint's [`format_version`](Self::format_version) must be one of
/// [`SUPPORTED_FORMAT_VERSIONS`] and its
/// [`model_fingerprint`](Self::model_fingerprint) must equal the
/// fingerprint the restoring executor was assembled with — mismatches
/// are the named [`RestoreError::UnsupportedVersion`] and
/// [`RestoreError::FingerprintMismatch`]. Only then do the structural
/// checks of decision 11's strictness run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Checkpoint {
    /// The wire format's version. Checkpoints serialized before the
    /// format was versioned carry no field and deserialize as 0; this
    /// build writes [`CHECKPOINT_FORMAT_VERSION`].
    #[serde(default)]
    pub format_version: u32,
    /// The fingerprint of the model the capturing run was assembled
    /// from, supplied by the assembling layer — the executor is
    /// model-agnostic and never derives it. `None` when the run was
    /// assembled without one (and on every checkpoint a pre-versioning
    /// build wrote); a fingerprinted executor accepts only a checkpoint
    /// carrying the same value.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model_fingerprint: Option<ModelFingerprint>,
    /// The tick-domain generation the capturing run belongs to —
    /// minted when a process begins a fresh run and adopted verbatim by
    /// every run whose executor applied, restored, or reinitialized one
    /// of its checkpoints, so the whole tracked line names the same
    /// generation while a cold-started or replaced source begins a new
    /// one. A tracking peer compares it against its own to tell the
    /// source's restart from its own tracking reset: a demoted peer's
    /// first pull on the uninterrupted successor regresses in tick but
    /// names the generation the demoted run's own captures stamped, so
    /// it journals no `SourceRestarted`. `None` on checkpoints a
    /// pre-generation build wrote or an unminted run captured — an
    /// unidentified generation can prove nothing, so a regression
    /// involving one journals the restart exactly as it always did.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub generation: Option<u64>,
    /// The capturing run's run tick at capture; the restored executor
    /// resumes numbering from here. A tracking peer reads it as a source
    /// tick — the tracked stream's own counter — and lands it at
    /// `tick + tick_offset` in its own run domain.
    pub tick: Tick,
    /// Each component's captured state, keyed by the component's
    /// [`name`](crate::Component::name) — its identity within the run.
    /// Every registered component has an entry, possibly an empty map, so
    /// the checkpoint describes the component set exactly and a
    /// mismatched restore is caught by name.
    pub components: BTreeMap<String, StateMap>,
    /// The driver's captured state, or `None` when the driver does not
    /// implement the state-capture contract. `None` leaves the standby's
    /// driver to its own channels — the live-hardware case, where the
    /// field's real values are the state.
    pub driver: Option<StateMap>,
    /// The scan image's `Out` samples at capture: the last written output
    /// values, including image-carried internal `Out` points. Restoring
    /// them means an output a component does not rewrite on the next scan
    /// keeps its last value, exactly as the uninterrupted run would.
    pub outputs: BTreeMap<PointId, Sample>,
    /// The scan image's internal `In` samples at capture: held operator
    /// values and link carriers, which field reads never refresh.
    /// Restoring them means a commanded setpoint survives a switchover
    /// instead of reverting to its declared initial. Absent from
    /// checkpoints written before internal points existed; defaults to
    /// empty.
    #[serde(default)]
    pub internal: BTreeMap<PointId, Sample>,
    /// The operator force set at capture: each forced point and the
    /// value the scan image substitutes for its input read. Restoring
    /// them means a standby keeps forcing the same points through a
    /// switchover instead of reverting to live reads. Absent from
    /// checkpoints written before forces existed; defaults to empty.
    #[serde(default)]
    pub forces: BTreeMap<PointId, Value>,
    /// The command receipt log at capture: the retained tail of the
    /// run's audit — the most recent receipts in submission order,
    /// bounded by the capturing run's receipt-log capacity, served as
    /// `GET /receipts`. The window's place in the submission sequence
    /// is recoverable from the `command_admission` counters — see
    /// [`receipt_base`](Self::receipt_base). Restoring it converges
    /// the tracking peer's log to the active's, so the pair presents
    /// one continuous audit trail across a switchover — evictions the
    /// source already made showing on the peer as the same numbering
    /// gap — except the contiguous tail of the restoring run's own
    /// log this window's high-water never reached: those receipts are
    /// submissions the capture predates, not entries the line dropped,
    /// so the restore keeps them suspended rather than settling their
    /// absence as a verdict; entries still `Accepted` at capture
    /// re-queue on the restoring run — verbatim, past the pending
    /// queue's admission bound: carried run state is not new admission,
    /// so a command taken over between its submission boundary and its
    /// applying scan is not lost, and a queue restored at or over the
    /// bound admits nothing new until a scan drains it.
    /// Absent from checkpoints written before the section existed;
    /// defaults to empty.
    #[serde(default)]
    pub receipts: Vec<CommandReceipt>,
    /// The pending-command queue's admission counters at capture,
    /// converging beside the `receipts` log they measure — the pair's
    /// `command_queue` telemetry section answers identically on either
    /// peer. `attempts` doubles as the receipt window's high-water mark:
    /// every submission produced exactly one receipt. Absent from
    /// checkpoints written before the section existed; defaults to
    /// zeroed — a legacy log is then the never-evicted prefix it always
    /// was, and [`receipt_base`](Self::receipt_base) resolves to 0.
    #[serde(default)]
    pub command_admission: CommandAdmissionCounts,
    /// Whether the run serving this checkpoint owns field writes —
    /// stamped by the serving [`Peer`](crate::Peer) at capture, not by
    /// the executor, which has no role view. `Some(true)` marks a
    /// field-owning source — `active` or `promoting`; `Some(false)`
    /// marks a serving run that writes nothing — the stamp a tracking
    /// peer reads to name the mutual-standby wedge: a cleanly applied
    /// checkpoint whose serving run owns no field writes means the
    /// tracked line has no field owner, and the puller reports
    /// [`StandbySync::Orphaned`](dcs_core::StandbySync) instead of a
    /// converged `tracking` that only looks healthy. `None` — every
    /// checkpoint a bare executor captures, and everything a
    /// pre-stamping build wrote — carries no ownership claim, so an
    /// apply treats it as owner-produced exactly as it always did.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_owns_field: Option<bool>,
    /// The monitor address this checkpoint line currently names as its
    /// field owner — serve-time decoration like
    /// [`line_proof`](Self::line_proof), never run state: a serving
    /// field owner stamps its own address, a non-owning serving run
    /// propagates the address the checkpoints it pulls carried, and a
    /// bare [`Executor::checkpoint`] leaves it absent. A tracking peer
    /// whose tracked line reports no owner — [`StandbySync::Orphaned`]
    /// — probes it to re-resolve onto the run that actually holds the
    /// field, so a demoted peer pinned onto a sibling standby's
    /// checkpoints follows the line onward to the real owner instead
    /// of orphaned-tracking a stale island forever.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line_owner: Option<SocketAddr>,
    /// The keyed line proof a serving monitor injects into this
    /// document's wire form on a `?prove=` pull — response decoration,
    /// never run state: [`Executor::checkpoint`](crate::Executor::checkpoint)
    /// never sets it, [`Executor::restore`](crate::Executor::restore)
    /// ignores it, and it is absent on every captured checkpoint and
    /// every response to an unproven pull. A pulling peer compares it
    /// against the proof it computes over the received document and
    /// its request's nonce under the pair's shared key, so a checkpoint
    /// served by an endpoint that merely replays or fabricates this
    /// line's documents — without holding the key — cannot masquerade
    /// as a tracked peer's production.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub line_proof: Option<u64>,
}

/// Mints a fresh checkpoint-stream generation — the value a run's
/// assembling shell stamps through
/// [`Executor::with_generation`](crate::Executor::with_generation) so
/// every checkpoint the run serves names this tick-domain line.
///
/// Process id plus the wall clock keeps a restarted process's mint
/// distinct from the run it replaced, and a process-unique counter
/// keeps two mints inside one process distinct — the same uniqueness
/// budget the field-claim owner token accepts. The value carries no
/// meaning beyond identity: equal generations name the same tracked
/// line, different ones a new tick domain.
pub fn mint_generation() -> u64 {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let mut hasher = RandomState::new().build_hasher();
    hasher.write_u32(std::process::id());
    if let Ok(since) = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        hasher.write_u128(since.as_nanos());
    }
    hasher.write_u64(NEXT.fetch_add(1, Ordering::Relaxed));
    hasher.finish()
}

impl Checkpoint {
    /// The absolute submission index of `receipts[0]` — the count of
    /// settled receipts the capturing run had already evicted at
    /// capture.
    ///
    /// Derived rather than carried: `attempts` counts every lifetime
    /// submission and each produced exactly one receipt, so
    /// `attempts - receipts.len()` is the evicted prefix's length. A
    /// checkpoint captured before admission counters existed reports
    /// zeroed counters; saturating subtraction then resolves to 0 —
    /// the base those logs genuinely had, since nothing had ever been
    /// evicted.
    pub fn receipt_base(&self) -> u64 {
        self.command_admission
            .attempts
            .saturating_sub(self.receipts.len() as u64)
    }
}

/// Why [`Executor::restore`](crate::Executor::restore) failed.
///
/// A restore either rebuilds a run equivalent to the captured one or
/// fails outright — it never produces a partially restored executor.
/// (The supplied driver may reject its state section after inspection;
/// drivers are expected to validate before applying so a failed restore
/// leaves them untouched.)
#[derive(Debug, Clone, PartialEq)]
pub enum RestoreError {
    /// The checkpoint's format version is not one this build accepts —
    /// checked before anything else, so a checkpoint from a newer or
    /// unknown format never reaches the structural checks.
    UnsupportedVersion {
        /// The version the checkpoint declares — 0 when the field is
        /// absent, as on checkpoints a pre-versioning build wrote.
        found: u32,
        /// Every format version this build accepts.
        supported: &'static [u32],
    },
    /// The checkpoint's model fingerprint differs from the fingerprint
    /// the executor was assembled with — a checkpoint captured under a
    /// different model, including one whose component set happens to
    /// match structurally.
    FingerprintMismatch {
        /// The fingerprint the checkpoint carries — `None` when it was
        /// captured by a run assembled without one.
        found: Option<ModelFingerprint>,
        /// The fingerprint the restoring executor was assembled with.
        expected: Option<ModelFingerprint>,
    },
    /// The fresh components did not wire against the point map.
    Wiring(WiringError),
    /// The checkpoint names a component the executor does not register.
    UnknownComponent {
        /// The checkpoint entry with no registered component.
        component: String,
    },
    /// A registered component has no entry in the checkpoint.
    MissingComponent {
        /// The registered component the checkpoint does not describe.
        component: String,
    },
    /// A component rejected its captured state.
    Component(StateError),
    /// The driver rejected its captured state.
    Driver(StateError),
    /// The checkpoint's output image names a point the executor's map
    /// does not serve as `Out` — a checkpoint from a different I/O
    /// mapping.
    UnknownOutput {
        /// The unserved or misdirected point.
        point: PointId,
    },
    /// A checkpointed output's value kind differs from the kind the
    /// point map declares for it — again a different mapping's artifact.
    IncompatibleOutput {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// The checkpoint's internal section names a point the executor's
    /// map does not serve as an internal `In` — a checkpoint from a
    /// different I/O mapping.
    UnknownInternal {
        /// The unserved or misdirected point.
        point: PointId,
    },
    /// A checkpointed internal sample's value kind differs from the kind
    /// the point map declares for it — again a different mapping's
    /// artifact.
    IncompatibleInternal {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
    /// The checkpoint's force section names a point the executor's map
    /// does not serve as a writable `In` point — a checkpoint from a
    /// different I/O mapping.
    UnknownForce {
        /// The unserved or unforceable point.
        point: PointId,
    },
    /// A checkpointed force's value kind differs from the kind the
    /// point map declares for it — again a different mapping's
    /// artifact.
    IncompatibleForce {
        /// The mismatched point.
        point: PointId,
        /// The kind the point map declares.
        expected: ValueKind,
        /// The kind the checkpoint carries.
        found: Value,
    },
}

impl fmt::Display for RestoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnsupportedVersion { found, supported } => write!(
                f,
                "unsupported checkpoint format version {found} (this build accepts {supported:?})"
            ),
            Self::FingerprintMismatch { found, expected } => write!(
                f,
                "checkpoint model fingerprint {} does not match this run's {}",
                found
                    .map(|fingerprint| fingerprint.to_string())
                    .unwrap_or_else(|| "<none>".to_string()),
                expected
                    .map(|fingerprint| fingerprint.to_string())
                    .unwrap_or_else(|| "<none>".to_string()),
            ),
            Self::Wiring(error) => write!(f, "wiring the fresh components failed: {error}"),
            Self::UnknownComponent { component } => write!(
                f,
                "checkpoint captures component {component:?} the executor does not register"
            ),
            Self::MissingComponent { component } => {
                write!(f, "component {component:?} has no state in the checkpoint")
            }
            Self::Component(error) => write!(f, "component state restore failed: {error}"),
            Self::Driver(error) => write!(f, "driver state restore failed: {error}"),
            Self::UnknownOutput { point } => write!(
                f,
                "checkpoint carries output point {} the point map does not serve as out",
                point.0
            ),
            Self::IncompatibleOutput {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint output point {} expects {expected:?}, found {found:?}",
                point.0
            ),
            Self::UnknownInternal { point } => write!(
                f,
                "checkpoint carries internal point {} the point map does not serve as an internal in point",
                point.0
            ),
            Self::IncompatibleInternal {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint internal point {} expects {expected:?}, found {found:?}",
                point.0
            ),
            Self::UnknownForce { point } => write!(
                f,
                "checkpoint carries forced point {} the point map does not serve as a writable in point",
                point.0
            ),
            Self::IncompatibleForce {
                point,
                expected,
                found,
            } => write!(
                f,
                "checkpoint forced point {} expects {expected:?}, found {found:?}",
                point.0
            ),
        }
    }
}

impl std::error::Error for RestoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Wiring(error) => Some(error),
            Self::Component(error) | Self::Driver(error) => Some(error),
            _ => None,
        }
    }
}

impl From<WiringError> for RestoreError {
    fn from(error: WiringError) -> Self {
        Self::Wiring(error)
    }
}
