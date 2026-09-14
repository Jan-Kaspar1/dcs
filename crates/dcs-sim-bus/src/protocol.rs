//! The register-mapped wire protocol [`BusServer`](crate::BusServer)
//! serves and [`BusDriver`](crate::BusDriver) speaks.
//!
//! The byte-level frame, request, and response layout is documented in
//! the crate root. Unlike `dcs-sim-net`'s newline-delimited JSON plant
//! protocol — which addresses logical points — this protocol models a
//! fieldbus device: the peer's visible state is a bank of numbered
//! registers, every point access names a register address, and the
//! framing is binary. A client has at most one request in flight per
//! connection, which is what makes the synchronous exchange's failure
//! semantics well-defined.

use dcs_core::{Quality, QualityReason, Sample, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::io::{self, BufReader, Read};
use std::net::TcpStream;

/// The maximum payload size of one protocol frame in bytes, length
/// prefix excluded.
///
/// Register-mapped payloads are small and fixed-shape, so an oversized
/// frame is a misbehaving peer, not a large request.
pub const MAX_FRAME: usize = 8 * 1024;

// Request operation tags.
const OP_READ_REGISTER: u8 = 0x01;
const OP_WRITE_REGISTER: u8 = 0x02;
const OP_LIST_REGISTERS: u8 = 0x03;
const OP_STEP: u8 = 0x04;
const OP_CLAIM_WRITER: u8 = 0x05;
const OP_RELEASE_WRITER: u8 = 0x06;
const OP_INJECT_QUALITY: u8 = 0x07;
const OP_CLEAR_QUALITY: u8 = 0x08;

// Response variant tags.
const RESP_SAMPLE: u8 = 0x01;
const RESP_WRITTEN: u8 = 0x02;
const RESP_REGISTERS: u8 = 0x03;
const RESP_STEPPED: u8 = 0x04;
const RESP_ERROR: u8 = 0x05;
const RESP_DONE: u8 = 0x06;

// Error codes on the wire.
const ERR_UNKNOWN_REGISTER: u8 = 0x01;
const ERR_KIND_MISMATCH: u8 = 0x02;
const ERR_INVALID_REQUEST: u8 = 0x03;
const ERR_FENCED: u8 = 0x04;

// Value kind tags on the wire.
const KIND_BOOL: u8 = 0x01;
const KIND_INT: u8 = 0x02;
const KIND_FLOAT: u8 = 0x03;

// Quality severity tags on the wire.
const QUALITY_GOOD: u8 = 0x01;
const QUALITY_UNCERTAIN: u8 = 0x02;
const QUALITY_BAD: u8 = 0x03;

// Quality reason tags on the wire, in `QualityReason` declaration order.
const REASON_UNSPECIFIED: u8 = 0x01;
const REASON_SUBSTITUTED: u8 = 0x02;
const REASON_STALE: u8 = 0x03;
const REASON_OUT_OF_RANGE: u8 = 0x04;
const REASON_COMMUNICATION_FAULT: u8 = 0x05;
const REASON_DEVICE_FAULT: u8 = 0x06;
const REASON_CONFIGURATION_FAULT: u8 = 0x07;

/// One request a client sends to the device server.
///
/// The variant set mirrors the [`RegisterBank`](crate::RegisterBank)
/// operations the server wraps: register read and write — the two
/// [`IoDriver`](dcs_core::IoDriver) accesses, mapped through the
/// driver's point-to-register table — plus the register census and the
/// explicit step advancing the device's logical tick.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum BusRequest {
    /// Reads one register's stored sample.
    ReadRegister {
        /// The register address.
        register: u16,
    },
    /// Writes `value` to one register; the value's kind must match the
    /// register's declared kind.
    WriteRegister {
        /// The register address.
        register: u16,
        /// The value to store.
        value: Value,
    },
    /// Lists every register the device serves, ordered by address.
    ListRegisters,
    /// Advances the device's logical tick by one — the explicit
    /// simulation step — stepping any declared dynamics by `dt` time
    /// units. `dt` must be finite and non-negative; a step carrying
    /// one that is not is refused with
    /// [`BusError::InvalidRequest`].
    Step {
        /// The simulated time this step advances the declared dynamics
        /// by — the same caller-supplied time base the plant
        /// protocol's `step` carries.
        dt: f64,
    },
    /// Takes the device's write-ownership claim for `owner` — the
    /// single-writer arbitration the failover decision fences a
    /// superseded active out with.
    ///
    /// `owner` is an opaque token the caller picks, unique per field
    /// owner — one controller's several attachments claim the same
    /// token so all of them write, while a takeover claims a fresh one.
    /// The grant is unconditional: the claim preempts whichever owner
    /// held the device. The claim is bound to the attachments holding
    /// it — released when a holder's connection drops or sends
    /// [`BusRequest::ReleaseWriter`], the last release freeing the
    /// device — so a dead owner's claim dies with its link and a
    /// promoted peer's claim lands on a free field. Once any owner
    /// holds the claim, `write_register` and `step` requests from an
    /// attachment not holding it are refused; reads,
    /// `list_registers`, and quality injection stay open to every
    /// attachment.
    ClaimWriter {
        /// The ownership token the claim asserts.
        owner: u64,
    },
    /// Releases this attachment's hold on the write-ownership claim —
    /// the explicit half of the claim's release rule; the other is the
    /// connection dropping. Releasing a claim the attachment does not
    /// hold is a no-op.
    ReleaseWriter,
    /// Stamps `register`'s stored sample with `quality` — the inject
    /// half of the quality-override pair, this protocol's analogue of
    /// `dcs-sim-net`'s `inject_fault` carrying a quality fault. The
    /// stored value and tick are untouched; the declared quality stands
    /// on the stored sample — reported by every read and the register
    /// census — until [`BusRequest::ClearQuality`] or a real
    /// [`BusRequest::WriteRegister`], which stores a `Good` sample,
    /// overwrites it.
    ///
    /// Injection is development tooling, not field ownership: like a
    /// read it is never fenced, so an attachment not holding the
    /// write-ownership claim can fault a point while a controller pair
    /// owns the field.
    InjectQuality {
        /// The register address.
        register: u16,
        /// The quality the stored sample reports.
        quality: Quality,
    },
    /// Restores `register`'s stored sample to
    /// [`Quality::Good`](dcs_core::Quality::Good) — the clear half of
    /// the quality-override pair. Clearing a register carrying no
    /// injection leaves the `Good` it already reports. Like the
    /// inject, the clear is open to every attachment.
    ClearQuality {
        /// The register address.
        register: u16,
    },
}

