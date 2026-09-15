//! # `dcs-ethercat` — the EtherCAT device kind
//!
//! A cyclic [`IoDriver`] backend over EtherCrab: the `ethercat` device
//! kind declared in the plant model resolves to a point-facing
//! [`EthercatDevice`] that serves one logical point's read from the
//! bus's latched input image and stages its writes into the bus's
//! pending output image — exactly the cyclic-I/O contract the runtime
//! executor turns once per scan, never per point.
//!
//! ## The two halves
//!
//! - [`BusMaster`] is the contract half — synchronous, transport-
//!   agnostic, and fully exercisable without hardware: the staged and
//!   latched images, the miss accounting that escalates held reads to
//!   `IoError::Disconnected` at the declared `exchange_miss_threshold`,
//!   the working-counter shortfall attribution, and the
//!   [`DriverDiagnostics`] the monitoring surface consumes.
//! - [`EthercrabTransport`] is the hardware half — one background
//!   thread per logical bus owning EtherCrab's async socket pump and
//!   the master session (`init` with a declared-profile filter,
//!   `into_pre_op_pdi`, `into_safe_op`/`into_op`, per-scan `tx_rx`).
//!   No async runtime reaches `dcs-runtime`; the transport's callers
//!   wait on plain `std::sync::mpsc` replies.
//!
//! [`testing::FakeTransport`] implements the same [`BusTransport`]
//! seam against a scripted PDU loop, so every contract behavior —
//! startup identity and layout verification, exchange success and
//! failure sequences, staged-output retention, recovery re-entry —
//! runs without a rig.
//!
//! ## Deployment wiring
//!
//! The model carries the *logical* bus: the `bus` name, the expected
//! station profile, and the channel-to-process-image layout (see
//! [`params`]). The deployment supplies the binding between that
//! logical name and a host interface — [`EthercatBuses::new`] takes a
//! `{"bus": "enp2s0"}` map, and the interface name is the only thing
//! the real path opens. Two model devices on one logical bus share
//! one [`BusMaster`] — and therefore one socket, one thread, one
//! exchange per scan — while a second logical bus claiming an
//! already-bound interface is a startup failure.
//!
//! Register the kind onto a deployment's [`DriverRegistry`] with
//! [`EthercatBuses::register`]:
//!
//! ```no_run
//! use dcs_assembly::DriverRegistry;
//! use dcs_ethercat::EthercatBuses;
//!
//! let mut registry = DriverRegistry::standard();
//! let buses = EthercatBuses::new([("bus_a".to_string(), "enp2s0".to_string())].into());
//! buses.register(&mut registry);
//! ```
//!
//! The kind is `field_facing` (the field steps itself — no `StepHook`)
//! and keeps `claim` `None`: without a real single-writer arbitration
//! for the field, automatic failover stays honestly off for hardware
//! models.

#![warn(missing_docs)]

mod ethercrab_transport;
mod master;
pub mod params;
pub mod testing;
pub mod transport;

pub use ethercrab_transport::EthercrabTransport;
pub use master::{BusMaster, EthercatDevice};
pub use params::{ChannelLayout, DEVICE_KIND, DeviceParameters, StationProfile};
pub use transport::{
    BusTransport, CycleOutcome, DiscoveredStation, OpenRequest, Opener, TransportError,
};

use dcs_assembly::{DeviceBackend, DeviceDriver, DeviceError, DeviceSpec, DriverRegistry};
use std::any::Any;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

/// The set of logical EtherCAT buses a deployment runs — the seam
/// where the plant model's logical `bus` names meet the deployment's
/// host-interface bindings.
///
/// One `EthercatBuses` owns the mapping for one controller's driver
/// resolution: the first `ethercat` device on a logical bus opens it
/// (spawning its EtherCrab thread on the bound interface), every
/// device on that bus shares the resulting [`BusMaster`], and a bus
/// name the deployment did not bind — or an interface a second bus
/// tries to claim — is a `DeviceError::Backend` naming the device.
#[derive(Clone)]
pub struct EthercatBuses {
    shared: Arc<Mutex<BusSet>>,
}

/// The buses' resolved state: the deployment's bindings, the opener
/// (real or injected), the live masters by logical name, and which
/// interface each live master claimed.
struct BusSet {
    /// Deployment-supplied `logical bus → host interface` bindings.
    bindings: HashMap<String, String>,
    /// How a logical bus opens — [`EthercrabTransport::open`] by
    /// default, a fake in tests.
    opener: Opener,
    /// Live masters by logical bus name.
    buses: HashMap<String, Arc<BusMaster>>,
    /// Bound interface → the logical bus that claimed it — the
    /// single-writer fact for a host interface.
    claimed: HashMap<String, String>,
}

