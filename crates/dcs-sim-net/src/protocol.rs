//! The wire protocol [`PlantServer`](crate::PlantServer) serves and
//! [`RemoteDriver`](crate::RemoteDriver) speaks.
//!
//! The framing is newline-delimited JSON: a client writes one
//! [`PlantRequest`] object followed by `'\n'` and reads back exactly one
//! [`PlantResponse`] line. JSON's string escaping means a serialized
//! message can never contain a raw newline, so the delimiter is
//! unambiguous. Messages are capped at [`MAX_MESSAGE`] bytes including the
//! delimiter — the plant's payloads are small and fixed-shape, so an
//! oversized line is a misbehaving peer, not a large request.
//!
//! Requests are answered in arrival order on each connection, one at a
//! time: a client has at most one request in flight, which is what makes
//! the synchronous exchange and its failure semantics well-defined.

use dcs_core::{IoError, PointId, Sample, Tick, Value};
use dcs_sim::Fault;
use serde::{Deserialize, Serialize};
use std::io::{self, BufRead, BufReader};
use std::net::TcpStream;

/// The maximum size of one protocol message in bytes, delimiter included.
///
/// A line longer than this is a protocol violation: the server drops a
/// connection that sends one, and a client treats an oversized response
/// the same way it treats a dead peer.
pub const MAX_MESSAGE: usize = 64 * 1024;

/// One request a client sends to the plant server.
///
/// The variant set mirrors the [`SimDriver`](dcs_sim::SimDriver) API the
/// server wraps: the two [`IoDriver`](dcs_core::IoDriver) accesses plus
/// the explicit simulation controls — stepping and fault injection — that
/// keep the shared plant deterministic and testable. The `op` tag is the
/// wire discriminator: `{"op":"write","point":2,"value":{"Float":1.5}}`.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
pub enum PlantRequest {
    /// Reads the point's latest sample — `IoDriver::read`.
    Read {
        /// The point to read.
        point: PointId,
    },
    /// Writes `value` to `point` — `IoDriver::write`, including its
    /// kind check.
    Write {
        /// The point to write.
        point: PointId,
        /// The value to store; its variant must match the point's
        /// declared kind.
        value: Value,
    },
    /// Advances the shared plant one tick of `dt` time units —
    /// `SimDriver::step`. Stepping is an explicit protocol operation so a
    /// controller scan can advance the shared plant deterministically and
    /// a second client observes the same stepped values. `dt` must be
    /// finite and non-negative; the server refuses otherwise rather than
    /// panic.
    Step {
        /// The simulated time this step advances by.
        dt: f64,
    },
    /// Injects `fault` on `point` — `SimDriver::inject_fault`.
    InjectFault {
        /// The point to fault.
        point: PointId,
        /// The fault to activate, replacing any fault already set.
        fault: Fault,
    },
    /// Removes any fault injected on `point` — `SimDriver::clear_fault`.
    ClearFault {
        /// The point to clear.
        point: PointId,
    },
}

/// The server's answer to one [`PlantRequest`].
///
/// The `result` tag is the wire discriminator:
/// `{"result":"sample","sample":{...}}`, `{"result":"stepped","tick":7}`,
/// `{"result":"done"}`, or `{"result":"error","error":{...}}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "result", rename_all = "snake_case")]
pub enum PlantResponse {
    /// Answer to [`PlantRequest::Read`]: the point's latest sample.
    Sample {
        /// The stored sample — value, quality, and the plant tick that
        /// produced it.
        sample: Sample,
    },
    /// Answer to [`PlantRequest::Step`]: the plant's new tick.
    Stepped {
        /// The tick the step advanced to.
        tick: Tick,
    },
    /// Answer to [`PlantRequest::Write`], [`PlantRequest::InjectFault`],
    /// and [`PlantRequest::ClearFault`]: the request applied.
    Done,
    /// The request failed; `error` says why.
    Error {
        /// The failure the server reported.
        error: PlantError,
    },
}

/// A failure the plant server reports in a [`PlantResponse::Error`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum PlantError {
    /// A point-level failure: the [`IoError`] the server's
    /// [`SimDriver`](dcs_sim::SimDriver) returned, carried verbatim so the
    /// client surfaces the identical `UnknownPoint`, `TypeMismatch`,
    /// `Disconnected`, or `Timeout` a local driver would have produced —
    /// including faults injected through the protocol.
    Io {
        /// The driver's error.
        error: IoError,
    },
    /// The request itself could not be served: a line that does not parse
    /// as a [`PlantRequest`], or a [`PlantRequest::Step`] whose `dt` is
    /// negative or non-finite. `detail` is human-readable diagnostics,
    /// not a machine contract.
    InvalidRequest {
        /// Why the request was refused.
        detail: String,
    },
}

