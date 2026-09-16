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
use dcs_sim::{Fault, PointInfo};
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
/// wire discriminator: `{"op":"write","point":2,"value":{"float":1.5}}`.
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
    /// Lists every point the plant serves — `SimDriver::points`. This is
    /// the census development tooling (`dcs-plant-ctl`) needs to show the
    /// shared plant's surface without a copy of its channel map.
    ListPoints,
    /// Takes the plant's field-write ownership for `owner` — the
    /// single-writer claim the failover decision fences a superseded
    /// active out with.
    ///
    /// `owner` is an opaque token the caller picks, unique per field
    /// owner — one controller's several attachments claim the same
    /// token so all of them write, while a takeover claims a fresh one.
    /// The grant is unconditional: the claim preempts whichever owner
    /// held it, and it stands until another claim preempts it — never
    /// released, so a dead owner's silence keeps the field fenced for
    /// the claimed owner rather than reopening it. Once any owner is
    /// claimed, `write` and `step` requests from a connection that has
    /// not itself claimed the current owner are refused; reads, fault
    /// injection, and `list_points` stay open to every attachment.
    ClaimWriter {
        /// The ownership token the claim asserts.
        owner: u64,
    },
    /// The re-attach half of the write-ownership claim: takes the claim
    /// for `owner` only while the field is unclaimed or the standing
    /// claim already names `owner` — the conditional grant a
    /// reconnecting field owner asserts to re-arm the claim a server
    /// restart dropped. Unlike [`ClaimWriter`](Self::ClaimWriter) it
    /// never preempts: while a *different* owner holds the claim the
    /// request is refused [`PlantError::Fenced`], so a re-attaching
    /// superseded peer cannot steal the field back from the attachment
    /// that claimed it during the outage. A granted request also binds
    /// `owner` to this connection exactly as `claim_writer` does.
    EnsureWriter {
        /// The ownership token the claim asserts.
        owner: u64,
    },
}

/// The server's answer to one [`PlantRequest`].
///
/// The `result` tag is the wire discriminator:
/// `{"result":"sample","sample":{...}}`, `{"result":"stepped","tick":7}`,
/// `{"result":"points","points":[...]}`, `{"result":"done"}`, or
/// `{"result":"error","error":{...}}`.
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
    /// Answer to [`PlantRequest::ListPoints`]: every bound point's
    /// description — direction, the sample readers observe, and the
    /// active fault — ordered by [`PointId`].
    Points {
        /// The plant's points.
        points: Vec<PointInfo>,
    },
    /// Answer to [`PlantRequest::Write`], [`PlantRequest::InjectFault`],
    /// [`PlantRequest::ClearFault`], and [`PlantRequest::ClaimWriter`]:
    /// the request applied.
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
    /// The request mutates the shared field but the connection does not
    /// hold the field's write-ownership claim — the fencing verdict of
    /// the failover decision. A [`PlantRequest::Write`] instead answers
    /// the point's [`IoError::Fenced`], so a driver's write path surfaces
    /// the same named failure a local fenced driver would produce.
    Fenced {
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
            PlantRequest::ListPoints,
            PlantRequest::ClaimWriter { owner: 42 },
            PlantRequest::EnsureWriter { owner: 43 },
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
        assert_eq!(
            serde_json::to_string(&PlantRequest::ListPoints).unwrap(),
            r#"{"op":"list_points"}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::ClaimWriter { owner: 42 }).unwrap(),
            r#"{"op":"claim_writer","owner":42}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::EnsureWriter { owner: 43 }).unwrap(),
            r#"{"op":"ensure_writer","owner":43}"#
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
            PlantResponse::Points {
                points: vec![PointInfo {
                    point: PointId(10),
                    direction: dcs_core::Direction::In,
                    sample: Sample::good(Value::Float(1.5), Tick(3)),
                    fault: Some(Fault::Timeout),
                }],
            },
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
            PlantResponse::Error {
                error: PlantError::Fenced {
                    detail: "another attachment owns field writes".to_string(),
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
        // The point-census payload carries the shared `Direction` as
        // "in"/"out" — the shape the plant protocol has always emitted.
        assert_eq!(
            serde_json::to_string(&PlantResponse::Points {
                points: vec![PointInfo {
                    point: PointId(10),
                    direction: dcs_core::Direction::In,
                    sample: Sample::good(Value::Float(1.5), Tick(3)),
                    fault: None,
                }],
            })
            .unwrap(),
            r#"{"result":"points","points":[{"point":10,"direction":"in","sample":{"value":{"float":1.5},"quality":"good","tick":3},"fault":null}]}"#
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
            r#"{"result":"error","error":{"kind":"io","error":{"unknown_point":4}}}"#
        );
    }

    #[test]
    fn protocol_reads_legacy_pascal_case_payloads() {
        // A peer running an earlier build spells Value, Quality, and
        // IoError PascalCase; the variant aliases keep those payloads
        // readable on this side.
        let request: PlantRequest =
            serde_json::from_str(r#"{"op":"write","point":2,"value":{"Float":1.5}}"#).unwrap();
        assert_eq!(
            request,
            PlantRequest::Write {
                point: PointId(2),
                value: Value::Float(1.5),
            }
        );

        let response: PlantResponse = serde_json::from_str(
            r#"{"result":"sample","sample":{"value":{"Int":-3},"quality":{"Uncertain":"Substituted"},"tick":9}}"#,
        )
        .unwrap();
        assert_eq!(
            response,
            PlantResponse::Sample {
                sample: Sample::new(
                    Value::Int(-3),
                    Quality::Uncertain(QualityReason::Substituted),
                    Tick(9),
                ),
            }
        );

        let response: PlantResponse = serde_json::from_str(
            r#"{"result":"error","error":{"kind":"io","error":{"TypeMismatch":{"point":5,"expected":"Float","found":{"Bool":true}}}}}"#,
        )
        .unwrap();
        assert_eq!(
            response,
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::TypeMismatch {
                        point: PointId(5),
                        expected: ValueKind::Float,
                        found: Value::Bool(true),
                    },
                },
            }
        );
    }
}
