//! The device-kind driver registry: model `Device.kind` strings to
//! backend factories, and the fan-out driver presenting one
//! [`IoDriver`] surface over several backends.
//!
//! [`resolve_drivers`] walks a model's devices, builds each through the
//! factory its kind resolves to in a [`DriverRegistry`], and returns a
//! [`DriverPlan`]: the shared local simulated channel map — still open
//! for caller-added [`ProcessElement`](dcs_sim::ProcessElement)s — plus
//! every self-contained backend and the cross-backend wires between
//! them. [`DriverPlan::build`] finishes the driver side as a
//! [`FanoutDriver`], which the executor then uses exactly like a single
//! driver.
//!
//! The built-in registry [`DriverRegistry::standard`] serves the
//! `sim*` prefix (local simulated devices), [`SIM_TCP_KIND`]
//! (`sim-tcp`, the remote simulated plant of `dcs-sim-net`),
//! [`SIM_BUS_KIND`] (`sim-bus`, the register-mapped simulated fieldbus
//! device of `dcs-sim-bus`), [`SIM_CYCLIC_KIND`] (`sim-cyclic`, the
//! same device server behind the cyclic process-image contract),
//! [`SIM_SCRIPTED_KIND`] (`sim-scripted`,
//! the tick-indexed playback driver of `dcs-sim`), and
//! [`ETHERCAT_KIND`] (`ethercat`, the hardware-bound field-bus contract
//! of `dcs-ethercat`). New device
//! integrations register their own kind against the same [`DeviceSpec`]
//! contract — registering a device integration is what "adding a new
//! device" means.

use crate::assembly::{neutral, resolve};
use crate::error::AssemblyError;
use dcs_core::{
    CyclicIoDriver, DriverDiagnostics, ExchangeDiagnostics, FieldClaim, IoDriver, IoError,
    LinkState, PointId, Quality, QualityReason, Sample, StateError, StateMap, Tick, Value,
    ValueKind,
};
use dcs_ethercat::{AttachError, BusPoint, ChannelDecl, EthercatBuses};
use dcs_model::{Channel, DeviceId, Direction, PlantModel};
use dcs_sim::{
    ChannelId, ChannelMap, Loopback, PointBinding, ScriptEntry, ScriptError, ScriptedDriver,
    SimDriver,
};
use dcs_sim_bus::{
    BusDriver, CyclicBusDriver, CyclicDeviceParameters, CyclicPoint, DeviceParameters,
    PointRegister,
};
use dcs_sim_net::{RemoteDriver, RemoteError};
use std::any::Any;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// The remote simulated device kind: a [`PlantServer`](dcs_sim_net::PlantServer)
/// plant reached over TCP through [`RemoteDriver`].
///
/// The kind is registered exactly — it outranks the `sim` prefix, which
/// would otherwise read it as a local simulated device. Its device
/// `parameters` carry the addressing the factory validates at assembly:
///
/// - `"address"` (required string): the plant server's `host:port`;
/// - `"timeout_ms"` (optional non-negative number): the per-request
///   timeout in milliseconds, defaulting to
///   [`RemoteDriver::DEFAULT_TIMEOUT`].
///
/// Any other parameter is rejected. The factory connects eagerly — an
/// unreachable endpoint or a server that does not serve a declared
/// point is an assembly failure, not a mid-scan surprise.
pub const SIM_TCP_KIND: &str = "sim-tcp";

/// The scripted simulated device kind: a [`ScriptedDriver`] whose `In`
/// channels replay a tick-indexed script and whose `Out` channels record
/// every write for inspection through [`FanoutDriver::inspect`].
///
/// The kind is registered exactly — like [`SIM_TCP_KIND`] it outranks the
/// `sim` prefix, which would otherwise read it as a local simulated
/// device. Its device `parameters` carry exactly one entry:
///
/// - `"script"` (required object): channel name → array of entries, each
///   `{"tick": <non-negative integer>, "value": <matching the channel's
///   value kind>, "quality": "good" | "uncertain" | "bad", "reason":
///   <a `snake_case` [`QualityReason`] name, optional>}` in strictly
///   increasing tick order. Only `in` channels may be scripted, and only
///   channels an `io_point` binds; an unscripted `in` channel holds its
///   neutral `Good` value. `reason` is meaningful only on a
///   non-`"good"` entry and defaults to `"unspecified"`.
///
/// A script problem — an unparseable entry, a value whose kind does not
/// match its channel, non-increasing ticks, or a script naming an `out`,
/// undeclared, or unbound channel — is [`DeviceError::Parameters`],
/// surfacing as [`AssemblyError::InvalidDeviceParameters`] naming the
/// device.
pub const SIM_SCRIPTED_KIND: &str = "sim-scripted";

/// The register-mapped simulated fieldbus kind: a
/// [`BusServer`](dcs_sim_bus::BusServer) register bank reached over TCP
/// through [`BusDriver`] — a second transport beside `sim-tcp`'s
/// line-JSON plant protocol, addressed by register index.
///
/// The kind is registered exactly — it outranks the `sim` prefix, which
/// would otherwise read it as a local simulated device. Its device
/// `parameters` carry the addressing and the channel→register map the
/// factory validates at assembly:
///
/// - `"address"` (required string): the device server's `host:port`;
/// - `"timeout_ms"` (optional non-negative number): the per-request
///   timeout in milliseconds, defaulting to
///   [`BusDriver::DEFAULT_TIMEOUT`];
/// - `"registers"` (required object): every declared channel's name
///   mapped to its register — a bare index (`"level-raw": 4`) or
///   `{"register": <u16>, "initial": <value>}`; the map must cover the
///   declared channel set exactly and share no register between
///   channels.
///
/// Any other parameter is rejected. The factory connects eagerly and
/// probes each mapped register — an unreachable endpoint, a register
/// the server does not hold, or a kind disagreement is an assembly
/// failure, not a mid-scan surprise. The full contract is
/// [`DeviceParameters`]'s; the `dcs-sim-bus-device` binary parses the
/// same declaration when it serves the device's registers, so a rig's
/// two ends cannot diverge.
pub const SIM_BUS_KIND: &str = dcs_sim_bus::DEVICE_KIND;

/// The cyclic-exchange simulated fieldbus kind: a
/// [`BusServer`](dcs_sim_bus::BusServer) register image reached over
/// TCP through [`CyclicBusDriver`], which implements the
/// [`CyclicIoDriver`] contract — `read`/`write` operate on the held
/// input and staged output images and never touch the wire, while one
/// exchange per scan publishes the staged outputs and latches the
/// answered register census.
///
/// The kind is registered exactly — it outranks the `sim` prefix,
/// which would otherwise read it as a local simulated device. Its
/// device `parameters` carry the addressing, the miss threshold, and
/// the station layout the factory validates at assembly:
///
/// - `"address"` (required string): the device server's `host:port`;
/// - `"timeout_ms"` (optional non-negative number): the per-request
///   timeout in milliseconds, defaulting to
///   [`CyclicBusDriver::DEFAULT_TIMEOUT`];
/// - `"exchange_miss_threshold"` (required positive integer): the
///   consecutive missed exchanges before the driver's reads escalate
///   to `IoError::Disconnected`;
/// - `"stations"` (required object): station name → channel name →
///   register declaration — the `"registers"` entry grammar
///   [`SIM_BUS_KIND`] uses — partitioning every declared channel into
///   the stations a short exchange's working counter attributes.
///
/// Any other parameter is rejected. The factory connects eagerly and
/// its connect-time census probes that the server holds every declared
/// register with the declared kind — an unreachable endpoint or a
/// mismatched register map is an assembly failure. The full contract
/// is [`CyclicDeviceParameters`]'s; the `dcs-sim-bus-device` binary
/// parses the same declaration — registers *and* station layout — when
/// it serves the device, so a scripted `short-station` outcome
/// withholds exactly the registers the driver attributes.
pub const SIM_CYCLIC_KIND: &str = dcs_sim_bus::CYCLIC_DEVICE_KIND;

/// The EtherCAT device kind: a hardware-bound cyclic field-bus device —
/// an EtherCAT coupler or remote-I/O station — declared through
/// `Device.hardware` and the kind's `parameters`.
///
/// The kind is *hardware-bound*: the device must carry `"hardware":
/// true`, and the factory rejects the declaration without it — the
/// marker is the model's evidence that no simulated backend may serve
/// the device. Its `parameters` declare the field-bus contract the
/// master validates at startup (the full grammar is
/// `dcs-ethercat`'s [`params`](dcs_ethercat) contract):
///
/// - `"bus"` (required string): the *logical* bus name — deployment
///   configuration binds it to a host interface outside the model
///   (decision 47); the document never names an interface;
/// - `"identity"` (required object): the expected station identity —
///   `{"vendor": <u32>, "product": <u32>, "revision": <u32>}`;
/// - `"mapping"` (required object): `{"inputs": {...}, "outputs":
///   {...}}` placing every declared channel in the image matching its
///   direction — `{"byte", "bit"}` for a `bool` channel, a byte-aligned
///   `{"byte", "bits"}` field for `int`/`float` — with no overlapping
///   bit ranges;
/// - `"exchange_miss_threshold"` (required integer ≥ 1): the cyclic
///   contract's `Disconnected` escalation threshold (decision 78);
/// - `"safe_outputs"` (object, required when the device declares `out`
///   channels): each `out` channel's declared safe state staged into
///   the output image before the first exchange;
/// - `"startup"` (required object): `{"on_mismatch": "fail"}` — the only
///   admitted policy: a station identity or layout mismatch is a hard
///   startup failure.
///
/// A malformed declaration is [`DeviceError::Parameters`], surfacing as
/// [`AssemblyError::InvalidDeviceParameters`]. Until the EtherCAT
/// master integration lands (the Lenovo HQ-4 lane), a well-formed
/// declaration still fails assembly — [`DeviceError::Backend`] — since
/// no bus can be initialized: a hardware-bound kind is never silently
/// substituted by simulation.
pub const ETHERCAT_KIND: &str = dcs_ethercat::DEVICE_KIND;

/// One `io_point` bound to a channel on the device under construction.
#[derive(Debug, Clone)]
pub struct DevicePoint {
    /// The logical point.
    pub point: PointId,
    /// The bound channel's name on the device.
    pub channel: String,
    /// The point's declared direction — the model guarantees it agrees
    /// with the channel's.
    pub direction: Direction,
    /// The point's declared value kind — likewise guaranteed to agree
    /// with the channel's.
    pub kind: ValueKind,
}

/// What a registered device-kind factory receives to build one model
/// device: the declared channels, the kind's addressing parameters, and
/// the `io_point`s bound to those channels.
pub struct DeviceSpec<'m> {
    /// The model device's id.
    pub id: DeviceId,
    /// The kind string the model declares — under a prefix registration,
    /// the device's actual kind, not the prefix it matched.
    pub kind: &'m str,
    /// The model's `hardware` marker: `true` declares the device
    /// hardware-bound. The factory owns the marker's meaning for its
    /// kind — a hardware-bound kind requires it, a simulated kind
    /// rejects it, so the flag stays honest evidence rather than a hint
    /// a backend can ignore.
    pub hardware: bool,
    /// The device's kind-specific parameters, as declared in the model.
    pub parameters: &'m BTreeMap<String, serde_json::Value>,
    /// The channels the model declares on the device.
    pub channels: &'m BTreeMap<String, Channel>,
    /// The `io_point`s bound to this device's channels, in model order.
    /// The factory's backend must serve exactly these points.
    pub points: Vec<DevicePoint>,
}

/// Why a registered factory could not build its device — the vocabulary
/// [`resolve_drivers`] translates into [`AssemblyError`] variants naming
/// the device.
#[derive(Debug)]
pub enum DeviceError {
    /// The device's kind-specific parameters are missing, mistyped, or
    /// malformed — e.g. a `sim-tcp` device without a usable `address`.
    /// Surfaces as [`AssemblyError::InvalidDeviceParameters`].
    Parameters(String),
    /// The backend the kind integrates could not be built or does not
    /// serve the device's declared points — e.g. a `sim-tcp` endpoint
    /// that refused the connection. Surfaces as
    /// [`AssemblyError::DeviceBackend`].
    Backend(String),
}

impl DeviceError {
    /// A [`DeviceError::Parameters`] with the given detail.
    pub fn parameters(detail: impl Into<String>) -> Self {
        Self::Parameters(detail.into())
    }

