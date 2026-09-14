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
//! device of `dcs-sim-bus`), and [`SIM_SCRIPTED_KIND`] (`sim-scripted`,
//! the tick-indexed playback driver of `dcs-sim`). New device
//! integrations register their own kind against the same [`DeviceSpec`]
//! contract — registering a device integration is what "adding a new
//! device" means.

use crate::assembly::{neutral, resolve};
use crate::error::AssemblyError;
use dcs_core::{
    DriverDiagnostics, IoDriver, IoError, LinkState, PointId, Quality, QualityReason, Sample,
    StateError, StateMap, Tick, Value, ValueKind,
};
use dcs_model::{Channel, DeviceId, Direction, PlantModel};
use dcs_sim::{
    ChannelId, ChannelMap, Loopback, PointBinding, ScriptEntry, ScriptError, ScriptedDriver,
    SimDriver,
};
use dcs_sim_bus::{BusDriver, DeviceParameters, PointRegister};
use dcs_sim_net::RemoteDriver;
use std::any::Any;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::net::ToSocketAddrs;
use std::sync::Arc;
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
    /// the register-mapped fieldbus driver, and [`SIM_SCRIPTED_KIND`]
    /// (`sim-scripted`) served by the scripted playback driver.
    pub fn standard() -> Self {
        Self::new()
            .with(SIM_TCP_KIND, sim_tcp_device)
            .with(SIM_BUS_KIND, sim_bus_device)
            .with(SIM_SCRIPTED_KIND, scripted_device)
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

/// The local simulated device factory: every bound `io_point` becomes a
/// [`PointBinding`] in a [`ChannelMap`] fragment carrying the device's
/// id. The kind takes no parameters — a `sim*` device declaring any is
/// [`DeviceError::Parameters`].
pub(crate) fn sim_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
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
    let remote =
        RemoteDriver::connect_with_timeout(addresses.as_slice(), timeout).map_err(|error| {
            DeviceError::backend(format!(
                "cannot connect to plant server at {address:?}: {error}"
            ))
        })?;
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
        // promoted peer takes out on the old field owner.
        claim: Some(Arc::new(move |owner| {
            claiming
                .claim_writer(owner)
                .map_err(|error| StepError::Backend {
                    backend: format!("device {device}"),
                    detail: error.to_string(),
                })
        })),
        inspect: Some(inspect),
        field_facing: true,
    }))
}

/// The [`SIM_BUS_KIND`] factory: validates the addressing and register
/// map against the declared channels, connects to the device server,
/// and probes that it serves every mapped register with the declared
/// value kind.
fn sim_bus_device(spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
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
        step: Some(Arc::new(move |_dt| {
            stepping.step().map_err(|error| StepError::Backend {
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
        inspect: Some(inspect),
        field_facing: true,
    }))
}

/// A `snake_case` [`QualityReason`] name, as a script entry's `"reason"`.
fn script_reason(name: &str) -> Option<QualityReason> {
    Some(match name {
        "unspecified" => QualityReason::Unspecified,
        "substituted" => QualityReason::Substituted,
        "stale" => QualityReason::Stale,
        "out_of_range" => QualityReason::OutOfRange,
        "communication_fault" => QualityReason::CommunicationFault,
        "device_fault" => QualityReason::DeviceFault,
        "configuration_fault" => QualityReason::ConfigurationFault,
        _ => return None,
    })
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
                    "\"reason\" must name a QualityReason in snake_case, found {reason}"
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
                inspect: planned.backend.inspect,
                field_facing: planned.backend.field_facing,
            });
        }
        Ok(FanoutDriver {
            backends,
            points,
            routes: self.routes,
            sim,
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
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let mut link = LinkState::Connected;
        let mut errors = Vec::new();
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
        }
        reported.then_some(DriverDiagnostics {
            link,
            last_error: (!errors.is_empty()).then(|| errors.join("; ")),
        })
    }
}
