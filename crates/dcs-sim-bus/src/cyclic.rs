//! The cyclic-contract driver for the `sim-cyclic` device kind: an
//! [`IoDriver`] whose points live behind the register protocol of
//! [`BusServer`](crate::BusServer), exchanged as one whole image once
//! per scan under the [`CyclicIoDriver`] contract.
//!
//! Where [`BusDriver`](crate::BusDriver) turns every `read`/`write`
//! into its own request, `CyclicBusDriver` keeps the two images the
//! contract describes: `write` stages the pending output image and
//! `read` serves the held input image — neither touches the wire — and
//! [`exchange`](CyclicIoDriver::exchange) is the only call that does,
//! publishing the staged registers and latching the answered census in
//! one request. The scripted exchange outcomes
//! ([`BusRequest::ScriptExchange`]) let a rig drive every contract
//! case the executor's `CyclicStub` proves in memory — missed, late,
//! and station-attributed or unattributable short exchanges — over the
//! real transport, against the real server.
//!
//! Connection handling differs from `BusDriver`'s deliberately: a
//! cyclic device is expected to ride out a missed exchange — the held
//! image degrades gracefully across scans — so a dropped connection
//! reconnects lazily at the next boundary rather than killing the
//! driver for good. Reconnecting does not re-assert a held writer
//! claim: the claim binds to the attachment that took it, so a link
//! that drops loses it, and taking the field back goes through
//! [`claim_writer`](Self::claim_writer) again — the same deliberate
//! act the promotion path runs, never a silent re-arm.

use crate::client::{LinkError, exchange as roundtrip, refused};
use crate::protocol::{BusRequest, BusResponse, ExchangeOutcome, RegisterInfo, RegisterWrite};
use dcs_core::{
    CyclicIoDriver, Direction, DriverDiagnostics, ExchangeDiagnostics, IoDriver, IoError,
    LinkState, PointId, Sample, Tick, Value, ValueKind,
};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt;
use std::io::{self, BufReader};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::Duration;

/// One logical point's place in a `sim-cyclic` device's register
/// image.
///
/// The device-kind factory derives one `CyclicPoint` per bound
/// `io_point` from the device's declared station layout; the driver
/// serves exactly these points. `direction` decides which image a
/// `read` serves — the held input image for `In` points, the pending
/// output image for `Out` points — while `write` stages either: a
/// register bank owns no direction, so a writable `In` point (a
/// field-held setpoint, say) stages and publishes exactly like an
/// output.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CyclicPoint {
    /// The logical point.
    pub point: PointId,
    /// The device register backing it — its image slot.
    pub register: u16,
    /// The point's declared direction — the model guarantees it agrees
    /// with the channel's.
    pub direction: Direction,
    /// The point's declared value kind — writes carrying any other
    /// variant fail [`IoError::TypeMismatch`] before staging, and a
    /// latched sample of another kind fails `read` the same way.
    pub kind: ValueKind,
}

/// A point's resolved image slot: its register, kind, and the station
/// the device's layout attributes the register to.
#[derive(Debug, Clone)]
struct ImageSlot {
    register: u16,
    kind: ValueKind,
    /// The station holding the register — `None` when the declared
    /// station layout does not cover it, which only a hand-built driver
    /// can produce: a parsed [`CyclicDeviceParameters`](crate::CyclicDeviceParameters)
    /// places every channel. An unattributed point still escalates on
    /// misses and bus-wide shortfalls.
    station: Option<String>,
}

