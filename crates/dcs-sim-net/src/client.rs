//! The remote driver: an [`IoDriver`] whose points live behind TCP.

use crate::protocol::{
    MAX_MESSAGE, PlantError, PlantRequest, PlantResponse, encode_message, read_message,
};
use dcs_core::{DriverDiagnostics, IoDriver, IoError, LinkState, PointId, Sample, Tick, Value};
use dcs_sim::{Fault, PointInfo};
use std::fmt;
use std::io::{BufReader, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// A failure on a [`RemoteDriver`] operation.
///
/// This is the transport- and protocol-level error vocabulary; the
/// [`IoDriver`] implementation collapses it into the point-addressed
/// [`IoError`] contract via [`at_point`](Self::at_point).
#[derive(Debug, Clone, PartialEq)]
pub enum RemoteError {
    /// There is no live connection to the plant server: the link failed
    /// mid-request, the driver already dropped it, or the latest
    /// re-attach found the endpoint unanswerable. A dead link is not a
    /// dead driver — the next access re-attaches lazily, so the failure
    /// reads "not answerable now", never "dead for good".
    Disconnected,
    /// The server did not answer within the driver's configured timeout.
    /// The connection is dropped — a late answer would desync the
    /// request/response pairing — and the next request re-attaches on a
    /// fresh stream rather than risk reading a stale response.
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
    /// The request mutates the shared field but another attachment holds
    /// the field's write-ownership claim — the fencing verdict of the
    /// failover decision. Reads still succeed; writing again requires
    /// taking the claim back with [`claim_writer`](Self::claim_writer).
    Fenced,
    /// The request mutates the shared field but no write-ownership
    /// claim stands at all — the server is fresh or restarted, or the
    /// last holder released. The field fails closed rather than opening
    /// an unclaimed window any attachment could mutate through — or an
    /// interposer claim ahead of the legitimate owner's re-arm. Reads
    /// stay open; `write` and `step` resume once an owner claims,
    /// through [`ensure_writer`](Self::ensure_writer) for the owner
    /// re-arming or [`claim_writer`](Self::claim_writer) for a takeover.
    Unclaimed,
}

impl RemoteError {
    /// The [`IoError`] this failure presents as at `point`.
    ///
    /// Transport failures become that point's `Disconnected`/`Timeout`;
    /// a protocol-carried [`IoError`] passes through unchanged. A refused
    /// or incoherent answer — `InvalidRequest`, or a response that does
    /// not match the request — means the peer is not serving the point
    /// the protocol promises, which surfaces as `Disconnected`. An
    /// `Unclaimed` refusal surfaces as the point's `Fenced`: the
    /// point-level vocabulary has no "no claim stands" case, and the
    /// write is refused either way. (`write` re-arms a recorded owner
    /// once before surfacing, so a surfaced `Fenced` from an unclaimed
    /// field means no token was recorded or the re-arm was refused.)
    pub fn at_point(self, point: PointId) -> IoError {
        match self {
            Self::Disconnected | Self::InvalidRequest(_) => IoError::Disconnected(point),
            Self::Timeout => IoError::Timeout(point),
            Self::Io(error) => error,
            Self::Fenced | Self::Unclaimed => IoError::Fenced(point),
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
            Self::Fenced => {
                write!(
                    f,
                    "field mutation refused: another attachment owns field writes"
                )
            }
            Self::Unclaimed => {
                write!(f, "field mutation refused: no attachment owns field writes")
            }
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
            PlantError::Fenced { .. } => Self::Fenced,
            PlantError::Unclaimed { .. } => Self::Unclaimed,
        }
    }
}

/// What a granted [`RemoteDriver::claim_writer`] found at the field:
/// whether the claimed token was already held by another live
/// attachment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimGrant {
    /// No other live attachment held the token — the claim is the
    /// field's sole writer arbitration, as a field owner expects.
    Exclusive,
    /// Another live attachment already held the token and keeps writing
    /// under it: the documented shape for one owner's several
    /// attachments — or a harness sharing its owner's claim — and the
    /// signature of two field-owning processes pinned to one token,
    /// which defeats the single-writer fencing a promotion relies on.
    /// The grant stands either way; the flag exists so the sharing is
    /// never silent.
    Shared,
}

