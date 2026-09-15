//! The real [`BusTransport`]: an EtherCrab master on a deployment-bound
//! interface, driven from one dedicated thread per logical bus.
//!
//! [`EthercrabTransport::open`] spawns the bus thread, which owns
//! everything async: the `tx_rx_task` socket pump and the master
//! session — `MainDevice::init` with a group filter that rejects
//! unprofiled SubDevices as `Error::UnknownSubDevice`, the
//! `into_pre_op_pdi` image configuration, and the station measurement
//! that becomes [`BusTransport::discovered`]. From then on the thread
//! services a request queue: [`enter_op`](BusTransport::enter_op) and
//! [`recover`](BusTransport::recover) move the group through its
//! typestate, and [`exchange`](BusTransport::exchange) publishes the
//! staged output image, calls `tx_rx` once, and returns the input
//! image. `read`/`write` on the `IoDriver` never reach this module —
//! they touch only the master's images.
//!
//! No part of this runs inside `dcs-runtime`'s synchronous executor:
//! the bus thread blocks on `futures_lite::block_on`, the transport's
//! callers wait on `std::sync::mpsc` replies, and no async runtime is
//! exposed. Hardware is required only at `open` — every simulation and
//! contract path substitutes [`testing::FakeTransport`](crate::testing).

use crate::params::StationProfile;
use crate::transport::{
    BusTransport, CycleOutcome, DiscoveredStation, OpenRequest, TransportError,
};
use dcs_core::LinkState;
use ethercrab::error::Error;
use ethercrab::std::{ethercat_now, tx_rx_task};
use ethercrab::subdevice_group::{Op, PreOpPdi};
use ethercrab::{
    MainDevice, MainDeviceConfig, PduStorage, SubDeviceGroup, SubDeviceState, Timeouts,
};
use futures_lite::future;
use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::thread;
use std::time::Duration;

/// The bus's station ceiling — sized for the Lenovo QA rig's segment
/// plus headroom, not a product-wide limit.
const MAX_SUBDEVICES: usize = 16;
/// The bus's per-station process-data ceiling.
const MAX_PDI: usize = 1024;
/// Maximum concurrently outstanding frames the PDU loop tracks.
const MAX_FRAMES: usize = 16;
/// The maximum PDU payload a frame carries.
const MAX_PDU_DATA: usize = PduStorage::element_size(1486);

/// How long `open` waits for the bus thread's init and measurement —
/// the EtherCrab `Timeouts` govern the wire side; this is the last
/// resort against a wedged bring-up.
const OPEN_TIMEOUT: Duration = Duration::from_secs(30);
/// How long a synchronous request waits for the bus thread before
/// declaring it unresponsive.
const REPLY_TIMEOUT: Duration = Duration::from_secs(30);

/// A request the synchronous transport hands to the bus thread — the
/// reply comes back on the enclosed channel.
enum BusRequest {
    /// One-shot OP entry.
    EnterOp {
        /// The reply sink.
        reply: Reply<Result<(), TransportError>>,
    },
    /// One cyclic exchange: publish these staged outputs, answer with
    /// the input image and the cycle's outcome.
    Exchange {
        /// The staged output image to publish.
        outputs: Vec<u8>,
        /// The reply sink.
        reply: Reply<ExchangeReply>,
    },
    /// A recovery re-entry: PRE-OP → SAFE-OP → OP.
    Recover {
        /// The reply sink.
        reply: Reply<Result<(), TransportError>>,
    },
}

type Reply<T> = mpsc::Sender<T>;

/// An exchange's reply: the cycle outcome and, when it completed, the
/// latched input image.
struct ExchangeReply {
    outcome: Result<CycleOutcome, TransportError>,
    inputs: Vec<u8>,
}

/// The single group all discovered stations land in — the cyclic
/// kind's design decision for v1. A future multi-group profile adds
/// siblings here; `GroupIndex::Group(0)` in the filter names it.
struct Groups {
    group: SubDeviceGroup<MAX_SUBDEVICES, MAX_PDI>,
}

