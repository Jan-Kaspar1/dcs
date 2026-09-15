//! The transport seam beneath the cyclic contract: what one logical
//! EtherCAT bus must provide for [`BusMaster`](crate::BusMaster) to run
//! the image exchange on top of it.
//!
//! The seam exists so the cyclic contract — staged output image, latched
//! input image, miss accounting, recovery at the exchange boundary — is
//! provable without hardware: [`EthercrabTransport`](crate::EthercrabTransport)
//! is the real-socket implementation over EtherCrab, while
//! [`testing::FakeTransport`](crate::testing::FakeTransport) is the fake
//! PDU loop the contract tests drive. Both ends present the same shape:
//! discovery results the master verifies before OP, a one-shot OP entry,
//! a single-cycle exchange, and a boundary recovery step.

use dcs_core::LinkState;
use std::fmt;
use std::sync::Arc;

/// A station the transport discovered during bus init — the master
/// verifies each [`crate::StationProfile`] against this before outputs
/// may be enabled.
#[derive(Debug, Clone, PartialEq)]
pub struct DiscoveredStation {
    /// The station's position on the bus: `0` for the first SubDevice
    /// downstream of the master.
    pub position: usize,
    /// The station's reported name (e.g. the EEPROM device name), empty
    /// when the transport cannot read one.
    pub name: String,
    /// The station's vendor id, as reported by its identity register.
    pub vendor_id: u32,
    /// The station's product code, as reported by its identity register.
    pub product_id: u32,
    /// The station's revision, as reported by its identity register.
    pub revision: u32,
    /// The station's cyclic input area in bytes within the bus process
    /// image.
    pub input_bytes: usize,
    /// The station's cyclic output area in bytes within the bus process
    /// image.
    pub output_bytes: usize,
}

/// The outcome of one [`BusTransport::exchange`] cycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CycleOutcome {
    /// The frame completed: staged outputs published, every station's
    /// inputs returned.
    Complete,
    /// The frame completed but returned past the declared exchange
    /// deadline — counted under `missed_deadlines` in diagnostics.
    Late,
    /// The frame returned short of the expected working counter or with
    /// a station out of OP. `Some(position)` attributes the shortfall to
    /// one station — its inputs are not latched and its points escalate
    /// to [`dcs_core::IoError::Disconnected`] until a clean exchange —
    /// while `None` is unattributable and degrades the whole bus.
    ///
    /// The transport may write whatever it received into `inputs`; the
    /// master latches only the ranges it keeps.
    Short {
        /// The degraded station's bus position, when attributable.
        station: Option<usize>,
    },
}

/// Why a transport call failed — the bus-level failure vocabulary the
/// master counts and reports, distinct from per-point [`dcs_core::IoError`]s.
#[derive(Debug, Clone, PartialEq)]
pub enum TransportError {
    /// The link or the bus thread is down: the exchange did not
    /// complete and nothing moved on the wire.
    Disconnected(String),
    /// A frame did not return within its timeout.
    Timeout(String),
    /// A station or the group refused a requested state transition —
    /// OP entry or recovery re-entry.
    State(String),
    /// Anything else: an internal or unclassified transport failure.
    Internal(String),
}

impl TransportError {
    /// A [`TransportError::Disconnected`] with the given detail.
    pub fn disconnected(detail: impl Into<String>) -> Self {
        Self::Disconnected(detail.into())
    }

    /// A [`TransportError::Internal`] with the given detail.
    pub fn internal(detail: impl Into<String>) -> Self {
        Self::Internal(detail.into())
    }
}

impl fmt::Display for TransportError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Disconnected(detail) => write!(f, "link down: {detail}"),
            Self::Timeout(detail) => write!(f, "exchange timed out: {detail}"),
            Self::State(detail) => write!(f, "state transition refused: {detail}"),
            Self::Internal(detail) => write!(f, "transport failure: {detail}"),
        }
    }
}

impl std::error::Error for TransportError {}

