//! A register-mapped simulated fieldbus device: a [`BusServer`] sharing
//! a [`RegisterBank`] over TCP, with a matching
//! [`IoDriver`](dcs_core::IoDriver) client — the second-transport proof
//! of the hardware-abstraction seam.
//!
//! Where `dcs-sim-net` serves a simulated *plant* — a `SimDriver` whose
//! points are addressed by id over line-delimited JSON — this crate
//! serves a simulated *fieldbus device*: its visible state is a bank of
//! numbered registers, every request names a register address, and the
//! framing is binary. The plant model binds logical points to the
//! device through its kind-specific parameters: a `sim-bus` device
//! declares its server's listen address and each channel's register
//! index, and the registered factory in `dcs-assembly`
//! (`SIM_BUS_KIND`) builds a [`BusDriver`] serving exactly the bound
//! points.
//!
//! ## Wire protocol
//!
//! A frame is a `u16` big-endian length — the payload byte count,
//! prefix excluded — then that many payload bytes, capped at
//! [`MAX_FRAME`]. All multi-byte integers are big-endian. A client has
//! at most one request in flight per connection: requests are answered
//! in arrival order, which is what makes the synchronous exchange's
//! failure semantics well-defined.
//!
//! Request payloads, discriminated by their first byte:
//!
//! | tag | operation | rest of payload |
//! |---|---|---|
//! | `0x01` | read register | `u16 register` |
//! | `0x02` | write register | `u16 register`, `u8 kind`, value bytes |
//! | `0x03` | list registers | none |
//! | `0x04` | step | none |
//! | `0x05` | claim writer | `u64 owner` |
//! | `0x06` | release writer | none |
//!
//! A value's `kind` byte is `0x01` bool, `0x02` int, `0x03` float,
//! followed by its payload: one byte (`0x00`/`0x01`) for bool, eight
//! bytes for the `i64` or `f64` bit pattern.
//!
//! `step` is the explicit device clock advance: the register bank
//! holds values only — no time-dependent dynamics — so stepping
//! increments the logical tick that stamps written registers and
//! nothing else. Nothing on the wire advances on a wall clock.
//!
//! Response payloads:
//!
//! | tag | answer | rest of payload |
//! |---|---|---|
//! | `0x01` | sample | `u8 kind`, value bytes, `u64 tick` |
//! | `0x02` | written | `u64 tick` — the tick the write stamped |
//! | `0x03` | registers | `u16 count`, then per register `u16 register`, `u8 kind`, value bytes, `u64 tick` |
//! | `0x04` | stepped | `u64 tick` — the bank's new tick |
//! | `0x05` | error | `u8 code`, code body |
//! | `0x06` | done | none — a claim or release applied |
//!
//! Error codes: `0x01` unknown register (`u16 register`), `0x02` kind
//! mismatch (`u16 register`, `u8 expected kind`, found value bytes),
//! `0x03` invalid request (`u16 detail length`, UTF-8 detail), `0x04`
//! fenced (`u16 detail length`, UTF-8 detail). A malformed request
//! payload is answered with an `invalid_request` error — the
//! connection stays live — while a frame violating the length bound
//! ends the connection.
//!
//! ## Write-ownership fencing
//!
//! The device arbitrates a single writer — the failover decision's
//! fencing rule carried onto the register protocol, so a partitioned
//! old active cannot keep writing registers beside a promoted peer.
//! `claim_writer` grants the write claim to the requesting attachment
//! under an opaque `owner` token, the grant unconditional: it preempts
//! whichever owner held the device, and one owner's several
//! attachments claim the same token so all of them write. While a
//! claim stands, `write_register` and `step` from an attachment not
//! holding it answer the `fenced` error — surfaced through
//! [`BusDriver`] as `IoError::Fenced` on the addressed point and
//! [`LinkError::Fenced`] on a step — while reads and the register
//! census stay open to every attachment. An unclaimed device stays
//! open to all, the pre-claim behavior.
//!
//! The claim is bound to the attachments holding it: `release_writer`
//! drops the requesting connection's hold — a no-op when it holds
//! nothing — and a connection's drop releases it likewise, the last
//! release freeing the field so a dead owner's claim cannot fence a
//! promoted peer's. Where the claim cannot be held — an attachment
//! whose link is down holds nothing — the field-claim failure refuses
//! the promotion as `SwitchError::FieldClaimFailed`, and a field kind
//! that cannot arbitrate at all keeps automatic self-promotion
//! disabled per the failover decision.
//!
//! `BusDriver` maps the failure surface: a dead or severed link and
//! any incoherent answer surface as `IoError::Disconnected`, an
//! unanswered request as `IoError::Timeout`, an unmapped point as
//! `IoError::UnknownPoint`, a kind-mismatched write as
//! `IoError::TypeMismatch`, and a fenced-out write as
//! `IoError::Fenced` — the first failure drops the connection
//! for good, and [`BusDriver`] reports that link health through
//! `IoDriver::diagnostics` for the telemetry snapshot's I/O-health
//! section. The
//! `dcs-sim-bus-device` binary in this crate serves one model-declared
//! `sim-bus` device's registers for integration rigs, and the
//! `dcs-sim-bus-ctl` binary is the protocol's development-tooling
//! client — the register analogue of `dcs-sim-net`'s `dcs-plant-ctl` —
//! listing, reading, writing, and stepping a running device server's
//! registers. It is not part of the operator contract.

#![warn(missing_docs)]

mod bank;
mod client;
mod params;
mod protocol;
mod server;

pub use bank::{BankError, RegisterBank, RegisterDecl};
pub use client::{BusDriver, LinkError, PointRegister};
pub use params::{ChannelRegister, DEVICE_KIND, DeviceParameters};
pub use protocol::{BusError, BusRequest, BusResponse, MAX_FRAME, RegisterInfo};
pub use server::BusServer;