    /// A [`DeviceError::Backend`] with the given detail.
    pub fn backend(detail: impl Into<String>) -> Self {
        Self::Backend(detail.into())
    }
}

impl fmt::Display for DeviceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Parameters(detail) => write!(f, "invalid device parameters: {detail}"),
            Self::Backend(detail) => write!(f, "device backend unusable: {detail}"),
        }
    }
}

impl std::error::Error for DeviceError {}

/// Why [`FanoutDriver::step`] or a backend's [`StepHook`] failed.
#[derive(Debug, Clone, PartialEq)]
pub enum StepError {
    /// `dt` was negative or non-finite.
    InvalidDt(f64),
    /// A backend's step hook failed; `backend` names the model device or
    /// the shared local simulated backend.
    Backend {
        /// The failing backend's name.
        backend: String,
        /// The backend's own error, formatted.
        detail: String,
    },
    /// A cross-backend wire's read or write failed — the [`IoError`] the
    /// owning backend reported, at the point it names.
    Route(IoError),
}

impl fmt::Display for StepError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidDt(dt) => {
                write!(f, "step dt must be finite and non-negative, got {dt}")
            }
            Self::Backend { backend, detail } => {
                write!(f, "backend {backend} failed to step: {detail}")
            }
            Self::Route(error) => write!(f, "cross-backend wire failed: {error}"),
        }
    }
}

impl std::error::Error for StepError {}

/// Advances a backend's simulated plant one step of `dt` — the
/// per-backend half of [`FanoutDriver::step`].
///
/// Simulated backends carry a hook; field-observing kinds (a real
/// fieldbus advances itself) leave it `None`.
pub type StepHook = Arc<dyn Fn(f64) -> Result<Tick, StepError> + Send + Sync>;

/// Takes the backend's field write-ownership for `owner` — the
/// per-backend half of [`FanoutDriver::claim_field_writer`]: the
/// single-writer claim the failover fencing has a promoted standby take
/// out on the old field owner. `owner` is an opaque token one field
/// owner's several backends share, so all of them keep writing after
/// the claim.
pub type ClaimHook = Arc<dyn Fn(u64) -> Result<(), StepError> + Send + Sync>;

/// Forgets the backend's recorded field write-ownership — the
/// per-backend half of [`FanoutDriver::release_field_claims`], run when
/// this peer demotes: an attachment that gave the field up must not
/// re-assert a stale claim when a re-attach finds the field's
/// arbitration reset. `None` on kinds whose claim bookkeeping needs no
/// forgetting — e.g. `sim-bus`, where a claim dies with its connection.
pub type ReleaseHook = Arc<dyn Fn() + Send + Sync>;

/// The conditional counterpart of [`ClaimHook`] — the per-backend half
/// of [`FanoutDriver::ensure_field_writer`], run while the tracked
/// line reports no field owner: re-arms the claim under `owner` only
/// where the field stands unclaimed or already names the token —
/// `Ok(true)` — refusing `Ok(false)` where a different owner stands,
/// so a demoted ex-owner's released claim re-arms once the field frees
/// and no probe ever preempts a standing owner. `Err` reports the
/// backend could not be asked. `None` on kinds whose arbitration
/// cannot be probed conditionally.
pub type EnsureHook = Arc<dyn Fn(u64) -> Result<bool, StepError> + Send + Sync>;

/// The fencing-loss counterpart of [`EnsureHook`] — the per-backend
/// half of [`FanoutDriver::reclaim_field_writer`], run while a
/// fencing-demoted ex-owner's loss mark stands: re-takes the claim
/// under `owner` only where the field stands unclaimed or already
/// names the token — `Ok(true)` — refusing `Ok(false)` where a
/// different owner stands, so a released preemption ends with the
/// ex-owner holding the claim again and no probe ever preempts.
/// Unlike `ensure` the grant is *bound*: the reclaiming attachment
/// joins the claim's holders, because the peer's gate lifts on success
/// and its writes must pass the claim it just took back. `Err`
/// reports the backend could not be asked. `None` on kinds whose
/// arbitration has no bound conditional grant.
pub type ReclaimHook = Arc<dyn Fn(u64) -> Result<bool, StepError> + Send + Sync>;

/// The claimant-attribution counterpart of [`ProbeHook`] — the
/// per-backend half of [`FanoutDriver::fencing_claimant`]: reports the
/// owner token the field's standing claim named the last time it
/// fenced one of this backend's mutations, so a superseded field
/// owner's `field_claim_lost` journal record names the preempting
/// claimant rather than an anonymous "another". `None` answers mean
/// the verdict carried no claimant identity — no fenced answer
/// recorded yet, or a backend whose arbitration names no owner.
pub type FencedByHook = Arc<dyn Fn() -> Option<u64> + Send + Sync>;

/// The checkpoint-endpoint registration half of [`ClaimHook`] — the
/// per-backend half of [`FanoutDriver::set_claim_endpoint`]: declares
/// the port this instance's checkpoint monitor listens on, so every
/// write-ownership claim the backend asserts or re-arms carries it
/// and the field's claim record names where the claim's owner serves
/// checkpoints. `None` on kinds whose arbitration records no claimant
/// endpoint.
pub type ClaimEndpointHook = Arc<dyn Fn(u16) + Send + Sync>;

/// The claimant-endpoint counterpart of [`FencedByHook`] — reports the
/// checkpoint endpoint the field's arbitration carried on the last
/// verdict that fenced this backend's request: the address the
/// standing claim's owner itself registered for its monitor, attested
/// by the field rather than announced by the peer — the
/// field-attested tracking source a fencing-demoted ex-owner on an
/// unkeyed pair may follow where an announced hint cannot be trusted.
/// `None` answers mean the verdict carried no endpoint — no fenced
/// answer recorded yet, the field's last claim named no owner, the
/// claim's owner registered no monitor, or a backend whose
/// arbitration carries no endpoint.
pub type OwnerEndpointHook = Arc<dyn Fn() -> Option<SocketAddr> + Send + Sync>;

/// The launched-controller counterpart of [`ClaimHook`] — the
/// per-backend half of [`FanoutDriver::claim_field_writer_unless_held`],
/// run once at startup activation: claims the field's write-ownership
/// under `owner` only while no *live* attachment holds a different
/// owner's claim — `Ok(true)` — answering `Ok(false)` where a live
/// incumbent stands. A restarted controller cannot prove its resumed
/// state is current with that incumbent's — a stale `--state-file`
/// would silently roll back commands the incumbent receipted and
/// applied — so the startup refuses rather than preempting. A claim a
/// dead owner left standing — holders gone — is still preempted, the
/// restart-as-active recovery path. `Err` reports the backend could
/// not be asked. `None` on kinds whose arbitration cannot distinguish
/// live holders — for them the field falls back to the unconditional
/// [`ClaimHook`].
pub type StartupClaimHook = Arc<dyn Fn(u64) -> Result<bool, StepError> + Send + Sync>;

/// The read-only half of [`ClaimHook`] — the per-backend half of
/// [`FanoutDriver::probe_field_claim`]: reports the verdict a mutation
/// from this attachment would meet, without mutating —
/// [`FieldClaim::Held`] while an owner stands (this attachment's own
/// hold or a standing owner's, which the report need not distinguish),
/// [`FieldClaim::Unclaimed`] while no claim stands. `Err` reports the
/// backend could not be asked — no observation, so the last one
/// stands. The probe asserts, joins, and releases nothing: an
/// observation cannot seize the field it reports. `None` on kinds
/// whose arbitration cannot be observed without taking it.
pub type ProbeHook = Arc<dyn Fn() -> Result<FieldClaim, StepError> + Send + Sync>;

/// A self-contained device backend: the point-facing driver plus the
/// step hook advancing its simulated plant, when it has one.
pub struct DeviceBackend {
    /// The backend's [`IoDriver`] surface — the fan-out routes the
    /// device's points here.
    pub io: Arc<dyn IoDriver + Send + Sync>,
    /// Steps the backend's simulated plant one `dt`; `None` for
    /// field-observing backends.
    pub step: Option<StepHook>,
    /// Claims the backend's field write-ownership — meaningful only on a
    /// `field_facing` backend; `None` when the field kind cannot
    /// arbitrate a single writer, which keeps automatic failover off
    /// for models built on it.
    pub claim: Option<ClaimHook>,
    /// Forgets the backend's recorded write-ownership claim — the
    /// demotion counterpart of `claim`; `None` when the backend records
    /// no claim state a released owner could wrongly re-assert.
    pub release: Option<ReleaseHook>,
    /// The conditional claim re-arm — the orphan-cycle probe a demoted
    /// ex-owner runs while the tracked line reports no field owner:
    /// granted only while the field is unclaimed or already names the
    /// token, never preempting a standing owner. `None` on kinds whose
    /// arbitration cannot be probed conditionally.
    pub ensure: Option<EnsureHook>,
    /// The startup counterpart of `claim` — the conditional grant a
    /// launched controller's activation asserts: takes the claim only
    /// where no *live* attachment holds a different owner's, refusing
    /// (`Ok(false)`) while a live incumbent stands so a stale restart
    /// cannot preempt it. `None` on kinds whose arbitration cannot
    /// distinguish live holders; [`FanoutDriver`] falls back to the
    /// unconditional `claim` for them.
    pub startup_claim: Option<StartupClaimHook>,
    /// The claim's observational counterpart — the read-only probe a
    /// peer runs once per scan to learn the field's write-ownership
    /// state without disturbing it: `held` while an owner stands,
    /// `unclaimed` while none does. `None` on kinds whose arbitration
    /// cannot be observed without taking it — for them the served
    /// report carries no claim observation rather than a guessed one.
    pub probe: Option<ProbeHook>,
    /// The fencing-loss reclaim — the bound conditional re-grant a
    /// fencing-demoted ex-owner probes each scan while its loss mark
    /// stands: granted while the field is unclaimed or already names
    /// the token, refused while a different owner stands, never
    /// preempting. `None` on kinds whose arbitration has no bound
    /// conditional grant.
    pub reclaim: Option<ReclaimHook>,
    /// The claimant-attribution counterpart of `probe` — reports the
    /// owner token the field's arbitration named when it last fenced
    /// this backend's mutation, so the superseded owner's
    /// `field_claim_lost` journal entry can name the preempting
    /// claimant. `None` on kinds whose fencing verdicts carry no
    /// claimant identity.
    pub fenced_by: Option<FencedByHook>,
    /// Registers the checkpoint-monitor port this instance declares on
    /// its write-ownership claims — the claim record the field's
    /// fencing verdicts report a successor's endpoint through. `None`
    /// on kinds whose arbitration records no claimant endpoint.
    pub claim_endpoint: Option<ClaimEndpointHook>,
    /// The claimant-endpoint counterpart of `fenced_by` — reports the
    /// checkpoint endpoint the field's arbitration carried on the last
    /// verdict fencing this backend, the field-attested tracking
    /// source a fencing-demoted ex-owner may follow. `None` on kinds
    /// whose fencing verdicts carry no endpoint.
    pub owner_endpoint: Option<OwnerEndpointHook>,
    /// The backend's concrete driver, for typed inspection through
    /// [`FanoutDriver::inspect`] — e.g. a scripted device's
    /// recorded-write log. `None` when the backend exposes nothing
    /// beyond the [`IoDriver`] surface.
    pub inspect: Option<Arc<dyn Any + Send + Sync>>,
    /// Whether the backend reaches the shared field — a remote or real
    /// field kind every redundant peer attaches to (`sim-tcp` is, the
    /// local simulated kinds are not). A tracking peer's write gate
    /// quiesces writes to field-facing points, and
    /// [`FanoutDriver::step_local`] leaves a field-facing backend's
    /// plant clock to the peer owning the field.
    pub field_facing: bool,
}

/// What a registered device-kind factory contributes for its device.
pub enum DeviceDriver {
    /// The device's points join the shared local simulated channel map.
    ///
    /// The fragment merges with every other `Sim` contribution and the
    /// intra-map wires into one [`ChannelMap`] — the
    /// [`DriverPlan::sim_map`] seam stays open for caller-added process
    /// elements before [`DriverPlan::build`] turns it into a single
    /// [`SimDriver`].
    Sim(ChannelMap),
    /// A self-contained backend serving the device's points — remote or
    /// real field kinds.
    Backend(DeviceBackend),
}