/// Reads one `'\n'`-terminated message of at most `max` bytes, delimiter
/// included, from a client or server connection.
///
/// `Ok(None)` means the peer closed the connection — including closing it
/// mid-message, which loses the partial line; the framing is broken either
/// way, so the caller sees the same "peer gone" signal. An `Err` of kind
/// [`io::ErrorKind::InvalidData`] means the line exceeded `max` — a
/// protocol violation the caller answers by dropping the connection. Other
/// errors are ordinary I/O failures, `WouldBlock`/`TimedOut` included when
/// the stream carries a read timeout.
pub(crate) fn read_message(
    reader: &mut BufReader<TcpStream>,
    max: usize,
) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::with_capacity(128);
    loop {
        let chunk = reader.fill_buf()?;
        if chunk.is_empty() {
            return Ok(None);
        }
        let take = chunk
            .iter()
            .position(|byte| *byte == b'\n')
            .map_or(chunk.len(), |pos| pos + 1);
        line.extend_from_slice(&chunk[..take]);
        reader.consume(take);
        if line.len() > max {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "protocol message exceeds the size bound",
            ));
        }
        if line.last() == Some(&b'\n') {
            return Ok(Some(line));
        }
    }
}

/// Serializes `message` into one wire line (payload plus the `'\n'`
/// delimiter).
///
/// serde_json escapes control characters inside strings, so the emitted
/// line always ends with exactly one raw newline — the delimiter can never
/// be confused with payload content.
pub(crate) fn encode_message(message: &impl Serialize) -> Vec<u8> {
    let mut payload =
        serde_json::to_vec(message).expect("protocol contract types always serialize");
    payload.push(b'\n');
    payload
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{Quality, QualityReason, ValueKind};

    #[test]
    fn request_serde_roundtrip_and_shape() {
        let requests = [
            PlantRequest::Read { point: PointId(1) },
            PlantRequest::Write {
                point: PointId(2),
                value: Value::Float(1.5),
            },
            PlantRequest::Write {
                point: PointId(3),
                value: Value::Bool(true),
            },
            PlantRequest::Step { dt: 0.1 },
            PlantRequest::InjectFault {
                point: PointId(4),
                fault: Fault::Quality(Quality::Bad(QualityReason::Stale)),
            },
            PlantRequest::InjectFault {
                point: PointId(5),
                fault: Fault::Timeout,
            },
            PlantRequest::ClearFault { point: PointId(4) },
        ];
        for request in requests {
            let json = serde_json::to_string(&request).unwrap();
            assert_eq!(
                serde_json::from_str::<PlantRequest>(&json).unwrap(),
                request
            );
        }
        assert_eq!(
            serde_json::to_string(&PlantRequest::Read { point: PointId(1) }).unwrap(),
            r#"{"op":"read","point":1}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::Step { dt: 0.1 }).unwrap(),
            r#"{"op":"step","dt":0.1}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::InjectFault {
                point: PointId(4),
                fault: Fault::Disconnected,
            })
            .unwrap(),
            r#"{"op":"inject_fault","point":4,"fault":"disconnected"}"#
        );
    }

    #[test]
    fn response_and_error_serde_roundtrip_and_shape() {
        let responses = [
            PlantResponse::Sample {
                sample: Sample::new(
                    Value::Int(-3),
                    Quality::Uncertain(QualityReason::Substituted),
                    Tick(9),
                ),
            },
            PlantResponse::Stepped { tick: Tick(7) },
            PlantResponse::Done,
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::UnknownPoint(PointId(4)),
                },
            },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::TypeMismatch {
                        point: PointId(5),
                        expected: ValueKind::Float,
                        found: Value::Bool(true),
                    },
                },
            },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::Timeout(PointId(6)),
                },
            },
            PlantResponse::Error {
                error: PlantError::InvalidRequest {
                    detail: "bad dt".to_string(),
                },
            },
        ];
        for response in responses {
            let json = serde_json::to_string(&response).unwrap();
            assert_eq!(
                serde_json::from_str::<PlantResponse>(&json).unwrap(),
                response
            );
        }
        assert_eq!(
            serde_json::to_string(&PlantResponse::Done).unwrap(),
            r#"{"result":"done"}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantResponse::Stepped { tick: Tick(7) }).unwrap(),
            r#"{"result":"stepped","tick":7}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::UnknownPoint(PointId(4)),
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"io","error":{"UnknownPoint":4}}}"#
        );
    }
}