/// The bus thread's typestate holder: the group before OP entry and
/// after it. `None` means a failed transition consumed the group — the
/// bus stays down until a fresh `open`.
enum Stage {
    PreOpPdi(SubDeviceGroup<MAX_SUBDEVICES, MAX_PDI, ethercrab::DefaultLock, PreOpPdi>),
    Op(SubDeviceGroup<MAX_SUBDEVICES, MAX_PDI, ethercrab::DefaultLock, Op>),
}

/// An EtherCrab-backed [`BusTransport`]: one logical bus, one
/// background thread, one socket. Created by
/// [`EthercatBuses`](crate::EthercatBuses) through the default opener
/// when the deployment binds a real interface; tests substitute a
/// fake through [`EthercatBuses::with_opener`](crate::EthercatBuses::with_opener).
pub struct EthercrabTransport {
    /// The stations measured at `into_pre_op_pdi` — position-ordered,
    /// with each station's real input/output byte counts.
    discovered: Vec<DiscoveredStation>,
    /// The request queue to the bus thread.
    requests: async_channel::Sender<BusRequest>,
    /// Whether the bus thread is still running — the link read.
    alive: Arc<AtomicBool>,
    /// The bus thread's join handle, joined on drop.
    thread: Option<thread::JoinHandle<()>>,
}

impl fmt::Debug for EthercrabTransport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EthercrabTransport")
            .field("stations", &self.discovered.len())
            .field("alive", &self.alive.load(Ordering::Acquire))
            .finish()
    }
}

impl EthercrabTransport {
    /// Opens a logical bus: binds the deployment-named interface,
    /// spawns the bus thread, runs `MainDevice::init` with the
    /// declared-profile filter, configures the group process image,
    /// and answers with the measured station list. Any failure — the
    /// interface refusing, a station the profile does not expect, a
    /// dead thread — is a `TransportError` the caller reports as the
    /// device's startup failure before OP.
    pub fn open(request: &OpenRequest<'_>) -> Result<Self, TransportError> {
        if request.expected.len() > MAX_SUBDEVICES {
            return Err(TransportError::internal(format!(
                "bus {:?} declares {} stations, the master supports at most {MAX_SUBDEVICES}",
                request.bus,
                request.expected.len()
            )));
        }
        let (request_tx, request_rx) = async_channel::unbounded::<BusRequest>();
        let (ready_tx, ready_rx) = mpsc::channel();
        let alive = Arc::new(AtomicBool::new(true));
        let thread = thread::Builder::new()
            .name(format!("dcs-ethercat-{}", request.bus))
            .spawn({
                let interface = request.interface.to_string();
                let expected = request.expected.to_vec();
                let alive = Arc::clone(&alive);
                move || run(interface, expected, request_rx, ready_tx, alive)
            })
            .map_err(|error| {
                TransportError::internal(format!("cannot spawn the bus thread: {error}"))
            })?;
        match ready_rx.recv_timeout(OPEN_TIMEOUT) {
            Ok(Ok(discovered)) => Ok(Self {
                discovered,
                requests: request_tx,
                alive,
                thread: Some(thread),
            }),
            Ok(Err(detail)) => {
                // The thread already reported and ended.
                let _ = thread.join();
                Err(TransportError::internal(detail))
            }
            Err(error) => Err(TransportError::disconnected(format!(
                "bus {:?} init did not answer within {OPEN_TIMEOUT:?}: {error}",
                request.bus
            ))),
        }
    }

    /// Hands one request to the bus thread and waits for its reply —
    /// the synchronous boundary the `IoDriver`'s callers sit behind.
    fn request<T>(&self, make: impl FnOnce(Reply<T>) -> BusRequest) -> Result<T, TransportError> {
        let (reply_tx, reply_rx) = mpsc::channel();
        self.requests
            .send_blocking(make(reply_tx))
            .map_err(|_| TransportError::disconnected("the bus thread is gone"))?;
        reply_rx
            .recv_timeout(REPLY_TIMEOUT)
            .map_err(|error| match error {
                RecvTimeoutError::Timeout => TransportError::Timeout(format!(
                    "the bus thread did not answer within {REPLY_TIMEOUT:?}"
                )),
                RecvTimeoutError::Disconnected => {
                    TransportError::disconnected("the bus thread is gone")
                }
            })
    }
}

