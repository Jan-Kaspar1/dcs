//! The `ethercat` device kind: the field-facing contract half of the
//! cyclic field-I/O decision, and the model's entry point to the real
//! bus.
//!
//! An `ethercat` model device is *hardware-bound*: it declares one
//! physical station on a logical bus — its `identity`, the process-
//! image `mapping` of its channels, its `safe_outputs`, and the
//! `exchange_miss_threshold` reads escalate against — plus the
//! `"hardware": true` marker the assembly layer requires. The device
//! resolves through the `DriverRegistry` seam like any other kind, but
//! its backend is [`EthercatDevice`]: a synchronous [`IoDriver`]
//! surface over a shared [`BusMaster`] — the staged output image, the
//! latched input image, miss accounting, and exchange diagnostics of
//! one logical bus.
//!
//! One [`BusMaster`] per logical bus sits over a [`BusTransport`]:
//! production binds [`EthercrabTransport`] — an EtherCrab master
//! running on its own dedicated thread per bus, keeping every async
//! surface (the `tx_rx_task` socket pump, the state-transition
//! futures, the periodic exchange) off `dcs-runtime`'s synchronous
//! executor — while contract tests substitute a scripted
//! [`testing::FakeTransport`] as the fake PDU loop, no hardware
//! required.
//!
//! Deployment binds logical bus names to host interfaces through
//! [`EthercatBuses`]: the model owns the logical name, the deployment
//! owns the interface. A bus opens when its first device attaches —
//! discovery runs, the device's declared `identity` binds one
//! discovered station, its `mapping` is verified against the
//! discovered process-data layout, the staged output image seeds with
//! the declared safe outputs, and the bus enters PRE-OP → SAFE-OP → OP
//! — each mismatch a startup failure before outputs enable. A second
//! logical bus claiming one interface fails; a bus with no deployment
//! binding fails honestly rather than substituting simulation.
//!
//! Failover is honest: the kind exposes `claim: None`, so redundant
//! instances never arbitrate a single-writer field bus they cannot
//! fence.

pub use ethercrab_transport::EthercrabTransport;
pub use master::{AttachError, BusMaster, BusPoint, EthercatDevice};
pub use params::{
    ChannelDecl, DEVICE_KIND, DeviceParameters, ImageOffset, StartupPolicy, StationIdentity,
};
pub use replay::{Capture, CaptureError, CycleRecord, ReplayTransport};
pub use transport::{
    BusTransport, CycleOutcome, DiscoveredStation, OpenRequest, Opener, TransportError,
};

mod ethercrab_transport;
mod master;
pub mod params;
pub mod replay;
pub mod testing;
pub mod transport;

use dcs_model::DeviceId;
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

/// The deployment seam between the model's logical bus names and the
/// host's interfaces — decision 47's split: the model names `ecat0`,
/// the deployment says which NIC that is.
///
/// One `EthercatBuses` is shared by every `ethercat` device the
/// registry resolves: attaching a device opens its declared logical
/// bus on first use — one opener call, one [`BusMaster`] — and every
/// later device on the same bus attaches to the same master. The
/// `claimed` map rejects a second logical bus bound to one interface.
///
/// The default opener is [`EthercrabTransport::open`];
/// [`with_opener`](Self::with_opener) substitutes a fake transport for
/// simulation and contract tests — the only hardware-free path, since
/// the kind otherwise requires its bus.
#[derive(Clone)]
pub struct EthercatBuses {
    /// Logical bus name → bound host interface, from deployment config.
    bindings: Arc<BTreeMap<String, String>>,
    /// The transport constructor — EtherCrab in production, a fake in
    /// tests.
    opener: Opener,
    /// Interface → logical bus already bound to it.
    claimed: Arc<Mutex<BTreeMap<String, String>>>,
    /// Logical bus name → its live master.
    masters: Arc<Mutex<BTreeMap<String, Arc<BusMaster>>>>,
}

impl EthercatBuses {
    /// Buses over the real EtherCrab transport, with the deployment's
    /// logical-bus → interface bindings.
    pub fn new(bindings: BTreeMap<String, String>) -> Self {
        Self {
            bindings: Arc::new(bindings),
            opener: Arc::new(deployment_opener),
            claimed: Arc::new(Mutex::new(BTreeMap::new())),
            masters: Arc::new(Mutex::new(BTreeMap::new())),
        }
    }

    /// The same buses over a test opener — the simulation path.
    pub fn with_opener(bindings: BTreeMap<String, String>, opener: Opener) -> Self {
        Self {
            opener,
            ..Self::new(bindings)
        }
    }

