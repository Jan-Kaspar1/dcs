//! The remote driver: an [`IoDriver`] whose points live behind TCP.

use crate::protocol::{
    MAX_MESSAGE, PlantError, PlantRequest, PlantResponse, encode_message, read_message,
};
use dcs_core::{IoDriver, IoError, PointId, Sample, Tick, Value};
use dcs_sim::{Fault, PointInfo};
use std::fmt;
use std::io::{BufReader, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::Duration;

/// A failure on a [`RemoteDriver`] operation.
///
/// This is the transport- and protocol-level error vocabulary; the
/// [`IoDriver`] implementation collapses it into the point-addressed
/// [`IoError`] contract via [`at_point`](Self::at_point).
#[derive(Debug, Clone, PartialEq)]
pub enum RemoteError {
    /// There is no live connection to the plant server: the link failed
    /// mid-request or the driver already dropped it. A dead driver does
    /// not reconnect — attaching again means connecting a new
    /// `RemoteDriver`.
    Disconnected,
    /// The server did not answer within the driver's configured timeout.
    /// The connection is dropped — a late answer would desync the
    /// request/response pairing — so later requests report
    /// `Disconnected` rather than risk reading a stale response.
    Timeout,
    /// The server reported a point-level failure: the [`IoError`] its
    /// `SimDriver` produced, carried verbatim — `UnknownPoint`,
    /// `TypeMismatch`, or an injected fault's `Disconnected`/`Timeout`.
    Io(IoError),
    /// The server refused the request itself — a [`PlantRequest::Step`]
    /// with a negative or non-finite `dt`, or a payload JSON cannot
    /// express (e.g. a non-finite `Value::Float`, which has no JSON
    /// representation). `detail` is the server's diagnostic text.
    InvalidRequest(String),
}

impl RemoteError {
    /// The [`IoError`] this failure presents as at `point`.
    ///
    /// Transport failures become that point's `Disconnected`/`Timeout`;
    /// a protocol-carried [`IoError`] passes through unchanged. A refused
    /// or incoherent answer — `InvalidRequest`, or a response that does
    /// not match the request — means the peer is not serving the point
    /// the protocol promises, which surfaces as `Disconnected`.
    pub fn at_point(self, point: PointId) -> IoError {
        match self {
            Self::Disconnected | Self::InvalidRequest(_) => IoError::Disconnected(point),
            Self::Timeout => IoError::Timeout(point),
            Self::Io(error) => error,
        }
    }
}

impl fmt::Display for RemoteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Disconnected => write!(f, "no live connection to the plant server"),
            Self::Timeout => write!(f, "plant server did not answer in time"),
            Self::Io(error) => write!(f, "{error}"),
            Self::InvalidRequest(detail) => write!(f, "server refused the request: {detail}"),
        }
    }
}

impl std::error::Error for RemoteError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<PlantError> for RemoteError {
    fn from(error: PlantError) -> Self {
        match error {
            PlantError::Io { error } => Self::Io(error),
            PlantError::InvalidRequest { detail } => Self::InvalidRequest(detail),
        }
    }
}

/// The connection behind [`RemoteDriver`]'s lock: `Some` while the link
/// is live, `None` after the first failed exchange.
struct Connection {
    stream: Option<BufReader<TcpStream>>,
}

/// Writes the request line and reads the response line on `stream`,
/// translating `io::Error`s into the transport vocabulary.
fn exchange(
    stream: &mut BufReader<TcpStream>,
    request: &PlantRequest,
) -> Result<PlantResponse, RemoteError> {
    fn transport(error: std::io::Error) -> RemoteError {
        match error.kind() {
            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => RemoteError::Timeout,
            // A dropped connection, a mid-message EOF, and an oversized or
            // undecodable response all mean the peer is gone or is not
            // speaking the protocol — Disconnected either way.
            _ => RemoteError::Disconnected,
        }
    }

    stream
        .get_mut()
        .write_all(&encode_message(request))
        .map_err(transport)?;
    let Some(line) = read_message(stream, MAX_MESSAGE).map_err(transport)? else {
        return Err(RemoteError::Disconnected);
    };
    serde_json::from_slice(&line).map_err(|_| RemoteError::Disconnected)
}

/// An [`IoDriver`] whose logical points live in a [`PlantServer`] plant
/// reached over TCP.
///
/// `RemoteDriver` is the proof that the I/O abstraction hides transport:
/// components and the executor use it through `&dyn IoDriver` exactly as
/// they use an in-process [`SimDriver`](dcs_sim::SimDriver), and the
/// scripted-behavior tests show identical results for both. Reads and
/// writes are forwarded to the server one request at a time behind the
/// driver's lock; [`step`](Self::step) and the fault API are explicit
/// protocol operations so a controller scan advances the shared plant
/// deterministically and a second attached controller — the standby —
/// observes the same field state without driving it.
///
/// Per the standby-field-observation decision the driver is the
/// field-observing kind: [`capture_state`](IoDriver::capture_state) stays
/// at its `None` default because the driver's state is the shared plant
/// itself, observed through the protocol — there is nothing local to
/// checkpoint.
///
/// Failure handling: any failed exchange — broken pipe, closed
/// connection, timed-out or oversized response, undecodable answer —
/// drops the connection, and every later access fails fast with
/// `Disconnected`. A timed-out response could arrive after the fact and
/// pair with a later request, so the driver never reuses a suspect link.
/// `RemoteDriver` is [`Sync`] through its internal lock, like the driver
/// contract expects.
pub struct RemoteDriver {
    connection: Mutex<Connection>,
}