/// A registered device kind's factory.
type Factory = Box<dyn Fn(&DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError>>;

/// Maps plant-model `Device.kind` strings to driver factories.
///
/// Registration is explicit: [`resolve_drivers`] reports
/// [`AssemblyError::UnknownDeviceKind`] for a kind no factory serves,
/// before any scan runs. Lookup tries an exact kind match first, then
/// registered prefixes in registration order — so `standard`'s exact
/// [`SIM_TCP_KIND`] wins over its `sim` prefix. `dcs-assembly` ships no
/// kinds of its own beyond what [`standard`](Self::standard) installs;
/// a deployment registers the device integrations it supports, keeping
/// the crate independent of any particular backend set.
#[derive(Default)]
pub struct DriverRegistry {
    kinds: BTreeMap<String, Factory>,
    prefixes: Vec<(String, Factory)>,
}

impl DriverRegistry {
    /// An empty registry.
    pub fn new() -> Self {
        Self::default()
    }

    /// The built-in registry: the `sim*` prefix served by local
    /// simulated devices, [`SIM_TCP_KIND`] (`sim-tcp`) served by the
    /// remote simulated driver, [`SIM_BUS_KIND`] (`sim-bus`) served by
    /// the register-mapped fieldbus driver, [`SIM_CYCLIC_KIND`]
    /// (`sim-cyclic`) served by the cyclic register-image driver,
    /// [`SIM_SCRIPTED_KIND`]
    /// (`sim-scripted`) served by the scripted playback driver, and
    /// [`ETHERCAT_KIND`] (`ethercat`) served by the hardware-bound
    /// field-bus contract.
    pub fn standard() -> Self {
        Self::new()
            .with(SIM_TCP_KIND, sim_tcp_device)
            .with(SIM_BUS_KIND, sim_bus_device)
            .with(SIM_CYCLIC_KIND, sim_cyclic_device)
            .with(SIM_SCRIPTED_KIND, scripted_device)
            .with(ETHERCAT_KIND, ethercat_device)
            .with_prefix(crate::SIM_DEVICE_PREFIX, sim_device)
    }

    /// Registers `factory` under `kind` and returns the registry, for
    /// chained registration.
    pub fn with<F>(mut self, kind: impl Into<String>, factory: F) -> Self
    where
        F: Fn(&DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> + 'static,
    {
        self.register(kind, factory);
        self
    }

    /// Registers `factory` under `kind`; a repeated kind replaces the
    /// earlier factory.
    pub fn register<F>(&mut self, kind: impl Into<String>, factory: F) -> &mut Self
    where
        F: Fn(&DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> + 'static,
    {
        self.kinds.insert(kind.into(), Box::new(factory));
        self
    }

    /// Registers `factory` for every kind starting with `prefix` —
    /// families like `sim` whose members (`sim-ai`, `sim-ao`, …) share
    /// one integration — and returns the registry.
    pub fn with_prefix<F>(mut self, prefix: impl Into<String>, factory: F) -> Self
    where
        F: Fn(&DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> + 'static,
    {
        self.register_prefix(prefix, factory);
        self
    }

    /// Registers `factory` for kinds starting with `prefix`; a repeated
    /// prefix replaces the earlier factory. Exact kind registrations
    /// always win over prefix matches.
    pub fn register_prefix<F>(&mut self, prefix: impl Into<String>, factory: F) -> &mut Self
    where
        F: Fn(&DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> + 'static,
    {
        let prefix = prefix.into();
        self.prefixes
            .retain(|(registered, _)| *registered != prefix);
        self.prefixes.push((prefix, Box::new(factory)));
        self
    }

    /// Binds [`ETHERCAT_KIND`] to this deployment's EtherCAT buses —
    /// replaces the validating stub [`standard`](Self::standard)
    /// installs. `buses` carries the deployment's logical-bus →
    /// host-interface bindings (the model names the bus, the deployment
    /// names the NIC); a deployment without EtherCAT hardware keeps the
    /// stub and its honest startup failure.
    pub fn with_ethercat_buses(mut self, buses: &EthercatBuses) -> Self {
        let buses = buses.clone();
        self.register(ETHERCAT_KIND, move |spec| ethercat_backend(spec, &buses));
        self
    }

    /// The factory serving `kind`: the exact registration, else the
    /// first matching prefix in registration order.
    fn factory(&self, kind: &str) -> Option<&Factory> {
        self.kinds.get(kind).or_else(|| {
            self.prefixes
                .iter()
                .find(|(prefix, _)| kind.starts_with(prefix.as_str()))
                .map(|(_, factory)| factory)
        })
    }
}

/// The simulated kinds reject the `hardware` marker: it declares a
/// hardware-bound kind a simulated factory cannot serve, so a model
/// carrying it on a `sim*` device is an assembly error, keeping the
/// marker honest in both directions.
fn require_simulated(spec: &DeviceSpec<'_>) -> Result<(), DeviceError> {
    if spec.hardware {
        return Err(DeviceError::parameters(format!(
            "the {:?} kind is simulated; \"hardware\": true declares a hardware-bound device",
            spec.kind
        )));
    }
    Ok(())
}

/// The local simulated device factory: every bound `io_point` becomes a
/// [`PointBinding`] in a [`ChannelMap`] fragment carrying the device's
/// id. The kind takes no parameters — a `sim*` device declaring any is
/// [`DeviceError::Parameters`].
pub(crate) fn sim_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    require_simulated(spec)?;
    if let Some(unknown) = spec.parameters.keys().next() {
        return Err(DeviceError::parameters(format!(
            "the {:?} kind takes no parameters, found {unknown:?}",
            spec.kind
        )));
    }
    let mut map = ChannelMap::new();
    for point in &spec.points {
        map = map.with_point(PointBinding {
            point: point.point,
            channel: ChannelId {
                device: spec.id.0,
                name: point.channel.clone(),
            },
            direction: point.direction,
            initial: neutral(point.kind),
        });
    }
    Ok(DeviceDriver::Sim(map))
}

/// The [`SIM_TCP_KIND`] factory: validates the addressing parameters,
/// connects to the plant server, and probes that it serves every
/// declared point with the declared value kind.
fn sim_tcp_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    require_simulated(spec)?;
    for name in spec.parameters.keys() {
        if name != "address" && name != "timeout_ms" {
            return Err(DeviceError::parameters(format!(
                "unknown parameter {name:?}; {SIM_TCP_KIND:?} takes \"address\" and \"timeout_ms\""
            )));
        }
    }
    let address = spec
        .parameters
        .get("address")
        .ok_or_else(|| DeviceError::parameters("parameter \"address\" is required"))?;
    let Some(address) = address.as_str() else {
        return Err(DeviceError::parameters(format!(
            "parameter \"address\" must be a string, found {address}"
        )));
    };
    let addresses: Vec<_> = address
        .to_socket_addrs()
        .map_err(|error| {
            DeviceError::parameters(format!("parameter \"address\": {address:?} does not resolve to a host:port address ({error})"))
        })?
        .collect();
    if addresses.is_empty() {
        return Err(DeviceError::parameters(format!(
            "parameter \"address\": {address:?} resolved to no address"
        )));
    }
    let timeout = match spec.parameters.get("timeout_ms") {
        None => RemoteDriver::DEFAULT_TIMEOUT,
        Some(value) => {
            let milliseconds = value.as_f64().filter(|ms| {
                ms.is_finite() && ms.fract() == 0.0 && *ms >= 0.0 && *ms <= u64::MAX as f64
            });
            match milliseconds {
                Some(ms) => Duration::from_millis(ms as u64),
                None => {
                    return Err(DeviceError::parameters(format!(
                        "parameter \"timeout_ms\" must be a non-negative integral number of milliseconds, found {value}"
                    )));
                }
            }
        }
    };
    let remote = RemoteDriver::connect_with_timeout(addresses.as_slice(), timeout)
        .map_err(|error| {
            DeviceError::backend(format!(
                "cannot connect to plant server at {address:?}: {error}"
            ))
        })?
        // A controller's device backend claims the field as a
        // controller: the claim records the marker, so a peer's
        // conditional takeover refuses to preempt it while the owner
        // stays attached — where a tool attachment's claim never
        // blocks that recovery.
        .as_controller();
    // Probe every declared point: the remote plant must serve it, with
    // the value kind the model declares — a plant configured for a
    // different model fails here, at assembly, not mid-scan.
    for point in &spec.points {
        let sample = remote.read(point.point).map_err(|error| {
            DeviceError::backend(format!(
                "plant server at {address:?} does not serve io point {}: {error}",
                point.point.0
            ))
        })?;
        if sample.value.kind() != point.kind {
            return Err(DeviceError::backend(format!(
                "plant server at {address:?} serves io point {} as {:?}, model declares {:?}",
                point.point.0,
                sample.value.kind(),
                point.kind
            )));
        }
    }
    let remote = Arc::new(remote);
    let stepping = Arc::clone(&remote);
    let claiming = Arc::clone(&remote);
    let releasing = Arc::clone(&remote);
    let ensuring = Arc::clone(&remote);
    let starting = Arc::clone(&remote);
    let probing = Arc::clone(&remote);
    let reclaiming = Arc::clone(&remote);
    let attributing = Arc::clone(&remote);
    let registering = Arc::clone(&remote);
    let reporting = Arc::clone(&remote);
    let inspect: Arc<dyn Any + Send + Sync> = remote.clone();
    let device = spec.id.0;
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: remote,
        step: Some(Arc::new(move |dt| {
            stepping.step(dt).map_err(|error| StepError::Backend {
                backend: format!("device {device}"),
                detail: error.to_string(),
            })
        })),
        // The plant server's single-writer claim — the fencing a
        // promoted peer takes out on the old field owner. A grant
        // flagged `Shared` still holds: one owner's several sim-tcp
        // devices on one plant claim the same token by design, so the
        // flag is the claimer's to heed, not the hook's to refuse.
        claim: Some(Arc::new(move |owner| {
            claiming
                .claim_writer(owner)
                .map(|_| ())
                .map_err(|error| StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                })
        })),
        // The claim's demotion counterpart: the attachment drops its
        // hold on the field's claim and forgets its recorded owner —
        // the claim itself stays standing, marked yielded, so the
        // field never opens an unclaimed window and a successor's
        // conditional claim can still tell the step-down from a live
        // incumbent's. Best-effort: a dead plant drops the connection
        // — and the hold with it — anyway.
        release: Some(Arc::new(move || {
            let _ = releasing.release_writer_keep_claim();
        })),
        // The claim's orphan-cycle counterpart: the plant server's
        // conditional `ensure_writer` grant in its unbound shape — a
        // demoted ex-owner's probe keeps the released claim standing
        // for the token while the field stands unclaimed or already
        // names it, `Fenced` while a different owner stands, and never
        // joins the holders: the probing attachment must not read as a
        // live incumbent to another owner's conditional claim.
        ensure: Some(Arc::new(move |owner| {
            match ensuring.ensure_writer_unbound(owner) {
                Ok(()) => Ok(true),
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                }),
            }
        })),
        // The claim's startup counterpart: the plant server's
        // conditional `claim_writer_unless_held` grant — a launched
        // controller takes the field from a dead owner's standing
        // claim but never from a live incumbent, whose newer state a
        // stale restart would silently roll back.
        startup_claim: Some(Arc::new(move |owner| {
            match starting.claim_writer_unless_held(owner) {
                Ok(_) => Ok(true),
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                }),
            }
        })),
        // The claim's observational counterpart: the plant server's
        // `probe_writer` — the verdict a mutation from this attachment
        // would meet, without mutating. An observation cannot seize
        // the field it reports, so a peer may ask every scan.
        probe: Some(Arc::new(move || {
            probing.probe_writer().map_err(|error| StepError::Backend {
                backend: format!("device {device}"),
                detail: error.to_string(),
            })
        })),
        // The fencing-loss reclaim: the plant server's *bound*
        // `ensure_writer` — a fencing-demoted ex-owner takes its claim
        // back once the field stands unclaimed or already names its
        // token, the grant joining this attachment to the holders so
        // the re-lifted gate's writes pass the claim it re-took. A
        // different owner's claim refuses it whether held or standing
        // holderless — the reclaim never preempts, so a foreign claim
        // that outlives its attachment still wedges the pair as an
        // operator-promotable state rather than silently handing the
        // field back over a live arbitration.
        reclaim: Some(Arc::new(move |owner| {
            match reclaiming.ensure_writer(owner) {
                Ok(_) => Ok(true),
                Err(RemoteError::Fenced) => Ok(false),
                Err(error) => Err(StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                }),
            }
        })),
        // The claimant attribution: the owner token the plant server's
        // last fencing verdict named for this attachment — the
        // claimant a superseded field owner's `field_claim_lost`
        // journal entry attributes the preemption to.
        fenced_by: Some(Arc::new(move || attributing.fenced_by())),
        // The claim's endpoint registration: the checkpoint-monitor
        // port this instance declares on every claim it asserts or
        // re-arms, so the field's claim record names where its owner
        // serves checkpoints.
        claim_endpoint: Some(Arc::new(move |port| registering.set_claim_endpoint(port))),
        // The claimant-endpoint attribution: the checkpoint endpoint
        // the plant server's last fencing verdict carried for this
        // attachment — the field-attested tracking source a
        // fencing-demoted ex-owner follows to its successor.
        owner_endpoint: Some(Arc::new(move || reporting.fenced_endpoint())),
        inspect: Some(inspect),
        field_facing: true,
    }))
}