impl EthercatBuses {
    /// Buses bound to real interfaces: `bindings` maps each logical
    /// `bus` parameter value to the host interface EtherCrab opens.
    /// The model never names interfaces — this map is the whole of
    /// decision-47's deployment ownership for the kind.
    pub fn new(bindings: HashMap<String, String>) -> Self {
        Self::with_opener(
            bindings,
            Arc::new(|request: &OpenRequest<'_>| {
                EthercrabTransport::open(request)
                    .map(|transport| Box::new(transport) as Box<dyn BusTransport>)
            }),
        )
    }

    /// Buses whose transports the given opener produces — the seam
    /// simulations and contract tests inject fake PDU loops through.
    /// The opener receives the logical name, the bound interface, and
    /// the declared station profile, exactly like the real path.
    pub fn with_opener(bindings: HashMap<String, String>, opener: Opener) -> Self {
        Self {
            shared: Arc::new(Mutex::new(BusSet {
                bindings,
                opener,
                buses: HashMap::new(),
                claimed: HashMap::new(),
            })),
        }
    }

    /// Registers the `ethercat` device kind onto `registry` — the
    /// decision-29 seam: every `ethercat` model device resolves
    /// through this set's shared masters.
    pub fn register(&self, registry: &mut DriverRegistry) {
        let buses = self.clone();
        registry.register(DEVICE_KIND, move |spec: &DeviceSpec<'_>| buses.device(spec));
    }

    /// Builds one model device's driver contribution: parses and
    /// validates its declared profile and layout, attaches it to its
    /// logical bus's shared master — opening the bus first if this is
    /// the first device on it — and returns the point-facing
    /// [`EthercatDevice`] as a [`DeviceBackend`]. [`register`](Self::register)
    /// installs this as the kind's factory; it is public so tests and
    /// bespoke assembly paths can drive the same resolution.
    pub fn device(&self, spec: &DeviceSpec<'_>) -> Result<DeviceDriver, DeviceError> {
        let params = DeviceParameters::parse(spec.parameters, spec.channels)
            .map_err(DeviceError::parameters)?;
        let mut set = self.shared.lock().unwrap();
        let mut fresh_interface = None;
        let master = match set.buses.get(&params.bus) {
            Some(master) => Arc::clone(master),
            None => {
                let interface = set.bindings.get(&params.bus).cloned().ok_or_else(|| {
                    DeviceError::backend(format!(
                        "logical bus {:?} has no deployment interface binding",
                        params.bus
                    ))
                })?;
                if let Some(other) = set.claimed.get(&interface) {
                    return Err(DeviceError::backend(format!(
                        "interface {interface:?} is already claimed by logical bus {other:?}"
                    )));
                }
                let transport = (set.opener)(&OpenRequest {
                    bus: &params.bus,
                    interface: &interface,
                    expected: &params.stations,
                })
                .map_err(|error| {
                    DeviceError::backend(format!(
                        "bus {:?} on interface {interface:?}: {error}",
                        params.bus
                    ))
                })?;
                let master = Arc::new(BusMaster::new(&params.bus, transport));
                set.claimed.insert(interface.clone(), params.bus.clone());
                set.buses.insert(params.bus.clone(), Arc::clone(&master));
                fresh_interface = Some(interface);
                master
            }
        };
        // Attaching or the first OP entry failing must not leave a
        // half-claimed bus behind — a fresh master is evicted so the
        // interface binding frees again.
        let evict_fresh = |set: &mut BusSet| {
            if let Some(interface) = &fresh_interface {
                set.buses.remove(&params.bus);
                set.claimed.remove(interface);
            }
        };
        let attachment = match master.attach(spec.id, &params, &spec.points) {
            Ok(attachment) => attachment,
            Err(error) => {
                evict_fresh(&mut set);
                return Err(error);
            }
        };
        if attachment.first
            && let Err(error) = master.enter_op()
        {
            evict_fresh(&mut set);
            return Err(DeviceError::backend(format!(
                "bus {:?} refused OP entry: {error}",
                params.bus
            )));
        }
        Ok(DeviceDriver::Backend(DeviceBackend {
            io: attachment.device,
            // The field steps itself — no StepHook.
            step: None,
            // No single-writer arbitration exists for the bus, so the
            // claim hook stays off and failover fencing honestly
            // reports these devices as unfenced.
            claim: None,
            inspect: Some(master as Arc<dyn Any + Send + Sync>),
            field_facing: true,
        }))
    }
}
