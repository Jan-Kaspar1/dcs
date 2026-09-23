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
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::Duration;

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
    /// failed mid-request or the driver already dropped it. A dead
    /// driver does not reconnect — attaching again means connecting a
    /// new `BusDriver`.
    Disconnected,
    /// The server did not answer within the driver's configured
    /// timeout. The connection is dropped — a late answer would desync
    /// the request/response pairing — so later requests report
    /// `Disconnected` rather than risk reading a stale response.
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

/// The connection behind [`BusDriver`]'s lock: `Some` while the link is
/// live, `None` after the first failed exchange.
struct Connection {
    stream: Option<BufReader<TcpStream>>,
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
/// recorded for [`last_failure`](Self::last_failure), and every later
/// access fails fast with `Disconnected`. A timed-out response could
/// arrive after the fact and pair with a later request, so the driver
/// never reuses a suspect link. `BusDriver` is [`Sync`] through its
/// internal locks, like the driver contract expects.
///
/// Diagnostics: [`IoDriver::diagnostics`] reports the link as
/// [`LinkState::Disconnected`] once a failed exchange dropped the
/// connection — the named link degradation a dead device server
/// produces — with the last transport failure's description. That
/// surface is link health, distinct from the per-point [`IoError`]s
/// `read`/`write` return: every point's read failing with
/// `Disconnected` and the link reporting `disconnected` are the same
/// event told at the two levels the telemetry contract keeps separate.
pub struct BusDriver {
    connection: Mutex<Connection>,
    /// Point → register mapping plus the point's declared kind.
    points: HashMap<PointId, PointRegister>,
    /// The last transport failure, for the link-health surface.
    last_failure: Mutex<Option<LinkError>>,
}

impl BusDriver {
    /// The request timeout [`connect`](Self::connect) applies to each
    /// request's write and response wait — five seconds.
    pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

    /// Connects to the device server at `addr` with the
    /// [`DEFAULT_TIMEOUT`](Self::DEFAULT_TIMEOUT) request timeout,
    /// serving `points` — see
    /// [`connect_with_timeout`](Self::connect_with_timeout).
    pub fn connect<A: ToSocketAddrs>(addr: A, points: &[PointRegister]) -> std::io::Result<Self> {
        Self::connect_with_timeout(addr, Self::DEFAULT_TIMEOUT, points)
    }

    /// Connects with an explicit `timeout` applied to each request's
    /// write and to the wait for its response.
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
        let stream = loop {
            match TcpStream::connect(&addr) {
                // An interrupted connect attempt is abandoned with its
                // socket and retried fresh — a caught signal (e.g. a
                // spawned helper's `SIGCHLD`) is not a reachability
                // verdict on the address.
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                other => break other?,
            }
        };
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;
        // Requests are small and answered immediately; coalescing delays
        // would only add latency.
        stream.set_nodelay(true)?;
        Ok(Self {
            connection: Mutex::new(Connection {
                stream: Some(BufReader::new(stream)),
            }),
            points: points
                .iter()
                .map(|mapping| (mapping.point, *mapping))
                .collect(),
            last_failure: Mutex::new(None),
        })
    }

    /// Whether the link to the server is still live — `false` after the
    /// first failed exchange, permanently.
    pub fn connected(&self) -> bool {
        self.connection.lock().unwrap().stream.is_some()
    }

    /// The transport failure that ended the link, if one has occurred —
    /// the link-health data a driver-diagnostics surface reports.
    pub fn last_failure(&self) -> Option<LinkError> {
        self.last_failure.lock().unwrap().clone()
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
    pub fn claim_writer(&self, owner: u64) -> Result<(), LinkError> {
        match self.request(&BusRequest::ClaimWriter { owner })? {
            BusResponse::Done => Ok(()),
            BusResponse::Error { error } => Err(refused(error)),
            _ => Err(self.protocol_violation()),
        }
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
    pub fn release_writer(&self) -> Result<(), LinkError> {
        match self.request(&BusRequest::ReleaseWriter)? {
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
    /// and a later read could pick up a stale answer.
    ///
    /// The caller matches the [`BusResponse`] against its request. A
    /// [`BusResponse::Error`] is the server's reported refusal — a
    /// [`BusError`], not a transport failure — and a decodable response
    /// whose variant does not correspond to the request means the peer
    /// is not speaking this protocol; the link can no longer be
    /// trusted, exactly as after a failed exchange.
    pub fn request(&self, request: &BusRequest) -> Result<BusResponse, LinkError> {
        let mut connection = self.connection.lock().unwrap();
        let Some(stream) = connection.stream.as_mut() else {
            return Err(LinkError::Disconnected);
        };
        match exchange(stream, request) {
            Ok(response) => Ok(response),
            Err(error) => {
                connection.stream = None;
                *self.last_failure.lock().unwrap() = Some(error.clone());
                Err(error)
            }
        }
    }

    /// Drops the connection and reports the peer as gone: an answer
    /// that decodes but does not correspond to the request means the
    /// peer is not a device server, and the link can no longer be
    /// trusted.
    fn protocol_violation(&self) -> LinkError {
        self.connection.lock().unwrap().stream = None;
        let error = LinkError::Disconnected;
        *self.last_failure.lock().unwrap() = Some(error.clone());
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
    /// section: `disconnected` once a failed exchange severed the
    /// connection — permanently, since the driver never reconnects —
    /// plus the last transport failure's description.
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