/// The connection behind [`RemoteDriver`]'s lock: `Some` while the link
/// is live, `None` after a failed exchange — the next request lazily
/// re-attaches — plus the re-attach bookkeeping and the link-level
/// failure history [`IoDriver::diagnostics`] reports.
struct Connection {
    stream: Option<BufReader<TcpStream>>,
    /// The field-write ownership token a successful `claim_writer`
    /// recorded — re-asserted through `ensure_writer` on every
    /// re-attach, so a plant restart's dropped claim re-arms for the
    /// same owner. `None` on an attachment that never claimed, released
    /// its claim at demotion, or watched the field fence it out.
    owner: Option<u64>,
    /// The earliest instant the next re-attach may run: a failed attach
    /// backs the next attempt off by
    /// [`REATTACH_INTERVAL`](RemoteDriver::REATTACH_INTERVAL), so a dead
    /// endpoint costs one connect attempt per interval rather than one
    /// per point's access.
    retry_at: Instant,
    /// The most recent transport- or protocol-level failure's
    /// description. The failure that severed the link stays recorded —
    /// the `Disconnected`s every later access reports are its
    /// consequence, not new failures.
    last_error: Option<String>,
}

impl Connection {
    /// Re-establishes the link and re-arms the recorded writer claim —
    /// the `ensure_writer` grant a reconnecting field owner asserts so a
    /// server restart's dropped claim re-arms for the same owner rather
    /// than preempting whichever attachment claimed during the outage. A
    /// `fenced` answer keeps the fresh link but forgets the recorded
    /// owner: the field already serves a different claim, and this
    /// attachment's mutations will fence honestly against it. Any other
    /// failed attach drops the stream and backs the next attempt off.
    fn reattach(&mut self, addresses: &[SocketAddr], timeout: Duration) {
        let mut stream = match connect_stream(addresses, timeout) {
            Ok(stream) => BufReader::new(stream),
            Err(_) => {
                self.retry_at = Instant::now() + RemoteDriver::REATTACH_INTERVAL;
                return;
            }
        };
        if let Some(owner) = self.owner {
            match exchange(&mut stream, &PlantRequest::EnsureWriter { owner }) {
                // `ClaimedShared` is a grant: the re-armed claim joins a
                // token another live attachment still holds — possible
                // while the server has not yet reaped this driver's
                // own dropped link, or while a genuine second claimant
                // shares the token.
                Ok(PlantResponse::Done) | Ok(PlantResponse::ClaimedShared { .. }) => {}
                Ok(PlantResponse::Error {
                    error: PlantError::Fenced { .. },
                }) => self.owner = None,
                Ok(_) => {
                    self.last_error = Some(
                        "the ensure_writer answer did not match the request — the peer is not a plant server"
                            .to_string(),
                    );
                    self.retry_at = Instant::now() + RemoteDriver::REATTACH_INTERVAL;
                    return;
                }
                Err(error) => {
                    self.last_error = Some(error.to_string());
                    self.retry_at = Instant::now() + RemoteDriver::REATTACH_INTERVAL;
                    return;
                }
            }
        }
        self.stream = Some(stream);
    }
}

/// Connects a stream to the first answering of `addresses` with the
/// driver's request semantics — the timeouts and `nodelay` every
/// connection carries.
fn connect_stream(addresses: &[SocketAddr], timeout: Duration) -> std::io::Result<TcpStream> {
    let mut failure = std::io::Error::new(std::io::ErrorKind::NotFound, "no plant server address");
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
                // Requests are small and answered immediately; coalescing
                // delays would only add latency.
                stream.set_nodelay(true)?;
                return Ok(stream);
            }
            Err(error) => failure = error,
        }
    }
    Err(failure)
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
/// drops the connection, and the *next* access re-attaches lazily: a
/// field outage degrades every access to `Disconnected` while it lasts
/// rather than killing the driver for good, and a plant that returns is
/// served by the same `RemoteDriver` — the link-loss contract that lets
/// a field-owning controller ride a plant restart out instead of dying
/// with the link. Re-attach attempts are bounded to one per
/// [`REATTACH_INTERVAL`](Self::REATTACH_INTERVAL), so a dead endpoint
/// costs each access burst one refused connect rather than one
/// connect-timeout per point. A timed-out response could arrive after
/// the fact and pair with a later request, so the driver never reuses a
/// suspect link.
///
/// An attachment that claimed the field — [`claim_writer`](Self::claim_writer)
/// — records the token, and every re-attach re-asserts it through the
/// `ensure_writer` grant before the pending request runs: a restarted
/// plant dropped the claim with its process state, so the owner re-arms
/// it — conditionally, never preempting a different claim another
/// attachment took during the outage. A `fenced` answer forgets the
/// recorded token, and [`release_claim`](Self::release_claim) drops it
/// at demotion, so only an attachment the field still owes ownership
/// re-arms. `RemoteDriver` is [`Sync`] through its internal lock, like
/// the driver contract expects.
///
/// Diagnostics: [`IoDriver::diagnostics`] reports the link as
/// [`LinkState::Disconnected`] while no live connection stands — a dead
/// or unanswerable plant server, including the span between a severed
/// link's drop and its re-attach — with the last transport- or
/// protocol-level failure's description. That surface is link health,
/// distinct from the per-point [`IoError`]s `read`/`write` return: every
/// point's read failing with `Disconnected` and the link reporting
/// `disconnected` are the same event told at the two levels the
/// telemetry contract keeps separate.
pub struct RemoteDriver {
    /// The resolved server addresses, retried in order on re-attach.
    addresses: Vec<SocketAddr>,
    /// The per-request timeout — applied to each request's write and
    /// response wait and to each re-attach's connect attempt.
    timeout: Duration,
    connection: Mutex<Connection>,
}

