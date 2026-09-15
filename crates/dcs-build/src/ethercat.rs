//! The `ethercat` device kind's typed composition surface.
//!
//! [`PlantBuilder::ethercat`] declares a hardware-bound EtherCAT device
//! — a coupler or remote-I/O station — and [`ethercat_input`] /
//! [`ethercat_output`] declare its channels together with their
//! process-image offsets, so the emitted `parameters` carry exactly the
//! field-bus declaration the kind's factory validates at assembly:
//! logical `bus` name, expected station `identity`, the `mapping`
//! layout, `exchange_miss_threshold`, `safe_outputs`, and the `startup`
//! policy.
//!
//! The surface is a data mirror of `dcs-ethercat`'s parameter
//! vocabulary — like [`crate::specs`] mirrors the component
//! descriptors, the same names are written out so the emitted document
//! is identical to the hand-written one. `crates/dcs-build/tests/
//! ethercat.rs` pins the emission against the reference fixture and the
//! real `dcs-ethercat` parser, so a spec that drifts fails CI beside
//! the contract it mirrors.
//!
//! ```rust
//! use dcs_build::ethercat::{EthercatSpec, ImageOffset, StationIdentity};
//! use dcs_build::{PlantBuilder, PointId};
//!
//! let mut plant = PlantBuilder::new();
//! let coupler = plant.ethercat(EthercatSpec::new(
//!     "ecat0",
//!     StationIdentity {
//!         vendor: 21,
//!         product: 750354,
//!         revision: 1,
//!     },
//!     3,
//! ));
//! let di0 = plant.ethercat_input::<bool>(coupler, "di0", ImageOffset::Bit { byte: 0, bit: 0 });
//! let do0 =
//!     plant.ethercat_output::<bool>(coupler, "do0", ImageOffset::Bit { byte: 0, bit: 0 }, false);
//! plant.field_input::<bool>(PointId(1), di0, false);
//! plant.field_output::<bool>(PointId(2), do0);
//! # let _model = plant.build().unwrap();
//! ```
//!
//! [`ethercat_input`]: PlantBuilder::ethercat_input
//! [`ethercat_output`]: PlantBuilder::ethercat_output

use crate::builder::PlantBuilder;
use dcs_core::{Direction, PointType, ValueKind};
use dcs_model::{ChannelRef, DeviceId};
use serde_json::json;

/// The device kind string an [`EthercatSpec`] declares — the same
/// `ethercat` `dcs-ethercat`'s contract names.
pub const ETHERCAT_KIND: &str = "ethercat";

/// The expected station identity an EtherCAT master checks the
/// answering station against before outputs are enabled: vendor id,
/// product code, revision number.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StationIdentity {
    /// The expected vendor id.
    pub vendor: u32,
    /// The expected product code.
    pub product: u32,
    /// The expected revision number.
    pub revision: u32,
}

/// A channel's placement inside its direction's process image.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImageOffset {
    /// A single-bit entry — a `bool` channel: byte index plus the bit
    /// within it (0–7).
    Bit {
        /// The byte index in the image.
        byte: u32,
        /// The bit within the byte, 0–7.
        bit: u8,
    },
    /// A byte-aligned field — an `int` channel (8, 16, 32, or 64 bits)
    /// or a `float` channel (32 or 64 bits).
    Field {
        /// The byte index in the image.
        byte: u32,
        /// The field's width in bits.
        bits: u32,
    },
}

impl ImageOffset {
    /// The `mapping` entry for a channel of `kind`.
    ///
    /// # Panics
    ///
    /// The offset shape must fit the channel's kind — `Bit` for
    /// `bool`, `Field` of an admitted width for `int`/`float` — since a
    /// mistyped composition is a programming error the factory would
    /// otherwise reject at assembly.
    fn entry(self, channel: &str, kind: ValueKind) -> serde_json::Value {
        match (self, kind) {
            (Self::Bit { byte, bit }, ValueKind::Bool) if bit < 8 => {
                json!({"byte": byte, "bit": bit})
            }
            (Self::Field { byte, bits }, ValueKind::Int)
                if matches!(bits, 8 | 16 | 32 | 64) =>
            {
                json!({"byte": byte, "bits": bits})
            }
            (Self::Field { byte, bits }, ValueKind::Float) if matches!(bits, 32 | 64) => {
                json!({"byte": byte, "bits": bits})
            }
            (offset, kind) => panic!(
                "channel {channel:?} of kind {kind:?} cannot take offset {offset:?}: a bool \
                 channel takes ImageOffset::Bit, an int channel ImageOffset::Field of 8, 16, 32, \
                 or 64 bits, a float channel one of 32 or 64 bits"
            ),
        }
    }
}

/// An `ethercat` device declaration — the kind's spec, following the
/// same pattern as the component specs: a data mirror carrying the
/// fields the emitted `parameters` record.
///
/// The startup policy is not a spec field: the contract admits only
/// `{"on_mismatch": "fail"}` — a station identity or layout mismatch is
/// a hard startup failure — so the declaration emits it unconditionally.
#[derive(Debug, Clone, PartialEq)]
pub struct EthercatSpec {
    /// The *logical* bus name — deployment configuration binds it to a
    /// host interface outside the model; the name is never an
    /// interface itself.
    pub bus: String,
    /// The expected station identity.
    pub identity: StationIdentity,
    /// Consecutive failed cyclic exchanges before the device's reads
    /// escalate to `IoError::Disconnected` (the decision-78 cyclic
    /// contract's miss threshold).
    pub exchange_miss_threshold: u64,
}