/// The server's answer to one [`BusRequest`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum BusResponse {
    /// Answer to [`BusRequest::ReadRegister`]: the register's stored
    /// sample — its value, its quality (`Good` unless a
    /// [`BusRequest::InjectQuality`] stamped it), and the device tick
    /// that stamped it.
    Sample {
        /// The stored sample.
        sample: Sample,
    },
    /// Answer to [`BusRequest::WriteRegister`]: the write applied,
    /// reporting the tick the stored sample was stamped with.
    Written {
        /// The device tick at the moment of the write.
        tick: Tick,
    },
    /// Answer to [`BusRequest::ListRegisters`]: every register's
    /// address and stored sample, ordered by address.
    Registers {
        /// The register census.
        registers: Vec<RegisterInfo>,
    },
    /// Answer to [`BusRequest::Step`]: the bank's new tick.
    Stepped {
        /// The tick the step advanced to.
        tick: Tick,
    },
    /// Answer to [`BusRequest::ClaimWriter`],
    /// [`BusRequest::ReleaseWriter`], [`BusRequest::InjectQuality`],
    /// and [`BusRequest::ClearQuality`]: the request applied.
    Done,
    /// The request failed; `error` says why.
    Error {
        /// The failure the server reported.
        error: BusError,
    },
}

/// One register's description, as [`BusRequest::ListRegisters`] reports
/// it.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct RegisterInfo {
    /// The register address.
    pub register: u16,
    /// The stored sample readers observe.
    pub sample: Sample,
}

/// A failure the device server reports in a [`BusResponse::Error`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum BusError {
    /// The request names a register the device does not serve.
    UnknownRegister {
        /// The offending register address.
        register: u16,
    },
    /// A write carried a value whose kind differs from the register's
    /// declared kind.
    KindMismatch {
        /// The offending register address.
        register: u16,
        /// The register's declared kind.
        expected: ValueKind,
        /// The value the write carried.
        found: Value,
    },
    /// The request itself could not be served: a frame that does not
    /// decode as a [`BusRequest`]. `detail` is human-readable
    /// diagnostics, not a machine contract.
    InvalidRequest {
        /// Why the request was refused.
        detail: String,
    },
    /// The request mutates the shared device but the attachment does
    /// not hold the device's write-ownership claim — the fencing
    /// verdict of the failover decision. The driver's `write` path
    /// surfaces this as the point's
    /// [`IoError::Fenced`](dcs_core::IoError::Fenced), the same named
    /// failure a fenced plant-protocol write produces.
    Fenced {
        /// Why the request was refused.
        detail: String,
    },
}