/// The [`SIM_BUS_KIND`] factory: validates the addressing and register
/// map against the declared channels, connects to the device server,
/// and probes that it serves every mapped register with the declared
/// value kind.
fn sim_bus_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    require_simulated(spec)?;
    let channels: BTreeMap<String, ValueKind> = spec
        .channels
        .iter()
        .map(|(name, channel)| (name.clone(), channel.value_type))
        .collect();
    let parameters =
        DeviceParameters::parse(spec.parameters, &channels).map_err(DeviceError::parameters)?;
    let address = parameters.address.as_str();
    let addresses: Vec<_> = address
        .to_socket_addrs()
        .map_err(|error| {
            DeviceError::parameters(format!("parameter \"address\": {address:?} does not resolve to a host:port address ({error})"))
        })?
        .collect();
    if addresses.is_empty() {
        return Err(DeviceError::parameters(format!(
            "parameter \"address\": {address:?} resolved to no address"
        )));
    }
    // Point → register bindings: the parsed register map covers every
    // declared channel, so each bound point resolves.
    let bindings: Vec<PointRegister> = spec
        .points
        .iter()
        .map(|point| PointRegister {
            point: point.point,
            register: parameters.registers[point.channel.as_str()].register,
            kind: point.kind,
        })
        .collect();
    let bus = BusDriver::connect_with_timeout(addresses.as_slice(), parameters.timeout, &bindings)
        .map_err(|error| {
            DeviceError::backend(format!(
                "cannot connect to device server at {address:?}: {error}"
            ))
        })?;
    // Probe every declared point: the remote device must serve its
    // register with the value kind the model declares — a server
    // configured for a different register map fails here, at assembly,
    // not mid-scan.
    for point in &spec.points {
        let sample = bus.read(point.point).map_err(|error| {
            DeviceError::backend(format!(
                "device server at {address:?} does not serve io point {}: {error}",
                point.point.0
            ))
        })?;
        if sample.value.kind() != point.kind {
            return Err(DeviceError::backend(format!(
                "device server at {address:?} serves io point {} as {:?}, model declares {:?}",
                point.point.0,
                sample.value.kind(),
                point.kind
            )));
        }
    }
    let bus = Arc::new(bus);
    let stepping = Arc::clone(&bus);
    let claiming = Arc::clone(&bus);
    let inspect: Arc<dyn Any + Send + Sync> = bus.clone();
    let device = spec.id.0;
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: bus,
        step: Some(Arc::new(move |dt| {
            stepping.step(dt).map_err(|error| StepError::Backend {
                backend: format!("device {device}"),
                detail: error.to_string(),
            })
        })),
        // The device server's single-writer claim — the fencing a
        // promoted peer takes out on the old field owner.
        claim: Some(Arc::new(move |owner| {
            claiming
                .claim_writer(owner)
                .map_err(|error| StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                })
        })),
        // The device claim dies with its connection, so a re-attach
        // never re-asserts it — there is nothing to forget.
        release: None,
        // The device's claim protocol has no conditional grant — and
        // needs none: the claim dying with its connection frees the
        // field on the peer's death, so no dead token ever fences it.
        ensure: None,
        // The same protocol has no live-holder query the startup
        // claim could consult; the startup path falls back to the
        // unconditional grant.
        startup_claim: None,
        // Nor a read-only claim observation — the device claim binds
        // to the connection, so "unclaimed" never outlives a holder's
        // link and there is no probe to ask.
        probe: None,
        // No bound conditional grant either — a claim that dies with
        // its connection needs no reclaim path — and the device's
        // fencing verdict names no claimant.
        reclaim: None,
        fenced_by: None,
        claim_endpoint: None,
        owner_endpoint: None,
        inspect: Some(inspect),
        field_facing: true,
    }))
}

/// The [`SIM_CYCLIC_KIND`] factory: validates the addressing, miss
/// threshold, and station layout against the declared channels, then
/// connects to the device server — whose connect-time census already
/// probes that every declared register exists with the declared kind —
/// and returns the backend carrying the cyclic surface.
///
/// The backend is `field_facing` like `sim-bus`'s — the shared register
/// image is the field every redundant peer attaches to — with the same
/// step hook (the bank's logical tick still advances only on the
/// explicit `step` request; the exchange moves values, not time) and
/// the same writer claim, which a promoted peer takes out on the old
/// field owner: a fenced attachment's staged outputs never publish,
/// while its census-only exchanges — the tracking standby's — still
/// latch fresh inputs.
fn sim_cyclic_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    require_simulated(spec)?;
    let channels: BTreeMap<String, ValueKind> = spec
        .channels
        .iter()
        .map(|(name, channel)| (name.clone(), channel.value_type))
        .collect();
    let parameters = CyclicDeviceParameters::parse(spec.parameters, &channels)
        .map_err(DeviceError::parameters)?;
    let address = parameters.address.as_str();
    let addresses: Vec<_> = address
        .to_socket_addrs()
        .map_err(|error| {
            DeviceError::parameters(format!("parameter \"address\": {address:?} does not resolve to a host:port address ({error})"))
        })?
        .collect();
    if addresses.is_empty() {
        return Err(DeviceError::parameters(format!(
            "parameter \"address\": {address:?} resolved to no address"
        )));
    }
    // Point → image slots: the parsed station layout places every
    // declared channel, so each bound point resolves to its register.
    let points: Vec<CyclicPoint> = spec
        .points
        .iter()
        .map(|point| {
            let (_, declaration) = parameters
                .channel_register(point.channel.as_str())
                .expect("the parsed station layout places every declared channel");
            CyclicPoint {
                point: point.point,
                register: declaration.register,
                direction: point.direction,
                kind: point.kind,
            }
        })
        .collect();
    let bus = CyclicBusDriver::connect_with_timeout(
        addresses.as_slice(),
        parameters.timeout,
        &points,
        &parameters.station_registers(),
        parameters.exchange_miss_threshold,
    )
    .map_err(|error| {
        DeviceError::backend(format!(
            "cannot connect to device server at {address:?}: {error}"
        ))
    })?;
    let bus = Arc::new(bus);
    let stepping = Arc::clone(&bus);
    let claiming = Arc::clone(&bus);
    let inspect: Arc<dyn Any + Send + Sync> = bus.clone();
    let device = spec.id.0;
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: bus,
        step: Some(Arc::new(move |dt| {
            stepping.step(dt).map_err(|error| StepError::Backend {
                backend: format!("device {device}"),
                detail: error.to_string(),
            })
        })),
        // The device server's single-writer claim — the fencing a
        // promoted peer takes out on the old field owner.
        claim: Some(Arc::new(move |owner| {
            claiming
                .claim_writer(owner)
                .map_err(|error| StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                })
        })),
        // As `sim-bus`: the claim is bound to the connection, so a
        // re-attach carries no stale claim to forget.
        release: None,
        // As `sim-bus`: no conditional grant exists — or is needed,
        // the claim dying with its connection.
        ensure: None,
        // As `sim-bus`: no live-holder query for the startup claim;
        // the startup path falls back to the unconditional grant.
        startup_claim: None,
        // As `sim-bus`: no read-only claim observation either.
        probe: None,
        // As `sim-bus`: no bound conditional grant and no claimant
        // attribution — the claim dies with its connection.
        reclaim: None,
        fenced_by: None,
        // As `sim-bus`: no claimant endpoint registration or report.
        claim_endpoint: None,
        owner_endpoint: None,
        inspect: Some(inspect),
        field_facing: true,
    }))
}

/// The [`ETHERCAT_KIND`] factory: validates the field-bus declaration —
/// the `hardware` marker plus the `dcs-ethercat` parameter grammar —
/// then fails the build because this image carries no EtherCAT master.
///
/// Both halves are deliberate: a model declaring a hardware kind must
/// fail startup when the hardware cannot initialize (no silent
/// simulation fallback), and a declaration's shape must be a named
/// parameter error before that backend check is even reached — exactly
/// what the master integration's own startup sequence will enforce
/// against the answering station's identity and layout.
fn ethercat_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    let declaration = ethercat_declaration(spec)?;
    Err(DeviceError::backend(format!(
        "logical bus {:?} cannot initialize: no EtherCAT master is available in this build — \
         a hardware-bound kind is never silently substituted by simulation",
        declaration.bus
    )))
}

/// The [`ETHERCAT_KIND`] validation both factories share: the
/// `hardware` marker plus the `dcs-ethercat` parameter grammar against
/// the device's declared channels — a declaration's shape is a named
/// parameter error before any backend check is reached.
fn ethercat_declaration(
    spec: &DeviceSpec<'_>,
) -> Result<dcs_ethercat::DeviceParameters, DeviceError> {
    if !spec.hardware {
        return Err(DeviceError::parameters(format!(
            "the {ETHERCAT_KIND:?} kind is hardware-bound; the device must declare \
             \"hardware\": true — a simulated backend may not serve it"
        )));
    }
    let channels: BTreeMap<String, ChannelDecl> = spec
        .channels
        .iter()
        .map(|(name, channel)| {
            (
                name.clone(),
                ChannelDecl {
                    direction: channel.direction,
                    kind: channel.value_type,
                },
            )
        })
        .collect();
    dcs_ethercat::DeviceParameters::parse(spec.parameters, &channels)
        .map_err(DeviceError::parameters)
}

/// The deployment-bound [`ETHERCAT_KIND`] factory
/// [`with_ethercat_buses`](DriverRegistry::with_ethercat_buses)
/// installs: the same declaration checks as the stub, then the device
/// attaches to its logical bus over the deployment's bindings — one
/// shared master per bus, identity and process-image layout verified
/// against discovery, safe outputs staged, OP entry, all before the
/// device serves a scan.
///
/// The backend observes the field: `field_facing` so promotion fencing
/// counts it, `step: None` because the field advances itself, and
/// `claim: None` because no single-writer arbitration exists — which
/// keeps automatic failover honestly off for the hardware model.
fn ethercat_backend(
    spec: &DeviceSpec<'_>,
    buses: &EthercatBuses,
) -> Result<DeviceDriver, DeviceError> {
    let declaration = ethercat_declaration(spec)?;
    let points: Vec<BusPoint> = spec
        .points
        .iter()
        .map(|point| BusPoint {
            point: point.point,
            channel: point.channel.clone(),
            direction: point.direction,
            kind: point.kind,
        })
        .collect();
    let device = buses
        .attach(spec.id, &declaration, &points)
        .map_err(|error| match error {
            AttachError::Parameters(detail) => DeviceError::parameters(detail),
            AttachError::Backend(detail) => DeviceError::backend(detail),
        })?;
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: device.clone(),
        step: None,
        claim: None,
        release: None,
        ensure: None,
        startup_claim: None,
        probe: None,
        reclaim: None,
        fenced_by: None,
        claim_endpoint: None,
        owner_endpoint: None,
        inspect: Some(Arc::clone(device.master()) as Arc<dyn Any + Send + Sync>),
        field_facing: true,
    }))
}

/// A [`QualityReason`] wire name, as a script entry's `"reason"` — the
/// serde vocabulary itself (`snake_case`, plus the legacy PascalCase
/// spellings the enum's aliases accept on read), not a hand-kept table.
fn script_reason(name: &str) -> Option<QualityReason> {
    serde_json::from_value(serde_json::Value::String(name.to_string())).ok()
}