impl RemoteDriver {
    /// The request timeout [`connect`](Self::connect) applies to each
    /// request's write and response wait — five seconds.
    pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

    /// Connects to the plant server at `addr` with the
    /// [`DEFAULT_TIMEOUT`](Self::DEFAULT_TIMEOUT) request timeout — see
    /// [`connect_with_timeout`](Self::connect_with_timeout).
    ///
    /// A refused or unreachable address fails here with the `io::Error`
    /// from the connect, before any point is involved; once connected,
    /// later link failures surface per-access as [`RemoteError`] /
    /// [`IoError`].
    pub fn connect<A: ToSocketAddrs>(addr: A) -> std::io::Result<Self> {
        Self::connect_with_timeout(addr, Self::DEFAULT_TIMEOUT)
    }

    /// Connects with an explicit `timeout` applied to each request's
    /// write and to the wait for its response.
    pub fn connect_with_timeout<A: ToSocketAddrs>(
        addr: A,
        timeout: Duration,
    ) -> std::io::Result<Self> {
        let stream = TcpStream::connect(addr)?;
        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;
        // Requests are small and answered immediately; coalescing delays
        // would only add latency.
        stream.set_nodelay(true)?;
        Ok(Self {
            connection: Mutex::new(Connection {
                stream: Some(BufReader::new(stream)),
            }),
        })
    }

    /// Whether the link to the server is still live — `false` after the
    /// first failed exchange, permanently.
    pub fn connected(&self) -> bool {
        self.connection.lock().unwrap().stream.is_some()
    }

    /// Advances the shared plant one tick of `dt` time units —
    /// `SimDriver::step` on the server — and returns the plant's new
    /// tick.
    ///
    /// Stepping is explicit rather than attached to `read`/`write`, so a
    /// controller paces the plant at its scan boundary exactly as it
    /// paces a local `SimDriver`, and every attached client observes the
    /// same stepped values.
    pub fn step(&self, dt: f64) -> Result<Tick, RemoteError> {
        match self.request(&PlantRequest::Step { dt })? {
            PlantResponse::Stepped { tick } => Ok(tick),
            PlantResponse::Error { error } => Err(error.into()),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Injects `fault` on `point` in the shared plant —
    /// `SimDriver::inject_fault` on the server. Every attached client
    /// observes the fault.
    pub fn inject_fault(&self, point: PointId, fault: Fault) -> Result<(), RemoteError> {
        match self.request(&PlantRequest::InjectFault { point, fault })? {
            PlantResponse::Done => Ok(()),
            PlantResponse::Error { error } => Err(error.into()),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Lists every point the shared plant serves — `SimDriver::points`
    /// on the server — ordered by [`PointId`]. Each entry reports the
    /// binding's direction, the sample readers observe, and the active
    /// fault, so a client can survey the shared field without holding a
    /// copy of its channel map.
    pub fn list_points(&self) -> Result<Vec<PointInfo>, RemoteError> {
        match self.request(&PlantRequest::ListPoints)? {
            PlantResponse::Points { points } => Ok(points),
            PlantResponse::Error { error } => Err(error.into()),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Removes any fault injected on `point` — `SimDriver::clear_fault`
    /// on the server.
    pub fn clear_fault(&self, point: PointId) -> Result<(), RemoteError> {
        match self.request(&PlantRequest::ClearFault { point })? {
            PlantResponse::Done => Ok(()),
            PlantResponse::Error { error } => Err(error.into()),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Sends one request and returns the server's response.
    ///
    /// The lock serializes exchanges so a response always pairs with the
    /// request that produced it. Any failed exchange drops the
    /// connection: the response stream's position is unknown afterward,
    /// and a later read could pick up a stale answer.
    fn request(&self, request: &PlantRequest) -> Result<PlantResponse, RemoteError> {
        let mut connection = self.connection.lock().unwrap();
        let Some(stream) = connection.stream.as_mut() else {
            return Err(RemoteError::Disconnected);
        };
        match exchange(stream, request) {
            Ok(response) => Ok(response),
            Err(error) => {
                connection.stream = None;
                Err(error)
            }
        }
    }

    /// Drops the connection and reports the peer as gone: an answer that
    /// parses but does not correspond to the request means the peer is
    /// not a plant server, and the link can no longer be trusted.
    fn protocol_violation(&self) -> RemoteError {
        self.connection.lock().unwrap().stream = None;
        RemoteError::Disconnected
    }
}

impl fmt::Debug for RemoteDriver {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RemoteDriver")
            .field("connected", &self.connected())
            .finish_non_exhaustive()
    }
}

impl IoDriver for RemoteDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        match self.request(&PlantRequest::Read { point }) {
            Ok(PlantResponse::Sample { sample }) => Ok(sample),
            // A point-level failure is the `IoError` contract already:
            // the server's answer passes through unchanged.
            Ok(PlantResponse::Error {
                error: PlantError::Io { error },
            }) => Err(error),
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        match self.request(&PlantRequest::Write { point, value }) {
            Ok(PlantResponse::Done) => Ok(()),
            Ok(PlantResponse::Error {
                error: PlantError::Io { error },
            }) => Err(error),
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
    }
}