impl EthercatSpec {
    /// A declaration for the EtherCAT device on logical bus `bus`,
    /// expecting `identity`, escalating to `Disconnected` after
    /// `exchange_miss_threshold` consecutive missed exchanges.
    pub fn new(
        bus: impl Into<String>,
        identity: StationIdentity,
        exchange_miss_threshold: u64,
    ) -> Self {
        Self {
            bus: bus.into(),
            identity,
            exchange_miss_threshold,
        }
    }
}

impl PlantBuilder {
    /// Declares a hardware-bound `ethercat` device from `spec` and
    /// returns its id: the device emits `"hardware": true` plus the
    /// bus, identity, miss-threshold, and startup-policy parameters,
    /// with its `mapping` and `safe_outputs` tables seeded for
    /// [`ethercat_input`](Self::ethercat_input) /
    /// [`ethercat_output`](Self::ethercat_output) to fill.
    pub fn ethercat(&mut self, spec: EthercatSpec) -> DeviceId {
        let device = self.device(ETHERCAT_KIND);
        device.hardware = true;
        device
            .parameters
            .insert("bus".to_string(), json!(spec.bus));
        device.parameters.insert(
            "identity".to_string(),
            json!({
                "vendor": spec.identity.vendor,
                "product": spec.identity.product,
                "revision": spec.identity.revision,
            }),
        );
        device
            .parameters
            .insert("mapping".to_string(), json!({"inputs": {}, "outputs": {}}));
        device.parameters.insert(
            "exchange_miss_threshold".to_string(),
            json!(spec.exchange_miss_threshold),
        );
        device
            .parameters
            .insert("safe_outputs".to_string(), json!({}));
        device
            .parameters
            .insert("startup".to_string(), json!({"on_mismatch": "fail"}));
        device.id
    }

    /// Declares an `in` channel of `T`'s kind on an `ethercat` device,
    /// placed at `offset` in the input process image, and returns the
    /// [`ChannelRef`] points bind.
    ///
    /// # Panics
    ///
    /// `device` must come from this builder's
    /// [`ethercat`](Self::ethercat), `offset` must fit `T`'s kind, and
    /// `offset.bit` must be in 0–7 — a mistyped composition is a
    /// programming error, reported eagerly rather than at assembly.
    pub fn ethercat_input<T: PointType>(
        &mut self,
        device: DeviceId,
        name: &str,
        offset: ImageOffset,
    ) -> ChannelRef {
        self.ethercat_channel::<T>(device, name, Direction::In, offset, None)
    }

    /// Declares an `out` channel of `T`'s kind on an `ethercat` device,
    /// placed at `offset` in the output process image with `safe` its
    /// declared safe state — staged into the output image before the
    /// first exchange — and returns the [`ChannelRef`] points bind.
    ///
    /// # Panics
    ///
    /// Same rules as [`ethercat_input`](Self::ethercat_input).
    pub fn ethercat_output<T: PointType>(
        &mut self,
        device: DeviceId,
        name: &str,
        offset: ImageOffset,
        safe: T,
    ) -> ChannelRef {
        self.ethercat_channel::<T>(device, name, Direction::Out, offset, Some(safe))
    }

    /// The shared declaration half of [`ethercat_input`](Self::ethercat_input)
    /// and [`ethercat_output`](Self::ethercat_output): declare the
    /// channel, then record its image offset and — for `out` — its safe
    /// state in the device's `parameters`.
    fn ethercat_channel<T: PointType>(
        &mut self,
        device: DeviceId,
        name: &str,
        direction: Direction,
        offset: ImageOffset,
        safe: Option<T>,
    ) -> ChannelRef {
        {
            let Some(declared) = self.device_mut(device) else {
                panic!("ethercat channel {name:?} names a device this builder did not declare");
            };
            assert!(
                declared.kind == ETHERCAT_KIND,
                "ethercat channel {name:?} declares on device {} of kind {:?}, not {ETHERCAT_KIND:?}",
                device.0,
                declared.kind
            );
        }
        let channel = self.channel::<T>(device, name, direction);
        let entry = offset.entry(name, T::KIND);
        let image = match direction {
            Direction::In => "inputs",
            Direction::Out => "outputs",
        };
        let device = self.device_mut(device).unwrap();
        device
            .parameters
            .get_mut("mapping")
            .and_then(|mapping| mapping.get_mut(image))
            .and_then(|image| image.as_object_mut())
            .unwrap()
            .insert(name.to_string(), entry);
        if let Some(safe) = safe {
            device
                .parameters
                .get_mut("safe_outputs")
                .and_then(|table| table.as_object_mut())
                .unwrap()
                .insert(
                    name.to_string(),
                    serde_json::to_value(safe.into_value()).unwrap(),
                );
        }
        channel
    }
}
