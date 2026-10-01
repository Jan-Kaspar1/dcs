//! The register-mapped driver: an [`IoDriver`] whose points live behind
//! the register protocol of [`BusServer`](crate::BusServer).

use crate::protocol::{
    BusError, BusRequest, BusResponse, MAX_FRAME, RegisterInfo, decode_response, encode_request,
    read_frame,
};
use dcs_core::{
    DriverDiagnostics, IoDriver, IoError, LinkState, PointId, Quality, Sample, Tick, Value,
    ValueKind,
};
use std::collections::HashMap;
use std::fmt;
use std::io::{BufReader, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// One logical point's mapping onto a device register.
///
/// The device-kind factory derives one `PointRegister` per bound
/// `io_point` from the device's declared register parameters; the driver
/// then serves exactly these points.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct PointRegister {
    /// The logical point.
    pub point: PointId,
    /// The device register backing it.
    pub register: u16,
    /// The point's declared value kind — writes carrying any other
    /// variant fail [`IoError::TypeMismatch`] before any request leaves.
    pub kind: ValueKind,
}

/// A failure on a [`BusDriver`] operation.
///
/// This is the transport- and protocol-level error vocabulary; the
/// [`IoDriver`] implementation collapses it into the point-addressed
/// [`IoError`] contract via [`at_point`](Self::at_point), and the driver
/// records the last such failure for the link-health surface
/// ([`last_failure`](BusDriver::last_failure)).
#[derive(Debug, Clone, PartialEq)]
pub enum LinkError {
    /// There is no live connection to the device server: the link
    /// failed mid-request, the driver already dropped it, or the
    /// latest re-attach found the endpoint unanswerable. A dead link
    /// is not a dead driver — the next access re-attaches lazily, so
    /// the failure reads "not answerable now", never "dead for good".
    Disconnected,
    /// The server did not answer within the driver's configured
    /// timeout. The connection is dropped — a late answer would desync
    /// the request/response pairing — and the next request re-attaches
    /// on a fresh stream rather than risk reading a stale response.
    Timeout,
    /// The server refused the request itself — a payload that does not
    /// decode as a [`BusRequest`]. `detail` is the server's diagnostic
    /// text.
    InvalidRequest(String),
    /// The request mutates the shared device but another attachment
    /// holds the device's write-ownership claim — the fencing verdict
    /// of the failover decision. Reads still succeed; writing again
    /// requires taking the claim back with
    /// [`claim_writer`](Self::claim_writer).
    Fenced,
}

impl LinkError {
    /// The [`IoError`] this failure presents as at `point`.
    ///
    /// Transport failures become that point's `Disconnected`/`Timeout`.
    /// A refused or incoherent answer means the peer is not serving the
    /// point the protocol promises, which surfaces as `Disconnected`.
    pub fn at_point(self, point: PointId) -> IoError {
        match self {
            Self::Disconnected | Self::InvalidRequest(_) => IoError::Disconnected(point),
            Self::Timeout => IoError::Timeout(point),
            Self::Fenced => IoError::Fenced(point),
        }
    }
}

impl fmt::Display for LinkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Disconnected => write!(f, "no live connection to the device server"),
            Self::Timeout => write!(f, "device server did not answer in time"),
            Self::InvalidRequest(detail) => {
                write!(f, "server refused the request: {detail}")
            }
            Self::Fenced => {
                write!(
                    f,
                    "field mutation refused: another attachment owns register writes"
                )
            }
        }
    }
}

impl std::error::Error for LinkError {}

impl BusError {
    /// The [`IoError`] this protocol-reported failure presents as at
    /// `point` — the point the caller addressed, whose register the
    /// error names.
    pub(crate) fn at_point(&self, point: PointId) -> IoError {
        match *self {
            Self::UnknownRegister { .. } => IoError::UnknownPoint(point),
            Self::KindMismatch {
                expected, found, ..
            } => IoError::TypeMismatch {
                point,
                expected,
                found,
            },
            Self::InvalidRequest { ref detail } => {
                LinkError::InvalidRequest(detail.clone()).at_point(point)
            }
            Self::Fenced { .. } => IoError::Fenced(point),
        }
    }
}

