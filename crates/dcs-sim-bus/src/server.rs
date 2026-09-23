//! The device server: one [`RegisterBank`] shared by every connected
//! client — the register-mapped analogue of `dcs-sim-net`'s plant
//! server.

use crate::bank::RegisterBank;
use crate::protocol::{
    BusError, BusRequest, BusResponse, ExchangeOutcome, MAX_FRAME, decode_request, encode_response,
    read_frame,
};
use dcs_core::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet, VecDeque};
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
    /// The device's write-ownership claim — `None` while no attachment
    /// has claimed the field, which stays open to every attachment.
    /// Unlike `dcs-sim-net`'s plant claim — held by owner token until
    /// preempted, never released — the register claim is bound to the
    /// attachments holding it: a holder's disconnect or
    /// [`BusRequest::ReleaseWriter`] drops it, and the last drop frees
    /// the device, so a dead owner's silence cannot fence the field
    /// against a promoted peer's claim.
    writer: Mutex<Option<WriterClaim>>,
    /// The station layout a `sim-cyclic` device declares: station name
    /// → the register addresses its channels occupy. A
    /// [`ExchangeOutcome::ShortStation`] withholds exactly one
    /// station's registers from the exchange answer, so the attribution
    /// the driver applies is the device's own declaration — an empty
    /// map serves a `sim-bus` device, whose `Exchange` requests a
    /// station shortfall can never be scripted against.
    stations: BTreeMap<String, BTreeSet<u16>>,
    /// The scripted exchange queue [`BusRequest::ScriptExchange`]
    /// appends to and every [`BusRequest::Exchange`] consumes one entry
    /// from — the development harness deciding which outcomes the
    /// device's exchanges present. An empty queue means steady state:
    /// every exchange completes.
    script: Mutex<VecDeque<ExchangeOutcome>>,
}

/// A held write-ownership claim: the owner token the last preempting
/// [`BusRequest::ClaimWriter`] asserted plus the live connections
/// holding it — one field owner's several attachments claim the same
/// token so all of them write.
struct WriterClaim {
    owner: u64,
    /// The connection ids holding `owner`. An attachment not in this
    /// set is fenced: its `write_register` and `step` requests are
    /// refused while the claim stands.
    holders: HashSet<u64>,
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
            // The claim is bound to its attachments: the connection's
            // end — orderly close, broken link, or a protocol-violation
            // drop — releases its hold, freeing the field when the
            // last holder goes.
            release_claim(&shared.writer, id);
            shared.clients.lock().unwrap().remove(&id);
        });
    }
}

/// Drops `connection`'s hold on the writer claim, freeing the device
/// when its last holder goes — the release half of the claim's rule,
/// shared by [`BusRequest::ReleaseWriter`] and connection teardown.
fn release_claim(writer: &Mutex<Option<WriterClaim>>, connection: u64) {
    let mut guard = writer.lock().unwrap();
    if let Some(claim) = guard.as_mut() {
        claim.holders.remove(&connection);
        if claim.holders.is_empty() {
            *guard = None;
        }
    }
}