impl fmt::Display for BusError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownRegister { register } => {
                write!(f, "the device serves no register {register}")
            }
            Self::KindMismatch {
                register,
                expected,
                found,
            } => write!(
                f,
                "register {register} is declared {expected:?}, found {found:?}"
            ),
            Self::InvalidRequest { detail } => write!(f, "invalid request: {detail}"),
            Self::Fenced { detail } => write!(f, "fenced: {detail}"),
        }
    }
}

impl std::error::Error for BusError {}

/// Reads one length-prefixed frame of at most `max` payload bytes from
/// a client or server connection.
///
/// `Ok(None)` means the peer closed the connection — including closing
/// it mid-frame, which loses the partial payload; the framing is broken
/// either way, so the caller sees the same "peer gone" signal. An `Err`
/// of kind [`io::ErrorKind::InvalidData`] means the announced length
/// exceeded `max` — a protocol violation the caller answers by dropping
/// the connection. Other errors are ordinary I/O failures,
/// `WouldBlock`/`TimedOut` included when the stream carries a read
/// timeout.
pub(crate) fn read_frame(
    reader: &mut BufReader<TcpStream>,
    max: usize,
) -> io::Result<Option<Vec<u8>>> {
    let mut header = [0u8; 2];
    match reader.read_exact(&mut header) {
        Ok(()) => {}
        // EOF anywhere in the prefix means the peer is gone.
        Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error),
    }
    let length = u16::from_be_bytes(header) as usize;
    if length > max {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "protocol frame exceeds the size bound",
        ));
    }
    let mut body = vec![0u8; length];
    reader.read_exact(&mut body)?;
    Ok(Some(body))
}

/// The decoding half of the codec: a cursor over one frame's payload.
struct Reader<'a> {
    body: &'a [u8],
}

impl<'a> Reader<'a> {
    fn new(body: &'a [u8]) -> Self {
        Self { body }
    }

    fn take(&mut self, n: usize) -> Option<&'a [u8]> {
        if self.body.len() < n {
            return None;
        }
        let (head, tail) = self.body.split_at(n);
        self.body = tail;
        Some(head)
    }

    fn u8(&mut self) -> Option<u8> {
        Some(self.take(1)?[0])
    }

    fn u16(&mut self) -> Option<u16> {
        Some(u16::from_be_bytes(self.take(2)?.try_into().unwrap()))
    }

    fn u64(&mut self) -> Option<u64> {
        Some(u64::from_be_bytes(self.take(8)?.try_into().unwrap()))
    }

    fn value(&mut self) -> Option<Value> {
        Some(match self.u8()? {
            KIND_BOOL => Value::Bool(match self.u8()? {
                0 => false,
                1 => true,
                _ => return None,
            }),
            KIND_INT => Value::Int(i64::from_be_bytes(self.take(8)?.try_into().unwrap())),
            KIND_FLOAT => Value::Float(f64::from_be_bytes(self.take(8)?.try_into().unwrap())),
            _ => return None,
        })
    }

    fn kind(&mut self) -> Option<ValueKind> {
        Some(match self.u8()? {
            KIND_BOOL => ValueKind::Bool,
            KIND_INT => ValueKind::Int,
            KIND_FLOAT => ValueKind::Float,
            _ => return None,
        })
    }

    fn reason(&mut self) -> Option<QualityReason> {
        Some(match self.u8()? {
            REASON_UNSPECIFIED => QualityReason::Unspecified,
            REASON_SUBSTITUTED => QualityReason::Substituted,
            REASON_STALE => QualityReason::Stale,
            REASON_OUT_OF_RANGE => QualityReason::OutOfRange,
            REASON_COMMUNICATION_FAULT => QualityReason::CommunicationFault,
            REASON_DEVICE_FAULT => QualityReason::DeviceFault,
            REASON_CONFIGURATION_FAULT => QualityReason::ConfigurationFault,
            _ => return None,
        })
    }

    fn quality(&mut self) -> Option<Quality> {
        Some(match self.u8()? {
            QUALITY_GOOD => Quality::Good,
            QUALITY_UNCERTAIN => Quality::Uncertain(self.reason()?),
            QUALITY_BAD => Quality::Bad(self.reason()?),
            _ => return None,
        })
    }

    /// A stored sample: value, the tick that stamped it, then its
    /// quality — `Good` unless an injection stamped it otherwise. The
    /// quality bytes were appended to the original value-plus-tick
    /// shape when injection made registers carry non-`Good` quality.
    fn sample(&mut self) -> Option<Sample> {
        let value = self.value()?;
        let tick = Tick(self.u64()?);
        Some(Sample::new(value, self.quality()?, tick))
    }

    fn text(&mut self) -> Option<String> {
        let length = self.u16()? as usize;
        String::from_utf8(self.take(length)?.to_vec()).ok()
    }

    /// Whether the payload is fully consumed — a well-formed frame
    /// carries no trailing bytes.
    fn done(&self) -> bool {
        self.body.is_empty()
    }
}