/// One logical bus beneath the cyclic contract: the transport the
/// [`BusMaster`](crate::BusMaster) drives once per scan.
///
/// Implementations own the wire entirely. The master never holds a
/// socket, frame, or async runtime: construction already discovered the
/// bus (EtherCrab's `MainDevice::init`, with the group filter rejecting
/// unrecognized stations), [`enter_op`](Self::enter_op) runs the
/// PRE-OP → SAFE-OP → OP entry once, [`exchange`](Self::exchange) turns
/// the process image exactly once per call, and
/// [`recover`](Self::recover) re-enters PRE-OP/SAFE-OP/OP — always at
/// the exchange boundary, never mid-scan.
///
/// A transport is bound to its host interface when it is created —
/// where the interface name comes from is the caller's deployment
/// concern (decision 47), not the model's.
pub trait BusTransport: Send {
    /// The stations discovered during init, in bus position order.
    fn discovered(&self) -> &[DiscoveredStation];

    /// The bus's cyclic input image length in bytes: the sum of every
    /// discovered station's [`DiscoveredStation::input_bytes`].
    fn input_len(&self) -> usize {
        self.discovered()
            .iter()
            .map(|station| station.input_bytes)
            .sum()
    }

    /// The bus's cyclic output image length in bytes.
    fn output_len(&self) -> usize {
        self.discovered()
            .iter()
            .map(|station| station.output_bytes)
            .sum()
    }

    /// Transitions the verified bus PRE-OP → SAFE-OP → OP, enabling
    /// cyclic I/O. Called once per bus after the first attached device
    /// verifies; subsequent attaches verify against
    /// [`discovered`](Self::discovered) without re-entering.
    fn enter_op(&mut self) -> Result<(), TransportError>;

    /// Runs exactly one process-image exchange: publishes `outputs`
    /// (the whole staged output image, in station position order) and
    /// fills `inputs` (the whole latched input image, same order).
    ///
    /// On [`CycleOutcome::Short`], `inputs` ranges outside the degraded
    /// station hold the received data; the degraded ranges' contents are
    /// unspecified — the master does not latch them. On `Err` nothing
    /// was exchanged: both images are left untouched.
    fn exchange(
        &mut self,
        outputs: &[u8],
        inputs: &mut [u8],
    ) -> Result<CycleOutcome, TransportError>;

    /// Re-enters the cyclic state path — PRE-OP/SAFE-OP/OP — after a
    /// failed exchange. The master calls it at the next exchange
    /// boundary, never mid-scan.
    fn recover(&mut self) -> Result<(), TransportError>;

    /// The transport's own link read, when it has one beyond "the last
    /// exchange completed". The default derives link health from the
    /// exchange record: a transport that answered the last exchange is
    /// [`LinkState::Connected`].
    fn link(&self) -> LinkState {
        LinkState::Connected
    }
}

/// What an [`Opener`] receives to bind one logical bus: the interface
/// the deployment bound it to and the station identities registered on
/// the bus so far — the first attaching device's at open time. The
/// model declares identity per device, so the complete expected set is
/// unknowable when the bus opens; per-device identity and layout
/// verification happens at attach under the `fail` startup policy.
pub struct OpenRequest<'a> {
    /// The logical bus name from the device parameters.
    pub bus: &'a str,
    /// The host interface the deployment bound the bus to (e.g. a NIC
    /// name such as `enx00e04c751f7c`).
    pub interface: &'a str,
    /// The station identities registered on the bus so far, in attach
    /// order.
    pub expected: &'a [crate::params::StationIdentity],
}

/// The transport-construction seam: production uses
/// [`EthercrabTransport::open`](crate::EthercrabTransport::open), tests
/// substitute a fake. Called once per logical bus, when its first device
/// resolves.
pub type Opener =
    Arc<dyn Fn(&OpenRequest<'_>) -> Result<Box<dyn BusTransport>, TransportError> + Send + Sync>;