impl BusTransport for EthercrabTransport {
    fn discovered(&self) -> &[DiscoveredStation] {
        &self.discovered
    }

    fn enter_op(&mut self) -> Result<(), TransportError> {
        self.request(|reply| BusRequest::EnterOp { reply })?
    }

    fn exchange(
        &mut self,
        outputs: &[u8],
        inputs: &mut [u8],
    ) -> Result<CycleOutcome, TransportError> {
        let reply = self.request(|reply| BusRequest::Exchange {
            outputs: outputs.to_vec(),
            reply,
        })?;
        let outcome = reply.outcome?;
        let kept = reply.inputs.len().min(inputs.len());
        inputs[..kept].copy_from_slice(&reply.inputs[..kept]);
        Ok(outcome)
    }

    fn recover(&mut self) -> Result<(), TransportError> {
        self.request(|reply| BusRequest::Recover { reply })?
    }

    fn link(&self) -> LinkState {
        if self.alive.load(Ordering::Acquire) {
            LinkState::Connected
        } else {
            LinkState::Disconnected
        }
    }
}

impl Drop for EthercrabTransport {
    fn drop(&mut self) {
        // Closing the request queue ends the session; the tx/rx pump
        // task then exits with the socket it owned.
        self.requests.close();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// The bus thread body: leak the PDU storage, start the socket pump,
/// run the master session — whichever ends first ends the bus.
fn run(
    interface: String,
    expected: Vec<StationProfile>,
    requests: async_channel::Receiver<BusRequest>,
    ready: mpsc::Sender<Result<Vec<DiscoveredStation>, String>>,
    alive: Arc<AtomicBool>,
) {
    futures_lite::future::block_on(async {
        let storage: &'static PduStorage<MAX_FRAMES, MAX_PDU_DATA> =
            Box::leak(Box::new(PduStorage::new()));
        let Ok((pdu_tx, pdu_rx, pdu_loop)) = storage.try_split() else {
            let _ = ready.send(Err("PDU storage is already split".to_string()));
            return;
        };
        let pump = match tx_rx_task(&interface, pdu_tx, pdu_rx) {
            Ok(pump) => pump,
            Err(error) => {
                let _ = ready.send(Err(format!("interface {interface:?}: {error}")));
                return;
            }
        };
        let maindevice =
            MainDevice::new(pdu_loop, Timeouts::default(), MainDeviceConfig::default());
        enum End {
            /// The socket pump ended — the link is gone.
            Pump,
            /// The session ended — the request queue closed.
            Session,
        }
        let pump = async {
            let _ = pump.await;
            End::Pump
        };
        let session = async {
            session(&maindevice, &expected, requests, ready).await;
            End::Session
        };
        future::race(pump, session).await;
    });
    alive.store(false, Ordering::Release);
}

/// The master session on the bus thread: init with the declared-
/// profile filter, image configuration, station measurement, then the
/// request loop.
async fn session(
    maindevice: &MainDevice<'static>,
    expected: &[StationProfile],
    requests: async_channel::Receiver<BusRequest>,
    ready: mpsc::Sender<Result<Vec<DiscoveredStation>, String>>,
) {
    macro_rules! fail {
        ($($arg:tt)*) => {{
            let _ = ready.send(Err(format!($($arg)*)));
            return;
        }};
    }
    // A filter rejection names the offending station in this detail —
    // `Error::UnknownSubDevice` carries nothing.
    let detail = std::cell::RefCell::new(String::new());
    let mut position = 0usize;
    let groups = Groups {
        group: SubDeviceGroup::default(),
    };
    let groups = match maindevice
        .init::<MAX_SUBDEVICES, _>(ethercat_now, groups, |groups: &Groups, subdevice| {
            let identity = subdevice.identity();
            let matches = expected.get(position).is_some_and(|want| {
                want.vendor_id == identity.vendor_id
                    && want.product_id == identity.product_id
                    && want.revision == identity.revision
                    && want.name.as_deref().is_none_or(|name| name == subdevice.name())
            });
            if matches {
                position += 1;
                Ok(&groups.group)
            } else {
                *detail.borrow_mut() = match expected.get(position) {
                    Some(want) => format!(
                        "station {position} {:?} ({identity}) does not match the declared profile {:?}/{:#010x}/{:#010x}",
                        subdevice.name(),
                        want.name.as_deref().unwrap_or("-"),
                        want.product_id,
                        want.revision
                    ),
                    None => format!(
                        "station {position} {:?} ({identity}) is beyond the {} declared stations",
                        subdevice.name(),
                        expected.len()
                    ),
                };
                Err(Error::UnknownSubDevice)
            }
        })
        .await
    {
        Ok(groups) => groups,
        Err(error) => {
            let detail = detail.into_inner();
            fail!(
                "bus init failed: {error}{}",
                if detail.is_empty() {
                    String::new()
                } else {
                    format!(" — {detail}")
                }
            );
        }
    };
    let group = match groups.group.into_pre_op_pdi(maindevice).await {
        Ok(group) => group,
        Err(error) => fail!("process-data image configuration failed: {error}"),
    };
    let mut discovered = Vec::with_capacity(group.len());
    for (index, subdevice) in group.iter(maindevice).enumerate() {
        let identity = subdevice.identity();
        let io = subdevice.io_raw();
        discovered.push(DiscoveredStation {
            position: index,
            name: subdevice.name().to_string(),
            vendor_id: identity.vendor_id,
            product_id: identity.product_id,
            revision: identity.revision,
            input_bytes: io.inputs().len(),
            output_bytes: io.outputs().len(),
        });
    }
    // The working counter a healthy LRW produces: one increment per
    // non-empty area — EtherCAT counts a station with both directions
    // twice.
    let expected_wkc: u16 = discovered
        .iter()
        .map(|station| u16::from(station.input_bytes > 0) + u16::from(station.output_bytes > 0))
        .sum();
    if ready.send(Ok(discovered)).is_err() {
        return;
    }
    let mut stage = Some(Stage::PreOpPdi(group));
    while let Ok(request) = requests.recv().await {
        match request {
            BusRequest::EnterOp { reply } => {
                let result = match stage.take() {
                    Some(Stage::PreOpPdi(group)) => match group.into_op(maindevice).await {
                        Ok(group) => {
                            stage = Some(Stage::Op(group));
                            Ok(())
                        }
                        // The failed transition consumed the group.
                        Err(error) => Err(classify(error, "OP entry refused")),
                    },
                    Some(Stage::Op(group)) => {
                        stage = Some(Stage::Op(group));
                        Err(TransportError::State(
                            "the bus is already in OP".to_string(),
                        ))
                    }
                    None => Err(TransportError::State(
                        "a failed transition left the bus without its group".to_string(),
                    )),
                };
                let _ = reply.send(result);
            }
            BusRequest::Exchange { outputs, reply } => {
                let result = match stage.as_ref() {
                    Some(Stage::Op(group)) => {
                        exchange_once(maindevice, group, &outputs, expected_wkc).await
                    }
                    _ => Err(TransportError::State("the bus is not in OP".to_string())),
                };
                let reply_value = match result {
                    Ok((outcome, inputs)) => ExchangeReply {
                        outcome: Ok(outcome),
                        inputs,
                    },
                    Err(error) => ExchangeReply {
                        outcome: Err(error),
                        inputs: Vec::new(),
                    },
                };
                let _ = reply.send(reply_value);
            }
            BusRequest::Recover { reply } => {
                let result = match stage.take() {
                    // Re-entry at the boundary: drop through SAFE-OP to
                    // PRE-OP, then climb back — the honest version of
                    // `init`'s PRE-OP → SAFE-OP → OP for a bus already
                    // measured.
                    Some(Stage::Op(group)) => {
                        let reentry = async {
                            let group = group.into_safe_op(maindevice).await?;
                            let group = group.into_pre_op(maindevice).await?;
                            group.into_op(maindevice).await
                        }
                        .await;
                        match reentry {
                            Ok(group) => {
                                stage = Some(Stage::Op(group));
                                Ok(())
                            }
                            Err(error) => Err(classify(error, "recovery re-entry refused")),
                        }
                    }
                    Some(Stage::PreOpPdi(group)) => {
                        stage = Some(Stage::PreOpPdi(group));
                        Err(TransportError::State(
                            "the bus has not entered OP".to_string(),
                        ))
                    }
                    None => Err(TransportError::State(
                        "a failed transition left the bus without its group".to_string(),
                    )),
                };
                let _ = reply.send(result);
            }
        }
    }
}

/// One exchange on the bus thread: the staged output image is copied
/// into the stations' output areas, one `tx_rx` turns the process
/// image, and the stations' input areas become the latched input
/// image.
async fn exchange_once(
    maindevice: &MainDevice<'static>,
    group: &SubDeviceGroup<MAX_SUBDEVICES, MAX_PDI, ethercrab::DefaultLock, Op>,
    outputs: &[u8],
    expected_wkc: u16,
) -> Result<(CycleOutcome, Vec<u8>), TransportError> {
    // Publish the staged output image across the stations' output
    // areas in bus position order.
    let mut offset = 0usize;
    for index in 0..group.len() {
        let subdevice = group
            .subdevice(maindevice, index)
            .map_err(|error| classify(error, "cannot address a station"))?;
        let mut io = subdevice.io_raw_mut();
        let segment = io.outputs();
        let end = offset + segment.len();
        if end > outputs.len() {
            return Err(TransportError::internal(
                "the staged image is shorter than the bus output areas",
            ));
        }
        segment.copy_from_slice(&outputs[offset..end]);
        offset = end;
    }
    if offset != outputs.len() {
        return Err(TransportError::internal(format!(
            "the staged image holds {} bytes, the bus output areas hold {offset}",
            outputs.len()
        )));
    }
    let response = group
        .tx_rx(maindevice)
        .await
        .map_err(|error| classify(error, "exchange"))?;
    // Latch the input image across the stations' input areas.
    let mut inputs = Vec::new();
    for index in 0..group.len() {
        let subdevice = group
            .subdevice(maindevice, index)
            .map_err(|error| classify(error, "cannot address a station"))?;
        let io = subdevice.io_raw();
        inputs.extend_from_slice(io.inputs());
    }
    // Attribute a degraded cycle to the station EtherCrab saw leave
    // OP; a working-counter shortfall with no degraded state cannot be
    // attributed and degrades the whole bus.
    let degraded = response
        .subdevice_states
        .iter()
        .position(|state| *state != SubDeviceState::Op);
    let outcome = if response.working_counter < expected_wkc || degraded.is_some() {
        CycleOutcome::Short { station: degraded }
    } else {
        CycleOutcome::Complete
    };
    Ok((outcome, inputs))
}

/// EtherCrab's errors folded onto the transport seam: timeouts stay
/// timeouts, wire losses are disconnections, state-path refusals are
/// state errors, and everything else is internal.
fn classify(error: Error, context: &str) -> TransportError {
    match error {
        Error::Timeout(timeout) => TransportError::Timeout(format!("{context}: {timeout}")),
        Error::SendFrame | Error::ReceiveFrame | Error::PartialSend { .. } | Error::Pdu(_) => {
            TransportError::Disconnected(format!("{context}: {error}"))
        }
        Error::StateTransition | Error::InvalidState { .. } | Error::SubDevice(_) => {
            TransportError::State(format!("{context}: {error}"))
        }
        _ => TransportError::Internal(format!("{context}: {error}")),
    }
}
