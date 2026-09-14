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

use dcs_core::{Sample, Tick, Value, ValueKind};
use serde::{Deserialize, Serialize};
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

// Response variant tags.
const RESP_SAMPLE: u8 = 0x01;
const RESP_WRITTEN: u8 = 0x02;
const RESP_REGISTERS: u8 = 0x03;
const RESP_STEPPED: u8 = 0x04;
const RESP_ERROR: u8 = 0x05;

// Error codes on the wire.
const ERR_UNKNOWN_REGISTER: u8 = 0x01;
const ERR_KIND_MISMATCH: u8 = 0x02;
const ERR_INVALID_REQUEST: u8 = 0x03;

// Value kind tags on the wire.
const KIND_BOOL: u8 = 0x01;
const KIND_INT: u8 = 0x02;
const KIND_FLOAT: u8 = 0x03;

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
    /// simulation step. The bank holds no time-dependent dynamics, so
    /// the request carries no `dt`.
    Step,
}

/// The server's answer to one [`BusRequest`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum BusResponse {
    /// Answer to [`BusRequest::ReadRegister`]: the register's stored
    /// sample — its value, [`Quality::Good`](dcs_core::Quality::Good),
    /// and the device tick that stamped it.
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
}

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

fn push_sample(out: &mut Vec<u8>, sample: Sample) {
    push_value(out, sample.value);
    out.extend_from_slice(&sample.tick.0.to_be_bytes());
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
        BusRequest::Step => body.push(OP_STEP),
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
    let request = match reader.u8().ok_or_else(|| short())? {
        OP_READ_REGISTER => BusRequest::ReadRegister {
            register: reader.u16().ok_or_else(|| short())?,
        },
        OP_WRITE_REGISTER => BusRequest::WriteRegister {
            register: reader.u16().ok_or_else(|| short())?,
            value: reader.value().ok_or_else(|| short())?,
        },
        OP_LIST_REGISTERS => BusRequest::ListRegisters,
        OP_STEP => BusRequest::Step,
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
    let response = match reader.u8().ok_or_else(|| short())? {
        RESP_SAMPLE => BusResponse::Sample {
            sample: Sample::good(
                reader.value().ok_or_else(|| short())?,
                Tick(reader.u64().ok_or_else(|| short())?),
            ),
        },
        RESP_WRITTEN => BusResponse::Written {
            tick: Tick(reader.u64().ok_or_else(|| short())?),
        },
        RESP_REGISTERS => {
            let count = reader.u16().ok_or_else(|| short())?;
            let mut registers = Vec::with_capacity(count as usize);
            for _ in 0..count {
                registers.push(RegisterInfo {
                    register: reader.u16().ok_or_else(|| short())?,
                    sample: Sample::good(
                        reader.value().ok_or_else(|| short())?,
                        Tick(reader.u64().ok_or_else(|| short())?),
                    ),
                });
            }
            BusResponse::Registers { registers }
        }
        RESP_STEPPED => BusResponse::Stepped {
            tick: Tick(reader.u64().ok_or_else(|| short())?),
        },
        RESP_ERROR => {
            let error = match reader.u8().ok_or_else(|| short())? {
                ERR_UNKNOWN_REGISTER => BusError::UnknownRegister {
                    register: reader.u16().ok_or_else(|| short())?,
                },
                ERR_KIND_MISMATCH => BusError::KindMismatch {
                    register: reader.u16().ok_or_else(|| short())?,
                    expected: reader.kind().ok_or_else(|| short())?,
                    found: reader.value().ok_or_else(|| short())?,
                },
                ERR_INVALID_REQUEST => BusError::InvalidRequest {
                    detail: reader.text().ok_or_else(|| short())?,
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
            BusRequest::Step,
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
        // The wire form of a read is exactly tag plus register.
        assert_eq!(
            encode_request(&BusRequest::ReadRegister { register: 4 }),
            vec![0, 3, 0x01, 0, 4]
        );
    }

    #[test]
    fn response_and_error_serde_roundtrip_and_shape() {
        let responses = [
            BusResponse::Sample {
                sample: Sample::new(
                    Value::Int(-3),
                    Quality::Uncertain(QualityReason::Substituted),
                    Tick(9),
                ),
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
            BusResponse::Error {
                error: BusError::InvalidRequest {
                    detail: "bad tag".to_string(),
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
            serde_json::to_string(&BusResponse::Error {
                error: BusError::UnknownRegister { register: 4 },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"unknown_register","register":4}}"#
        );
    }

    #[test]
    fn malformed_payloads_are_named_failures_not_panics() {
        // Truncations, unknown tags, bad kind bytes, and trailing bytes
        // all decode to Err — the server answers invalid_request and the
        // client drops the link; neither panics.
        for body in [
            &[][..],
            &[0x01][..],                 // read register, missing address
            &[0x02, 0, 1][..],           // write, missing value
            &[0x02, 0, 1, 0x09][..],     // write, unknown kind tag
            &[0x03, 0][..],              // trailing byte after list
            &[0xff][..],                 // unknown request tag
            &[0x01, 0][..],              // read, truncated address
        ] {
            assert!(decode_request(body).is_err(), "{body:02x?}");
        }
        for body in [
            &[][..],
            &[0x01, 0x03][..],           // sample, truncated value
            &[0x05, 0x09][..],           // unknown error code
            &[0x04, 0][..],              // stepped, truncated tick
        ] {
            assert!(decode_response(body).is_err(), "{body:02x?}");
        }
    }
}