/// One `"script"` entry: `{"tick", "value", "quality"?, "reason"?}` —
/// the parse half of the [`SIM_SCRIPTED_KIND`] contract; ordering and
/// kind rules are [`ScriptedDriver::new`]'s to enforce.
fn scripted_entry(
    channel: &str,
    kind: ValueKind,
    index: usize,
    entry: &serde_json::Value,
) -> Result<ScriptEntry, DeviceError> {
    let invalid = |detail: String| {
        DeviceError::parameters(format!(
            "script channel {channel:?} entry {index}: {detail}"
        ))
    };
    let Some(object) = entry.as_object() else {
        return Err(invalid(format!("must be an object, found {entry}")));
    };
    for key in object.keys() {
        if !matches!(key.as_str(), "tick" | "value" | "quality" | "reason") {
            return Err(invalid(format!("unknown key {key:?}")));
        }
    }
    let Some(tick) = object.get("tick") else {
        return Err(invalid("missing \"tick\"".to_string()));
    };
    let Some(tick) = tick.as_u64() else {
        return Err(invalid(format!(
            "\"tick\" must be a non-negative integer, found {tick}"
        )));
    };
    let Some(json_value) = object.get("value") else {
        return Err(invalid("missing \"value\"".to_string()));
    };
    let value = match kind {
        ValueKind::Bool => json_value.as_bool().map(Value::Bool),
        ValueKind::Int => json_value.as_i64().map(Value::Int),
        ValueKind::Float => json_value
            .as_f64()
            .filter(|v| v.is_finite())
            .map(Value::Float),
    };
    let Some(value) = value else {
        return Err(invalid(format!(
            "\"value\" must match the channel's {kind:?} kind, found {json_value}"
        )));
    };
    let quality = match object.get("quality") {
        None => "good",
        Some(quality) => match quality.as_str() {
            Some(name @ ("good" | "uncertain" | "bad")) => name,
            _ => {
                return Err(invalid(format!(
                    "\"quality\" must be \"good\", \"uncertain\", or \"bad\", found {quality}"
                )));
            }
        },
    };
    let reason = match object.get("reason") {
        None => None,
        Some(reason) => match reason.as_str().and_then(script_reason) {
            Some(reason) => Some(reason),
            None => {
                return Err(invalid(format!(
                    "\"reason\" must name a QualityReason, found {reason}"
                )));
            }
        },
    };
    let quality = match (quality, reason) {
        ("good", None) => Quality::Good,
        ("good", Some(_)) => {
            return Err(invalid(
                "\"reason\" is meaningful only on a non-\"good\" entry".to_string(),
            ));
        }
        ("uncertain", reason) => Quality::Uncertain(reason.unwrap_or(QualityReason::Unspecified)),
        ("bad", reason) => Quality::Bad(reason.unwrap_or(QualityReason::Unspecified)),
        _ => unreachable!("quality is one of the three names above"),
    };
    Ok(ScriptEntry {
        tick: Tick(tick),
        value,
        quality,
    })
}

/// The [`SIM_SCRIPTED_KIND`] factory: parses the device's `"script"`
/// parameter into per-point [`ScriptEntry`] lists and builds the
/// [`ScriptedDriver`] serving every bound point. The driver goes in as a
/// self-contained [`DeviceBackend`] — the `inspect` handle is the
/// scripted driver itself, so [`FanoutDriver::inspect`] reaches its
/// recorded-write log.
fn scripted_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
    require_simulated(spec)?;
    for name in spec.parameters.keys() {
        if name != "script" {
            return Err(DeviceError::parameters(format!(
                "unknown parameter {name:?}; {SIM_SCRIPTED_KIND:?} takes \"script\""
            )));
        }
    }
    let Some(script) = spec.parameters.get("script") else {
        return Err(DeviceError::parameters("parameter \"script\" is required"));
    };
    let Some(script) = script.as_object() else {
        return Err(DeviceError::parameters(format!(
            "parameter \"script\" must be an object mapping channel names to entry lists, found {script}"
        )));
    };

    // Scripts key channels, the driver keys points: resolve each scripted
    // channel to the point bound to it.
    let bound: HashMap<&str, &DevicePoint> = spec
        .points
        .iter()
        .map(|point| (point.channel.as_str(), point))
        .collect();
    let mut scripts: BTreeMap<PointId, Vec<ScriptEntry>> = BTreeMap::new();
    for (channel, entries) in script {
        let Some(declared) = spec.channels.get(channel) else {
            return Err(DeviceError::parameters(format!(
                "script names channel {channel:?} the device does not declare"
            )));
        };
        if declared.direction != Direction::In {
            return Err(DeviceError::parameters(format!(
                "script names {channel:?}, an {:?} channel; scripts drive in channels",
                declared.direction
            )));
        }
        let Some(point) = bound.get(channel.as_str()) else {
            return Err(DeviceError::parameters(format!(
                "script names channel {channel:?} no io_point binds"
            )));
        };
        let Some(entries) = entries.as_array() else {
            return Err(DeviceError::parameters(format!(
                "script channel {channel:?} must be an array of entries, found {entries}"
            )));
        };
        let parsed = entries
            .iter()
            .enumerate()
            .map(|(index, entry)| scripted_entry(channel, point.kind, index, entry))
            .collect::<Result<Vec<_>, _>>()?;
        scripts.insert(point.point, parsed);
    }

    let points = spec
        .points
        .iter()
        .map(|point| PointBinding {
            point: point.point,
            channel: ChannelId {
                device: spec.id.0,
                name: point.channel.clone(),
            },
            direction: point.direction,
            initial: neutral(point.kind),
        })
        .collect();
    let driver = Arc::new(
        ScriptedDriver::new(points, scripts).map_err(|error| match error {
            // Script content problems are parameter problems; structural
            // ones — the duplicate bindings a validated model can still
            // carry — mean the backend could not be built.
            ScriptError::DuplicatePoint(_) | ScriptError::DuplicateChannel(_) => {
                DeviceError::backend(error.to_string())
            }
            _ => DeviceError::parameters(error.to_string()),
        })?,
    );
    let stepping = Arc::clone(&driver);
    let inspect: Arc<dyn Any + Send + Sync> = driver.clone();
    Ok(DeviceDriver::Backend(DeviceBackend {
        io: driver,
        step: Some(Arc::new(move |dt| Ok(stepping.step(dt)))),
        // Not field-facing — there is no shared field to claim.
        claim: None,
        release: None,
        ensure: None,
        startup_claim: None,
        probe: None,
        reclaim: None,
        fenced_by: None,
        claim_endpoint: None,
        owner_endpoint: None,
        inspect: Some(inspect),
        field_facing: false,
    }))
}

/// One backend a [`FanoutDriver`] routes to.
struct Backend {
    /// The model device the backend serves, or `None` for the shared
    /// local simulated backend.
    device: Option<DeviceId>,
    io: Arc<dyn IoDriver + Send + Sync>,
    step: Option<StepHook>,
    /// [`DeviceBackend::claim`] carried into the built driver — the
    /// field-ownership claim a promotion takes out.
    claim: Option<ClaimHook>,
    /// [`DeviceBackend::release`] carried into the built driver — the
    /// claim-forgetting hook a demotion runs.
    release: Option<ReleaseHook>,
    /// [`DeviceBackend::ensure`] carried into the built driver — the
    /// conditional claim re-arm an orphan cycle probes.
    ensure: Option<EnsureHook>,
    /// [`DeviceBackend::startup_claim`] carried into the built driver —
    /// the conditional grant a launched controller's activation asserts.
    startup_claim: Option<StartupClaimHook>,
    /// [`DeviceBackend::probe`] carried into the built driver — the
    /// read-only claim-state observation a peer reports.
    probe: Option<ProbeHook>,
    /// [`DeviceBackend::reclaim`] carried into the built driver — the
    /// bound conditional re-grant a fencing-demoted ex-owner probes.
    reclaim: Option<ReclaimHook>,
    /// [`DeviceBackend::fenced_by`] carried into the built driver —
    /// the claimant attribution a fencing-loss report reads.
    fenced_by: Option<FencedByHook>,
    /// [`DeviceBackend::claim_endpoint`] carried into the built
    /// driver — the claim's checkpoint-endpoint registration.
    claim_endpoint: Option<ClaimEndpointHook>,
    /// [`DeviceBackend::owner_endpoint`] carried into the built
    /// driver — the field-attested claimant endpoint a fencing-loss
    /// report reads.
    owner_endpoint: Option<OwnerEndpointHook>,
    /// The factory-installed typed inspection handle, if any.
    inspect: Option<Arc<dyn Any + Send + Sync>>,
    /// [`DeviceBackend::field_facing`] carried into the built driver —
    /// the shared local simulated backend is always `false`.
    field_facing: bool,
}

/// A device backend a [`DriverPlan`] builds: the contributed driver and
/// the points it must serve.
struct PlannedBackend {
    device: DeviceId,
    backend: DeviceBackend,
    /// The ids of `spec.points` — the points the fan-out routes here.
    points: Vec<PointId>,
}

/// The resolved driver side of a model: device contributions and wire
/// routing, with the shared local simulated map still open.
///
/// [`resolve_drivers`] produces it; [`build`](Self::build) finishes it.
/// Between the two, `sim_map` is open for caller-added
/// [`ProcessElement`](dcs_sim::ProcessElement)s — the simulated physics
/// the model does not describe, exactly as
/// [`sim_channel_map`](crate::sim_channel_map) left it open.
pub struct DriverPlan {
    /// The merged local simulated channel map: every `Sim` contribution
    /// and the point-to-point wires whose ends are both sim-served.
    /// When it is non-empty at [`build`](Self::build), one [`SimDriver`]
    /// backend serves it — the local plant — and its points route there.
    pub sim_map: ChannelMap,
    backends: Vec<PlannedBackend>,
    /// Point-to-point wires whose ends live on different backends —
    /// applied by [`FanoutDriver::step`].
    routes: Vec<Loopback>,
}

impl DriverPlan {
    /// Builds the [`FanoutDriver`]: the local [`SimDriver`] for
    /// `sim_map` when it binds any points — its map consistency surfaces
    /// as [`AssemblyError::InvalidChannelMap`] — plus every contributed
    /// backend, wired by the point-routing table and the cross-backend
    /// routes.
    pub fn build(self) -> Result<FanoutDriver, AssemblyError> {
        let mut backends: Vec<Backend> = Vec::with_capacity(self.backends.len() + 1);
        let mut points = HashMap::new();
        let mut sim = None;
        if !self.sim_map.points.is_empty() {
            let served: Vec<PointId> = self
                .sim_map
                .points
                .iter()
                .map(|binding| binding.point)
                .collect();
            let driver = Arc::new(
                SimDriver::new(self.sim_map)
                    .map_err(|detail| AssemblyError::InvalidChannelMap { detail })?,
            );
            let stepping = Arc::clone(&driver);
            let index = backends.len();
            for point in served {
                points.insert(point, index);
            }
            backends.push(Backend {
                device: None,
                io: driver.clone(),
                step: Some(Arc::new(move |dt| Ok(stepping.step(dt)))),
                claim: None,
                release: None,
                ensure: None,
                startup_claim: None,
                probe: None,
                reclaim: None,
                fenced_by: None,
                claim_endpoint: None,
                owner_endpoint: None,
                inspect: None,
                field_facing: false,
            });
            sim = Some(driver);
        }
        for planned in self.backends {
            let index = backends.len();
            for point in planned.points {
                points.insert(point, index);
            }
            backends.push(Backend {
                device: Some(planned.device),
                io: planned.backend.io,
                step: planned.backend.step,
                claim: planned.backend.claim,
                release: planned.backend.release,
                ensure: planned.backend.ensure,
                startup_claim: planned.backend.startup_claim,
                probe: planned.backend.probe,
                reclaim: planned.backend.reclaim,
                fenced_by: planned.backend.fenced_by,
                claim_endpoint: planned.backend.claim_endpoint,
                owner_endpoint: planned.backend.owner_endpoint,
                inspect: planned.backend.inspect,
                field_facing: planned.backend.field_facing,
            });
        }
        Ok(FanoutDriver {
            backends,
            points,
            routes: self.routes,
            sim,
            refusals: Mutex::new(Vec::new()),
        })
    }
}

