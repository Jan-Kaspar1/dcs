//! A [`SimDriver`](dcs_sim::SimDriver) plant served over TCP, with a
//! matching [`IoDriver`](dcs_core::IoDriver) client — the simulated
//! backend split across a process boundary.
//!
//! The crate is the proof that the hardware-abstraction boundary hides
//! transport: a component or executor uses [`RemoteDriver`] through
//! `&dyn IoDriver` exactly as it uses an in-process `SimDriver`, and the
//! scripted-behavior tests show identical results for both. It is also
//! the mechanism by which a standby controller observes the same field
//! state as the active (the standby-field-observation architecture
//! decision): the simulated plant lives in the [`PlantServer`] process,
//! so every attached client reads the same points, and `RemoteDriver`
//! deliberately stays the field-observing driver kind — `capture_state`
//! unimplemented, nothing local to checkpoint.
//!
//! Stepping is explicit: [`RemoteDriver::step`] issues a
//! [`PlantRequest::Step`], so a controller scan advances the shared plant
//! deterministically — once per cycle, exactly where the local driver
//! call sat — while a second client that only reads observes the stepped
//! values the stepping client produced.
//!
//! ## Wire protocol
//!
//! Newline-delimited JSON: the client writes one [`PlantRequest`] object
//! followed by `'\n'` and reads back exactly one [`PlantResponse`] line,
//! strictly request/response with at most one request in flight per
//! connection. Lines are capped at [`MAX_MESSAGE`] bytes.
//!
//! Requests, discriminated by the `op` field:
//!
//! - `{"op":"read","point":1}` — `IoDriver::read`; answers
//!   `{"result":"sample","sample":{"value":…,"quality":…,"tick":…}}`.
//! - `{"op":"write","point":2,"value":{"float":1.5}}` —
//!   `IoDriver::write` with its kind check; answers `{"result":"done"}`.
//! - `{"op":"step","dt":0.1}` — `SimDriver::step`; answers
//!   `{"result":"stepped","tick":7}`. A negative or non-finite `dt` is
//!   refused as `invalid_request`, never a panic.
//! - `{"op":"inject_fault","point":3,"fault":"timeout"}` —
//!   `SimDriver::inject_fault`; answers `done`.
//! - `{"op":"clear_fault","point":3}` — `SimDriver::clear_fault`;
//!   answers `done`.
//! - `{"op":"list_points"}` — `SimDriver::points`; answers
//!   `{"result":"points","points":[…]}` with every bound point's
//!   direction, observed sample, and active fault, ordered by point id.
//! - `{"op":"claim_writer","owner":7}` — takes the plant's
//!   field-write ownership for the `owner` token; answers
//!   `{"result":"done"}`.
//! - `{"op":"ensure_writer","owner":7}` — the conditional re-grant a
//!   re-attaching owner asserts; answers `done` while the field is
//!   unclaimed or already claims `owner`, `fenced` while a different
//!   owner stands.
//!
//! ## Field write-ownership fencing
//!
//! The server arbitrates a single field writer, per the failover
//! decision: a connection issues `claim_writer` naming an `owner` token
//! — one controller's several attachments claim the same token — and
//! the grant preempts unconditionally. Once an owner is claimed, `write`
//! and `step` requests from a connection that has not claimed the
//! current owner are refused — `write` with the point's
//! `IoError::Fenced`, `step` with `PlantError::Fenced` — so a promoted
//! standby's claim stops a still-alive old owner's writes at the field
//! itself. The claim stands until preempted, never released on
//! disconnect: a dead owner's silence is the failure the claim exists
//! to fence. Reads, fault injection, and `list_points` stay open to
//! every attachment. Until the first `claim_writer`, every attachment
//! writes freely.
//!
//! A restarted server is a new claim lifetime — the `owner` mutex dies
//! with the process — so `ensure_writer` is the conditional re-grant:
//! it claims for `owner` only while the field is unclaimed or already
//! claims that owner, answering `fenced` while a *different* owner
//! stands. A reconnecting field owner re-arms the claim through it
//! without preempting whichever attachment claimed during the outage.
//!
//! The `dcs-plant-ctl` binary in this crate is the protocol's
//! development-tooling client: it lists, reads, and writes points and
//! drives stepping and fault injection on a running plant server, so a
//! live demonstration can be perturbed without recompiling fixtures. It
//! is not part of the operator contract.
//!
//! Failures answer `{"result":"error","error":…}` with a
//! [`PlantError`]: `{"kind":"io","error":…}` carries the driver's
//! [`IoError`](dcs_core::IoError) verbatim (`UnknownPoint`,
//! `TypeMismatch`, or an injected fault's `Disconnected`/`Timeout`), and
//! `{"kind":"invalid_request","detail":…}` covers an unparseable line or
//! an invalid `step`, and `{"kind":"fenced","detail":…}` the
//! write-ownership refusal described above. A line exceeding
//! [`MAX_MESSAGE`] is answered by closing the connection.
//!
//! Point values must cross the wire bit-exactly or remote and local runs
//! diverge, so the crate builds `serde_json` with its `float_roundtrip`
//! feature — the precise float parser; any other implementation of this
//! protocol needs the same guarantee for `f64` payloads.
//!
//! `RemoteDriver` maps the remaining failure surface: a dead or severed
//! link and any incoherent answer surface as `IoError::Disconnected`, an
//! unanswered request as `IoError::Timeout`, and a failed exchange drops
//! the connection — a late response could otherwise pair with the next
//! request — while the next access re-attaches lazily, so a field outage
//! degrades telemetry instead of killing the driver, and a returned
//! plant is served by the same attachment.
//!
//! All protocol types are shared serde contracts in this crate, so any
//! JSON-capable tool can drive or inspect the simulated plant.

#![warn(missing_docs)]

mod client;
mod protocol;
mod server;

pub use client::{RemoteDriver, RemoteError};
pub use protocol::{MAX_MESSAGE, PlantError, PlantRequest, PlantResponse};
pub use server::PlantServer;
