//! The plant server: one [`SimDriver`] shared by every connected client.

use crate::protocol::encode_message;
use crate::protocol::{MAX_MESSAGE, PlantError, PlantRequest, PlantResponse, read_message};
use dcs_core::{IoDriver, IoError};
use dcs_sim::SimDriver;
use std::collections::HashMap;
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
            serve_connection(&shared, stream);
            shared.clients.lock().unwrap().remove(&id);
        });
    }
}

/// One client connection's request loop: read a line, dispatch it, write
/// the response. Ends when the peer goes away, the link fails, the peer
/// violates the message bound, or the server stops.
fn serve_connection(shared: &Shared, stream: TcpStream) {
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
            Ok(request) => dispatch(&shared.driver, request),
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

/// Applies one parsed request to the shared driver.
///
/// Every request produces exactly one response; a request the driver
/// refuses comes back as [`PlantError::Io`] carrying the driver's
/// [`IoError`](dcs_core::IoError) verbatim so the remote client surfaces
/// the same failure a local one would.
fn dispatch(driver: &SimDriver, request: PlantRequest) -> PlantResponse {
    let applied = |result: Result<(), IoError>| match result {
        Ok(()) => PlantResponse::Done,
        Err(error) => PlantResponse::Error {
            error: PlantError::Io { error },
        },
    };
    match request {
        PlantRequest::Read { point } => match driver.read(point) {
            Ok(sample) => PlantResponse::Sample { sample },
            Err(error) => PlantResponse::Error {
                error: PlantError::Io { error },
            },
        },
        PlantRequest::Write { point, value } => applied(driver.write(point, value)),
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
            PlantResponse::Stepped {
                tick: driver.step(dt),
            }
        }
        PlantRequest::InjectFault { point, fault } => applied(driver.inject_fault(point, fault)),
        PlantRequest::ClearFault { point } => applied(driver.clear_fault(point)),
        PlantRequest::ListPoints => PlantResponse::Points {
            points: driver.points(),
        },
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
/// [`serve`](Self::serve) runs the blocking accept loop on the caller's
/// thread — run it on a dedicated thread — and [`shutdown`](Self::shutdown)
/// stops it, force-closing live connections so blocked handler threads and
/// remote clients see the server go away.
pub struct PlantServer {
    shared: Arc<Shared>,
    listener: TcpListener,
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
        Ok(Self {
            shared: Arc::new(Shared {
                driver,
                stopped: AtomicBool::new(false),
                clients: Mutex::new(HashMap::new()),
                next_client: AtomicU64::new(0),
            }),
            listener: TcpListener::bind(addr)?,
        })
    }

    /// The address the listener is bound to.
    pub fn local_addr(&self) -> io::Result<SocketAddr> {
        self.listener.local_addr()
    }

    /// The simulated plant the server is sharing.
    pub fn driver(&self) -> &SimDriver {
        &self.shared.driver
    }

    /// Serves connections until [`shutdown`](Self::shutdown).
    ///
    /// Blocking: run this on a dedicated thread — a scoped thread suffices
    /// since the server only borrows itself. Each accepted connection gets
    /// its own handler thread speaking the line protocol; transient accept
    /// failures are retried rather than killing the server.
    pub fn serve(&self) {
        loop {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    if self.shared.stopped() {
                        return;
                    }
                    self.shared.spawn_client(stream);
                }
                Err(_) => {
                    if self.shared.stopped() {
                        return;
                    }
                    // A persistent accept failure (e.g. descriptor
                    // exhaustion) must not spin hot.
                    thread::sleep(std::time::Duration::from_millis(1));
                }
            }
        }
    }

    /// Stops a [`serve`](Self::serve) loop running on another thread and
    /// closes every live client connection.
    ///
    /// The flag is set first, then the accept loop is woken with a
    /// throwaway self-connection, then each registered client stream is
    /// shut down — so a client blocked in, or next issuing, a request sees
    /// the connection fail rather than hang. The listener itself is
    /// released when the `PlantServer` is dropped.
    pub fn shutdown(&self) {
        self.shared.stopped.store(true, Ordering::Relaxed);
        // Wake the blocking accept so the serve loop observes the flag.
        if let Ok(addr) = self.listener.local_addr() {
            let _ = TcpStream::connect(addr);
        }
        for (_, client) in self.shared.clients.lock().unwrap().drain() {
            let _ = client.shutdown(Shutdown::Both);
        }
    }
}