/// Resolves the model's devices through `registry` into a [`DriverPlan`].
///
/// Each device is built by the factory its `kind` resolves to — an exact
/// registration, else a matching prefix — receiving its declared
/// channels, its kind-specific `parameters`, and the `io_point`s bound
/// to it. An unregistered kind is
/// [`AssemblyError::UnknownDeviceKind`]; a factory rejection is
/// [`AssemblyError::InvalidDeviceParameters`] or
/// [`AssemblyError::DeviceBackend`]; both fail here, before any scan.
///
/// `Sim` contributions merge into one local simulated map holding the
/// devices' point bindings and every point-to-point wire whose ends are
/// both sim-served; a wire spanning backends becomes a [`FanoutDriver`]
/// route instead. Channel-less internal `io_point`s are image-carried —
/// the executor's scan image serves them, so no backend sees them.
pub fn resolve_drivers(
    model: &PlantModel,
    registry: &DriverRegistry,
) -> Result<DriverPlan, AssemblyError> {
    let resolved = resolve(model);

    let mut points_by_device: BTreeMap<DeviceId, Vec<DevicePoint>> = BTreeMap::new();
    for point in &model.io_points {
        // Channel-less internal points are image-carried: no device
        // serves them.
        let Some(channel) = &point.channel else {
            continue;
        };
        points_by_device
            .entry(channel.device)
            .or_default()
            .push(DevicePoint {
                point: point.id,
                channel: channel.name.clone(),
                direction: point.direction,
                kind: point.value_type,
            });
    }

    let mut sim_map = ChannelMap::new();
    let mut sim_points = HashSet::new();

    let mut backends = Vec::new();
    let mut routed = HashSet::new();
    for device in &model.devices {
        let spec = DeviceSpec {
            id: device.id,
            kind: &device.kind,
            hardware: device.hardware,
            parameters: &device.parameters,
            channels: &device.channels,
            points: points_by_device.remove(&device.id).unwrap_or_default(),
        };
        let factory =
            registry
                .factory(&device.kind)
                .ok_or_else(|| AssemblyError::UnknownDeviceKind {
                    device: device.id,
                    kind: device.kind.clone(),
                })?;
        let served: Vec<PointId> = spec.points.iter().map(|point| point.point).collect();
        routed.extend(served.iter().copied());
        match factory(&spec).map_err(|error| match error {
            DeviceError::Parameters(detail) => AssemblyError::InvalidDeviceParameters {
                device: device.id,
                kind: device.kind.clone(),
                detail,
            },
            DeviceError::Backend(detail) => AssemblyError::DeviceBackend {
                device: device.id,
                kind: device.kind.clone(),
                detail,
            },
        })? {
            DeviceDriver::Sim(fragment) => {
                for binding in &fragment.points {
                    sim_points.insert(binding.point);
                }
                sim_map.points.extend(fragment.points);
                sim_map.loopbacks.extend(fragment.loopbacks);
                sim_map.elements.extend(fragment.elements);
            }
            DeviceDriver::Backend(backend) => {
                backends.push(PlannedBackend {
                    device: device.id,
                    backend,
                    points: served,
                });
            }
        }
    }

    // An io_point bound to a device the model never declares is unrouted —
    // reachable only for a model assembled without validation. Internal
    // points need no routing: the scan image serves them.
    for point in &model.io_points {
        let Some(channel) = &point.channel else {
            continue;
        };
        if !routed.contains(&point.id) {
            return Err(AssemblyError::UnroutedPoint {
                point: point.id,
                device: channel.device,
            });
        }
    }

    // A point-to-point wire joins the local simulated map when both ends
    // are sim-served — the same placement the single-backend path always
    // used — and becomes a fan-out route across backends otherwise.
    let mut routes = Vec::new();
    for wire in resolved.wires {
        if sim_points.contains(&wire.output) && sim_points.contains(&wire.input) {
            sim_map.loopbacks.push(wire);
        } else {
            routes.push(wire);
        }
    }

    Ok(DriverPlan {
        sim_map,
        backends,
        routes,
    })
}

/// The single [`IoDriver`] surface over every backend a model assembled:
/// each point's reads and writes route to the backend owning it.
///
/// The executor uses a `&FanoutDriver` exactly like a single driver —
/// a model mixing a local simulated device with a remote `sim-tcp`
/// device still presents one point space. [`step`](Self::step) paces
/// the simulated plant: cross-backend wires apply first — the position
/// loopbacks hold inside a [`SimDriver::step`] — then every backend's
/// step hook runs in order: the shared local simulated backend first,
/// then devices in model order.
///
/// State capture merges each backend's [`IoDriver::capture_state`] under
/// a `"{index}.{field}"` namespace so a checkpoint of a mixed-backend
/// run still carries the local simulated state; backends that capture
/// nothing contribute nothing, and restoring replays each backend's
/// fields to it.
pub struct FanoutDriver {
    backends: Vec<Backend>,
    /// Point → index into `backends`.
    points: HashMap<PointId, usize>,
    /// Cross-backend point-to-point wires, applied by `step`.
    routes: Vec<Loopback>,
    /// The shared local simulated backend, when the plan built one —
    /// kept for fault injection and inspection beside `step`.
    sim: Option<Arc<SimDriver>>,
    /// The standing-owner tokens the most recent conditional grant
    /// probe's refusals named — recorded by
    /// [`ensure_field_writer`](Self::ensure_field_writer) and
    /// [`reclaim_field_writer`](Self::reclaim_field_writer) at the
    /// refusing backend, read from that backend's own claimant verdict
    /// the moment its grant refused, so
    /// [`refused_claimants`](Self::refused_claimants) answers the
    /// refusal that just landed rather than a verdict an older episode
    /// left standing.
    refusals: Mutex<Vec<u64>>,
}

impl FanoutDriver {
    /// The backend serving `point`, or [`IoError::UnknownPoint`].
    fn owner(&self, point: PointId) -> Result<&Backend, IoError> {
        self.points
            .get(&point)
            .map(|&index| &self.backends[index])
            .ok_or(IoError::UnknownPoint(point))
    }

    /// The shared local simulated backend, when the model contributed
    /// `sim` points — exposed for fault injection and inspection;
    /// stepping goes through [`step`](Self::step).
    pub fn sim(&self) -> Option<&SimDriver> {
        self.sim.as_deref()
    }

    /// The [`IoDriver`] surface of the backend serving `device`, when a
    /// device-built backend owns it — `None` for the shared local
    /// simulated backend (see [`sim`](Self::sim)) and unknown devices.
    pub fn backend(&self, device: DeviceId) -> Option<&(dyn IoDriver + Send + Sync)> {
        self.backends
            .iter()
            .find(|backend| backend.device == Some(device))
            .map(|backend| &*backend.io)
    }