/// The image and exchange state behind the driver's mutex: the two
/// process images plus the contract's miss, shortfall, and counter
/// bookkeeping.
struct Image {
    /// The held input image: register → sample the last completed
    /// exchange latched, stamped with that exchange's scan tick — the
    /// acquisition stamp a point's `stale_after_ticks` budget measures.
    /// Seeded by the connect-time census at `Tick::ZERO`, so before the
    /// first exchange every declared register has a defined — and
    /// immediately agable — held sample.
    latched: BTreeMap<u16, Sample>,
    /// The pending output image `write` stages into — the value the
    /// next completed exchange publishes. Retained across failed and
    /// refused exchanges; seeded neutral at connect.
    staged: BTreeMap<u16, Value>,
    /// The registers whose staged values have not published yet — the
    /// payload of the next `exchange` request. A completed exchange
    /// clears the registers its census covered; registers a short
    /// exchange withheld stay dirty and are re-presented until they
    /// publish.
    dirty: BTreeSet<u16>,
    /// Consecutive uncompleted exchanges — compared against the
    /// declared `exchange_miss_threshold` on read.
    misses: u64,
    /// The stations a short exchange's withheld register set last
    /// partitioned into — their points escalate to `Disconnected`
    /// until a clean exchange.
    short_stations: BTreeSet<String>,
    /// An unattributable shortfall degraded the whole device — every
    /// read escalates until a clean exchange.
    bus_degraded: bool,
    /// The exchange counters diagnostics reports.
    attempted: u64,
    succeeded: u64,
    wkc_mismatches: u64,
    missed_deadlines: u64,
    last_exchange_tick: Option<Tick>,
    /// The last exchange failure or shortfall's description — the
    /// diagnostics surface's `last_error`.
    last_error: Option<String>,
}

/// An [`IoDriver`] whose logical points are registers in a
/// [`BusServer`](crate::BusServer) reached over TCP, exchanged as a
/// whole image once per scan under the [`CyclicIoDriver`] contract —
/// the simulated form of a cyclic fieldbus device, proving the
/// decision-78 exchange semantics end to end without hardware.
///
/// `read` serves the held image — the census the last completed
/// exchange latched, `In` and `Out` points alike — and `write` stages
/// into the output image: neither performs a transport operation, and
/// the [`IoError::UnknownPoint`]/[`IoError::TypeMismatch`] semantics
/// are the same as any point-wise driver's. [`exchange`](CyclicIoDriver::exchange)
/// is the only call that touches the wire: one `exchange` request
/// carrying every register staged since the last completed exchange,
/// its answer latching the input image atomically at the scan tick.
/// A command-staged write therefore publishes in the same exchange the
/// applying scan runs, and a component's scan-`t` write publishes in
/// scan `t + 1`'s — the contract's documented one-scan actuation
/// delay.
///
/// Failure semantics are the contract's:
///
/// - a failed exchange — dropped connection, timed-out or refused
///   request, incoherent answer — completes nothing: the held image
///   keeps serving, the staged image is retained, and the miss counts
///   once at the boundary;
/// - reads keep serving the held image while consecutive misses stay
///   under the declared `exchange_miss_threshold`, then escalate to
///   [`IoError::Disconnected`];
/// - an answered exchange that serves fewer registers than the image
///   declares — a working-counter shortfall — attributes its withheld
///   set to the stations it partitions into and degrades only those
///   stations' points; a withheld set no station layout can name
///   degrades the whole device. Either way the mismatch counts under
///   `working_counter_mismatches` and the exchange itself succeeded;
/// - a completed exchange resets the miss count, clears every
///   shortfall, and relatches the image — link health recovers with
///   it;
/// - a `late` answer counts `missed_deadlines` while otherwise
///   completing normally.
///
/// The connection the first `exchange` drops is not the end: unlike
/// the point-wise `BusDriver`, a dead connection reconnects lazily on
/// the next request, so a scripted missed exchange is a recoverable
/// miss rather than a permanent sever. What a reconnect does not
/// restore is the writer claim — claim holds bind to their
/// connection, so a dropped link releases them and output-bearing
/// exchanges then answer the `fenced` verdict until
/// [`claim_writer`](Self::claim_writer) runs again.
///
/// Like `BusDriver`, the driver is field-observing —
/// `capture_state` keeps its `None` default — and [`Sync`] through
/// its internal locks.
pub struct CyclicBusDriver {
    /// The resolved server addresses, retried in order on reconnect.
    addresses: Vec<SocketAddr>,
    /// The per-request timeout, applied to each request's write and
    /// response wait and to reconnect attempts.
    timeout: Duration,
    /// The live connection: `Some` while the link is up, `None` after a
    /// failed request — the next request re-establishes it.
    connection: Mutex<Option<BufReader<TcpStream>>>,
    /// Point → image slot.
    points: HashMap<PointId, ImageSlot>,
    /// Every register the image declares — the census set an answered
    /// exchange is compared against.
    registers: BTreeSet<u16>,
    /// Station name → the registers it holds — the attribution table a
    /// short exchange's withheld set is matched against.
    stations: BTreeMap<String, BTreeSet<u16>>,
    /// The point a failed exchange's `IoError` names — the lowest
    /// covered id, the executor's boundary attribution.
    attribution: PointId,
    /// The declared `exchange_miss_threshold`.
    miss_threshold: u64,
    /// The image and exchange state.
    image: Mutex<Image>,
    /// The last transport-level failure, for the link-health surface —
    /// `None` while no request has failed.
    last_failure: Mutex<Option<LinkError>>,
}