/// Collapses a server-reported refusal on a non-point request into the
/// link vocabulary: the fencing verdict is its own named variant;
/// anything else is a request the server could not serve.
pub(crate) fn refused(error: BusError) -> LinkError {
    match error {
        BusError::Fenced { .. } => LinkError::Fenced,
        other => LinkError::InvalidRequest(format!("{other:?}")),
    }
}

/// The connection behind [`BusDriver`]'s lock: `Some` while the link
/// is live, `None` after a failed exchange — the next request lazily
/// re-attaches — plus the re-attach bookkeeping and the link-level
/// failure record [`IoDriver::diagnostics`] reports.
struct Connection {
    stream: Option<BufReader<TcpStream>>,
    /// The writer token a successful `claim_writer` recorded —
    /// re-asserted through `ensure_writer` on every re-attach, so a
    /// device restart's dropped claim re-arms for the same owner.
    /// `None` on an attachment that never claimed, released its claim
    /// at demotion, or watched the field's verdict show a different
    /// owner standing.
    owner: Option<u64>,
    /// The earliest instant the next re-attach may run: a failed attach
    /// — or a failed exchange, which counts as the window's attempt —
    /// backs the next attempt off by
    /// [`REATTACH_INTERVAL`](BusDriver::REATTACH_INTERVAL), so a dead
    /// endpoint costs one connect attempt per interval rather than one
    /// per point's access.
    retry_at: Instant,
    /// The most recent transport- or protocol-level failure. The
    /// failure that severed the link stays recorded — the
    /// `Disconnected`s every later access reports are its consequence,
    /// not new failures — and the first successful exchange clears it,
    /// so the health surface reports the standing failure while it
    /// stands and nothing once it clears.
    last_failure: Option<LinkError>,
}

impl Connection {
    /// Re-establishes the link and re-arms the recorded writer claim —
    /// the [`BusRequest::EnsureWriter`] grant a reconnecting field
    /// owner asserts so a device restart's dropped claim re-arms for
    /// the same owner rather than preempting whichever attachment
    /// claimed during the outage. A `fenced` answer keeps the fresh
    /// link but forgets the recorded owner: the field already serves a
    /// different claim, and this attachment's mutations will fence
    /// honestly against it. Any other failed attach drops the stream
    /// and backs the next attempt off.
    fn reattach(&mut self, addresses: &[SocketAddr], timeout: Duration) {
        let mut stream = match connect_stream(addresses, timeout) {
            Ok(stream) => BufReader::new(stream),
            Err(_) => {
                self.retry_at = Instant::now() + BusDriver::REATTACH_INTERVAL;
                return;
            }
        };
        if let Some(owner) = self.owner {
            match exchange(&mut stream, &BusRequest::EnsureWriter { owner }) {
                Ok(BusResponse::Done) => {}
                Ok(BusResponse::Error {
                    error: BusError::Fenced { .. },
                }) => {
                    self.owner = None;
                }
                Ok(_) => {
                    self.last_failure = Some(LinkError::Disconnected);
                    self.retry_at = Instant::now() + BusDriver::REATTACH_INTERVAL;
                    return;
                }
                Err(error) => {
                    self.last_failure = Some(error);
                    self.retry_at = Instant::now() + BusDriver::REATTACH_INTERVAL;
                    return;
                }
            }
        }
        self.stream = Some(stream);
    }