/// One client connection's request loop: read a frame, dispatch it,
/// write the response. Ends when the peer goes away, the link fails,
/// the peer violates the frame bound, or the server stops.
fn serve_connection(shared: &Shared, stream: TcpStream, connection: u64) {
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
            Ok(request) => match dispatch(shared, connection, request) {
                Some(response) => response,
                // A scripted missed exchange: the device answers
                // nothing — the dropped connection is the link
                // failure the driver's exchange observes.
                None => return,
            },
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

/// The refusal a fenced attachment's field-mutating request answers
/// with — the driver's `write` path surfaces it as the addressed
/// point's `IoError::Fenced`, the same named failure a fenced
/// plant-protocol write produces.
fn fenced_out() -> BusResponse {
    BusResponse::Error {
        error: BusError::Fenced {
            detail: "another attachment owns register writes".to_string(),
        },
    }
}

/// Applies one parsed request to the shared bank, fencing
/// field-mutating requests by the caller's claim hold.
///
/// Every request produces exactly one response; a register-level
/// failure comes back as [`BusResponse::Error`] carrying the bank's
/// [`BusError`] so the client surfaces it at the point it addressed.
/// `WriteRegister` and `Step` are the field-mutating operations: while
/// a claim is held, a connection not holding it sees both refused with
/// [`BusError::Fenced`] — the old owner's writes stop at the field,
/// not merely at its own gate. `InjectQuality` and `ClearQuality`
/// mutate the bank too but are deliberately unfenced: fault injection
/// is development tooling, so a test or operator tool not holding the
/// claim can fault a point while a controller pair owns the field.
///
/// The answer is `Some` for every request but a scripted
/// [`ExchangeOutcome::Miss`]: the miss is the connection dropping
/// unanswered, so `None` tells the connection loop to hang up without
/// a frame.
fn dispatch(shared: &Shared, connection: u64, request: BusRequest) -> Option<BusResponse> {
    Some(match request {
        BusRequest::ReadRegister { register } => match shared.bank.read(register) {
            Ok(sample) => BusResponse::Sample { sample },
            Err(error) => BusResponse::Error { error },
        },
        BusRequest::Exchange { outputs } => {
            // Publishing staged outputs is field mutation: a fenced
            // attachment's image never reaches the bank. A census-only
            // exchange — a tracking standby's, which stages nothing —
            // is a read and always completes.
            let writer = shared.writer.lock().unwrap();
            if !outputs.is_empty()
                && writer
                    .as_ref()
                    .is_some_and(|claim| !claim.holders.contains(&connection))
            {
                return Some(fenced_out());
            }
            drop(writer);
            // The exchange applies all of its outputs or none: every
            // staged write is validated against the bank before the
            // scripted outcome is consumed, so a refused exchange
            // completes nothing.
            for output in &outputs {
                match shared.bank.read(output.register) {
                    Ok(sample) if sample.value.kind() != output.value.kind() => {
                        return Some(BusResponse::Error {
                            error: BusError::KindMismatch {
                                register: output.register,
                                expected: sample.value.kind(),
                                found: output.value,
                            },
                        });
                    }
                    Ok(_) => {
                        if let Value::Float(v) = output.value
                            && !v.is_finite()
                        {
                            return Some(BusResponse::Error {
                                error: BusError::InvalidRequest {
                                    detail: format!(
                                        "register {} refused a non-finite value",
                                        output.register
                                    ),
                                },
                            });
                        }
                    }
                    Err(error) => return Some(BusResponse::Error { error }),
                }
            }
            let outcome = shared
                .script
                .lock()
                .unwrap()
                .pop_front()
                .unwrap_or(ExchangeOutcome::Complete);
            let withhold: BTreeSet<u16> = match &outcome {
                // The miss answers with nothing at all.
                ExchangeOutcome::Miss => return None,
                ExchangeOutcome::ShortStation { station } => {
                    match shared.stations.get(station.as_str()) {
                        Some(registers) => registers.clone(),
                        None => {
                            return Some(BusResponse::Error {
                                error: BusError::InvalidRequest {
                                    detail: format!(
                                        "scripted short exchange names station {station:?} the device does not declare"
                                    ),
                                },
                            });
                        }
                    }
                }
                ExchangeOutcome::ShortRegisters { registers } => {
                    registers.iter().copied().collect()
                }
                ExchangeOutcome::Complete | ExchangeOutcome::Late => BTreeSet::new(),
            };
            // Publish before the census: the outputs a short exchange
            // withholds stay unpublished — the exchange completed
            // nothing for them.
            for output in &outputs {
                if !withhold.contains(&output.register) {
                    shared
                        .bank
                        .write(output.register, output.value)
                        .expect("every staged output was validated above");
                }
            }
            let registers = shared
                .bank
                .registers()
                .into_iter()
                .filter(|info| !withhold.contains(&info.register))
                .collect();
            BusResponse::Exchanged {
                registers,
                late: matches!(outcome, ExchangeOutcome::Late),
            }
        }
        BusRequest::ScriptExchange { outcomes } => {
            // Scripting is development tooling — unfenced like quality
            // injection. The whole queue validates before any of it
            // lands, so a malformed script changes nothing.
            for outcome in &outcomes {
                match outcome {
                    ExchangeOutcome::ShortStation { station } => {
                        if !shared.stations.contains_key(station.as_str()) {
                            return Some(BusResponse::Error {
                                error: BusError::InvalidRequest {
                                    detail: format!(
                                        "scripted short exchange names station {station:?} the device does not declare"
                                    ),
                                },
                            });
                        }
                    }
                    ExchangeOutcome::ShortRegisters { registers } => {
                        for &register in registers {
                            if let Err(error) = shared.bank.read(register) {
                                return Some(BusResponse::Error { error });
                            }
                        }
                    }
                    ExchangeOutcome::Complete | ExchangeOutcome::Miss | ExchangeOutcome::Late => {}
                }
            }
            shared.script.lock().unwrap().extend(outcomes);
            BusResponse::Done
        }
        BusRequest::WriteRegister { register, value } => {
            // The claim stays locked across the write itself, keeping a
            // claim strictly ordered against a write already in flight
            // on another connection.
            let writer = shared.writer.lock().unwrap();
            if writer
                .as_ref()
                .is_some_and(|claim| !claim.holders.contains(&connection))
            {
                return Some(fenced_out());
            }
            match shared.bank.write(register, value) {
                Ok(tick) => BusResponse::Written { tick },
                Err(error) => BusResponse::Error { error },
            }
        }
        BusRequest::Step { dt } => {
            // `RegisterBank::step` panics on a non-finite or negative
            // dt; the protocol turns that contract violation into a
            // named refusal — the same rule the plant protocol's step
            // applies.
            if !dt.is_finite() || dt < 0.0 {
                return Some(BusResponse::Error {
                    error: BusError::InvalidRequest {
                        detail: format!("step dt must be finite and non-negative, got {dt}"),
                    },
                });
            }
            let writer = shared.writer.lock().unwrap();
            if writer
                .as_ref()
                .is_some_and(|claim| !claim.holders.contains(&connection))
            {
                return Some(fenced_out());
            }
            BusResponse::Stepped {
                tick: shared.bank.step(dt),
            }
        }
        BusRequest::ListRegisters => BusResponse::Registers {
            registers: shared.bank.registers(),
        },
        BusRequest::ClaimWriter { owner } => {
            // The grant is unconditional — the promoted peer's claim
            // must beat the old owner's, wherever it still lives.
            // Claiming the held owner joins this attachment to the
            // claim's holders, so one owner's several connections all
            // write.
            let mut writer = shared.writer.lock().unwrap();
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
            BusResponse::Done
        }
        BusRequest::ReleaseWriter => {
            release_claim(&shared.writer, connection);
            BusResponse::Done
        }
        // Quality injection is development tooling, not field
        // ownership: like reads and the census it is never fenced, so
        // a scripted rig can fault a register while a controller pair
        // owns the field.
        BusRequest::InjectQuality { register, quality } => {
            match shared.bank.inject_quality(register, quality) {
                Ok(()) => BusResponse::Done,
                Err(error) => BusResponse::Error { error },
            }
        }
        BusRequest::ClearQuality { register } => match shared.bank.clear_quality(register) {
            Ok(()) => BusResponse::Done,
            Err(error) => BusResponse::Error { error },
        },
    })
}

/// A TCP server sharing one [`RegisterBank`] with every connected
/// [`BusDriver`](crate::BusDriver) client — a register-mapped simulated
/// fieldbus device.
///
/// The server owns the device's visible state: all register values,
/// the logical tick, and any declared dynamics' element state live
/// here, so two attached clients observe — and, stepping aside, drive
/// — the same device. Each connection is served on its own thread; the
/// bank's internal mutex makes every request atomic, and because the
/// tick advances only on an explicit [`BusRequest::Step`], an attached
/// client that only reads sees the registers exactly as the stepping
/// client left them.
///
/// The server arbitrates a single writer for the failover fencing the
/// crate root documents: a [`BusRequest::ClaimWriter`] grants the
/// write claim to the requesting attachment — preempting whichever
/// owner held it — and while a claim stands, `write_register` and
/// `step` from an attachment not holding it answer
/// [`BusError::Fenced`]. The claim is bound to its attachments: it
/// releases on the holder's disconnect or
/// [`BusRequest::ReleaseWriter`], the last release reopening the field.
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
    ///
    /// The device declares no stations: `exchange` requests are served
    /// (a full census every time) but no station-attributed short
    /// outcome can be scripted against them — [`bind_stationed`] is the
    /// `sim-cyclic` half.
    pub fn bind<A: ToSocketAddrs>(addr: A, bank: RegisterBank) -> io::Result<Self> {
        Self::bind_stationed(addr, bank, BTreeMap::new())
    }

    /// As [`bind`](Self::bind) for a `sim-cyclic` device: `stations`
    /// declares the station layout the exchange protocol attributes
    /// short exchanges to — station name → the register addresses its
    /// channels occupy, the map
    /// [`CyclicDeviceParameters::station_registers`](crate::CyclicDeviceParameters::station_registers)
    /// produces.
    pub fn bind_stationed<A: ToSocketAddrs>(
        addr: A,
        bank: RegisterBank,
        stations: BTreeMap<String, BTreeSet<u16>>,
    ) -> io::Result<Self> {
        Ok(Self {
            shared: Arc::new(Shared {
                bank,
                stopped: AtomicBool::new(false),
                clients: Mutex::new(HashMap::new()),
                next_client: AtomicU64::new(0),
                writer: Mutex::new(None),
                stations,
                script: Mutex::new(VecDeque::new()),
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