    /// Attaches one model device to its declared logical bus: opens
    /// the bus on first attach (one opener call per bus, the interface
    /// claim taken then), binds the device's `identity` to a
    /// discovered station, verifies its `mapping` against the
    /// discovered layout, and — for the bus's first device — runs the
    /// one-shot OP entry. Failures are [`AttachError`]s the registered
    /// factory maps onto the assembly's device error.
    pub fn attach(
        &self,
        device: DeviceId,
        params: &DeviceParameters,
        points: &[BusPoint],
    ) -> Result<Arc<EthercatDevice>, AttachError> {
        let (master, fresh) = {
            let mut masters = self.masters.lock().unwrap();
            if let Some(master) = masters.get(&params.bus) {
                (Arc::clone(master), false)
            } else {
                let Some(interface) = self.bindings.get(&params.bus) else {
                    return Err(AttachError::backend(format!(
                        "logical bus {:?} has no interface binding in this deployment",
                        params.bus
                    )));
                };
                let mut claimed = self.claimed.lock().unwrap();
                if let Some(other) = claimed.get(interface) {
                    return Err(AttachError::backend(format!(
                        "interface {interface:?} is already bound to logical bus {other:?}; \
                         one interface serves one EtherCAT segment"
                    )));
                }
                let transport = (self.opener)(&OpenRequest {
                    bus: &params.bus,
                    interface,
                    expected: std::slice::from_ref(&params.identity),
                })
                .map_err(|error| {
                    AttachError::backend(format!("bus {:?} failed to open: {error}", params.bus))
                })?;
                claimed.insert(interface.clone(), params.bus.clone());
                let master = Arc::new(BusMaster::new(&params.bus, interface, transport));
                masters.insert(params.bus.clone(), Arc::clone(&master));
                (master, true)
            }
        };
        let attachment = match master.attach(device, params, points) {
            Ok(attachment) => attachment,
            Err(error) => {
                // A failed first attach must not poison the interface
                // claim — a later correct device still opens the bus.
                if fresh {
                    self.masters.lock().unwrap().remove(&params.bus);
                    if let Some(interface) = self.bindings.get(&params.bus) {
                        self.claimed.lock().unwrap().remove(interface);
                    }
                }
                return Err(error);
            }
        };
        if attachment.first
            && let Err(error) = master.enter_op()
        {
            // Same eviction: OP entry refused, the fresh master and
            // its claim release for a later attempt.
            let mut masters = self.masters.lock().unwrap();
            masters.remove(&params.bus);
            if let Some(interface) = self.bindings.get(&params.bus) {
                self.claimed.lock().unwrap().remove(interface);
            }
            return Err(AttachError::backend(format!(
                "bus {:?} refused OP entry: {error}",
                params.bus
            )));
        }
        Ok(attachment.device)
    }
}

/// The prefix a deployment binding carries when it names a *recorded
/// run* rather than a host interface — the `@` of `@capture.json`.
pub const RECORDED_BINDING_PREFIX: char = '@';

/// The deployment transport constructor: what one logical bus's
/// binding resolves to.
///
/// A binding naming a host interface — `ecat0=enx00e04c751f7c` — opens
/// the real EtherCrab master on that NIC. A binding carrying
/// [`RECORDED_BINDING_PREFIX`] names a recorded run instead —
/// `ecat0=@captures/rig-session.json` — and replays it through
/// [`replay::Capture`], which is how the hardware path is exercised
/// with no rig and no privileges.
///
/// The choice is the deployment's and is never inferred: an interface
/// binding never falls back to a recording, and a recorded binding
/// never opens a socket. Either resolution failing is a startup failure
/// naming the bus, never a degraded run.
fn deployment_opener(request: &OpenRequest<'_>) -> Result<Box<dyn BusTransport>, TransportError> {
    let Some(path) = request.interface.strip_prefix(RECORDED_BINDING_PREFIX) else {
        return EthercrabTransport::open(request)
            .map(|transport| Box::new(transport) as Box<dyn BusTransport>);
    };
    replay::Capture::load(std::path::Path::new(path))
        .map(|capture| Box::new(capture.transport()) as Box<dyn BusTransport>)
        .map_err(|error| {
            TransportError::internal(format!(
                "recorded run {path:?} could not be replayed: {error}"
            ))
        })
}

impl std::fmt::Debug for EthercatBuses {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EthercatBuses")
            .field("bindings", &self.bindings)
            .finish()
    }
}