    /// Runs `request` on the live link, keeping the connection's
    /// failure bookkeeping: a successful exchange clears the standing
    /// failure; a `fenced` answer forgets the recorded writer token —
    /// the field's standing claim names another owner, so the
    /// attachment holds nothing left to re-assert; and a failed
    /// exchange drops the link — the response stream's position is
    /// unknown afterward — and counts as the window's re-attach
    /// attempt.
    fn exchange_on(&mut self, request: &BusRequest) -> Result<BusResponse, LinkError> {
        let Some(stream) = self.stream.as_mut() else {
            return Err(LinkError::Disconnected);
        };
        match exchange(stream, request) {
            Ok(response) => {
                if matches!(
                    response,
                    BusResponse::Error {
                        error: BusError::Fenced { .. }
                    }
                ) {
                    self.owner = None;
                }
                self.last_failure = None;
                Ok(response)
            }
            Err(error) => {
                self.stream = None;
                self.retry_at = Instant::now() + BusDriver::REATTACH_INTERVAL;
                self.last_failure = Some(error.clone());
                Err(error)
            }
        }
    }
}

/// Connects a stream to the first answering of `addresses` with the
/// driver's request semantics — the timeouts and `nodelay` every
/// connection carries.
pub(crate) fn connect_stream(
    addresses: &[SocketAddr],
    timeout: Duration,
) -> std::io::Result<TcpStream> {
    let mut failure = std::io::Error::new(std::io::ErrorKind::NotFound, "no device server address");
    for &address in addresses {
        let attempt = loop {
            match TcpStream::connect_timeout(&address, timeout) {
                // An interrupted connect attempt is abandoned with its
                // socket and retried fresh — a caught signal (e.g. a
                // spawned helper's `SIGCHLD`) is not a reachability
                // verdict on the address.
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                other => break other,
            }
        };
        match attempt {
            Ok(stream) => {
                stream.set_read_timeout(Some(timeout))?;
                stream.set_write_timeout(Some(timeout))?;
                // Requests are small and answered immediately;
                // coalescing delays would only add latency.
                stream.set_nodelay(true)?;
                return Ok(stream);
            }
            Err(error) => failure = error,
        }
    }
    Err(failure)
}

/// Writes the request frame and reads the response frame on `stream`,
/// translating `io::Error`s into the transport vocabulary.
pub(crate) fn exchange(
    stream: &mut BufReader<TcpStream>,
    request: &BusRequest,
) -> Result<BusResponse, LinkError> {
    fn transport(error: std::io::Error) -> LinkError {
        match error.kind() {
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => LinkError::Timeout,
            // A dropped connection, a mid-frame EOF, and an oversized or
            // undecodable response all mean the peer is gone or is not
            // speaking the protocol — Disconnected either way.
            _ => LinkError::Disconnected,
        }
    }

    stream
        .get_mut()
        .write_all(&encode_request(request))
        .map_err(transport)?;
    let Some(body) = read_frame(stream, MAX_FRAME).map_err(transport)? else {
        return Err(LinkError::Disconnected);
    };
    decode_response(&body).map_err(|_| LinkError::Disconnected)
}

