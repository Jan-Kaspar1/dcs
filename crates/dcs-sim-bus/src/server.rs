//! The device server: one [`RegisterBank`] shared by every connected
//! client — the register-mapped analogue of `dcs-sim-net`'s plant
//! server.

use crate::bank::RegisterBank;
use crate::protocol::{
    BusError, BusRequest, BusResponse, MAX_FRAME, decode_request, encode_response, read_frame,
};
use std::collections::HashMap;
use std::io::{self, BufReader, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

/// State shared between the accept loop and every client handler
/// thread.
struct Shared {
    /// The register bank all connections observe and write. The bank's
    /// own mutex serializes requests across connections.
    bank: RegisterBank,
    /// Set by [`BusServer::shutdown`]; ends the accept loop and lets
    /// handlers exit at their next request.
    stopped: AtomicBool,
    /// A clone of every live client stream so [`BusServer::shutdown`]
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

/// One client connection's request loop: read a frame, dispatch it,
/// write the response. Ends when the peer goes away, the link fails,
/// the peer violates the frame bound, or the server stops.
fn serve_connection(shared: &Shared, stream: TcpStream) {
    let _ = stream.set_nodelay(true);
    let mut reader = BufReader::new(stream);
    loop {
        if shared.stopped() {
            return;
        }
        let body = match read_frame(&mut reader, MAX_FRAME) {
            Ok(Some(body)) => body,
            // Orderly close, broken link, or oversized frame: the
            // connection is done either way.
            Ok(None) | Err(_) => return,
        };
        let response = match decode_request(&body) {
            Ok(request) => dispatch(&shared.bank, request),
            Err(detail) => BusResponse::Error {
                error: BusError::InvalidRequest { detail },
            },
        };
        if reader
            .get_mut()
            .write_all(&encode_response(&response))
            .is_err()
        {
            return;
        }
    }
}

/// Applies one parsed request to the shared bank.
///
/// Every request produces exactly one response; a register-level
/// failure comes back as [`BusResponse::Error`] carrying the bank's
/// [`BusError`] so the client surfaces it at the point it addressed.
fn dispatch(bank: &RegisterBank, request: BusRequest) -> BusResponse {
    match request {
        BusRequest::ReadRegister { register } => match bank.read(register) {
            Ok(sample) => BusResponse::Sample { sample },
            Err(error) => BusResponse::Error { error },
        },
        BusRequest::WriteRegister { register, value } => match bank.write(register, value) {
            Ok(tick) => BusResponse::Written { tick },
            Err(error) => BusResponse::Error { error },
        },
        BusRequest::ListRegisters => BusResponse::Registers {
            registers: bank.registers(),
        },
        BusRequest::Step => BusResponse::Stepped { tick: bank.step() },
    }
}

/// A TCP server sharing one [`RegisterBank`] with every connected
/// [`BusDriver`](crate::BusDriver) client — a register-mapped simulated
/// fieldbus device.
///
/// The server owns the device's visible state: all register values and
/// the logical tick live here, so two attached clients observe — and,
/// stepping aside, drive — the same device. Each connection is served
/// on its own thread; the bank's internal mutex makes every request
/// atomic, and because the tick advances only on an explicit
/// [`BusRequest::Step`], an attached client that only reads sees the
/// registers exactly as the stepping client left them.
///
/// [`serve`](Self::serve) runs the blocking accept loop on the caller's
/// thread — run it on a dedicated thread — and
/// [`shutdown`](Self::shutdown) stops it, force-closing live
/// connections so blocked handler threads and remote clients see the
/// server go away.
pub struct BusServer {
    shared: Arc<Shared>,
    listener: TcpListener,
}

impl BusServer {
    /// Binds a listener on `addr` serving `bank`'s registers.
    /// `("127.0.0.1", 0)` binds an ephemeral loopback port;
    /// [`local_addr`](Self::local_addr) reports the bound address.
    ///
    /// Taking the constructed bank — rather than its declarations —
    /// lets the caller serve a device whose registers were already
    /// written or stepped.
    pub fn bind<A: ToSocketAddrs>(addr: A, bank: RegisterBank) -> io::Result<Self> {
        Ok(Self {
            shared: Arc::new(Shared {
                bank,
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

    /// The register bank the server is sharing.
    pub fn bank(&self) -> &RegisterBank {
        &self.shared.bank
    }

    /// Serves connections until [`shutdown`](Self::shutdown).
    ///
    /// Blocking: run this on a dedicated thread — a scoped thread
    /// suffices since the server only borrows itself. Each accepted
    /// connection gets its own handler thread speaking the frame
    /// protocol; transient accept failures are retried rather than
    /// killing the server.
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

    /// Stops a [`serve`](Self::serve) loop running on another thread
    /// and closes every live client connection.
    ///
    /// The flag is set first, then the accept loop is woken with a
    /// throwaway self-connection, then each registered client stream is
    /// shut down — so a client blocked in, or next issuing, a request
    /// sees the connection fail rather than hang. The listener itself
    /// is released when the `BusServer` is dropped.
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
