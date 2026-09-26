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
use std::net::{SocketAddr, TcpStream};

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
    /// held it, and it stands until another claim preempts it or every
    /// holder releases it — never released on its own, so a dead
    /// owner's silence keeps the field fenced for the claimed owner
    /// rather than reopening it. The field itself fails closed:
    /// `write` and `step` requests from a connection that does not
    /// hold the current claim are refused — [`PlantError::Unclaimed`]
    /// while no claim stands (a fresh or restarted server included),
    /// [`PlantError::Fenced`] — `IoError::Fenced` for a `write` — once
    /// another owner does. Reads, fault injection, and `list_points`
    /// stay open to every attachment.
    ///
    /// A grant for the standing owner while another *live* attachment
    /// already holds the token is answered
    /// [`PlantResponse::ClaimedShared`] rather than `Done`: the grant
    /// stands — the token cannot tell one owner's second attachment
    /// from a second process reusing it — but the sharing is flagged,
    /// because two field-owning processes pinned to one token defeat
    /// the arbitration this claim exists to provide.
    ///
    /// `controller` records whether the claiming attachment belongs to
    /// a controller peer rather than a field tool: only controller
    /// claims are the live incumbents a peer's conditional
    /// [`ClaimWriterUnlessHeld`](Self::ClaimWriterUnlessHeld) refuses
    /// to preempt — the stale-island rule — while a tool's claim is
    /// always preemptable by it, so a rogue or merely lingering tool
    /// hold can never wedge a peer's documented promote recovery.
    /// Payloads from builds predating the flag carry none and read as
    /// `true` — an unmarked claim is treated as a controller's, the
    /// conservative verdict: a mislabeled tool claim only ever refuses
    /// a takeover a deliberate unconditional claim still runs, where a
    /// mislabeled controller claim would reopen the stale-island
    /// preemption the conditional grant exists to refuse.
    ClaimWriter {
        /// The ownership token the claim asserts.
        owner: u64,
        /// Whether the claiming attachment belongs to a controller.
        #[serde(default = "default_controller_claim")]
        controller: bool,
        /// The claimant's monitor endpoint, declared so a peer the
        /// claim preempts can find the successor's tracking surface:
        /// the fencing verdicts this claim produces carry it back, and
        /// the demoted peer's tracking path can then resolve the
        /// field-arbitrated owner where no announced hint could ever
        /// prove itself. `None` — the default on requests predating
        /// the field, and every non-controller claim — leaves the
        /// verdicts naming no monitor.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        monitor: Option<SocketAddr>,
    },
    /// The launched-controller half of the write-ownership claim: takes
    /// the claim for `owner` only while no *live* attachment holds a
    /// different owner's claim — the grant a controller's startup
    /// activation asserts, and the conditional claim an orphaned peer's
    /// promotion runs so an islanded run can never seize the field from
    /// a live incumbent. A claim left standing by a dead owner — its
    /// holder set empty — is still preempted, so the restart-as-active
    /// recovery of a crashed owner keeps working, and so is a field
    /// tool's claim — a tool is not an incumbent a peer must defer to.
    /// A live *controller's* different-owner unyielded claim is refused
    /// [`PlantError::Fenced`]: a controller restarted onto stale state,
    /// or an orphaned peer tracking a diverged island, cannot prove its
    /// image is current with the incumbent's, so it cannot seize the
    /// field and silently roll back commands it receipted and applied.
    /// A granted request binds `owner` to this connection exactly as
    /// `claim_writer` does — including the
    /// [`PlantResponse::ClaimedShared`] flag when the token is already
    /// held by another live attachment — and the claim it lands is
    /// always recorded as a controller's.
    ClaimWriterUnlessHeld {
        /// The ownership token the claim asserts.
        owner: u64,
        /// The claimant's monitor endpoint — the same
        /// [`ClaimWriter`](Self::ClaimWriter) declaration: a granted
        /// conditional claim is still the field's owner, so the
        /// verdicts it later produces name this monitor to the peers
        /// it supersedes.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        monitor: Option<SocketAddr>,
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
    /// `owner` to this connection exactly as `claim_writer` does —
    /// including the [`PlantResponse::ClaimedShared`] flag when the
    /// token is already held by another live attachment.
    ///
    /// `rebind: false` is the orphan cycle's probe shape: the claim is
    /// raised or confirmed *for* the token without this attachment
    /// joining its holders, so a demoted ex-owner can keep its released
    /// claim fencing the field — and a conditional
    /// [`ClaimWriterUnlessHeld`](Self::ClaimWriterUnlessHeld) can still
    /// tell the claim is ownerless — without ever becoming a live
    /// holder a different owner's conditional claim would read as a
    /// live incumbent. Requests from builds predating the flag carry
    /// none and bind as they always did.
    ///
    /// `controller` carries the same marker [`ClaimWriter`]'s does: a
    /// claim this grant raises for a controller's token is recorded as
    /// a controller claim, so a peer's conditional takeover still
    /// refuses to preempt it while the owner stays attached. Payloads
    /// predating the flag read `true`, as `claim_writer`'s does.
    EnsureWriter {
        /// The ownership token the claim asserts.
        owner: u64,
        /// Whether a grant binds this connection to the claim.
        #[serde(default = "default_rebind")]
        rebind: bool,
        /// Whether the claiming attachment belongs to a controller.
        #[serde(default = "default_controller_claim")]
        controller: bool,
        /// The claimant's monitor endpoint — the same
        /// [`ClaimWriter`](Self::ClaimWriter) declaration: a re-armed
        /// or probe-raised claim carries its owner's tracking surface
        /// forward, so a claim rebuilt across a server restart keeps
        /// naming where the owner serves.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        monitor: Option<SocketAddr>,
    },
    /// Drop this connection's hold on the write claim. `keep_claim:
    /// false` — the deliberate hand-back a mutation tool performs —
    /// releases the claim itself when the release empties the holder
    /// set: the field returns to `unclaimed`, still closed to mutation.
    /// `keep_claim: true` — the demotion half of the contract — leaves
    /// the claim standing with this connection's hold removed and marks
    /// it yielded: the token keeps fencing the field for the ex-owner's
    /// conditional re-arm, and a conditional
    /// [`ClaimWriterUnlessHeld`](Self::ClaimWriterUnlessHeld) still
    /// preempts it despite other attachments — a mutation tool's —
    /// holding the yielded token live, while a *live incumbent's*
    /// unyielded claim keeps refusing it. A release from an attachment
    /// holding nothing changes nothing either way: an empty holder set
    /// is the dead-owner state the claim exists to fence, not a
    /// hand-back. Requests from builds predating the flag carry none
    /// and release fully, as they always did.
    ReleaseWriter {
        /// Leave the claim standing, marked yielded, instead of
        /// releasing it when the holder set empties.
        #[serde(default)]
        keep_claim: bool,
    },
    /// The read-only half of the writer claim — the claim-state
    /// observation a tracking peer reports through its role surface.
    /// The answer is the verdict a mutation from this connection would
    /// meet, without any mutation: [`PlantResponse::Done`] while this
    /// connection holds the claim, [`PlantError::Fenced`] while another
    /// owner does, [`PlantError::Unclaimed`] while no claim stands.
    /// The probe asserts, joins, and releases nothing — an observation
    /// cannot seize the field it reports, so reporting `unclaimed`
    /// leaves the claim exactly as closed as it found it.
    ProbeWriter,
}