impl RemoteDriver {
    /// The request timeout [`connect`](Self::connect) applies to each
    /// request's write and response wait — five seconds.
    pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(5);

    /// The minimum spacing between re-attach attempts — one second: long
    /// enough that a dead endpoint does not stall every point's access on
    /// its own connect timeout, short enough that a returned plant is
    /// re-served inside a few scan cycles.
    pub const REATTACH_INTERVAL: Duration = Duration::from_secs(1);

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
    /// write, to the wait for its response, and to later re-attach
    /// attempts. `addr` resolves once, at connect; a re-attach retries
    /// the same resolved addresses.
    pub fn connect_with_timeout<A: ToSocketAddrs>(
        addr: A,
        timeout: Duration,
    ) -> std::io::Result<Self> {
        let addresses: Vec<SocketAddr> = addr.to_socket_addrs()?.collect();
        if addresses.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "the plant server address resolves to nothing",
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
                last_error: None,
            }),
        })
    }

    /// Whether the link to the server is live — `false` between a failed
    /// request's drop and the next request's re-attach.
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
    ///
    /// Like `write`, the step is a field mutation: it fences
    /// [`RemoteError::Fenced`] while another owner claims the field and
    /// [`RemoteError::Unclaimed`] while no claim stands at all — the
    /// fail-closed state a fresh or restarted server serves until an
    /// owner claims. An `Unclaimed` step with a recorded owner re-arms
    /// once through the conditional `ensure_writer` grant (never
    /// preempting) and retries, so a claim-state reset behind a live
    /// connection reclaims instead of degrading the cycle.
    pub fn step(&self, dt: f64) -> Result<Tick, RemoteError> {
        match self.request(&PlantRequest::Step { dt })? {
            PlantResponse::Stepped { tick } => Ok(tick),
            PlantResponse::Error { error } => {
                let error: RemoteError = error.into();
                if matches!(error, RemoteError::Unclaimed) {
                    // No claim stands at all — nothing claimed the field
                    // away — so a recorded owner re-arms conditionally
                    // (never preempting) and the step retries once. A
                    // genuinely stolen field refuses the re-arm as
                    // `Fenced`, and that verdict stands.
                    match self.try_rearm() {
                        Ok(true) => return self.retry_step(dt),
                        Ok(false) => return Err(self.fail(error)),
                        Err(rearm) => return Err(rearm),
                    }
                }
                if matches!(error, RemoteError::Fenced) {
                    // The field's claim moved to another owner — forget
                    // the recorded token so a later re-attach does not
                    // re-assert a claim this attachment no longer holds.
                    self.connection.lock().unwrap().owner = None;
                }
                Err(self.fail(error))
            }
            _ => Err(self.protocol_violation()),
        }
    }

    /// Issues one `Step` after a successful re-arm — the retry half of
    /// the unclaimed recovery. A race that claimed the field between the
    /// re-arm and this retry surfaces as this step's own `Fenced`, which
    /// forgets the recorded token exactly like every fenced path.
    fn retry_step(&self, dt: f64) -> Result<Tick, RemoteError> {
        match self.request(&PlantRequest::Step { dt }) {
            Ok(PlantResponse::Stepped { tick }) => Ok(tick),
            Ok(PlantResponse::Error { error }) => {
                let error: RemoteError = error.into();
                if matches!(error, RemoteError::Fenced) {
                    self.connection.lock().unwrap().owner = None;
                }
                Err(self.fail(error))
            }
            Ok(_) => Err(self.protocol_violation()),
            Err(error) => Err(error),
        }
    }

    /// The single conditional re-grant an `Unclaimed` refusal triggers:
    /// when this attachment recorded a writer token, re-assert it through
    /// `ensure_writer` — granted while the field is unclaimed or already
    /// names the token, refused `Fenced` while a different owner stands
    /// (the recorded token is then forgotten, as in every fenced path).
    ///
    /// Returns `Ok(true)` when the claim stands for this owner again and
    /// the caller should retry its refused mutation exactly once,
    /// `Ok(false)` when no token is recorded (nothing to re-arm), and
    /// `Err` when the re-arm itself was refused or the link failed — the
    /// verdict the caller reports instead of the original `Unclaimed`.
    /// Exactly one `ensure_writer` exchange runs per refused mutation; no
    /// preemption is possible because `ensure_writer` never preempts.
    fn try_rearm(&self) -> Result<bool, RemoteError> {
        let owner = self.connection.lock().unwrap().owner;
        let Some(owner) = owner else {
            return Ok(false);
        };
        match self.request(&PlantRequest::EnsureWriter { owner }) {
            Ok(PlantResponse::Done) | Ok(PlantResponse::ClaimedShared { .. }) => {
                self.connection.lock().unwrap().owner = Some(owner);
                Ok(true)
            }
            Ok(PlantResponse::Error { error }) => {
                let error: RemoteError = error.into();
                if matches!(error, RemoteError::Fenced) {
                    self.connection.lock().unwrap().owner = None;
                }
                Err(self.fail(error))
            }
            Ok(_) => Err(self.protocol_violation()),
            Err(error) => Err(error),
        }
    }

    /// Injects `fault` on `point` in the shared plant —
    /// `SimDriver::inject_fault` on the server. Every attached client
    /// observes the fault.
    pub fn inject_fault(&self, point: PointId, fault: Fault) -> Result<(), RemoteError> {
        match self.request(&PlantRequest::InjectFault { point, fault })? {
            PlantResponse::Done => Ok(()),
            PlantResponse::Error { error } => Err(self.fail(error.into())),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Takes the shared plant's field-write ownership for `owner` — the
    /// single-writer claim the failover decision fences a superseded
    /// active out with.
    ///
    /// `owner` is an opaque token the caller picks, unique per field
    /// owner: one controller's several attachments claim the same token
    /// so all of them write, while a takeover claims a fresh one. The
    /// grant is unconditional — it preempts whichever owner held the
    /// field — and it stands until preempted or the last holder's
    /// [`release_writer`](Self::release_writer) releases it, never on
    /// disconnect: a dead owner's silence is exactly what the claim
    /// exists to fence. The field fails closed on mutation: a `write`
    /// from a connection not holding the claim answers the point's
    /// [`IoError::Fenced`] and a `step` answers [`RemoteError::Fenced`]
    /// — or [`RemoteError::Unclaimed`] while no claim stands at all,
    /// the state a fresh or restarted server serves until a claim
    /// lands. Reads and the plant-tooling requests stay open to every
    /// attachment.
    ///
    /// The granted token is recorded on the attachment: every later
    /// re-attach re-asserts it through `ensure_writer`, re-arming the
    /// claim a plant restart dropped without preempting a different
    /// owner. [`release_claim`](Self::release_claim) forgets it — the
    /// demotion path's half of the rule that only the field's owner
    /// re-arms.
    ///
    /// The returned [`ClaimGrant`] reports what the field saw: a grant
    /// answering `claimed_shared` means another live attachment already
    /// holds the token — expected for a deliberate same-owner
    /// attachment, but the signature of a second field-owning process
    /// pinned to the same token, which defeats the single-writer
    /// fencing promotion relies on.
    pub fn claim_writer(&self, owner: u64) -> Result<ClaimGrant, RemoteError> {
        let grant = match self.request(&PlantRequest::ClaimWriter { owner })? {
            PlantResponse::Done => ClaimGrant::Exclusive,
            PlantResponse::ClaimedShared { .. } => ClaimGrant::Shared,
            PlantResponse::Error { error } => return Err(self.fail(error.into())),
            _ => return Err(self.protocol_violation()),
        };
        self.connection.lock().unwrap().owner = Some(owner);
        Ok(grant)
    }

    /// The conditional counterpart of [`claim_writer`](Self::claim_writer):
    /// granted while the field is unclaimed or the standing claim already
    /// names `owner` — the re-arm a field owner asserts after its
    /// attachment drops, and the claim a mutation tool takes so it never
    /// preempts a live owner. Refused [`RemoteError::Fenced`] while a
    /// *different* owner stands: a superseded attachment cannot re-arm
    /// past the attachment that claimed during its outage.
    ///
    /// A granted token is recorded exactly as `claim_writer` records it —
    /// every later re-attach re-asserts it — so an attachment that
    /// claimed only to mutate must follow with
    /// [`release_writer`](Self::release_writer): a tool's claim left
    /// standing would outlive the tool's connection and fence the field
    /// owner's re-arm, the restart-window seize the unclaimed refusal
    /// exists to prevent.
    pub fn ensure_writer(&self, owner: u64) -> Result<ClaimGrant, RemoteError> {
        let grant = match self.request(&PlantRequest::EnsureWriter { owner })? {
            PlantResponse::Done => ClaimGrant::Exclusive,
            PlantResponse::ClaimedShared { .. } => ClaimGrant::Shared,
            PlantResponse::Error { error } => {
                let error: RemoteError = error.into();
                if matches!(error, RemoteError::Fenced) {
                    // A refused re-arm means a different owner stands —
                    // forget the recorded token so a later re-attach does
                    // not re-assert a claim this attachment no longer
                    // holds, exactly like every fenced path.
                    self.connection.lock().unwrap().owner = None;
                }
                return Err(self.fail(error));
            }
            _ => return Err(self.protocol_violation()),
        };
        self.connection.lock().unwrap().owner = Some(owner);
        Ok(grant)
    }

    /// Drops this connection's hold on the write claim — the field-side
    /// half of a conditional claim's lifecycle. When the release empties
    /// the claim's holder set the field returns to `unclaimed`: closed
    /// to mutation again until the next claim lands, so a tool's write
    /// window stays bounded rather than leaving a dead tool token the
    /// field owner's re-arm would fence against.
    ///
    /// The recorded token is forgotten whether the release lands or not:
    /// a failed exchange already severed the link, and re-asserting a
    /// claim the caller meant to hand back is the stale-token race
    /// `ensure_writer` re-arms against. [`release_claim`](Self::release_claim)
    /// is the demotion counterpart — local-only, leaving the field's
    /// standing claim untouched for the new owner to keep fencing it.
    pub fn release_writer(&self) -> Result<(), RemoteError> {
        self.connection.lock().unwrap().owner = None;
        match self.request(&PlantRequest::ReleaseWriter)? {
            PlantResponse::Done => Ok(()),
            PlantResponse::Error { error } => Err(self.fail(error.into())),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Forgets the recorded writer claim — the demotion counterpart of
    /// [`claim_writer`](Self::claim_writer): the demoted peer's write
    /// gate is already closed, and without this its next re-attach would
    /// re-assert a claim the field's new owner has taken, racing it when
    /// a restarted plant's claim comes back empty. The release is local
    /// only — the field's standing claim is the server's to arbitrate,
    /// and a released attachment's mutations stay fenced against it.
    pub fn release_claim(&self) {
        self.connection.lock().unwrap().owner = None;
    }

    /// Lists every point the shared plant serves — `SimDriver::points`
    /// on the server — ordered by [`PointId`]. Each entry reports the
    /// binding's direction, the sample readers observe, and the active
    /// fault, so a client can survey the shared field without holding a
    /// copy of its channel map.
    pub fn list_points(&self) -> Result<Vec<PointInfo>, RemoteError> {
        match self.request(&PlantRequest::ListPoints)? {
            PlantResponse::Points { points } => Ok(points),
            PlantResponse::Error { error } => Err(self.fail(error.into())),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Removes any fault injected on `point` — `SimDriver::clear_fault`
    /// on the server.
    pub fn clear_fault(&self, point: PointId) -> Result<(), RemoteError> {
        match self.request(&PlantRequest::ClearFault { point })? {
            PlantResponse::Done => Ok(()),
            PlantResponse::Error { error } => Err(self.fail(error.into())),
            _ => Err(self.protocol_violation()),
        }
    }

    /// Sends one request and returns the server's response, re-attaching
    /// first when the link is down — the lazy re-attach a remote field
    /// rides its outage out with.
    ///
    /// The lock serializes exchanges so a response always pairs with the
    /// request that produced it. Any failed exchange drops the
    /// connection: the response stream's position is unknown afterward,
    /// and a later read could pick up a stale answer. A dead link reports
    /// `Disconnected` until the next
    /// [`REATTACH_INTERVAL`](Self::REATTACH_INTERVAL) window opens.
    fn request(&self, request: &PlantRequest) -> Result<PlantResponse, RemoteError> {
        let mut connection = self.connection.lock().unwrap();
        if connection.stream.is_none() {
            if Instant::now() < connection.retry_at {
                return Err(RemoteError::Disconnected);
            }
            connection.reattach(&self.addresses, self.timeout);
        }
        let Some(stream) = connection.stream.as_mut() else {
            return Err(RemoteError::Disconnected);
        };
        match exchange(stream, request) {
            Ok(response) => Ok(response),
            Err(error) => {
                connection.stream = None;
                connection.last_error = Some(error.to_string());
                Err(error)
            }
        }
    }

    /// Drops the connection and reports the peer as gone: an answer that
    /// parses but does not correspond to the request means the peer is
    /// not a plant server, and the link can no longer be trusted.
    fn protocol_violation(&self) -> RemoteError {
        let mut connection = self.connection.lock().unwrap();
        connection.stream = None;
        connection.last_error =
            Some("response did not match the request — the peer is not a plant server".to_string());
        RemoteError::Disconnected
    }

    /// Records a non-point failure — a request the server refused — as
    /// the link's last protocol failure, then returns it for the caller
    /// to propagate. A protocol-carried [`IoError`] is a point fault,
    /// not link trouble, so it is not recorded here.
    fn fail(&self, error: RemoteError) -> RemoteError {
        if !matches!(error, RemoteError::Io(_)) {
            self.connection.lock().unwrap().last_error = Some(error.to_string());
        }
        error
    }

    /// Issues one `Write` after a successful re-arm — the retry half of
    /// the unclaimed recovery. A race that claimed the field between the
    /// re-arm and this retry surfaces as the point's `Fenced`, which
    /// forgets the recorded token exactly like every fenced path.
    fn retry_write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        match self.request(&PlantRequest::Write { point, value }) {
            Ok(PlantResponse::Done) => Ok(()),
            Ok(PlantResponse::Error {
                error: PlantError::Io { error },
            }) => {
                if matches!(error, IoError::Fenced(_)) {
                    self.connection.lock().unwrap().owner = None;
                }
                Err(error)
            }
            Ok(PlantResponse::Error {
                error: PlantError::Unclaimed { .. },
            }) => Err(self.fail(RemoteError::Unclaimed).at_point(point)),
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
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
            }) => {
                if matches!(error, IoError::Fenced(_)) {
                    // As in `step`: a fenced mutation means the field's
                    // claim belongs to another owner now — the recorded
                    // token must not ride a later re-attach back in.
                    self.connection.lock().unwrap().owner = None;
                }
                Err(error)
            }
            // The unclaimed refusal — no claim stands at all — re-arms
            // a recorded owner once through the conditional
            // `ensure_writer` grant (never preempting) and retries the
            // write: nothing claimed the field away, so the healthy
            // owner reclaims it instead of demoting. A genuinely stolen
            // field refuses the re-arm as `Fenced`, and that verdict —
            // surfaced as the point's `Fenced` — still demotes.
            Ok(PlantResponse::Error {
                error: PlantError::Unclaimed { .. },
            }) => match self.try_rearm() {
                Ok(true) => self.retry_write(point, value),
                Ok(false) => Err(self.fail(RemoteError::Unclaimed).at_point(point)),
                Err(rearm) => Err(rearm.at_point(point)),
            },
            Ok(_) => Err(self.protocol_violation().at_point(point)),
            Err(error) => Err(error.at_point(point)),
        }
    }

    /// The link's transport-level health for the snapshot's I/O-health
    /// section: `disconnected` while no live connection stands — a dead
    /// or unanswerable plant, including the span between a severed
    /// link's drop and its lazy re-attach — plus the last transport- or
    /// protocol-level failure's description.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        let connection = self.connection.lock().unwrap();
        Some(DriverDiagnostics {
            link: if connection.stream.is_some() {
                LinkState::Connected
            } else {
                LinkState::Disconnected
            },
            last_error: connection.last_error.clone(),
            // A point-wise driver has no cyclic exchange surface.
            exchange: None,
        })
    }
}