fn push_kind(out: &mut Vec<u8>, kind: ValueKind) {
    out.push(match kind {
        ValueKind::Bool => KIND_BOOL,
        ValueKind::Int => KIND_INT,
        ValueKind::Float => KIND_FLOAT,
    });
}

fn push_value(out: &mut Vec<u8>, value: Value) {
    push_kind(out, value.kind());
    match value {
        Value::Bool(v) => out.push(v as u8),
        Value::Int(v) => out.extend_from_slice(&v.to_be_bytes()),
        Value::Float(v) => out.extend_from_slice(&v.to_be_bytes()),
    }
}

fn push_reason(out: &mut Vec<u8>, reason: QualityReason) {
    out.push(match reason {
        QualityReason::Unspecified => REASON_UNSPECIFIED,
        QualityReason::Substituted => REASON_SUBSTITUTED,
        QualityReason::Stale => REASON_STALE,
        QualityReason::OutOfRange => REASON_OUT_OF_RANGE,
        QualityReason::CommunicationFault => REASON_COMMUNICATION_FAULT,
        QualityReason::DeviceFault => REASON_DEVICE_FAULT,
        QualityReason::ConfigurationFault => REASON_CONFIGURATION_FAULT,
    });
}

/// A quality is its severity tag; a non-`Good` severity is followed by
/// its reason tag — `Good` carries no reason, so none is written.
fn push_quality(out: &mut Vec<u8>, quality: Quality) {
    match quality {
        Quality::Good => out.push(QUALITY_GOOD),
        Quality::Uncertain(reason) => {
            out.push(QUALITY_UNCERTAIN);
            push_reason(out, reason);
        }
        Quality::Bad(reason) => {
            out.push(QUALITY_BAD);
            push_reason(out, reason);
        }
    }
}

fn push_sample(out: &mut Vec<u8>, sample: Sample) {
    push_value(out, sample.value);
    out.extend_from_slice(&sample.tick.0.to_be_bytes());
    push_quality(out, sample.quality);
}

/// Frames `body`: the two-byte length prefix plus the payload.
fn frame(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 2);
    out.extend_from_slice(&(body.len() as u16).to_be_bytes());
    out.extend_from_slice(body);
    out
}

/// Serializes `request` into one wire frame.
pub(crate) fn encode_request(request: &BusRequest) -> Vec<u8> {
    let mut body = Vec::with_capacity(16);
    match *request {
        BusRequest::ReadRegister { register } => {
            body.push(OP_READ_REGISTER);
            body.extend_from_slice(&register.to_be_bytes());
        }
        BusRequest::WriteRegister { register, value } => {
            body.push(OP_WRITE_REGISTER);
            body.extend_from_slice(&register.to_be_bytes());
            push_value(&mut body, value);
        }
        BusRequest::ListRegisters => body.push(OP_LIST_REGISTERS),
        BusRequest::Step { dt } => {
            body.push(OP_STEP);
            body.extend_from_slice(&dt.to_be_bytes());
        }
        BusRequest::ClaimWriter { owner } => {
            body.push(OP_CLAIM_WRITER);
            body.extend_from_slice(&owner.to_be_bytes());
        }
        BusRequest::ReleaseWriter => body.push(OP_RELEASE_WRITER),
        BusRequest::InjectQuality { register, quality } => {
            body.push(OP_INJECT_QUALITY);
            body.extend_from_slice(&register.to_be_bytes());
            push_quality(&mut body, quality);
        }
        BusRequest::ClearQuality { register } => {
            body.push(OP_CLEAR_QUALITY);
            body.extend_from_slice(&register.to_be_bytes());
        }
    }
    frame(&body)
}