    /// The typed inspection handle the device's factory installed,
    /// downcast to `T` — e.g. `driver.inspect::<ScriptedDriver>(id)`
    /// reaches a scripted device's recorded-write log, or
    /// `driver.inspect::<RemoteDriver>(id)` the remote driver's
    /// fault-injection API. `None` when no device-built backend serves
    /// `device`, when the factory installed no handle, or when the
    /// handle is of another type. The shared local simulated backend is
    /// reached through [`sim`](Self::sim) instead.
    pub fn inspect<T: Send + Sync + 'static>(&self, device: DeviceId) -> Option<&T> {
        self.backends
            .iter()
            .find(|backend| backend.device == Some(device))
            .and_then(|backend| backend.inspect.as_ref())
            .and_then(|handle| handle.downcast_ref::<T>())
    }

    /// Whether a field-facing backend — one whose device reaches the
    /// shared plant every redundant peer attaches to — serves `point`.
    /// Points no backend owns, and channel-less internal points, are not
    /// field points. A redundant pair gates its writes by this: only
    /// field points are the shared field.
    pub fn is_field_point(&self, point: PointId) -> bool {
        self.points
            .get(&point)
            .is_some_and(|&index| self.backends[index].field_facing)
    }

    /// Whether any backend is field-facing — when not, every point is a
    /// private simulated one and a redundant pair needs no write gate.
    pub fn has_field_backend(&self) -> bool {
        self.backends.iter().any(|backend| backend.field_facing)
    }

    /// Declares the checkpoint-monitor port this instance serves on
    /// every field-facing backend that registers one — carried on each
    /// write-ownership claim the backend asserts or re-arms, so the
    /// field's claim record names where its owner serves checkpoints
    /// and the verdict fencing a superseded owner out can carry the
    /// successor's endpoint with it. Backends without a registration
    /// hook are skipped; their claims simply record no endpoint.
    pub fn set_claim_endpoint(&self, port: u16) {
        for backend in &self.backends {
            if backend.field_facing
                && let Some(claim_endpoint) = &backend.claim_endpoint
            {
                claim_endpoint(port);
            }
        }
    }

    /// Claims the field's write-ownership under `owner` on every
    /// field-facing backend that can arbitrate it — the fencing action a
    /// promotion runs before lifting the write gate, so the shared field
    /// itself refuses a fenced-out old owner's writes. `owner` is the
    /// same token on every backend, so all of this field owner's
    /// attachments keep writing. Field-facing backends without a claim
    /// hook are skipped — [`unfenced_field_devices`](Self::unfenced_field_devices)
    /// reports them; a deployment arming automatic failover must have
    /// none.
    pub fn claim_field_writer(&self, owner: u64) -> Result<(), StepError> {
        for backend in &self.backends {
            if backend.field_facing
                && let Some(claim) = &backend.claim
            {
                claim(owner)?;
            }
        }
        Ok(())
    }

    /// The launched-controller counterpart of
    /// [`claim_field_writer`](Self::claim_field_writer) — run once at
    /// startup activation: claims the field's write-ownership under
    /// `owner` on every field-facing backend only where no *live*
    /// attachment holds a different owner's claim. `Ok(true)` means the
    /// claim now stands under `owner`; `Ok(false)` that a live
    /// incumbent stands on at least one arbitrating backend — a
    /// restarted controller refusing the stale-checkpoint takeover it
    /// cannot prove safe rather than rolling back the incumbent's
    /// receipted state. A claim a dead owner left standing is still
    /// preempted, the restart-as-active recovery path. Field-facing
    /// backends without a startup hook — kinds whose arbitration cannot
    /// distinguish live holders — fall back to the unconditional
    /// [`DeviceBackend::claim`] hook exactly as `claim_field_writer`
    /// runs it.
    pub fn claim_field_writer_unless_held(&self, owner: u64) -> Result<bool, StepError> {
        let mut held = true;
        for backend in &self.backends {
            if !backend.field_facing {
                continue;
            }
            if let Some(startup_claim) = &backend.startup_claim {
                held &= startup_claim(owner)?;
            } else if let Some(claim) = &backend.claim {
                claim(owner)?;
            }
        }
        Ok(held)
    }

    /// Forgets every field-facing backend's recorded write-ownership
    /// claim — the demotion counterpart of
    /// [`claim_field_writer`](Self::claim_field_writer): a peer that gave
    /// the field up does not re-assert a stale claim when a re-attach
    /// finds the field's arbitration reset, so the restarted field's
    /// claim stays free for the peer that legitimately owns it.
    /// Backends without a release hook record nothing to forget.
    pub fn release_field_claims(&self) {
        for backend in &self.backends {
            if backend.field_facing
                && let Some(release) = &backend.release
            {
                release();
            }
        }
    }

    /// The conditional counterpart of
    /// [`claim_field_writer`](Self::claim_field_writer): the orphan-cycle
    /// probe a demoted ex-owner runs while the tracked line reports no
    /// field owner — re-arms the claim under `owner` only where the
    /// field stands unclaimed or already names the token, never
    /// preempting a standing owner. `Ok(true)` means the claim now
    /// stands under `owner` on every probed backend; `Ok(false)` that a
    /// different owner stands on at least one; `Err` that a backend
    /// could not be asked. Field-facing backends without an ensure hook
    /// are skipped, exactly as `claim_field_writer` skips unfenceable
    /// kinds — for them the claim either dies with the connection or
    /// the deployment simply reports the orphan wedge without re-arm.
    /// A refused backend's fencing verdict — the standing owner the
    /// refusal named — lands in [`refused_claimants`](Self::refused_claimants)
    /// for the observed-claimant journal record.
    pub fn ensure_field_writer(&self, owner: u64) -> Result<bool, StepError> {
        let mut held = true;
        let mut refused = Vec::new();
        for backend in &self.backends {
            if backend.field_facing
                && let Some(ensure) = &backend.ensure
            {
                match ensure(owner)? {
                    true => {}
                    // The refusal's own verdict named the standing
                    // owner — the backend's fencing record carries the
                    // claimant this probe just met, so the audit can
                    // attribute the foreign claim rather than journal
                    // an anonymous refusal.
                    false => {
                        held = false;
                        if let Some(fenced_by) = &backend.fenced_by {
                            refused.extend(fenced_by());
                        }
                    }
                }
            }
        }
        *self.refusals.lock().unwrap() = refused;
        Ok(held)
    }

    /// The read-only half of the field's write-ownership claim — the
    /// per-scan observation a peer reports as
    /// [`RoleReport::field_claim`](dcs_core::RoleReport::field_claim):
    /// asks every field-facing backend that can answer for the verdict a
    /// mutation from this attachment would meet, without mutating.
    /// [`FieldClaim::Unclaimed`] while at least one answering backend
    /// holds no claim — some of this field's writes would meet the
    /// fail-closed refusal — and [`FieldClaim::Held`] while every
    /// answering backend reports an owner standing. The probe asserts,
    /// joins, and releases nothing: an observation cannot seize the
    /// field it reports. `Err` reports that no field-facing backend
    /// could answer — no observation, so the run's last one stands.
    pub fn probe_field_claim(&self) -> Result<FieldClaim, StepError> {
        let mut claim = None;
        for backend in &self.backends {
            if backend.field_facing
                && let Some(probe) = &backend.probe
            {
                match probe()? {
                    FieldClaim::Unclaimed => return Ok(FieldClaim::Unclaimed),
                    FieldClaim::Held => claim = Some(FieldClaim::Held),
                }
            }
        }
        claim.ok_or_else(|| StepError::Backend {
            backend: "field claim probe".to_string(),
            detail: "no field-facing backend can answer the claim observation".to_string(),
        })
    }

    /// The fencing-loss counterpart of
    /// [`ensure_field_writer`](Self::ensure_field_writer) — the *bound*
    /// conditional re-grant a fencing-demoted ex-owner probes each scan
    /// while its loss mark stands: takes the claim under `owner` on
    /// every field-facing backend that answers, granted only where the
    /// field stands unclaimed or already names the token — the grant
    /// joining this attachment to the claim's holders, so the peer's
    /// re-lifted gate writes pass the claim it just took back.
    /// `Ok(true)` means the claim stands under `owner` on every probed
    /// backend; `Ok(false)` that a different owner stands on at least
    /// one — the reclaim never preempts, so a preemptor's claim that
    /// outlives its attachment leaves the wedge standing as an
    /// operator-promotable state — or that no backend can answer a
    /// conditional grant at all; `Err` that a backend could not be
    /// asked. Field-facing backends without a reclaim hook are skipped
    /// exactly as `ensure_field_writer` skips unprobeable kinds. A
    /// refused backend's fencing verdict lands in
    /// [`refused_claimants`](Self::refused_claimants) exactly as the
    /// orphan probe's does.
    pub fn reclaim_field_writer(&self, owner: u64) -> Result<bool, StepError> {
        let mut asked = false;
        let mut held = true;
        let mut refused = Vec::new();
        for backend in &self.backends {
            if backend.field_facing
                && let Some(reclaim) = &backend.reclaim
            {
                asked = true;
                match reclaim(owner)? {
                    true => {}
                    false => {
                        held = false;
                        if let Some(fenced_by) = &backend.fenced_by {
                            refused.extend(fenced_by());
                        }
                    }
                }
            }
        }
        *self.refusals.lock().unwrap() = refused;
        Ok(asked && held)
    }

    /// The standing-owner tokens the last conditional grant probe's
    /// refusals named — one per refusing field-facing backend, read
    /// from the backend's own fencing verdict the moment its grant
    /// refused. A peer's `field_claim_observed` journal record
    /// attributes through this answer the foreign claim its
    /// `ensure`/`reclaim` probe just met — not a stale or anonymous
    /// refusal. Empty when the last probe round granted everywhere,
    /// ran no conditional grant, or met refusals carrying no claimant
    /// identity.
    pub fn refused_claimants(&self) -> Vec<u64> {
        self.refusals.lock().unwrap().clone()
    }

    /// The owner token the field's standing claim named the last time
    /// it fenced a mutation on the backend serving `point` — the
    /// claimant a superseded field owner's `field_claim_lost` journal
    /// record attributes the preemption to. `None` where the backend
    /// records no verdict or its fencing answers carry no claimant
    /// identity: the journal then records the loss unattributed rather
    /// than guessing a claimant.
    pub fn fencing_claimant(&self, point: PointId) -> Option<u64> {
        self.points
            .get(&point)
            .and_then(|&index| self.backends[index].fenced_by.as_ref())
            .and_then(|fenced_by| fenced_by())
    }

    /// The checkpoint endpoint the field's standing claim carried on
    /// the last verdict fencing a mutation on the backend serving
    /// `point` — the address the claim's owner itself registered for
    /// its checkpoint monitor, attested by the field's own
    /// arbitration. A fencing-demoted ex-owner reads it as the
    /// tracking-source candidate the unauthenticated announced-hint
    /// channel cannot supply on an unkeyed pair — a candidate still
    /// verified by pulling its checkpoint before it is tracked.
    /// `None` where the backend records no verdict endpoint or its
    /// fencing answers carry none.
    pub fn field_owner_endpoint(&self, point: PointId) -> Option<SocketAddr> {
        self.points
            .get(&point)
            .and_then(|&index| self.backends[index].owner_endpoint.as_ref())
            .and_then(|owner_endpoint| owner_endpoint())
    }

    /// The field-facing devices whose backends cannot arbitrate a single
    /// writer — the ids a promotion cannot take a claim out on. The
    /// failover decision makes automatic promotion honest only when this
    /// is empty; a model built on unfenceable field kinds keeps manual
    /// promotion.
    pub fn unfenced_field_devices(&self) -> Vec<DeviceId> {
        self.backends
            .iter()
            .filter(|backend| backend.field_facing && backend.claim.is_none())
            .filter_map(|backend| backend.device)
            .collect()
    }

    /// Advances the assembled plant one step of `dt`: applies every
    /// cross-backend wire — copying the output point's value onto the
    /// input — then runs each backend's step hook.
    ///
    /// A route carries the value only: quality does not propagate across
    /// backends. `dt` must be finite and non-negative; unlike
    /// [`SimDriver::step`] an invalid `dt` is [`StepError::InvalidDt`],
    /// never a panic.
    pub fn step(&self, dt: f64) -> Result<(), StepError> {
        self.step_impl(dt, true)
    }

    /// The tracking standby's half of [`step`](Self::step): steps only
    /// the local — non-field-facing — backends, and applies only the
    /// routes delivering to their points. A field-facing backend's
    /// plant is the shared field: its clock belongs to the peer owning
    /// field writes, so a tracking peer leaves it alone and lets every
    /// checkpoint resynchronize its reads.
    pub fn step_local(&self, dt: f64) -> Result<(), StepError> {
        self.step_impl(dt, false)
    }

    fn step_impl(&self, dt: f64, include_field: bool) -> Result<(), StepError> {
        if !dt.is_finite() || dt < 0.0 {
            return Err(StepError::InvalidDt(dt));
        }
        for route in &self.routes {
            if include_field || !self.is_field_point(route.input) {
                let value = self.read(route.output).map_err(StepError::Route)?.value;
                self.write(route.input, value).map_err(StepError::Route)?;
            }
        }
        for backend in &self.backends {
            if (include_field || !backend.field_facing)
                && let Some(step) = &backend.step
            {
                step(dt)?;
            }
        }
        Ok(())
    }
}

/// The state-capture namespace: `"{backend index}.{field}"`.
const STATE_ELEMENT: &str = "fanout-driver";

impl IoDriver for FanoutDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.owner(point)?.io.read(point)
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        self.owner(point)?.io.write(point, value)
    }

    fn capture_state(&self) -> Option<StateMap> {
        let mut merged = StateMap::new();
        for (index, backend) in self.backends.iter().enumerate() {
            if let Some(state) = backend.io.capture_state() {
                for (field, value) in state.iter() {
                    merged.insert(format!("{index}.{field}"), value);
                }
            }
        }
        (!merged.is_empty()).then_some(merged)
    }

    /// Restores a map [`capture_state`](IoDriver::capture_state) produced:
    /// fields are partitioned by their `"{index}."` prefix and each
    /// backend restores its own section — a backend that captured nothing
    /// restores an empty map, so a checkpoint from a different backend
    /// mix fails naming the field it cannot place.
    fn restore_state(&self, state: &StateMap) -> Result<(), StateError> {
        let unknown = |field: &str| StateError::UnknownField {
            element: STATE_ELEMENT.to_string(),
            field: field.to_string(),
        };
        let mut sections: Vec<StateMap> =
            (0..self.backends.len()).map(|_| StateMap::new()).collect();
        for (field, value) in state.iter() {
            let Some((index, rest)) = field.split_once('.') else {
                return Err(unknown(field));
            };
            let index: usize = index.parse().map_err(|_| unknown(field))?;
            let Some(section) = sections.get_mut(index) else {
                return Err(unknown(field));
            };
            section.insert(rest, value);
        }
        for (backend, section) in self.backends.iter().zip(sections) {
            backend.io.restore_state(&section)?;
        }
        Ok(())
    }

    /// The aggregate link health over the backends: `None` when none
    /// reports — the all-local-simulated case has no transport to
    /// diagnose — otherwise `disconnected` when any reporting backend's
    /// link is down, with each backend's last protocol failure named by
    /// the device it serves.
    ///
    /// Each reporting backend's cyclic exchange section merges into the
    /// aggregate's own: counters sum over the buses and
    /// `last_exchange_tick` takes the earliest reported — the freshest
    /// exchange every bus has completed is the aggregate's honest bound.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let mut link = LinkState::Connected;
        let mut errors = Vec::new();
        let mut exchange: Option<ExchangeDiagnostics> = None;
        let mut reported = false;
        for backend in &self.backends {
            let Some(diagnostics) = backend.io.diagnostics() else {
                continue;
            };
            reported = true;
            if diagnostics.link == LinkState::Disconnected {
                link = LinkState::Disconnected;
            }
            if let Some(error) = diagnostics.last_error {
                let name = backend
                    .device
                    .map_or_else(|| "local sim".to_string(), |id| format!("device {}", id.0));
                errors.push(format!("{name}: {error}"));
            }
            if let Some(section) = diagnostics.exchange {
                let merged = exchange.get_or_insert_with(ExchangeDiagnostics::default);
                merged.attempted += section.attempted;
                merged.succeeded += section.succeeded;
                merged.working_counter_mismatches += section.working_counter_mismatches;
                merged.missed_deadlines += section.missed_deadlines;
                merged.last_exchange_tick =
                    match (merged.last_exchange_tick, section.last_exchange_tick) {
                        (Some(held), Some(fresh)) => Some(held.min(fresh)),
                        (held, fresh) => held.or(fresh),
                    };
            }
        }
        reported.then_some(DriverDiagnostics {
            link,
            last_error: (!errors.is_empty()).then(|| errors.join("; ")),
            exchange,
        })
    }

    /// The fan-out answers `Some` — reporting
    /// [`CyclicIoDriver`](dcs_core::CyclicIoDriver) through itself — when
    /// any backend implements the cyclic contract; its
    /// [`exchange`](CyclicIoDriver::exchange) then turns each cyclic
    /// backend's image in backend order.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        self.backends
            .iter()
            .any(|backend| backend.io.cyclic().is_some())
            .then_some(self)
    }
}

/// The fan-out's cyclic surface: each backend owns its process image, so
/// the aggregate `exchange` calls every cyclic backend's exchange in
/// backend order — one call publishing and latching each bus's image.
///
/// Every cyclic backend gets its attempt each scan (#547): the iteration
/// is bounded and never returns early, so a failure on one bus cannot
/// skip another's exchange — each backend's exchange counters, miss
/// streak, and `last_error` record its own boundary outcome, and its
/// declared `exchange_miss_threshold` escalation tracks its own link.
/// A scan in which any exchange failed returns the first failing
/// backend's error in backend order — the single-failure shape the
/// boundary's [`IoError`] carries; every failed bus's own diagnostics
/// still name it through the aggregate
/// [`diagnostics`](IoDriver::diagnostics) merge, which prefixes each
/// reporting backend's `last_error` with its device.
impl CyclicIoDriver for FanoutDriver {
    fn exchange(&self, tick: Tick) -> Result<(), IoError> {
        let mut failure = None;
        for backend in &self.backends {
            if let Some(cyclic) = backend.io.cyclic()
                && let Err(error) = cyclic.exchange(tick)
            {
                failure.get_or_insert(error);
            }
        }
        failure.map_or(Ok(()), Err)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

    /// A minimal per-point backend with no cyclic surface — the shape
    /// every shipped kind has.
    struct PointDriver;

    impl IoDriver for PointDriver {
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            Err(IoError::UnknownPoint(point))
        }

        fn write(&self, point: PointId, _value: Value) -> Result<(), IoError> {
            Err(IoError::UnknownPoint(point))
        }
    }