impl CyclicBusDriver {
    /// The request timeout [`connect`](Self::connect) applies —
    /// [`BusDriver::DEFAULT_TIMEOUT`](crate::BusDriver::DEFAULT_TIMEOUT)'s
    /// five seconds.
    pub const DEFAULT_TIMEOUT: Duration = crate::BusDriver::DEFAULT_TIMEOUT;

    /// Connects to the device server at `addr` with the
    /// [`DEFAULT_TIMEOUT`](Self::DEFAULT_TIMEOUT) request timeout,
    /// serving `points` over the station layout `stations` declares —
    /// see [`connect_with_timeout`](Self::connect_with_timeout).
    pub fn connect<A: ToSocketAddrs>(
        addr: A,
        points: &[CyclicPoint],
        stations: &BTreeMap<String, BTreeSet<u16>>,
        exchange_miss_threshold: u64,
    ) -> io::Result<Self> {
        Self::connect_with_timeout(
            addr,
            Self::DEFAULT_TIMEOUT,
            points,
            stations,
            exchange_miss_threshold,
        )
    }

    /// Connects with an explicit `timeout` applied to each request's
    /// write, response wait, and later reconnect attempts.
    ///
    /// The connect performs one register census before serving: every
    /// register `points` declares must exist on the server with the
    /// declared value kind — a device serving a different register map
    /// fails here, at assembly, not mid-scan — and the answered census
    /// seeds the held input image at `Tick::ZERO`, the pre-first-
    /// exchange state the contract describes.
    ///
    /// `stations` carries the declared station layout (station name →
    /// its registers): the attribution table a short exchange's
    /// withheld set is partitioned against.
    /// `exchange_miss_threshold` is the device's declared consecutive-
    /// miss budget: at or past it, reads escalate to
    /// [`IoError::Disconnected`].
    pub fn connect_with_timeout<A: ToSocketAddrs>(
        addr: A,
        timeout: Duration,
        points: &[CyclicPoint],
        stations: &BTreeMap<String, BTreeSet<u16>>,
        exchange_miss_threshold: u64,
    ) -> io::Result<Self> {
        if exchange_miss_threshold == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "exchange_miss_threshold must be at least 1",
            ));
        }
        let addresses: Vec<SocketAddr> = addr.to_socket_addrs()?.collect();
        if addresses.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "the device server address resolved to nothing",
            ));
        }
        // Every point's image slot, with its register's station
        // attribution resolved; two points may share a register only
        // when they agree on its declared kind.
        let mut mapped = HashMap::with_capacity(points.len());
        // Sorted so the census probe reports the lowest failing
        // register deterministically.
        let mut register_kinds: BTreeMap<u16, ValueKind> = BTreeMap::new();
        let register_station: HashMap<u16, &str> = stations
            .iter()
            .flat_map(|(station, registers)| {
                registers
                    .iter()
                    .map(move |register| (*register, station.as_str()))
            })
            .collect();
        let mut registers = BTreeSet::new();
        for point in points {
            registers.insert(point.register);
            match register_kinds.entry(point.register) {
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(point.kind);
                }
                std::collections::btree_map::Entry::Occupied(entry)
                    if *entry.get() == point.kind => {}
                std::collections::btree_map::Entry::Occupied(_) => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!(
                            "register {} is declared with conflicting value kinds",
                            point.register
                        ),
                    ));
                }
            }
            mapped.insert(
                point.point,
                ImageSlot {
                    register: point.register,
                    kind: point.kind,
                    station: register_station
                        .get(&point.register)
                        .map(|name| name.to_string()),
                },
            );
        }
        let stream = connect_stream(&addresses, timeout)?;
        let mut connection = BufReader::new(stream);
        // The connect-time census: probes that the server holds every
        // declared register with the declared kind, and seeds the held
        // input image at `Tick::ZERO` — real field data, but stamped
        // as the pre-run state no scan produced.
        let census = match roundtrip(&mut connection, &BusRequest::ListRegisters) {
            Ok(BusResponse::Registers { registers }) => registers,
            Ok(BusResponse::Error { error }) => {
                return Err(io::Error::new(
                    io::ErrorKind::ConnectionRefused,
                    format!("the device server refused the register census: {error}"),
                ));
            }
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "the peer is not speaking the register protocol",
                ));
            }
            Err(error) => {
                return Err(io::Error::new(
                    io::ErrorKind::ConnectionRefused,
                    format!("the device server did not answer the register census: {error}"),
                ));
            }
        };
        let served: HashMap<u16, Sample> = census
            .iter()
            .map(|info| (info.register, info.sample))
            .collect();
        let mut latched = BTreeMap::new();
        for (&register, &kind) in &register_kinds {
            let Some(&sample) = served.get(&register) else {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!("the device server holds no register {register}"),
                ));
            };
            if sample.value.kind() != kind {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    format!(
                        "register {register} is declared {kind:?}, the device serves {:?}",
                        sample.value.kind()
                    ),
                ));
            }
            latched.insert(
                register,
                Sample {
                    tick: Tick::ZERO,
                    ..sample
                },
            );
        }
        let staged: BTreeMap<u16, Value> = register_kinds
            .iter()
            .map(|(&register, &kind)| (register, neutral(kind)))
            .collect();
        Ok(Self {
            addresses,
            timeout,
            connection: Mutex::new(Some(connection)),
            attribution: points
                .iter()
                .map(|point| point.point)
                .min()
                .unwrap_or(PointId(0)),
            points: mapped,
            registers,
            stations: stations.clone(),
            miss_threshold: exchange_miss_threshold,
            image: Mutex::new(Image {
                latched,
                staged,
                dirty: BTreeSet::new(),
                misses: 0,
                short_stations: BTreeSet::new(),
                bus_degraded: false,
                attempted: 0,
                succeeded: 0,
                wkc_mismatches: 0,
                missed_deadlines: 0,
                last_exchange_tick: None,
                last_error: None,
            }),
            last_failure: Mutex::new(None),
        })
    }

    /// Whether the link to the server is live — `false` between a
    /// failed request's drop and the next request's reconnect.
    pub fn connected(&self) -> bool {
        self.connection.lock().unwrap().is_some()
    }

    /// The last transport-level failure on the link, if one has
    /// occurred — the driver-level record behind the diagnostics
    /// surface.
    pub fn last_failure(&self) -> Option<LinkError> {
        self.last_failure.lock().unwrap().clone()
    }

    /// Sends `request` over the live connection, reconnecting first
    /// when the link is down — the lazy re-attach a cyclic device
    /// rides its missed exchanges out with.
    ///
    /// A failed request drops the connection and records the failure;
    /// a refused request (a [`BusResponse::Error`]) answers the
    /// server's own [`BusError`](crate::BusError) verdict — the caller
    /// maps it onto the vocabulary it reports.
    fn request(&self, request: &BusRequest) -> Result<BusResponse, LinkError> {
        let mut connection = self.connection.lock().unwrap();
        if connection.is_none() {
            // A dead cyclic link is not a dead driver: the next request
            // re-attaches. A failed reconnect reports `Disconnected`
            // like any other unanswerable request.
            *connection = match connect_stream(&self.addresses, self.timeout) {
                Ok(stream) => Some(BufReader::new(stream)),
                Err(_) => None,
            };
        }
        let Some(stream) = connection.as_mut() else {
            let error = LinkError::Disconnected;
            *self.last_failure.lock().unwrap() = Some(error.clone());
            return Err(error);
        };
        match roundtrip(stream, request) {
            Ok(response) => Ok(response),
            Err(error) => {
                *connection = None;
                *self.last_failure.lock().unwrap() = Some(error.clone());
                Err(error)
            }
        }
    }

    /// Advances the device server's bank one tick of `dt` time units —
    /// the explicit step behind the register protocol, stepping any
    /// declared dynamics by `dt` — and returns the bank's new tick.
    /// `dt` must be finite and non-negative. Stepping mutates the
    /// shared device, so while an attachment holds the write-ownership
    /// claim a non-holder's step answers [`LinkError::Fenced`].
    pub fn step(&self, dt: f64) -> Result<Tick, LinkError> {
        match self.request(&BusRequest::Step { dt })? {
            BusResponse::Stepped { tick } => Ok(tick),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Takes the device's write-ownership claim for `owner` — the
    /// single-writer arbitration the failover decision fences a
    /// superseded active out with, on the same terms
    /// [`BusDriver::claim_writer`](crate::BusDriver::claim_writer)
    /// documents: an unconditional, attachment-bound grant.
    ///
    /// While the claim stands, an `exchange` carrying staged outputs
    /// from a non-holder answers the fenced verdict and completes
    /// nothing; a census-only exchange — a tracking standby's, which
    /// stages nothing — stays open to every attachment. The claim dies
    /// with the connection holding it, so a link that dropped must
    /// claim again: reconnects never silently re-arm it.
    pub fn claim_writer(&self, owner: u64) -> Result<(), LinkError> {
        match self.request(&BusRequest::ClaimWriter { owner })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Releases this attachment's hold on the write-ownership claim —
    /// the explicit half of the claim's release rule; the other is the
    /// connection dropping.
    pub fn release_writer(&self) -> Result<(), LinkError> {
        match self.request(&BusRequest::ReleaseWriter)? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Stamps `register`'s stored sample with `quality` —
    /// [`BusRequest::InjectQuality`], open to every attachment like the
    /// point-wise driver's.
    pub fn inject_quality(
        &self,
        register: u16,
        quality: dcs_core::Quality,
    ) -> Result<(), LinkError> {
        match self.request(&BusRequest::InjectQuality { register, quality })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Restores `register`'s stored sample to `Quality::Good` — the
    /// clear half of the injection pair.
    pub fn clear_quality(&self, register: u16) -> Result<(), LinkError> {
        match self.request(&BusRequest::ClearQuality { register })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Lists every register the device server holds — the census a rig
    /// inspects the device with.
    pub fn list_registers(&self) -> Result<Vec<RegisterInfo>, LinkError> {
        match self.request(&BusRequest::ListRegisters)? {
            BusResponse::Registers { registers } => Ok(registers),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Appends `outcomes` to the device's scripted exchange queue —
    /// [`BusRequest::ScriptExchange`]: the development harness that
    /// decides what the next `exchange` requests observe. Scripting is
    /// development tooling, never fenced, so a rig drives outcomes from
    /// its own attachment while a controller pair owns the field.
    pub fn script_exchange(&self, outcomes: &[ExchangeOutcome]) -> Result<(), LinkError> {
        match self.request(&BusRequest::ScriptExchange {
            outcomes: outcomes.to_vec(),
        })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Drops the connection and reports the peer as gone: an answer
    /// that decodes but does not correspond to the request means the
    /// peer is not a device server, and the link can no longer be
    /// trusted — the next request reconnects.
    fn protocol_violation(&self) -> LinkError {
        *self.connection.lock().unwrap() = None;
        let error = LinkError::Disconnected;
        *self.last_failure.lock().unwrap() = Some(error.clone());
        error
    }

    /// Records one uncompleted exchange under `image`: the miss count,
    /// the diagnostics error text, and the boundary `IoError` —
    /// `Fenced` when the field's own verdict refused it,
    /// `Disconnected` for everything else. Nothing publishes, nothing
    /// latches, the staged image is retained.
    fn miss(&self, image: &mut Image, error: LinkError) -> IoError {
        image.misses += 1;
        let attribution = self.attribution;
        match error {
            LinkError::Fenced => {
                image.last_error =
                    Some("exchange refused: another attachment owns register writes".to_string());
                IoError::Fenced(attribution)
            }
            error => {
                image.last_error = Some(format!("the exchange did not complete: {error}"));
                IoError::Disconnected(attribution)
            }
        }
    }

    /// One process-image exchange for scan `tick` over the live
    /// connection — the whole `exchange` request body. Called with the
    /// image lock held so the staged set a mid-exchange `write` adds to
    /// cannot be lost between the payload build and the dirty-set
    /// update.
    fn exchange_once(&self, image: &mut Image, tick: Tick) -> Result<(), IoError> {
        let outputs: Vec<RegisterWrite> = image
            .dirty
            .iter()
            .map(|&register| RegisterWrite {
                register,
                value: image.staged[&register],
            })
            .collect();
        let response = self.request(&BusRequest::Exchange { outputs });
        let (served, late) = match response {
            Ok(BusResponse::Exchanged { registers, late }) => (registers, late),
            Ok(BusResponse::Error { error }) => {
                return Err(self.miss(image, refused(error)));
            }
            Ok(_) => {
                let error = self.protocol_violation();
                return Err(self.miss(image, error));
            }
            Err(error) => return Err(self.miss(image, error)),
        };
        image.misses = 0;
        image.succeeded += 1;
        image.last_exchange_tick = Some(tick);
        if late {
            image.missed_deadlines += 1;
        }
        // The answered census against the declared image: registers the
        // answer withholds are the working-counter shortfall.
        let answered: BTreeSet<u16> = served.iter().map(|info| info.register).collect();
        let missing: BTreeSet<u16> = self
            .registers
            .iter()
            .copied()
            .filter(|register| !answered.contains(register))
            .collect();
        if missing.is_empty() {
            image.short_stations.clear();
            image.bus_degraded = false;
        } else {
            image.wkc_mismatches += 1;
            // Attribution: the withheld set that partitions exactly
            // into whole declared stations names them; anything else —
            // a partial station, a span — is unattributable and
            // degrades the whole device.
            let covered: BTreeSet<&String> = self
                .stations
                .iter()
                .filter(|(_, registers)| !registers.is_empty() && registers.is_subset(&missing))
                .map(|(station, _)| station)
                .collect();
            let covers: BTreeSet<u16> = covered
                .iter()
                .flat_map(|station| self.stations[*station].iter().copied())
                .collect();
            if !covered.is_empty() && covers == missing {
                image.short_stations = covered.iter().map(|station| station.to_string()).collect();
                image.bus_degraded = false;
                image.last_error = Some(format!(
                    "working counter shortfall attributed to {}",
                    image
                        .short_stations
                        .iter()
                        .map(|station| format!("station {station:?}"))
                        .collect::<Vec<_>>()
                        .join(", ")
                ));
            } else {
                image.short_stations.clear();
                image.bus_degraded = true;
                image.last_error = Some("unattributable working counter shortfall".to_string());
            }
        }
        // Latch the answered registers at the scan tick — the
        // acquisition stamp a `stale_after_ticks` budget measures —
        // atomically with respect to reads; withheld registers keep
        // their held samples.
        for info in served {
            if self.registers.contains(&info.register) {
                image.latched.insert(
                    info.register,
                    Sample {
                        tick,
                        ..info.sample
                    },
                );
            }
        }
        // Published outputs leave the staged set; registers the
        // shortfall withheld stay dirty — the exchange completed
        // nothing for them and the next exchange re-presents them.
        image.dirty.retain(|register| missing.contains(register));
        Ok(())
    }
}

/// Connects a stream to the first answering of `addresses` with the
/// driver's request semantics — the timeouts and `nodelay` a
/// [`BusDriver`](crate::BusDriver) connection carries.
fn connect_stream(addresses: &[SocketAddr], timeout: Duration) -> io::Result<TcpStream> {
    let mut failure = io::Error::new(io::ErrorKind::NotFound, "no device server address");
    for &address in addresses {
        let attempt = loop {
            match TcpStream::connect_timeout(&address, timeout) {
                // An interrupted connect attempt is abandoned with its
                // socket and retried fresh — a caught signal (e.g. a
                // spawned helper's `SIGCHLD`) is not a reachability
                // verdict on the address.
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                other => break other,
            }
        };
        match attempt {
            Ok(stream) => {
                stream.set_read_timeout(Some(timeout))?;
                stream.set_write_timeout(Some(timeout))?;
                stream.set_nodelay(true)?;
                return Ok(stream);
            }
            Err(error) => failure = error,
        }
    }
    Err(failure)
}

/// A register's value before anything stages it — the kind's zero.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

impl fmt::Debug for CyclicBusDriver {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CyclicBusDriver")
            .field("connected", &self.connected())
            .field("points", &self.points.len())
            .finish_non_exhaustive()
    }
}

impl IoDriver for CyclicBusDriver {
    /// Serves the held image — never the transport. The read escalates
    /// to [`IoError::Disconnected`] once the miss count reaches the
    /// declared `exchange_miss_threshold`, while the whole device is
    /// degraded by an unattributable shortfall, or while the point's
    /// station is one a short exchange named. `In` and `Out` points
    /// alike serve the latched census — the register bank holds values,
    /// not directions, so an `Out` read reports the field's last
    /// published value, never the pending staged image: the staged map
    /// is evidence of what the next completed exchange publishes, and
    /// the held image is what the field demonstrably carried.
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let image = self.image.lock().unwrap();
        let slot = self
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if image.misses >= self.miss_threshold
            || image.bus_degraded
            || slot
                .station
                .as_ref()
                .is_some_and(|station| image.short_stations.contains(station))
        {
            return Err(IoError::Disconnected(point));
        }
        let sample = image
            .latched
            .get(&slot.register)
            .copied()
            .ok_or(IoError::UnknownPoint(point))?;
        // A register serving a kind other than the point's declared
        // kind is the same failure a point-wise driver reports: never a
        // coercion.
        if sample.value.kind() != slot.kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: slot.kind,
                found: sample.value,
            });
        }
        Ok(sample)
    }

    /// Stages `value` into the pending output image — a local mutation
    /// the next completed exchange publishes, never a transport call,
    /// and never an escalation: the staged image survives a failed
    /// exchange and re-publishes on the next completed one.
    ///
    /// Every served point stages, `In` and `Out` alike: a register bank
    /// owns no direction, and a writable `In` point — a field-held
    /// setpoint — publishes its staged value exactly like an output,
    /// its read then serving the latched echo the exchange brought back.
    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut image = self.image.lock().unwrap();
        let slot = self
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != slot.kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: slot.kind,
                found: value,
            });
        }
        // The bank refuses a non-finite `Float` — and a staged one
        // would refuse every later exchange the same way, wedging the
        // image permanently; the refusal lands here, where the
        // caller's mistake is still a point error.
        if let Value::Float(v) = value
            && !v.is_finite()
        {
            return Err(IoError::InvalidValue { point });
        }
        image.staged.insert(slot.register, value);
        image.dirty.insert(slot.register);
        Ok(())
    }

    /// The link's health for the snapshot's I/O-health section:
    /// `disconnected` while a miss stands or the link is down, the last
    /// exchange failure or shortfall's description, and the exchange
    /// counters — the cyclic half of the surface, beside the per-point
    /// [`IoError`]s `read` returns.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let image = self.image.lock().unwrap();
        Some(DriverDiagnostics {
            link: if image.misses > 0 || !self.connected() {
                LinkState::Disconnected
            } else {
                LinkState::Connected
            },
            last_error: image.last_error.clone(),
            exchange: Some(ExchangeDiagnostics {
                attempted: image.attempted,
                succeeded: image.succeeded,
                working_counter_mismatches: image.wkc_mismatches,
                last_exchange_tick: image.last_exchange_tick,
                missed_deadlines: image.missed_deadlines,
            }),
        })
    }

    /// The cyclic surface — this driver implements the contract.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        Some(self)
    }
}

impl CyclicIoDriver for CyclicBusDriver {
    /// One process-image exchange for scan `tick`: the registers staged
    /// since the last completed exchange publish, then the answered
    /// census latches into the input image at `tick` — atomically, so
    /// a scan never reads a half-moved image. The only transport call
    /// the driver makes per exchange.
    fn exchange(&self, tick: Tick) -> Result<(), IoError> {
        let mut image = self.image.lock().unwrap();
        image.attempted += 1;
        self.exchange_once(&mut image, tick)
    }
}
