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
//! - `{"op":"claim_writer","owner":7,"controller":true}` — takes the
//!   plant's field-write ownership for the `owner` token; answers
//!   `{"result":"done"}`, or `{"result":"claimed_shared","owner":7}`
//!   when another live attachment already holds the token.
//!   `controller` records whether the claimer is a controller peer —
//!   only a controller's live unyielded claim refuses the conditional
//!   takeover below; a tool's claim never does. Payloads predating the
//!   flag decode as `true`, the conservative verdict.
//! - `{"op":"ensure_writer","owner":7,"rebind":true,"controller":true}`
//!   — the conditional re-grant a re-attaching owner asserts; answers
//!   `done` while the field is unclaimed or already claims `owner` —
//!   `claimed_shared` when other live attachments hold the token —
//!   `fenced` while a different owner stands. `rebind:false` raises or
//!   confirms the claim without joining its holders — the demoted
//!   ex-owner's orphan-cycle probe. `controller` is `claim_writer`'s
//!   marker on the claim this grant raises.
//! - `{"op":"claim_writer_unless_held","owner":7}` — the conditional
//!   takeover grant a controller's startup activation and an orphaned
//!   peer's promotion run; answers `done` while no live *controller*
//!   attachment holds a different owner's unyielded claim, `fenced`
//!   while one stands — a dead, yielded, or tool-held claim still
//!   preempts, so a crashed owner's recovery and a rogue claim's
//!   cleanup keep working where a live incumbent is protected.
//! - `{"op":"release_writer","keep_claim":false}` — drops this
//!   connection's hold on the write claim, releasing the claim itself
//!   when the last holder leaves; answers `done`. `keep_claim:true` —
//!   the demotion shape — keeps the claim standing, marked yielded.
//!
//! ## Field write-ownership fencing
//!
//! The server arbitrates a single field writer, per the failover
//! decision: a connection issues `claim_writer` naming an `owner` token
//! — one controller's several attachments claim the same token — and
//! the grant preempts unconditionally. The field fails closed: `write`
//! and `step` requests from a connection not holding the current claim
//! are refused — `write` with the point's `IoError::Fenced`, `step`
//! with `PlantError::Fenced` — so a promoted standby's claim stops a
//! still-alive old owner's writes at the field itself. The claim
//! stands until preempted or the last holder's `release_writer` hands
//! it back, never released on disconnect: a dead owner's silence is
//! the failure the claim exists to fence. Reads, fault injection, and
//! `list_points` stay open to every attachment.
//!
//! Unclaimed is a closed state too, not an open window: a fresh or
//! restarted server — and one whose last holder released — refuses
//! `write` and `step` with `PlantError::Unclaimed` (surfaced by
//! `RemoteDriver` as the point's `IoError::Fenced`, respectively
//! [`RemoteError::Unclaimed`]) until a claim lands. A plant restart
//! therefore drops the claim into a named refusal rather than a field
//! any attachment can write through, and the returning owner's
//! `ensure_writer` re-arms deterministically instead of racing an
//! interposer that could otherwise seize the field first. A live
//! attachment whose write or step is refused `Unclaimed` re-arms its
//! recorded owner once through the conditional `ensure_writer` grant
//! and retries, so a claim-state reset behind a live connection
//! reclaims instead of demoting the healthy owner; a genuinely stolen
//! field refuses that re-arm as fenced.
//!
//! The claim tracks the live connections holding it, so a grant joining
//! a token another live attachment already holds is flagged
//! `claimed_shared` — granted, since one owner's several attachments
//! share a token by design, but reported because the token cannot
//! distinguish that from a second field-owning *process* reusing it:
//! two controllers pinned to one `--owner-token` would both write and
//! step, defeating the arbitration silently. A connection's end drops
//! only its own hold — the claim itself stands — so the flag reports
//! sharing among *live* holders and a re-attach after the last holder
//! died answers plain `done`.
//!
//! A restarted server is a new claim lifetime — the `owner` mutex dies
//! with the process — so `ensure_writer` is the conditional re-grant:
//! it claims for `owner` only while the field is unclaimed or already
//! claims that owner, answering `fenced` while a *different* owner
//! stands. A reconnecting field owner re-arms the claim through it
//! without preempting whichever attachment claimed during the outage.
//! `release_writer` is the deliberate hand-back: it drops only this
//! connection's hold, releasing the claim itself when the last holder
//! leaves — the shape a mutation tool that claimed conditionally
//! (`dcs-plant-ctl`) needs so its claim cannot outlive its connection
//! and fence the owner's re-arm. Its `keep_claim` half is the
//! demotion's: the ex-owner's hold drops but the claim stands, marked
//! yielded — the token keeps fencing the field, and a successor's
//! `claim_writer_unless_held` still preempts it despite other
//! attachments holding the yielded token, where a live *controller's*
//! unyielded claim refuses it: the field's own arbitration of "the
//! owner deliberately stepped down" against "a live incumbent still
//! stands", with the `controller` marker telling a real peer's claim
//! from a tool's so the conditional grant never wedges a peer's
//! recovery on a rogue or lingering tool hold.
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
//! an invalid `step`. The write-ownership refusals are
//! `{"kind":"fenced","detail":…}` while another owner stands and
//! `{"kind":"unclaimed","detail":…}` while none does — distinct kinds so
//! a probe can tell "closed until an owner claims" from "fenced out by
//! one". A line exceeding [`MAX_MESSAGE`] is answered by closing the
//! connection.
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

pub use client::{ClaimGrant, RemoteDriver, RemoteError};
pub use protocol::{MAX_MESSAGE, PlantError, PlantRequest, PlantResponse};
pub use server::PlantServer;