    /// A cyclic backend stub: `exchange` is the only transport call,
    /// counted and ticked; while `fail` stands every exchange misses.
    /// The stub keeps the contract's per-bus bookkeeping — a miss
    /// streak escalating `read` at `miss_threshold`, and the boundary
    /// failure's record in `last_error` — so tests can assert each
    /// backend's diagnostics reflect its own outcome.
    struct CyclicBackend {
        /// The point the bus attributes its boundary errors to.
        point: PointId,
        miss_threshold: u64,
        attempted: AtomicU64,
        succeeded: AtomicU64,
        misses: AtomicU64,
        last_tick: Mutex<Option<Tick>>,
        last_error: Mutex<Option<String>>,
        fail: AtomicBool,
    }

    impl CyclicBackend {
        fn new(point: PointId, miss_threshold: u64) -> Self {
            Self {
                point,
                miss_threshold,
                attempted: AtomicU64::new(0),
                succeeded: AtomicU64::new(0),
                misses: AtomicU64::new(0),
                last_tick: Mutex::new(None),
                last_error: Mutex::new(None),
                fail: AtomicBool::new(false),
            }
        }
    }

    impl IoDriver for CyclicBackend {
        /// The held image serves while the miss streak stays under the
        /// threshold, then escalates — the contract's per-bus
        /// `exchange_miss_threshold` rule.
        fn read(&self, point: PointId) -> Result<Sample, IoError> {
            if point != self.point {
                return Err(IoError::UnknownPoint(point));
            }
            if self.misses.load(Ordering::Relaxed) >= self.miss_threshold {
                return Err(IoError::Disconnected(point));
            }
            Ok(Sample::good(
                Value::Float(0.0),
                self.last_tick.lock().unwrap().unwrap_or(Tick::ZERO),
            ))
        }

        /// Staging is local and never escalates.
        fn write(&self, point: PointId, _value: Value) -> Result<(), IoError> {
            if point != self.point {
                return Err(IoError::UnknownPoint(point));
            }
            Ok(())
        }

        /// The per-bus diagnostics surface: the link reports any
        /// standing miss, `last_error` the most recent boundary
        /// failure.
        fn diagnostics(&self) -> Option<DriverDiagnostics> {
            Some(DriverDiagnostics {
                link: if self.misses.load(Ordering::Relaxed) > 0 {
                    LinkState::Disconnected
                } else {
                    LinkState::Connected
                },
                last_error: self.last_error.lock().unwrap().clone(),
                exchange: Some(ExchangeDiagnostics {
                    attempted: self.attempted.load(Ordering::Relaxed),
                    succeeded: self.succeeded.load(Ordering::Relaxed),
                    working_counter_mismatches: 0,
                    last_exchange_tick: *self.last_tick.lock().unwrap(),
                    missed_deadlines: 0,
                }),
            })
        }

        fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
            Some(self)
        }
    }

    impl CyclicIoDriver for CyclicBackend {
        fn exchange(&self, tick: Tick) -> Result<(), IoError> {
            self.attempted.fetch_add(1, Ordering::Relaxed);
            if self.fail.load(Ordering::Relaxed) {
                self.misses.fetch_add(1, Ordering::Relaxed);
                let error = IoError::Disconnected(self.point);
                *self.last_error.lock().unwrap() = Some(error.to_string());
                return Err(error);
            }
            self.misses.store(0, Ordering::Relaxed);
            self.succeeded.fetch_add(1, Ordering::Relaxed);
            *self.last_tick.lock().unwrap() = Some(tick);
            Ok(())
        }
    }

    fn backend(device: u64, io: Arc<dyn IoDriver + Send + Sync>) -> Backend {
        Backend {
            device: Some(DeviceId(device)),
            io,
            step: None,
            claim: None,
            release: None,
            ensure: None,
            startup_claim: None,
            probe: None,
            reclaim: None,
            fenced_by: None,
            claim_endpoint: None,
            owner_endpoint: None,
            inspect: None,
            field_facing: false,
        }
    }

    #[test]
    fn fanout_aggregates_the_cyclic_surface_over_its_backends() {
        // An all-point-wise fan-out is not cyclic — the executor never
        // calls `exchange` on it.
        let plain = FanoutDriver {
            backends: vec![backend(1, Arc::new(PointDriver))],
            points: HashMap::new(),
            routes: Vec::new(),
            sim: None,
            refusals: Mutex::new(Vec::new()),
        };
        assert!(plain.cyclic().is_none());

        // A fan-out with cyclic backends answers `Some`, and one
        // `exchange` turns each cyclic backend's image in order —
        // the point-wise backend has no exchange to run.
        let bus_a = Arc::new(CyclicBackend::new(PointId(31), 2));
        let bus_b = Arc::new(CyclicBackend::new(PointId(33), 2));
        let fanout = FanoutDriver {
            backends: vec![
                backend(1, bus_a.clone()),
                backend(2, Arc::new(PointDriver)),
                backend(3, bus_b.clone()),
            ],
            points: HashMap::new(),
            routes: Vec::new(),
            sim: None,
            refusals: Mutex::new(Vec::new()),
        };
        let cyclic = fanout.cyclic().unwrap();
        cyclic.exchange(Tick(7)).unwrap();
        assert_eq!(bus_a.attempted.load(Ordering::Relaxed), 1);
        assert_eq!(bus_b.attempted.load(Ordering::Relaxed), 1);

        // A failing backend's error propagates — the scan's reported
        // failure is the first failing bus's own — but the exchange
        // still reaches bus_b that scan: a failure on one bus never
        // skips another's exchange (#547).
        bus_a.fail.store(true, Ordering::Relaxed);
        assert_eq!(
            cyclic.exchange(Tick(8)),
            Err(IoError::Disconnected(PointId(31)))
        );
        assert_eq!(bus_a.attempted.load(Ordering::Relaxed), 2);
        assert_eq!(bus_b.attempted.load(Ordering::Relaxed), 2);
        assert_eq!(bus_b.succeeded.load(Ordering::Relaxed), 2);

        // The aggregate diagnostics merge each reporting backend's
        // exchange section: counters sum, the freshest exchange every
        // bus completed bounds `last_exchange_tick`.
        let diagnostics = fanout.diagnostics().unwrap();
        assert_eq!(
            diagnostics.exchange,
            Some(ExchangeDiagnostics {
                attempted: 4,
                succeeded: 3,
                working_counter_mismatches: 0,
                last_exchange_tick: Some(Tick(7)),
                missed_deadlines: 0,
            })
        );
    }

    #[test]
    fn a_failed_cyclic_exchange_skips_no_later_backend() {
        // #547's shape: two cyclic backends, the first's exchange
        // fails — the second must still get its attempt that scan,
        // each backend's counters and miss streak recording its own
        // boundary outcome.
        let bus_a = Arc::new(CyclicBackend::new(PointId(31), 2));
        let bus_b = Arc::new(CyclicBackend::new(PointId(32), 2));
        let fanout = FanoutDriver {
            backends: vec![backend(1, bus_a.clone()), backend(2, bus_b.clone())],
            points: HashMap::from([(PointId(31), 0), (PointId(32), 1)]),
            routes: Vec::new(),
            sim: None,
            refusals: Mutex::new(Vec::new()),
        };
        let cyclic = fanout.cyclic().unwrap();

        bus_a.fail.store(true, Ordering::Relaxed);
        assert_eq!(
            cyclic.exchange(Tick(1)),
            Err(IoError::Disconnected(PointId(31))),
            "the reported failure is the first failing bus's own error"
        );
        // bus_b still exchanged — its counters show its own completed
        // boundary, not a skip.
        assert_eq!(bus_b.attempted.load(Ordering::Relaxed), 1);
        assert_eq!(bus_b.succeeded.load(Ordering::Relaxed), 1);
        assert_eq!(*bus_b.last_tick.lock().unwrap(), Some(Tick(1)));
        // bus_a's counters and miss streak show its own failure.
        assert_eq!(bus_a.attempted.load(Ordering::Relaxed), 1);
        assert_eq!(bus_a.succeeded.load(Ordering::Relaxed), 0);
        assert_eq!(bus_a.misses.load(Ordering::Relaxed), 1);

        // The aggregate diagnostics name the failing bus by device;
        // the still-healthy bus contributes no error.
        let diagnostics = fanout.diagnostics().unwrap();
        assert_eq!(diagnostics.link, LinkState::Disconnected);
        let last_error = diagnostics.last_error.unwrap();
        assert!(last_error.contains("device 1"), "{last_error}");
        assert!(!last_error.contains("device 2"), "{last_error}");

        // Miss-threshold escalation tracks each bus independently:
        // bus_a's second consecutive miss reaches its declared
        // threshold and its reads escalate, while bus_b — still
        // exchanging every scan — keeps serving.
        assert_eq!(
            cyclic.exchange(Tick(2)),
            Err(IoError::Disconnected(PointId(31)))
        );
        assert_eq!(
            fanout.read(PointId(31)),
            Err(IoError::Disconnected(PointId(31)))
        );
        assert!(fanout.read(PointId(32)).is_ok());
        assert_eq!(bus_b.succeeded.load(Ordering::Relaxed), 2);

        // bus_a's recovery is its own too: its next completed exchange
        // resets its miss streak while bus_b never missed.
        bus_a.fail.store(false, Ordering::Relaxed);
        cyclic.exchange(Tick(3)).unwrap();
        assert_eq!(bus_a.misses.load(Ordering::Relaxed), 0);
        assert!(fanout.read(PointId(31)).is_ok());
    }

    #[test]
    fn a_later_cyclic_failure_still_reports_its_own_bus() {
        // The reverse-order case: the failing backend sits behind a
        // healthy one — the earlier bus's exchange is untouched and
        // the reported error names the bus that actually failed.
        let bus_a = Arc::new(CyclicBackend::new(PointId(31), 2));
        let bus_b = Arc::new(CyclicBackend::new(PointId(32), 2));
        let fanout = FanoutDriver {
            backends: vec![backend(1, bus_a.clone()), backend(2, bus_b.clone())],
            points: HashMap::from([(PointId(31), 0), (PointId(32), 1)]),
            routes: Vec::new(),
            sim: None,
            refusals: Mutex::new(Vec::new()),
        };
        let cyclic = fanout.cyclic().unwrap();

        bus_b.fail.store(true, Ordering::Relaxed);
        assert_eq!(
            cyclic.exchange(Tick(1)),
            Err(IoError::Disconnected(PointId(32)))
        );
        // bus_a's own boundary completed and shows it.
        assert_eq!(bus_a.attempted.load(Ordering::Relaxed), 1);
        assert_eq!(bus_a.succeeded.load(Ordering::Relaxed), 1);
        assert_eq!(*bus_a.last_tick.lock().unwrap(), Some(Tick(1)));
        assert_eq!(bus_a.misses.load(Ordering::Relaxed), 0);
        // bus_b's own boundary failed and shows it.
        assert_eq!(bus_b.attempted.load(Ordering::Relaxed), 1);
        assert_eq!(bus_b.succeeded.load(Ordering::Relaxed), 0);
        assert_eq!(bus_b.misses.load(Ordering::Relaxed), 1);

        // Both buses failing in one scan still visits each: the
        // reported error is the first failing bus's in backend order —
        // the documented single-failure shape — while every failed bus
        // names itself in the merged diagnostics.
        bus_a.fail.store(true, Ordering::Relaxed);
        assert_eq!(
            cyclic.exchange(Tick(2)),
            Err(IoError::Disconnected(PointId(31)))
        );
        assert_eq!(bus_b.attempted.load(Ordering::Relaxed), 2);
        let last_error = fanout.diagnostics().unwrap().last_error.unwrap();
        assert!(last_error.contains("device 1"), "{last_error}");
        assert!(last_error.contains("device 2"), "{last_error}");
    }
}