/// An [`IoDriver`] whose logical points are registers in a
/// [`BusServer`](crate::BusServer) reached over TCP.
///
/// `BusDriver` is the second-transport proof that the I/O abstraction
/// hides more than one wire protocol: where `dcs-sim-net`'s
/// `RemoteDriver` speaks line-delimited JSON addressed by point, this
/// driver speaks the binary register protocol, mapping each served
/// [`PointId`] to the register address the plant model's device
/// parameters declare. Components and the executor use it through
/// `&dyn IoDriver` exactly as they use an in-process `SimDriver`.
///
/// The driver is the field-observing kind: the register bank lives in
/// the server process, so
/// [`capture_state`](IoDriver::capture_state) stays at its `None`
/// default — there is nothing local to checkpoint, the device-side
/// element state a declared dynamics document merges included.
/// [`step`](Self::step) issues the explicit device-clock advance with
/// the caller's `dt`, so a field-owning controller paces the bank —
/// and any declared process on it — exactly where a local driver's
/// step call sat; a second attached client observes the same stepped
/// registers.
///
/// Fencing: the device arbitrates a single writer — the failover
/// decision's rule that a promoted peer's field writes must beat a
/// still-alive old owner's. [`claim_writer`](Self::claim_writer) takes
/// the claim under this attachment and [`release_writer`](Self::release_writer)
/// drops it; while any attachment holds it, a non-holder's `write`
/// answers [`IoError::Fenced`] and its `step` [`LinkError::Fenced`].
/// Quality injection — [`inject_quality`](Self::inject_quality) /
/// [`clear_quality`](Self::clear_quality) — is development tooling
/// beside the claim: open to every attachment, never fenced.
///
/// Failure handling: a point the driver's map does not serve is
/// [`IoError::UnknownPoint`] without a request; a write carrying the
/// wrong value kind is [`IoError::TypeMismatch`] likewise. On the wire,
/// any failed exchange — broken pipe, closed connection, timed-out or
/// oversized response, undecodable answer — drops the connection, is
/// recorded for [`last_failure`](Self::last_failure), and the *next*
/// access re-attaches lazily: a device outage degrades every access to
/// `Disconnected` while it lasts rather than killing the driver for
/// good, and a device that returns — a restarted server on its bound
/// address — is served by the same `BusDriver`. Contact attempts are
/// bounded to one per [`REATTACH_INTERVAL`](Self::REATTACH_INTERVAL):
/// a dead endpoint costs each access burst one refused connect rather
/// than one connect-timeout per point, and an endpoint that completes
/// the handshake but never answers — a frozen or blackholed peer —
/// costs one timed-out exchange per interval rather than one per
/// request, so a scan's burst of accesses stalls once near the request
/// timeout instead of once per point. A timed-out response could
/// arrive after the fact and pair with a later request, so the driver
/// never reuses a suspect link. `BusDriver` is [`Sync`] through its
/// internal locks, like the driver contract expects.
///
/// An attachment that claimed the device —
/// [`claim_writer`](Self::claim_writer) — records the token, and every
/// re-attach re-asserts it through [`BusRequest::EnsureWriter`] before
/// the pending request runs: a device restart or link drop releases
/// the claim server-side, so the owner re-arms it — conditionally,
/// never preempting a different claim another attachment took during
/// the outage. A `fenced` verdict — on the re-arm or on any mutating
/// request — forgets the recorded token, and
/// [`release_claim`](Self::release_claim) drops it at demotion, so
/// only an attachment the field still owes ownership re-arms.
///
/// Diagnostics: [`IoDriver::diagnostics`] reports the link as
/// [`LinkState::Disconnected`] while no live connection stands — a
/// dead or unanswerable device server, including the span between a
/// severed link's drop and its re-attach — with the last transport
/// failure's description, which the first successful exchange after
/// recovery clears so the surface describes the link as it is. That
/// surface is link health, distinct from the per-point [`IoError`]s
/// `read`/`write` return: every point's read failing with
/// `Disconnected` and the link reporting `disconnected` are the same
/// event told at the two levels the telemetry contract keeps separate.
pub struct BusDriver {
    /// The resolved server addresses, retried in order on re-attach.
    addresses: Vec<SocketAddr>,
    /// The per-request timeout — applied to each request's write and
    /// response wait and to each re-attach's connect attempt.
    timeout: Duration,
    connection: Mutex<Connection>,
    /// Point → register mapping plus the point's declared kind.
    points: HashMap<PointId, PointRegister>,
}

impl BusDriver {
    /// The request timeout [`connect`](Self::connect) applies to each
    /// request's write and response wait — five seconds.
    pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

    /// The minimum spacing between re-attach attempts — one second:
    /// long enough that a dead endpoint does not stall every point's
    /// access on its own connect timeout, short enough that a returned
    /// device is re-served inside a few scan cycles.
    pub const REATTACH_INTERVAL: Duration = Duration::from_secs(1);

    /// Connects to the device server at `addr` with the
    /// [`DEFAULT_TIMEOUT`](Self::DEFAULT_TIMEOUT) request timeout,
    /// serving `points` — see
    /// [`connect_with_timeout`](Self::connect_with_timeout).
    pub fn connect<A: ToSocketAddrs>(addr: A, points: &[PointRegister]) -> std::io::Result<Self> {
        Self::connect_with_timeout(addr, Self::DEFAULT_TIMEOUT, points)
    }

