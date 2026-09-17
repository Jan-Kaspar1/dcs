//! The plant server: one [`SimDriver`] shared by every connected client.

use crate::protocol::encode_message;
use crate::protocol::{MAX_MESSAGE, PlantError, PlantRequest, PlantResponse, read_message};
use dcs_core::{IoDriver, IoError};
use dcs_sim::SimDriver;
use std::collections::{HashMap, HashSet};
use std::io::{self, BufReader, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

/// State shared between the accept loop and every client handler thread.
struct Shared {
    /// The simulated plant all connections observe and drive. The
    /// driver's own mutex serializes requests across connections.
    driver: SimDriver,
    /// Set by [`PlantServer::shutdown`]; ends the accept loop and lets
    /// handlers exit at their next request.
    stopped: AtomicBool,
    /// A clone of every live client stream so [`PlantServer::shutdown`]
    /// can force-close them — a handler blocked in `read` does not
    /// otherwise notice the server stopping.
    clients: Mutex<HashMap<u64, TcpStream>>,
    next_client: AtomicU64,
    /// The field's write-ownership claim, or `None` while the plant has
    /// never been claimed and stays open to every attachment. Once set
    /// it is never cleared: a dead owner's silence is exactly the
    /// failure the claim exists to fence, so only a fresh claim moves
    /// the ownership.
    writer: Mutex<Option<WriterClaim>>,
}

/// A held write-ownership claim: the owner token the last preempting
/// [`PlantRequest::ClaimWriter`] asserted plus the live connections
/// holding it — one field owner's several attachments claim the same
/// token so all of them write.
///
/// The holder set is what lets the server flag a duplicate owner: a
/// claim joining a token another live attachment already holds answers
/// [`PlantResponse::ClaimedShared`]. A connection's end drops only its
/// own hold — the claim stands even with no holders left, so a dead
/// owner keeps the field fenced for its token until a fresh claim
/// preempts.
struct WriterClaim {
    owner: u64,
    /// The connection ids holding `owner`. An attachment not in this
    /// set is fenced: its `write` and `step` requests are refused while
    /// the claim stands.
    holders: HashSet<u64>,
}

impl WriterClaim {
    /// Whether this claim names `owner` and a live attachment besides
    /// `connection` already holds it — the duplicate-owner signal a
    /// claim grant flags `ClaimedShared`.
    fn shared_with(&self, owner: u64, connection: u64) -> bool {
        self.owner == owner && self.holders.iter().any(|holder| *holder != connection)
    }
}

impl Shared {
    fn stopped(&self) -> bool {
        self.stopped.load(Ordering::Relaxed)
    }

    /// Registers `stream` and serves it on a dedicated thread.
    fn spawn_client(self: &Arc<Self>, stream: TcpStream) {
        let id = self.next_client.fetch_add(1, Ordering::Relaxed);
        if let Ok(registered) = stream.try_clone() {
            self.clients.lock().unwrap().insert(id, registered);
        }
        let shared = Arc::clone(self);
        thread::spawn(move || {
            serve_connection(&shared, stream, id);
            // The connection's end releases its hold on the writer
            // claim — never the claim itself, which keeps the field
            // fenced for the dead owner's token — so a later claim of
            // that token is not flagged shared with a corpse.
            release_hold(&shared.writer, id);
            shared.clients.lock().unwrap().remove(&id);
        });
    }
}

/// Drops `connection`'s hold on the writer claim. Unlike `dcs-sim-bus`'s
/// register claim — which a holder's disconnect frees — the plant claim
/// outlives its holders: an empty holder set still fences the field for
/// the claimed token, per the never-released rule.
fn release_hold(writer: &Mutex<Option<WriterClaim>>, connection: u64) {
    if let Some(claim) = writer.lock().unwrap().as_mut() {
        claim.holders.remove(&connection);
    }
}

/// One client connection's request loop: read a line, dispatch it, write
/// the response. Ends when the peer goes away, the link fails, the peer
/// violates the message bound, or the server stops.
fn serve_connection(shared: &Shared, stream: TcpStream, id: u64) {
    let _ = stream.set_nodelay(true);
    let mut reader = BufReader::new(stream);
    loop {
        if shared.stopped() {
            return;
        }
        let line = match read_message(&mut reader, MAX_MESSAGE) {
            Ok(Some(line)) => line,
            // Orderly close, broken link, or oversized message: the
            // connection is done either way.
            Ok(None) | Err(_) => return,
        };
        let response = match serde_json::from_slice::<PlantRequest>(&line) {
            Ok(request) => dispatch(shared, id, request),
            Err(error) => PlantResponse::Error {
                error: PlantError::InvalidRequest {
                    detail: error.to_string(),
                },
            },
        };
        if reader
            .get_mut()
            .write_all(&encode_message(&response))
            .is_err()
        {
            return;
        }
    }
}

/// Applies one parsed request to the shared driver, fencing
/// field-mutating requests by the caller's claim hold.
///
/// Every request produces exactly one response; a request the driver
/// refuses comes back as [`PlantError::Io`] carrying the driver's
/// [`IoError`](dcs_core::IoError) verbatim so the remote client surfaces
/// the same failure a local one would. `Write` and `Step` are the
/// field-mutating operations: while an owner is claimed, a connection
/// not holding the current claim sees its `write` refused with the
/// point's [`IoError::Fenced`] and its `step` with
/// [`PlantError::Fenced`] — the old owner's writes stop at the field,
/// not merely at its own gate.
fn dispatch(shared: &Shared, connection: u64, request: PlantRequest) -> PlantResponse {
    let applied = |result: Result<(), IoError>| match result {
        Ok(()) => PlantResponse::Done,
        Err(error) => PlantResponse::Error {
            error: PlantError::Io { error },
        },
    };
    match request {
        PlantRequest::Read { point } => match shared.driver.read(point) {
            Ok(sample) => PlantResponse::Sample { sample },
            Err(error) => PlantResponse::Error {
                error: PlantError::Io { error },
            },
        },
        // `Write` and `Step` mutate the shared field, so they fence on
        // the claimed owner — and the writer lock stays held across the
        // mutation itself, keeping a claim strictly ordered against a
        // write already in flight on another connection.
        PlantRequest::Write { point, value } => {
            let writer = shared.writer.lock().unwrap();
            if writer
                .as_ref()
                .is_some_and(|claim| !claim.holders.contains(&connection))
            {
                return PlantResponse::Error {
                    error: PlantError::Io {
                        error: IoError::Fenced(point),
                    },
                };
            }
            applied(shared.driver.write(point, value))
        }
        PlantRequest::Step { dt } => {
            // `SimDriver::step` panics on a non-finite or negative dt; the
            // protocol turns that contract violation into a named refusal.
            if !dt.is_finite() || dt < 0.0 {
                return PlantResponse::Error {
                    error: PlantError::InvalidRequest {
                        detail: format!("step dt must be finite and non-negative, got {dt}"),
                    },
                };
            }
            let writer = shared.writer.lock().unwrap();
            if writer
                .as_ref()
                .is_some_and(|claim| !claim.holders.contains(&connection))
            {
                return PlantResponse::Error {
                    error: PlantError::Fenced {
                        detail: "another attachment owns field writes".to_string(),
                    },
                };
            }
            PlantResponse::Stepped {
                tick: shared.driver.step(dt),
            }
        }
        PlantRequest::InjectFault { point, fault } => {
            applied(shared.driver.inject_fault(point, fault))
        }
        PlantRequest::ClearFault { point } => applied(shared.driver.clear_fault(point)),
        PlantRequest::ListPoints => PlantResponse::Points {
            points: shared.driver.points(),
        },
        PlantRequest::ClaimWriter { owner } => {
            // The grant preempts unconditionally: the promoted standby's
            // claim must beat the old owner's, wherever it still lives.
            // Claiming the standing owner joins this attachment to the
            // claim's holders — flagged `ClaimedShared` when another
            // live attachment already holds the token: the token cannot
            // tell one owner's second attachment from a second process
            // reusing it, and the second case silently defeats the
            // single-writer fencing a promotion relies on, so the grant
            // reports the sharing rather than hiding it.
            let mut writer = shared.writer.lock().unwrap();
            let shared_claim = writer
                .as_ref()
                .is_some_and(|claim| claim.shared_with(owner, connection));
            match writer.as_mut() {
                Some(claim) if claim.owner == owner => {
                    claim.holders.insert(connection);
                }
                _ => {
                    *writer = Some(WriterClaim {
                        owner,
                        holders: HashSet::from([connection]),
                    });
                }
            }
            if shared_claim {
                PlantResponse::ClaimedShared { owner }
            } else {
                PlantResponse::Done
            }
        }
        PlantRequest::EnsureWriter { owner } => {
            // The re-attach grant: the claim a reconnecting field owner
            // re-arms after a server restart dropped it. It is refused
            // while a *different* owner holds the field — a superseded
            // peer re-attaching cannot preempt the attachment that
            // claimed during the outage. Joining the standing owner is
            // flagged `ClaimedShared` exactly as `claim_writer` is.
            let mut writer = shared.writer.lock().unwrap();
            match writer.as_mut() {
                Some(claim) if claim.owner != owner => PlantResponse::Error {
                    error: PlantError::Fenced {
                        detail: "another attachment owns field writes".to_string(),
                    },
                },
                Some(claim) => {
                    let shared_claim = claim.shared_with(owner, connection);
                    claim.holders.insert(connection);
                    if shared_claim {
                        PlantResponse::ClaimedShared { owner }
                    } else {
                        PlantResponse::Done
                    }
                }
                None => {
                    *writer = Some(WriterClaim {
                        owner,
                        holders: HashSet::from([connection]),
                    });
                    PlantResponse::Done
                }
            }
        }
    }
}

/// A TCP server sharing one [`SimDriver`] plant with every connected
/// [`RemoteDriver`](crate::RemoteDriver) client.
///
/// The server owns the simulated field: all point state, loopbacks,
/// process elements, and injected faults live here, so two attached
/// controllers observe — and, stepping aside, drive — the same plant.
/// Each connection is served on its own thread; the driver's internal
/// mutex makes every request atomic, and because stepping only happens on
/// an explicit [`PlantRequest::Step`], an attached standby that only reads
/// sees the field exactly as the stepping client left it.
///
/// [`serve`](Self::serve) runs the accept loop on the caller's thread —
/// run it on a dedicated thread — and [`shutdown`](Self::shutdown) stops
/// it, closing the listener so the address refuses new connections and a
/// restarted plant may rebind the same port, and force-closing live
/// connections so blocked handler threads and remote clients see the
/// server go away.
pub struct PlantServer {
    shared: Arc<Shared>,
    /// `Some` while the server accepts; [`shutdown`](Self::shutdown)
    /// takes and drops it, closing the port — the stopped-plant shape a
    /// `docker stop` produces — rather than leaving a bound socket that
    /// accepts into a backlog nothing serves.
    listener: Mutex<Option<TcpListener>>,
    /// The bound address, captured at [`bind`](Self::bind) — still
    /// answerable after `shutdown` released the listener.
    addr: SocketAddr,
}

impl PlantServer {
    /// Binds a listener on `addr` serving `driver`'s plant.
    /// `("127.0.0.1", 0)` binds an ephemeral loopback port;
    /// [`local_addr`](Self::local_addr) reports the bound address.
    ///
    /// Taking the constructed driver — rather than a [`ChannelMap`]
    /// (dcs_sim::ChannelMap) — lets the caller serve a plant whose state
    /// was already stepped, faulted, or restored from a checkpoint.
    pub fn bind<A: ToSocketAddrs>(addr: A, driver: SimDriver) -> io::Result<Self> {
        let listener = TcpListener::bind(addr)?;
        let bound = listener.local_addr()?;
        // The accept loop polls rather than blocking so `shutdown` can
        // take the listener — the port closes and a rebound server gets
        // it — without waking a parked accept.
        listener.set_nonblocking(true)?;
        Ok(Self {
            shared: Arc::new(Shared {
                driver,
                stopped: AtomicBool::new(false),
                clients: Mutex::new(HashMap::new()),
                next_client: AtomicU64::new(0),
                writer: Mutex::new(None),
            }),
            listener: Mutex::new(Some(listener)),
            addr: bound,
        })
    }

    /// The address the listener is bound to — still reported after
    /// [`shutdown`](Self::shutdown) released it, so a restarted server
    /// can rebind the same port.
    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        Ok(self.addr)
    }

    /// The simulated plant the server is sharing.
    pub fn driver(&self) -> &SimDriver {
        &self.shared.driver
    }

    /// Serves connections until [`shutdown`](Self::shutdown).
    ///
    /// Runs on a dedicated thread — a scoped thread suffices since the
    /// server only borrows itself. Each accepted connection gets its own
    /// handler thread speaking the line protocol; transient accept
    /// failures are retried rather than killing the server. The listener
    /// is polled nonblocking so [`shutdown`](Self::shutdown) can close
    /// the port while this loop sits between accepts.
    pub fn serve(&self) {
        loop {
            if self.shared.stopped() {
                return;
            }
            let accepted = {
                let listener = self.listener.lock().unwrap();
                listener.as_ref().map(|listener| listener.accept())
            };
            match accepted {
                None => return,
                Some(Ok((stream, _))) => {
                    // An accepted stream is a fresh socket — restore the
                    // blocking mode the handler's read loop expects.
                    let _ = stream.set_nonblocking(false);
                    self.shared.spawn_client(stream);
                }
                Some(Err(error)) => {
                    if error.kind() != io::ErrorKind::WouldBlock {
                        // A persistent accept failure (e.g. descriptor
                        // exhaustion) must not spin hot — and neither
                        // may the WouldBlock poll.
                        thread::sleep(std::time::Duration::from_millis(1));
                    } else {
                        thread::sleep(std::time::Duration::from_millis(2));
                    }
                }
            }
        }
    }

    /// Stops a [`serve`](Self::serve) loop running on another thread and
    /// closes every live client connection.
    ///
    /// The flag is set first, then the listener is taken and dropped —
    /// the port refuses new connections from there on, and a restarted
    /// plant may rebind it — then each registered client stream is shut
    /// down, so a client blocked in, or next issuing, a request sees the
    /// connection fail rather than hang. Idempotent: a second call is a
    /// no-op.
    pub fn shutdown(&self) {
        if self.shared.stopped.swap(true, Ordering::Relaxed) {
            return;
        }
        drop(self.listener.lock().unwrap().take());
        for (_, client) in self.shared.clients.lock().unwrap().drain() {
            let _ = client.shutdown(Shutdown::Both);
        }
    }
}
