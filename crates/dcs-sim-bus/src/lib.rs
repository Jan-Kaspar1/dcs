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
//!
//! Error codes: `0x01` unknown register (`u16 register`), `0x02` kind
//! mismatch (`u16 register`, `u8 expected kind`, found value bytes),
//! `0x03` invalid request (`u16 detail length`, UTF-8 detail). A
//! malformed request payload is answered with an `invalid_request`
//! error — the connection stays live — while a frame violating the
//! length bound ends the connection.
//!
//! `BusDriver` maps the failure surface: a dead or severed link and
//! any incoherent answer surface as `IoError::Disconnected`, an
//! unanswered request as `IoError::Timeout`, an unmapped point as
//! `IoError::UnknownPoint`, and a kind-mismatched write as
//! `IoError::TypeMismatch` — the first failure drops the connection
//! for good, and [`BusDriver::connected`]/[`BusDriver::last_failure`]
//! report link health for the driver-diagnostics surface. The
//! `dcs-sim-bus-device` binary in this crate serves one model-declared
//! `sim-bus` device's registers for integration rigs.

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