/// The serde default for [`PlantRequest::EnsureWriter`]'s `rebind`:
/// requests from builds predating the flag bind the granted token to
/// the connection exactly as `ensure_writer` always did.
fn default_rebind() -> bool {
    true
}

/// The serde default for the `controller` flag on
/// [`PlantRequest::ClaimWriter`] and [`PlantRequest::EnsureWriter`]:
/// requests from builds predating the flag read as controller claims —
/// the conservative verdict, since a claim mislabeled as a tool's could
/// be preempted by a peer's conditional grant while its live owner is
/// exactly the incumbent that grant exists to protect.
fn default_controller_claim() -> bool {
    true
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
    /// [`PlantRequest::ClearFault`], [`PlantRequest::ReleaseWriter`],
    /// and the claim requests: the request applied.
    Done,
    /// Answer to a granted [`PlantRequest::ClaimWriter`] or
    /// [`PlantRequest::EnsureWriter`] whose `owner` token another live
    /// attachment already holds. The grant stands — one field owner's
    /// several attachments claim the same token by design — but the
    /// sharing is flagged because the token alone cannot distinguish
    /// that from a second field-owning *process* reusing it, which
    /// would defeat the single-writer arbitration a promotion relies
    /// on. `Done` remains the answer when no other live attachment
    /// holds the token.
    ClaimedShared {
        /// The token now held by more than one live attachment.
        owner: u64,
    },
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
    /// `InvalidValue`, `Disconnected`, or `Timeout` a local driver would
    /// have produced — including faults injected through the protocol.
    Io {
        /// The driver's error.
        error: IoError,
        /// When `error` is the fencing verdict — the write-ownership
        /// claim refused this attachment's mutation — the standing
        /// claim's owner token: who the field serves instead. `None`
        /// on every other point error and absent on the wire from
        /// servers predating the field, where the fence's claimant is
        /// recorded only as "another".
        #[serde(default, skip_serializing_if = "Option::is_none")]
        owner: Option<u64>,
        /// The monitor endpoint the standing claim's owner declared —
        /// the tracking surface the superseded peer can re-join on:
        /// the field's arbitration names the successor's address where
        /// no announced hint could ever prove one. `None` when the
        /// claim declared none or the server predates the field.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        monitor: Option<SocketAddr>,
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
        /// The standing claim's owner token — who the field's
        /// arbitration serves instead of this attachment. Carried so a
        /// fenced-out field owner's durable audit can name the
        /// preempting claimant, not just "another". Absent on the wire
        /// from servers predating the field.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        owner: Option<u64>,
        /// The monitor endpoint the standing claim's owner declared —
        /// the field arbitration's word for where the successor
        /// serves checkpoints. A demoted peer's tracking path can
        /// prove this address where no announced `?peer=` hint ever
        /// proves itself: only actually holding the claim puts a
        /// monitor under it. `None` when the claim declared none or
        /// the server predates the field.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        monitor: Option<SocketAddr>,
    },
    /// The request mutates the shared field but no write-ownership
    /// claim stands at all — the server is fresh or restarted, or the
    /// last holder released. The field fails closed rather than
    /// opening a window any attachment could write through or an
    /// interposer could claim ahead of the legitimate owner's re-arm.
    /// Named separately from [`PlantError::Fenced`] so a probe can tell
    /// "the field is closed until an owner claims" from "another owner
    /// stands": the first waits for a re-arm, the second means the
    /// probe's writer is fenced out. A [`PlantRequest::Write`] maps
    /// this refusal to [`IoError::Fenced`] at the client — the point
    /// level cannot express "unclaimed" — while a [`PlantRequest::Step`]
    /// carries the named error.
    Unclaimed {
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
/// the stream carries a read timeout. An interrupted wait
/// ([`io::ErrorKind::Interrupted`]) is retried, not reported: a caught
/// signal is not link trouble — the process can field `SIGCHLD` from
/// spawned helpers while a request is in flight, and that must not drop
/// the connection.
pub(crate) fn read_message(
    reader: &mut BufReader<TcpStream>,
    max: usize,
) -> io::Result<Option<Vec<u8>>> {
    let mut line = Vec::with_capacity(128);
    loop {
        let chunk = match reader.fill_buf() {
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            other => other?,
        };
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
            PlantRequest::ClaimWriter {
                owner: 42,
                controller: false,
                monitor: Some("127.0.0.1:4190".parse().unwrap()),
            },
            PlantRequest::ClaimWriterUnlessHeld {
                owner: 44,
                monitor: None,
            },
            PlantRequest::EnsureWriter {
                owner: 43,
                rebind: true,
                controller: true,
                monitor: None,
            },
            PlantRequest::EnsureWriter {
                owner: 45,
                rebind: false,
                controller: true,
                monitor: None,
            },
            PlantRequest::ReleaseWriter { keep_claim: false },
            PlantRequest::ReleaseWriter { keep_claim: true },
            PlantRequest::ProbeWriter,
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
            serde_json::to_string(&PlantRequest::ClaimWriter {
                owner: 42,
                controller: false,
                monitor: None,
            })
            .unwrap(),
            r#"{"op":"claim_writer","owner":42,"controller":false}"#
        );
        // A claim declaring its monitor carries it on the wire: the
        // successors this claim's verdicts name find its tracking
        // surface there.
        assert_eq!(
            serde_json::to_string(&PlantRequest::ClaimWriter {
                owner: 42,
                controller: true,
                monitor: Some("127.0.0.1:4190".parse().unwrap()),
            })
            .unwrap(),
            r#"{"op":"claim_writer","owner":42,"controller":true,"monitor":"127.0.0.1:4190"}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::ClaimWriterUnlessHeld {
                owner: 44,
                monitor: None
            })
            .unwrap(),
            r#"{"op":"claim_writer_unless_held","owner":44}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::EnsureWriter {
                owner: 43,
                rebind: true,
                controller: true,
                monitor: None,
            })
            .unwrap(),
            r#"{"op":"ensure_writer","owner":43,"rebind":true,"controller":true}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::ReleaseWriter { keep_claim: false }).unwrap(),
            r#"{"op":"release_writer","keep_claim":false}"#
        );
        // The pre-flag wire shapes still decode: an absent `rebind`
        // binds, an absent `keep_claim` releases fully, and an absent
        // `controller` reads as a controller claim — the contract every
        // earlier build's requests carried.
        assert_eq!(
            serde_json::from_str::<PlantRequest>(r#"{"op":"ensure_writer","owner":43}"#).unwrap(),
            PlantRequest::EnsureWriter {
                owner: 43,
                rebind: true,
                controller: true,
                monitor: None,
            }
        );
        assert_eq!(
            serde_json::from_str::<PlantRequest>(r#"{"op":"claim_writer","owner":42}"#).unwrap(),
            PlantRequest::ClaimWriter {
                owner: 42,
                controller: true,
                monitor: None,
            }
        );
        assert_eq!(
            serde_json::from_str::<PlantRequest>(r#"{"op":"release_writer"}"#).unwrap(),
            PlantRequest::ReleaseWriter { keep_claim: false }
        );
        assert_eq!(
            serde_json::to_string(&PlantRequest::ProbeWriter).unwrap(),
            r#"{"op":"probe_writer"}"#
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
            PlantResponse::ClaimedShared { owner: 7 },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::UnknownPoint(PointId(4)),
                    owner: None,
                    monitor: None,
                },
            },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::TypeMismatch {
                        point: PointId(5),
                        expected: ValueKind::Float,
                        found: Value::Bool(true),
                    },
                    owner: None,
                    monitor: None,
                },
            },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::Timeout(PointId(6)),
                    owner: None,
                    monitor: None,
                },
            },
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::InvalidValue { point: PointId(8) },
                    owner: None,
                    monitor: None,
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
                    owner: Some(424242),
                    monitor: None,
                },
            },
            PlantResponse::Error {
                error: PlantError::Unclaimed {
                    detail: "no attachment holds field writes".to_string(),
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
            serde_json::to_string(&PlantResponse::ClaimedShared { owner: 7 }).unwrap(),
            r#"{"result":"claimed_shared","owner":7}"#
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
                    owner: None,
                    monitor: None,
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"io","error":{"unknown_point":4}}}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantResponse::Error {
                error: PlantError::Unclaimed {
                    detail: "no attachment holds field writes".to_string(),
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"unclaimed","detail":"no attachment holds field writes"}}"#
        );
        // The fencing verdicts name the standing claim's owner — the
        // claimant the fenced-out owner's audit trail records — while
        // a `None` keeps the pre-field payload shape byte-identical.
        assert_eq!(
            serde_json::to_string(&PlantResponse::Error {
                error: PlantError::Fenced {
                    detail: "another attachment owns field writes".to_string(),
                    owner: Some(424242),
                    monitor: None,
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"fenced","detail":"another attachment owns field writes","owner":424242}}"#
        );
        assert_eq!(
            serde_json::to_string(&PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::Fenced(PointId(101)),
                    owner: Some(424242),
                    monitor: None,
                },
            })
            .unwrap(),
            r#"{"result":"error","error":{"kind":"io","error":{"fenced":101},"owner":424242}}"#
        );
        // A fenced answer from a build predating the field carries no
        // owner and decodes to the unattributed verdict.
        assert_eq!(
            serde_json::from_str::<PlantResponse>(
                r#"{"result":"error","error":{"kind":"fenced","detail":"another attachment owns field writes"}}"#
            )
            .unwrap(),
            PlantResponse::Error {
                error: PlantError::Fenced {
                    detail: "another attachment owns field writes".to_string(),
                    owner: None,
                    monitor: None,
                },
            }
        );
        assert_eq!(
            serde_json::from_str::<PlantResponse>(
                r#"{"result":"error","error":{"kind":"io","error":{"fenced":101}}}"#
            )
            .unwrap(),
            PlantResponse::Error {
                error: PlantError::Io {
                    error: IoError::Fenced(PointId(101)),
                    owner: None,
                    monitor: None,
                },
            }
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
                    owner: None,
                    monitor: None,
                },
            }
        );
    }
}