    /// Connects with an explicit `timeout` applied to each request's
    /// write, to the wait for its response, and to later re-attach
    /// attempts. `addr` resolves once, at connect; a re-attach retries
    /// the same resolved addresses.
    ///
    /// A refused or unreachable address fails here with the `io::Error`
    /// from the connect, before any point is involved; once connected,
    /// later link failures surface per-access as [`LinkError`] /
    /// [`IoError`].
    pub fn connect_with_timeout<A: ToSocketAddrs>(
        addr: A,
        timeout: Duration,
        points: &[PointRegister],
    ) -> std::io::Result<Self> {
        let addresses: Vec<SocketAddr> = addr.to_socket_addrs()?.collect();
        if addresses.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "the device server address resolves to nothing",
            ));
        }
        let stream = connect_stream(&addresses, timeout)?;
        Ok(Self {
            addresses,
            timeout,
            connection: Mutex::new(Connection {
                stream: Some(BufReader::new(stream)),
                owner: None,
                retry_at: Instant::now(),
                last_failure: None,
            }),
            points: points
                .iter()
                .map(|mapping| (mapping.point, *mapping))
                .collect(),
        })
    }

    /// Whether the link to the server is live — `false` between a
    /// failed request's drop and the next request's re-attach.
    pub fn connected(&self) -> bool {
        self.connection.lock().unwrap().stream.is_some()
    }

    /// The transport failure last recorded on the link, if one stands
    /// — the link-health data a driver-diagnostics surface reports.
    /// The first successful exchange after a failure clears it, so the
    /// report names the standing failure while it stands and nothing
    /// once it clears.
    pub fn last_failure(&self) -> Option<LinkError> {
        self.connection.lock().unwrap().last_failure.clone()
    }

    /// Advances the device server's bank one tick of `dt` time units —
    /// the explicit step behind the register protocol, stepping any
    /// declared dynamics by `dt` — and returns the bank's new tick.
    /// `dt` must be finite and non-negative; a step carrying one that
    /// is not is refused with [`LinkError::InvalidRequest`]. Stepping
    /// mutates the shared device, so while an attachment holds the
    /// write-ownership claim a non-holder's step answers
    /// [`LinkError::Fenced`].
    pub fn step(&self, dt: f64) -> Result<Tick, LinkError> {
        match self.request(&BusRequest::Step { dt })? {
            BusResponse::Stepped { tick } => Ok(tick),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Takes the device's write-ownership claim for `owner` — the
    /// single-writer arbitration the failover decision fences a
    /// superseded active out with.
    ///
    /// `owner` is an opaque token the caller picks, unique per field
    /// owner: one controller's several attachments claim the same
    /// token so all of them write, while a takeover claims a fresh one.
    /// The grant is unconditional — it preempts whichever owner held
    /// the device — and the claim binds to the claiming attachment:
    /// released when this connection drops or sends
    /// [`release_writer`](Self::release_writer), the last release
    /// freeing the field, so a dead owner's claim dies with its link
    /// rather than fencing the device forever. While the claim is
    /// held, a `write` from an attachment not holding it answers the
    /// point's [`IoError::Fenced`] and a `step` answers
    /// [`LinkError::Fenced`]; reads, the register census, and quality
    /// injection stay open to every attachment.
    ///
    /// The granted token is recorded on the attachment: every later
    /// re-attach re-asserts it through
    /// [`BusRequest::EnsureWriter`], re-arming the claim a link drop or
    /// device restart released without preempting a different owner.
    /// [`release_claim`](Self::release_claim) forgets it — the
    /// demotion path's half of the rule that only the field's owner
    /// re-arms.
    ///
    /// The ask rides the claim path's
    /// [`claim_request`](Self::claim_request): a link that died
    /// unexercised — a promotion's first touch of an idle attachment —
    /// replays once on a fresh link rather than refusing a recovered
    /// field.
    pub fn claim_writer(&self, owner: u64) -> Result<(), LinkError> {
        match self.claim_request(&BusRequest::ClaimWriter { owner })? {
            BusResponse::Done => {
                self.connection.lock().unwrap().owner = Some(owner);
                Ok(())
            }
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// The conditional counterpart of [`claim_writer`](Self::claim_writer):
    /// takes the claim for `owner` — binding this attachment as a
    /// holder — while the field is unclaimed or the standing claim
    /// already names `owner`. Refused [`LinkError::Fenced`] while a
    /// *different* owner stands: a re-attached or superseded
    /// attachment cannot re-arm past the claim another owner took
    /// during its outage.
    ///
    /// A granted token is recorded exactly as `claim_writer` records
    /// it — every later re-attach re-asserts it; a refused one is
    /// forgotten, since the field's standing claim belongs to another
    /// owner and this attachment holds nothing to re-assert. Like the
    /// claim, the ask rides [`claim_request`](Self::claim_request) — a
    /// link that died unexercised replays once on a fresh attachment.
    pub fn ensure_writer(&self, owner: u64) -> Result<(), LinkError> {
        match self.claim_request(&BusRequest::EnsureWriter { owner })? {
            BusResponse::Done => {
                self.connection.lock().unwrap().owner = Some(owner);
                Ok(())
            }
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// The conditional counterpart of [`claim_writer`](Self::claim_writer)
    /// — the grant a launched controller's startup claim asks. Takes
    /// the claim for `owner` where the device stands unclaimed or the
    /// standing claim already names `owner` — this attachment then
    /// joining the claim's holders, exactly as `claim_writer` joins
    /// them — and refuses [`LinkError::Fenced`] while a *different*
    /// owner's claim stands, the claim it met left untouched: the ask
    /// never preempts, never joins, never mutates.
    ///
    /// On this protocol a standing claim always has live holders — it
    /// dies with its last holder's link — so the refused ask is
    /// exactly the live-incumbent verdict the born-active startup
    /// contract refuses startup on: a restarted controller cannot
    /// prove its resumed state is current with the incumbent's and
    /// must not preempt it. The deliberate takeover — a promotion's
    /// claim — stays unconditional: it calls `claim_writer`.
    ///
    /// A granted token is recorded exactly as `claim_writer` records
    /// it — every later re-attach re-asserts it — and the ask rides
    /// [`claim_request`](Self::claim_request): a link that died
    /// unexercised — an orphaned peer's claim on an idle attachment —
    /// replays once on a fresh link rather than refusing a recovered
    /// field.
    pub fn claim_writer_unless_held(&self, owner: u64) -> Result<(), LinkError> {
        match self.claim_request(&BusRequest::ClaimWriterUnlessHeld { owner })? {
            BusResponse::Done => {
                self.connection.lock().unwrap().owner = Some(owner);
                Ok(())
            }
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Forgets the recorded writer claim — the demotion counterpart of
    /// [`claim_writer`](Self::claim_writer): the demoted peer's write
    /// gate is already closed, and without this its next re-attach
    /// would re-assert a claim the field's new owner has taken, racing
    /// it when a restarted device's claim table comes back empty. The
    /// forget is local only — the field's standing claim is the
    /// server's to arbitrate, and a released attachment's mutations
    /// stay fenced against it.
    pub fn release_claim(&self) {
        self.connection.lock().unwrap().owner = None;
    }

    /// Stamps `register`'s stored sample with `quality` —
    /// [`BusRequest::InjectQuality`], the register protocol's analogue
    /// of `dcs-sim-net`'s `RemoteDriver::inject_fault` carrying a
    /// quality fault. The stored value and tick are untouched; the
    /// declared quality stands on the sample every read and the
    /// register census report until [`clear_quality`](Self::clear_quality)
    /// or a real write — which stores a `Good` sample — overwrites it.
    ///
    /// Injection is development tooling, not field ownership: it is
    /// never fenced by the write-ownership claim, so an attachment not
    /// holding the claim can fault a register while a controller pair
    /// owns the field.
    pub fn inject_quality(&self, register: u16, quality: Quality) -> Result<(), LinkError> {
        match self.request(&BusRequest::InjectQuality { register, quality })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Restores `register`'s stored sample to `Quality::Good` —
    /// [`BusRequest::ClearQuality`], the clear half of the injection
    /// pair. Like the inject it is open to every attachment; clearing
    /// a register carrying no injection is a no-op.
    pub fn clear_quality(&self, register: u16) -> Result<(), LinkError> {
        match self.request(&BusRequest::ClearQuality { register })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Releases this attachment's hold on the write-ownership claim —
    /// the explicit half of the claim's release rule; the other is the
    /// connection dropping. Releasing a claim this attachment does not
    /// hold is a no-op.
    ///
    /// The recorded token is forgotten whether the release lands or
    /// not: a failed exchange already severed the link, and
    /// re-asserting a claim the caller meant to hand back is the
    /// stale-token race [`release_claim`](Self::release_claim) exists
    /// to close. The ask rides [`claim_request`](Self::claim_request)
    /// — a demotion landing on a link that died unexercised replays
    /// the release on the fresh attachment, freeing the field's claim
    /// rather than leaving it to the dead link's holder bookkeeping.
    pub fn release_writer(&self) -> Result<(), LinkError> {
        self.connection.lock().unwrap().owner = None;
        match self.claim_request(&BusRequest::ReleaseWriter)? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Lists every register the device server holds —
    /// `RegisterBank::registers` on the server — ordered by address. The
    /// census a rig or test inspects the device with.
    pub fn list_registers(&self) -> Result<Vec<RegisterInfo>, LinkError> {
        match self.request(&BusRequest::ListRegisters)? {
            BusResponse::Registers { registers } => Ok(registers),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Sends one request and returns the server's response — the raw
    /// protocol exchange every other driver method wraps, public so
    /// development tooling can speak the whole documented register
    /// protocol: the `dcs-sim-bus-ctl` binary drives a device server
    /// through exactly this.
    ///
    /// The lock serializes exchanges so a response always pairs with the
    /// request that produced it. Any failed exchange drops the
    /// connection and is recorded as the driver's last transport
    /// failure: the response stream's position is unknown afterward,
    /// and a later read could pick up a stale answer. The drop is the
    /// link's, not the driver's — the next `request` re-attaches
    /// lazily, spacing contact attempts by
    /// [`REATTACH_INTERVAL`](Self::REATTACH_INTERVAL) so a dead endpoint
    /// costs a burst of accesses one refused connect rather than one
    /// connect-timeout each.
    ///
    /// A re-attach that lands while the attachment records a writer
    /// claim first re-asserts it with [`BusRequest::EnsureWriter`] —
    /// a `fenced` re-arm keeps the fresh link but forgets the recorded
    /// token, and a `fenced` answer to `request` itself forgets it
    /// likewise: the field's standing claim names another owner, so
    /// this attachment holds nothing left to re-assert.
    ///
    /// The caller matches the [`BusResponse`] against its request. A
    /// [`BusResponse::Error`] is the server's reported refusal — a
    /// [`BusError`], not a transport failure — and a decodable response
    /// whose variant does not correspond to the request means the peer
    /// is not speaking this protocol; the link can no longer be
    /// trusted, exactly as after a failed exchange.
    pub fn request(&self, request: &BusRequest) -> Result<BusResponse, LinkError> {
        let mut connection = self.connection.lock().unwrap();
        if connection.stream.is_none() {
            if Instant::now() < connection.retry_at {
                return Err(LinkError::Disconnected);
            }
            connection.reattach(&self.addresses, self.timeout);
        }
        connection.exchange_on(request)
    }

    /// The claim family's `request`: [`request`](Self::request)'s
    /// windowed laziness plus one replay for the corpse case — an
    /// attachment whose link died unexercised still reports a live
    /// stream, nothing having touched it since the outage, so a
    /// lifecycle ask like a promotion's claim would otherwise spend
    /// itself discovering the drop and refuse a recovered field. The
    /// ask replays once on a fresh link: every claim operation is
    /// replay-safe — where the severed link's copy was delivered, the
    /// grant or release it produced is the same verdict the replay
    /// lands — while a still-dead endpoint refuses the replay's
    /// re-attach exactly as it refused the first ask. The replay is
    /// bounded to asks that rode a stream the driver already held: a
    /// failure on a link this call just attached is the endpoint's
    /// genuine answer, not a corpse.
    fn claim_request(&self, request: &BusRequest) -> Result<BusResponse, LinkError> {
        let mut connection = self.connection.lock().unwrap();
        let held_stream = connection.stream.is_some();
        if connection.stream.is_none() {
            if Instant::now() < connection.retry_at {
                return Err(LinkError::Disconnected);
            }
            connection.reattach(&self.addresses, self.timeout);
        }
        match connection.exchange_on(request) {
            Err(_) if held_stream => {
                connection.reattach(&self.addresses, self.timeout);
                connection.exchange_on(request)
            }
            other => other,
        }
    }

    /// Drops the connection and reports the peer as gone: an answer
    /// that decodes but does not correspond to the request means the
    /// peer is not a device server, and the link can no longer be
    /// trusted. The next request re-attaches after the interval, as
    /// after any failed exchange.
    fn protocol_violation(&self) -> LinkError {
        let mut connection = self.connection.lock().unwrap();
        connection.stream = None;
        connection.retry_at = Instant::now() + BusDriver::REATTACH_INTERVAL;
        let error = LinkError::Disconnected;
        connection.last_failure = Some(error.clone());
        error
    }
}

impl fmt::Debug for BusDriver {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BusDriver")
            .field("connected", &self.connected())
            .field("points", &self.points.len())
            .finish_non_exhaustive()
    }
}

impl IoDriver for BusDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        let Some(mapping) = self.points.get(&point).copied() else {
            return Err(IoError::UnknownPoint(point));
        };
        match self.request(&BusRequest::ReadRegister {
            register: mapping.register,
        }) {
            Ok(BusResponse::Sample { sample }) => {
                // A register serving a kind other than the point's
                // declared kind is the same failure a local driver
                // reports: never a coercion.
                if sample.value.kind() != mapping.kind {
                    return Err(IoError::TypeMismatch {
                        point,
                        expected: mapping.kind,
                        found: sample.value,
                    });
                }
                Ok(sample)
            }
            Ok(BusResponse::Error { error }) => Err(error.at_point(point)),
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let Some(mapping) = self.points.get(&point).copied() else {
            return Err(IoError::UnknownPoint(point));
        };
        if value.kind() != mapping.kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: mapping.kind,
                found: value,
            });
        }
        // The bank refuses a non-finite `Float`; the same refusal a
        // local driver reports, made before any request leaves — the
        // write the device cannot represent is a caller error, not a
        // link fault.
        if let Value::Float(v) = value
            && !v.is_finite()
        {
            return Err(IoError::InvalidValue { point });
        }
        match self.request(&BusRequest::WriteRegister {
            register: mapping.register,
            value,
        }) {
            Ok(BusResponse::Written { .. }) => Ok(()),
            Ok(BusResponse::Error { error }) => Err(error.at_point(point)),
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
    }

    /// The link's transport-level health for the snapshot's I/O-health
    /// section: `disconnected` while no live connection stands — the
    /// span between a severed link's drop and its re-attach, or a dead
    /// endpoint's outage — plus the last transport failure's
    /// description, which the first exchange after re-attach clears.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        Some(DriverDiagnostics {
            link: if self.connected() {
                LinkState::Connected
            } else {
                LinkState::Disconnected
            },
            last_error: self.last_failure().map(|error| error.to_string()),
            // A point-wise driver has no cyclic exchange surface.
            exchange: None,
        })
    }
}