/// Serializes `response` into one wire frame.
pub(crate) fn encode_response(response: &BusResponse) -> Vec<u8> {
    let mut body = Vec::with_capacity(16);
    match response {
        BusResponse::Sample { sample } => {
            body.push(RESP_SAMPLE);
            push_sample(&mut body, *sample);
        }
        BusResponse::Written { tick } => {
            body.push(RESP_WRITTEN);
            body.extend_from_slice(&tick.0.to_be_bytes());
        }
        BusResponse::Stepped { tick } => {
            body.push(RESP_STEPPED);
            body.extend_from_slice(&tick.0.to_be_bytes());
        }
        BusResponse::Registers { registers } => {
            body.push(RESP_REGISTERS);
            body.extend_from_slice(&(registers.len() as u16).to_be_bytes());
            for info in registers {
                body.extend_from_slice(&info.register.to_be_bytes());
                push_sample(&mut body, info.sample);
            }
        }
        BusResponse::Done => body.push(RESP_DONE),
        BusResponse::Error { error } => {
            body.push(RESP_ERROR);
            match error {
                BusError::UnknownRegister { register } => {
                    body.push(ERR_UNKNOWN_REGISTER);
                    body.extend_from_slice(&register.to_be_bytes());
                }
                BusError::KindMismatch {
                    register,
                    expected,
                    found,
                } => {
                    body.push(ERR_KIND_MISMATCH);
                    body.extend_from_slice(&register.to_be_bytes());
                    push_kind(&mut body, *expected);
                    push_value(&mut body, *found);
                }
                BusError::InvalidRequest { detail } => {
                    body.push(ERR_INVALID_REQUEST);
                    body.extend_from_slice(&(detail.len() as u16).to_be_bytes());
                    body.extend_from_slice(detail.as_bytes());
                }
                BusError::Fenced { detail } => {
                    body.push(ERR_FENCED);
                    body.extend_from_slice(&(detail.len() as u16).to_be_bytes());
                    body.extend_from_slice(detail.as_bytes());
                }
            }
        }
    }
    frame(&body)
}

fn truncated(what: &str) -> String {
    format!("truncated {what}")
}

/// Decodes one request payload — the body a [`read_frame`] returned.
///
/// A payload that does not decode is [`Err`] carrying diagnostics for
/// [`BusError::InvalidRequest`]; the frame itself was already bounded
/// and length-checked by the framing layer.
pub(crate) fn decode_request(body: &[u8]) -> Result<BusRequest, String> {
    let mut reader = Reader::new(body);
    let short = || truncated("request frame");
    let request = match reader.u8().ok_or_else(short)? {
        OP_READ_REGISTER => BusRequest::ReadRegister {
            register: reader.u16().ok_or_else(short)?,
        },
        OP_WRITE_REGISTER => BusRequest::WriteRegister {
            register: reader.u16().ok_or_else(short)?,
            value: reader.value().ok_or_else(short)?,
        },
        OP_LIST_REGISTERS => BusRequest::ListRegisters,
        OP_STEP => BusRequest::Step {
            dt: f64::from_be_bytes(reader.take(8).ok_or_else(short)?.try_into().unwrap()),
        },
        OP_CLAIM_WRITER => BusRequest::ClaimWriter {
            owner: reader.u64().ok_or_else(short)?,
        },
        OP_RELEASE_WRITER => BusRequest::ReleaseWriter,
        OP_INJECT_QUALITY => BusRequest::InjectQuality {
            register: reader.u16().ok_or_else(short)?,
            quality: reader.quality().ok_or_else(short)?,
        },
        OP_CLEAR_QUALITY => BusRequest::ClearQuality {
            register: reader.u16().ok_or_else(short)?,
        },
        tag => return Err(format!("unknown request tag {tag:#04x}")),
    };
    if !reader.done() {
        return Err("trailing bytes after request".to_string());
    }
    Ok(request)
}

/// Decodes one response payload — the client half of
/// [`decode_request`]. A payload that does not decode means the peer is
/// not speaking this protocol; the caller drops the connection.
pub(crate) fn decode_response(body: &[u8]) -> Result<BusResponse, String> {
    let mut reader = Reader::new(body);
    let short = || truncated("response frame");
    let response = match reader.u8().ok_or_else(short)? {
        RESP_SAMPLE => BusResponse::Sample {
            sample: reader.sample().ok_or_else(short)?,
        },
        RESP_WRITTEN => BusResponse::Written {
            tick: Tick(reader.u64().ok_or_else(short)?),
        },
        RESP_REGISTERS => {
            let count = reader.u16().ok_or_else(short)?;
            let mut registers = Vec::with_capacity(count as usize);
            for _ in 0..count {
                registers.push(RegisterInfo {
                    register: reader.u16().ok_or_else(short)?,
                    sample: reader.sample().ok_or_else(short)?,
                });
            }
            BusResponse::Registers { registers }
        }
        RESP_STEPPED => BusResponse::Stepped {
            tick: Tick(reader.u64().ok_or_else(short)?),
        },
        RESP_DONE => BusResponse::Done,
        RESP_ERROR => {
            let error = match reader.u8().ok_or_else(short)? {
                ERR_UNKNOWN_REGISTER => BusError::UnknownRegister {
                    register: reader.u16().ok_or_else(short)?,
                },
                ERR_KIND_MISMATCH => BusError::KindMismatch {
                    register: reader.u16().ok_or_else(short)?,
                    expected: reader.kind().ok_or_else(short)?,
                    found: reader.value().ok_or_else(short)?,
                },
                ERR_INVALID_REQUEST => BusError::InvalidRequest {
                    detail: reader.text().ok_or_else(short)?,
                },
                ERR_FENCED => BusError::Fenced {
                    detail: reader.text().ok_or_else(short)?,
                },
                code => return Err(format!("unknown error code {code:#04x}")),
            };
            BusResponse::Error { error }
        }
        tag => return Err(format!("unknown response tag {tag:#04x}")),
    };
    if !reader.done() {
        return Err("trailing bytes after response".to_string());
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{Quality, QualityReason};

    /// The framed wire form of `message` decodes back to it, and the
    /// frame is exactly length prefix plus payload.
    fn framed_roundtrip<M>(
        message: &M,
        encode: impl Fn(&M) -> Vec<u8>,
        decode: impl Fn(&[u8]) -> Result<M, String>,
    ) where
        M: PartialEq + std::fmt::Debug,
    {
        let wire = encode(message);
        let length = u16::from_be_bytes([wire[0], wire[1]]) as usize;
        assert_eq!(length, wire.len() - 2);
        assert_eq!(decode(&wire[2..]).unwrap(), *message);
    }

    #[test]
    fn request_serde_roundtrip_and_shape() {
        let requests = [
            BusRequest::ReadRegister { register: 4 },
            BusRequest::WriteRegister {
                register: 7,
                value: Value::Float(1.5),
            },
            BusRequest::WriteRegister {
                register: 9,
                value: Value::Bool(true),
            },
            BusRequest::WriteRegister {
                register: 10,
                value: Value::Int(-3),
            },
            BusRequest::ListRegisters,
            BusRequest::Step { dt: 0.1 },
            BusRequest::ClaimWriter { owner: 42 },
            BusRequest::ReleaseWriter,
            BusRequest::InjectQuality {
                register: 4,
                quality: Quality::Bad(QualityReason::DeviceFault),
            },
            BusRequest::InjectQuality {
                register: 7,
                quality: Quality::Uncertain(QualityReason::Stale),
            },
            BusRequest::InjectQuality {
                register: 9,
                quality: Quality::Good,
            },
            BusRequest::ClearQuality { register: 4 },
        ];
        for request in requests {
            let json = serde_json::to_string(&request).unwrap();
            assert_eq!(serde_json::from_str::<BusRequest>(&json).unwrap(), request);
            framed_roundtrip(&request, encode_request, decode_request);
        }
        assert_eq!(
            serde_json::to_string(&BusRequest::ReadRegister { register: 4 }).unwrap(),
            r#"{"op":"read_register","register":4}"#
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::ListRegisters).unwrap(),
            r#"{"op":"list_registers"}"#
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::ClaimWriter { owner: 42 }).unwrap(),
            r#"{"op":"claim_writer","owner":42}"#
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::ReleaseWriter).unwrap(),
            r#"{"op":"release_writer"}"#
        );
        // The wire form of a read is exactly tag plus register, and a
        // step is tag plus the eight-byte dt.
        assert_eq!(
            encode_request(&BusRequest::ReadRegister { register: 4 }),
            vec![0, 3, 0x01, 0, 4]
        );
        assert_eq!(
            encode_request(&BusRequest::Step { dt: 0.5 }),
            [&[0, 9, 0x04][..], &0.5f64.to_be_bytes()[..],].concat()
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::Step { dt: 0.5 }).unwrap(),
            r#"{"op":"step","dt":0.5}"#
        );
        // A claim is tag plus the eight-byte owner token.
        assert_eq!(
            encode_request(&BusRequest::ClaimWriter { owner: 0x0102 }),
            vec![0, 9, 0x05, 0, 0, 0, 0, 0, 0, 1, 2]
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::InjectQuality {
                register: 4,
                quality: Quality::Bad(QualityReason::DeviceFault),
            })
            .unwrap(),
            r#"{"op":"inject_quality","register":4,"quality":{"bad":"device_fault"}}"#
        );
        assert_eq!(
            serde_json::to_string(&BusRequest::ClearQuality { register: 4 }).unwrap(),
            r#"{"op":"clear_quality","register":4}"#
        );
        // An inject is tag, register, then the quality's severity and
        // reason bytes — 0x03 bad, 0x06 device_fault. A `Good` inject
        // carries no reason byte; a clear is tag plus register.
        assert_eq!(
            encode_request(&BusRequest::InjectQuality {
                register: 4,
                quality: Quality::Bad(QualityReason::DeviceFault),
            }),
            vec![0, 5, 0x07, 0, 4, 0x03, 0x06]
        );
        assert_eq!(
            encode_request(&BusRequest::InjectQuality {
                register: 4,
                quality: Quality::Good,
            }),
            vec![0, 4, 0x07, 0, 4, 0x01]
        );
        assert_eq!(
            encode_request(&BusRequest::ClearQuality { register: 4 }),
            vec![0, 3, 0x08, 0, 4]
        );
    }

    #[test]
    fn requests_read_legacy_pascal_case_payloads() {
        // A peer running an earlier build spells Quality and its reason
        // PascalCase; the variant aliases keep those payloads readable.
        let request: BusRequest = serde_json::from_str(
            r#"{"op":"inject_quality","register":4,"quality":{"Bad":"DeviceFault"}}"#,
        )
        .unwrap();
        assert_eq!(
            request,
            BusRequest::InjectQuality {
                register: 4,
                quality: Quality::Bad(QualityReason::DeviceFault),
            }
        );
        let response: BusResponse = serde_json::from_str(
            r#"{"result":"sample","sample":{"value":{"Int":-3},"quality":{"Uncertain":"Substituted"},"tick":9}}"#,
        )
        .unwrap();
        assert_eq!(
            response,
            BusResponse::Sample {
                sample: Sample::new(
                    Value::Int(-3),
                    Quality::Uncertain(QualityReason::Substituted),
                    Tick(9),
                ),
            }
        );
        // The binary frame carries the same quality by severity and
        // reason tags — unchanged by the spelling normalization.
        assert_eq!(encode_request(&request), vec![0, 5, 0x07, 0, 4, 0x03, 0x06]);
    }

    #[test]
    fn response_and_error_serde_roundtrip_and_shape() {
        let responses = [
            BusResponse::Sample {
                sample: Sample::good(Value::Int(-3), Tick(9)),
            },
            BusResponse::Written { tick: Tick(7) },
            BusResponse::Stepped { tick: Tick(8) },
            BusResponse::Registers {
                registers: vec![
                    RegisterInfo {
                        register: 0,
                        sample: Sample::good(Value::Float(1.5), Tick(3)),
                    },
                    RegisterInfo {
                        register: 5,
                        sample: Sample::good(Value::Bool(true), Tick(6)),
                    },
                ],
            },
            BusResponse::Error {
                error: BusError::UnknownRegister { register: 4 },
            },
            BusResponse::Error {
                error: BusError::KindMismatch {
                    register: 5,
                    expected: ValueKind::Float,
                    found: Value::Bool(true),
                },
            },
            BusResponse::Done,
            BusResponse::Error {
                error: BusError::InvalidRequest {
                    detail: "bad tag".to_string(),
                },
            },
            BusResponse::Error {
                error: BusError::Fenced {
                    detail: "another attachment owns register writes".to_string(),
                },
            },
        ];
        for response in responses {
            let json = serde_json::to_string(&response).unwrap();
            assert_eq!(
                serde_json::from_str::<BusResponse>(&json).unwrap(),
                response
            );
            framed_roundtrip(&response, encode_response, decode_response);
        }
        assert_eq!(
            serde_json::to_string(&BusResponse::Stepped { tick: Tick(8) }).unwrap(),
            r#"{"result":"stepped","tick":8}"#
        );
        assert_eq!(
            serde_json::to_string(&BusResponse::Done).unwrap(),
            r#"{"result":"done"}"#
        );
        assert_eq!(
            serde_json::to_string(&BusResponse::Error {
                error: BusError::Fenced {
                    detail: "another attachment owns register writes".to_string(),
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"fenced","detail":"another attachment owns register writes"}}"#
        );
        assert_eq!(
            serde_json::to_string(&BusResponse::Error {
                error: BusError::UnknownRegister { register: 4 },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"unknown_register","register":4}}"#
        );
        // The serde contract and the wire form both carry a full
        // `Sample`, quality included: an injected quality survives the
        // framing round trip.
        let degraded = BusResponse::Sample {
            sample: Sample::new(
                Value::Int(-3),
                Quality::Uncertain(QualityReason::Substituted),
                Tick(9),
            ),
        };
        let json = serde_json::to_string(&degraded).unwrap();
        assert_eq!(
            serde_json::from_str::<BusResponse>(&json).unwrap(),
            degraded
        );
        framed_roundtrip(&degraded, encode_response, decode_response);
        // The wire sample is value, tick, then severity and reason —
        // int kind 0x02, uncertain 0x02, substituted 0x02.
        assert_eq!(
            encode_response(&degraded),
            vec![
                0, 20, 0x01, 0x02, 255, 255, 255, 255, 255, 255, 255, 253, 0, 0, 0, 0, 0, 0, 0, 9,
                0x02, 0x02,
            ]
        );
    }

    #[test]
    fn malformed_payloads_are_named_failures_not_panics() {
        // Truncations, unknown tags, bad kind bytes, and trailing bytes
        // all decode to Err — the server answers invalid_request and the
        // client drops the link; neither panics.
        for body in [
            &[][..],
            &[0x01][..],                            // read register, missing address
            &[0x02, 0, 1][..],                      // write, missing value
            &[0x02, 0, 1, 0x09][..],                // write, unknown kind tag
            &[0x03, 0][..],                         // trailing byte after list
            &[0x04][..],                            // step, missing dt
            &[0x04, 0, 0][..],                      // step, truncated dt
            &[0x04, 0, 0, 0, 0, 0, 0, 0, 0, 0][..], // step, trailing byte after dt
            &[0xff][..],                            // unknown request tag
            &[0x01, 0][..],                         // read, truncated address
            &[0x05, 0, 0][..],                      // claim, truncated owner
            &[0x06, 0][..],                         // trailing byte after release
            &[0x07, 0][..],                         // inject, truncated register
            &[0x07, 0, 4][..],                      // inject, missing quality
            &[0x07, 0, 4, 0x09][..],                // inject, unknown severity
            &[0x07, 0, 4, 0x02][..],                // inject, missing reason
            &[0x07, 0, 4, 0x02, 0x09][..],          // inject, unknown reason
            &[0x08][..],                            // clear, missing register
            &[0x08, 0, 4, 0][..],                   // trailing byte after clear
        ] {
            assert!(decode_request(body).is_err(), "{body:02x?}");
        }
        for body in [
            &[][..],
            &[0x01, 0x03][..],    // sample, truncated value
            &[0x05, 0x09][..],    // unknown error code
            &[0x04, 0][..],       // stepped, truncated tick
            &[0x05, 0x04, 0][..], // fenced error, truncated detail
            &[0x06, 0][..],       // trailing byte after done
            // Sample carrying value and tick but no quality.
            &[0x01, 0x03, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0][..],
            // Sample with an unknown quality severity.
            &[
                0x01, 0x03, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x09,
            ][..],
        ] {
            assert!(decode_response(body).is_err(), "{body:02x?}");
        }
    }
}
