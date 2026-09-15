//! The `ethercat` device-parameter contract: the field-bus identity,
//! process-image mapping, and startup policy a model declares for a
//! hardware-bound EtherCAT device.
//!
//! An `ethercat` device's `parameters` carry:
//!
//! - `"bus"` (required non-empty string): the *logical* bus name — the
//!   model names the bus, deployment configuration binds it to a host
//!   interface outside the document (decision 47), so no parameter
//!   names an interface;
//! - `"identity"` (required object): the expected station identity —
//!   `{"vendor": <u32>, "product": <u32>, "revision": <u32>}` — the
//!   master checks the answering station against it before outputs are
//!   enabled;
//! - `"mapping"` (required object): the channel → process-data-offset
//!   layout, `{"inputs": {...}, "outputs": {...}}` — each direction's
//!   image maps every declared channel of that direction to its offset:
//!   `{"byte": <u32>, "bit": <0–7>}` for a `bool` channel, or a
//!   byte-aligned `{"byte": <u32>, "bits": <width>}` field for an `int`
//!   channel (width 8, 16, 32, or 64) or a `float` channel (width 32 or
//!   64). No two channels' bit ranges may overlap inside an image;
//! - `"exchange_miss_threshold"` (required integer ≥ 1): consecutive
//!   failed cyclic exchanges before the device's reads escalate to
//!   `IoError::Disconnected` under the decision-78 cyclic contract;
//! - `"safe_outputs"` (object, required when the device declares `out`
//!   channels): every `out` channel's declared safe state — a tagged
//!   [`Value`] matching the channel's kind — staged into the output
//!   image before the first exchange;
//! - `"startup"` (required object): `{"on_mismatch": "fail"}` — the only
//!   policy the contract admits: a station identity or layout mismatch
//!   is a hard startup failure, never a silent fallback to simulation
//!   or degraded operation.
//!
//! [`DeviceParameters::parse`] validates the vocabulary against the
//! device's declared channels; the kind's registered factory calls it
//! at assembly so a malformed declaration fails before any scan.

use dcs_core::{Direction, Value, ValueKind};
use std::collections::BTreeMap;

/// The device kind string a hardware-bound EtherCAT `Device` declares.
pub const DEVICE_KIND: &str = "ethercat";

/// The part of a model `Channel` the `ethercat` grammar validates
/// against: direction selects the process image, value kind selects the
/// offset entry's shape.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChannelDecl {
    /// Whether the channel reads from (`In`) or writes to (`Out`) the field.
    pub direction: Direction,
    /// The channel's value kind.
    pub kind: ValueKind,
}

/// The expected station identity the master's discovery must match:
/// the EtherCAT vendor id, product code, and revision number.
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
pub struct ImageOffset {
    /// The bit position of the channel's first bit inside the image —
    /// `byte * 8` for a field-aligned entry, `byte * 8 + bit` for a
    /// single-bit `bool` channel.
    pub bit_offset: u64,
    /// The channel's width in bits: `1` for a `bool`, the declared width
    /// for an `int` or `float` field.
    pub bits: u32,
}

/// The declared startup policy — what a station identity or process
/// layout mismatch does at startup.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StartupPolicy {
    /// `"on_mismatch": "fail"` — a mismatch is a hard startup failure:
    /// no exchange begins and no degraded or simulated operation is
    /// substituted. This is the only policy the contract admits; a
    /// model declaring a hardware kind must fail startup if the
    /// hardware cannot initialize or does not match the declaration.
    FailOnMismatch,
}

/// A parsed `ethercat` device's `parameters`.
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceParameters {
    /// The logical bus name deployment configuration binds to a host
    /// interface; never an interface name itself.
    pub bus: String,
    /// The expected station identity checked before outputs enable.
    pub identity: StationIdentity,
    /// `in`-channel name → input-image offset.
    pub inputs: BTreeMap<String, ImageOffset>,
    /// `out`-channel name → output-image offset.
    pub outputs: BTreeMap<String, ImageOffset>,
    /// Consecutive failed exchanges before reads escalate to
    /// `IoError::Disconnected` (the cyclic contract's miss threshold).
    pub exchange_miss_threshold: u64,
    /// `out`-channel name → declared safe state staged into the output
    /// image before the first exchange.
    pub safe_outputs: BTreeMap<String, Value>,
    /// The declared startup policy.
    pub startup: StartupPolicy,
}

impl DeviceParameters {
    /// Parses and validates an `ethercat` device's `parameters` against
    /// its declared `channels`, or names the first violation found.
    ///
    /// `channels` maps each declared channel name to its
    /// [`ChannelDecl`]; the factory hands it the model's channel table.
    /// The grammar admits only the six documented keys.
    pub fn parse(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ChannelDecl>,
    ) -> Result<Self, String> {
        for name in parameters.keys() {
            if !matches!(
                name.as_str(),
                "bus"
                    | "identity"
                    | "mapping"
                    | "exchange_miss_threshold"
                    | "safe_outputs"
                    | "startup"
            ) {
                return Err(format!(
                    "unknown parameter {name:?}; {DEVICE_KIND:?} takes \"bus\", \"identity\", \
                     \"mapping\", \"exchange_miss_threshold\", \"safe_outputs\", and \"startup\""
                ));
            }
        }

        let bus = Self::bus(parameters)?;
        let identity = Self::identity(parameters)?;
        let exchange_miss_threshold = Self::exchange_miss_threshold(parameters)?;
        let startup = Self::startup(parameters)?;
        let (inputs, outputs) = Self::mapping(parameters, channels)?;
        let safe_outputs = Self::safe_outputs(parameters, channels)?;

        Ok(Self {
            bus,
            identity,
            inputs,
            outputs,
            exchange_miss_threshold,
            safe_outputs,
            startup,
        })
    }

    /// The required `"bus"` logical name — a non-empty string.
    fn bus(parameters: &BTreeMap<String, serde_json::Value>) -> Result<String, String> {
        let bus = required(parameters, "bus")?;
        let Some(bus) = bus.as_str() else {
            return Err(format!("parameter \"bus\" must be a string, found {bus}"));
        };
        if bus.is_empty() {
            return Err("parameter \"bus\" must be a non-empty string".to_string());
        }
        Ok(bus.to_string())
    }

    /// The required `"identity"` object — `vendor`, `product`, and
    /// `revision` u32 fields, nothing else.
    fn identity(
        parameters: &BTreeMap<String, serde_json::Value>,
    ) -> Result<StationIdentity, String> {
        let identity = required(parameters, "identity")?;
        let Some(object) = identity.as_object() else {
            return Err(format!(
                "parameter \"identity\" must be an object, found {identity}"
            ));
        };
        for name in object.keys() {
            if !matches!(name.as_str(), "vendor" | "product" | "revision") {
                return Err(format!(
                    "unknown \"identity\" field {name:?}; takes \"vendor\", \"product\", and \"revision\""
                ));
            }
        }
        let field = |name: &str| -> Result<u32, String> {
            u32_of(
                &format!("identity field {name:?}"),
                object
                    .get(name)
                    .ok_or_else(|| format!("parameter \"identity\" is missing field {name:?}"))?,
            )
        };
        Ok(StationIdentity {
            vendor: field("vendor")?,
            product: field("product")?,
            revision: field("revision")?,
        })
    }

    /// The required `"exchange_miss_threshold"` — a positive integer.
    fn exchange_miss_threshold(
        parameters: &BTreeMap<String, serde_json::Value>,
    ) -> Result<u64, String> {
        let threshold = required(parameters, "exchange_miss_threshold")?;
        let Some(n) = threshold.as_u64() else {
            return Err(format!(
                "parameter \"exchange_miss_threshold\" must be a positive integer, found {threshold}"
            ));
        };
        if n == 0 {
            return Err("parameter \"exchange_miss_threshold\" must be at least 1".to_string());
        }
        Ok(n)
    }

    /// The required `"startup"` policy — only `"on_mismatch": "fail"`.
    fn startup(parameters: &BTreeMap<String, serde_json::Value>) -> Result<StartupPolicy, String> {
        let startup = required(parameters, "startup")?;
        let Some(object) = startup.as_object() else {
            return Err(format!(
                "parameter \"startup\" must be an object, found {startup}"
            ));
        };
        for name in object.keys() {
            if name != "on_mismatch" {
                return Err(format!(
                    "unknown \"startup\" field {name:?}; takes \"on_mismatch\" only"
                ));
            }
        }
        match object.get("on_mismatch") {
            None => Err("parameter \"startup\" is missing field \"on_mismatch\"".to_string()),
            Some(v) if v.as_str() == Some("fail") => Ok(StartupPolicy::FailOnMismatch),
            Some(v) => Err(format!(
                "startup field \"on_mismatch\" admits \"fail\" only — a hardware-bound kind is \
                 never silently substituted, found {v}"
            )),
        }
    }

    /// The required `"mapping"` — `inputs`/`outputs` images whose
    /// entries place every declared channel, in the image matching its
    /// direction, at an offset shaped for its value kind, with no
    /// overlapping bit ranges.
    fn mapping(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ChannelDecl>,
    ) -> Result<(BTreeMap<String, ImageOffset>, BTreeMap<String, ImageOffset>), String> {
        let mapping = required(parameters, "mapping")?;
        let Some(object) = mapping.as_object() else {
            return Err(format!(
                "parameter \"mapping\" must be an object, found {mapping}"
            ));
        };
        for name in object.keys() {
            if !matches!(name.as_str(), "inputs" | "outputs") {
                return Err(format!(
                    "unknown \"mapping\" image {name:?}; takes \"inputs\" and \"outputs\""
                ));
            }
        }

        let inputs = Self::image(object.get("inputs"), "inputs", Direction::In, channels)?;
        let outputs = Self::image(object.get("outputs"), "outputs", Direction::Out, channels)?;

        for (name, decl) in channels {
            let image = match decl.direction {
                Direction::In => &inputs,
                Direction::Out => &outputs,
            };
            if !image.contains_key(name) {
                return Err(format!(
                    "mapping does not place declared channel {name:?} in its {image:?} image"
                ));
            }
        }

        Ok((inputs, outputs))
    }

    /// One direction's image map: every entry names a declared channel
    /// of that direction and parses its offset, and no two entries'
    /// bit ranges overlap.
    fn image(
        image: Option<&serde_json::Value>,
        image_name: &str,
        direction: Direction,
        channels: &BTreeMap<String, ChannelDecl>,
    ) -> Result<BTreeMap<String, ImageOffset>, String> {
        let mut offsets = BTreeMap::new();
        let Some(image) = image else {
            return Ok(offsets);
        };
        let Some(entries) = image.as_object() else {
            return Err(format!(
                "mapping image {image_name:?} must be an object, found {image}"
            ));
        };
        let mut taken: Vec<(u64, u64, &str)> = Vec::new();
        for (name, entry) in entries {
            let Some(decl) = channels.get(name) else {
                return Err(format!(
                    "mapping image {image_name:?} names channel {name:?} the device does not declare"
                ));
            };
            if decl.direction != direction {
                return Err(format!(
                    "mapping image {image_name:?} places {:?} channel {name:?}",
                    decl.direction
                ));
            }
            let offset = Self::offset(name, decl.kind, entry)?;
            let range = offset.bit_offset..offset.bit_offset + u64::from(offset.bits);
            for (start, end, other) in &taken {
                if range.start < *end && *start < range.end {
                    return Err(format!(
                        "mapping image {image_name:?}: channels {other:?} and {name:?} overlap \
                         (bits {start}..{end} vs {start_r}..{end_r})",
                        start_r = range.start,
                        end_r = range.end,
                    ));
                }
            }
            taken.push((range.start, range.end, name));
            offsets.insert(name.clone(), offset);
        }
        Ok(offsets)
    }

    /// One channel's offset entry: `{"byte", "bit"}` for a `bool`, a
    /// byte-aligned `{"byte", "bits"}` field for `int`/`float`.
    fn offset(
        channel: &str,
        kind: ValueKind,
        entry: &serde_json::Value,
    ) -> Result<ImageOffset, String> {
        let Some(object) = entry.as_object() else {
            return Err(format!(
                "mapping entry for channel {channel:?} must be an object, found {entry}"
            ));
        };
        let (allowed, widths) = match kind {
            ValueKind::Bool => (&["byte", "bit"][..], &[][..]),
            ValueKind::Int => (&["byte", "bits"][..], &[8, 16, 32, 64][..]),
            ValueKind::Float => (&["byte", "bits"][..], &[32, 64][..]),
        };
        for name in object.keys() {
            if !allowed.contains(&name.as_str()) {
                let shape = match kind {
                    ValueKind::Bool => "\"byte\" and \"bit\"",
                    _ => "\"byte\" and \"bits\"",
                };
                return Err(format!(
                    "mapping entry for channel {channel:?} (kind {kind:?}) has unknown field \
                     {name:?}; takes {shape}"
                ));
            }
        }
        let byte = u32_of(
            &format!("mapping entry for channel {channel:?} field \"byte\""),
            object.get("byte").ok_or_else(|| {
                format!("mapping entry for channel {channel:?} is missing field \"byte\"")
            })?,
        )?;
        let bit_offset = u64::from(byte) * 8;
        match kind {
            ValueKind::Bool => {
                let bit = u32_of(
                    &format!("mapping entry for channel {channel:?} field \"bit\""),
                    object.get("bit").ok_or_else(|| {
                        format!("mapping entry for channel {channel:?} is missing field \"bit\"")
                    })?,
                )?;
                if bit > 7 {
                    return Err(format!(
                        "mapping entry for channel {channel:?} field \"bit\" must be in 0..=7, \
                         found {bit}"
                    ));
                }
                Ok(ImageOffset {
                    bit_offset: bit_offset + u64::from(bit),
                    bits: 1,
                })
            }
            _ => {
                let bits = u32_of(
                    &format!("mapping entry for channel {channel:?} field \"bits\""),
                    object.get("bits").ok_or_else(|| {
                        format!("mapping entry for channel {channel:?} is missing field \"bits\"")
                    })?,
                )?;
                if !widths.contains(&bits) {
                    return Err(format!(
                        "mapping entry for channel {channel:?} (kind {kind:?}) field \"bits\" \
                         admits {widths:?} only, found {bits}"
                    ));
                }
                Ok(ImageOffset { bit_offset, bits })
            }
        }
    }

    /// The `"safe_outputs"` table: every declared `out` channel's safe
    /// state — a tagged [`Value`] matching the channel's kind —
    /// staged into the output image before the first exchange. Required
    /// when the device declares `out` channels; an absent or empty
    /// table is accepted only for a device with none.
    fn safe_outputs(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, ChannelDecl>,
    ) -> Result<BTreeMap<String, Value>, String> {
        let mut safe = BTreeMap::new();
        if let Some(value) = parameters.get("safe_outputs") {
            let Some(entries) = value.as_object() else {
                return Err(format!(
                    "parameter \"safe_outputs\" must be an object, found {value}"
                ));
            };
            for (name, json) in entries {
                let Some(decl) = channels.get(name) else {
                    return Err(format!(
                        "safe_outputs names channel {name:?} the device does not declare"
                    ));
                };
                if decl.direction != Direction::Out {
                    return Err(format!(
                        "safe_outputs names {:?} channel {name:?}; safe states apply to out \
                         channels",
                        decl.direction
                    ));
                }
                let value: Value = serde_json::from_value(json.clone()).map_err(|_| {
                    format!("safe_outputs entry for channel {name:?} is not a tagged Value")
                })?;
                if value.kind() != decl.kind {
                    return Err(format!(
                        "safe_outputs entry for channel {name:?} is kind {:?}, the channel is {kind:?}",
                        value.kind(),
                        kind = decl.kind,
                    ));
                }
                safe.insert(name.clone(), value);
            }
        }
        for (name, decl) in channels {
            if decl.direction == Direction::Out && !safe.contains_key(name) {
                return Err(format!(
                    "safe_outputs must declare a safe state for out channel {name:?}"
                ));
            }
        }
        Ok(safe)
    }
}

/// The parameter `name` must be present.
fn required<'a>(
    parameters: &'a BTreeMap<String, serde_json::Value>,
    name: &str,
) -> Result<&'a serde_json::Value, String> {
    parameters
        .get(name)
        .ok_or_else(|| format!("parameter {name:?} is required"))
}

/// `value` as a `u32`, or a named-shape error.
fn u32_of(context: &str, value: &serde_json::Value) -> Result<u32, String> {
    let Some(n) = value.as_u64() else {
        return Err(format!(
            "{context} must be a non-negative integer, found {value}"
        ));
    };
    u32::try_from(n).map_err(|_| format!("{context} must fit in 0..=4294967295, found {n}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// The declaration a four-channel coupler (2×DI, 1×AI, 2×DO)
    /// carries — the well-formed document the malformed cases mutate.
    fn declaration() -> BTreeMap<String, serde_json::Value> {
        [
            ("bus", json!("ecat0")),
            (
                "identity",
                json!({"vendor": 21, "product": 750354, "revision": 1}),
            ),
            (
                "mapping",
                json!({
                    "inputs": {
                        "di0": {"byte": 0, "bit": 0},
                        "di1": {"byte": 0, "bit": 1},
                        "ai0": {"byte": 1, "bits": 16}
                    },
                    "outputs": {
                        "do0": {"byte": 0, "bit": 0},
                        "do1": {"byte": 0, "bit": 1}
                    }
                }),
            ),
            ("exchange_miss_threshold", json!(3)),
            (
                "safe_outputs",
                json!({"do0": {"bool": false}, "do1": {"bool": false}}),
            ),
            ("startup", json!({"on_mismatch": "fail"})),
        ]
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
    }

    /// The channels the declaration maps.
    fn channels() -> BTreeMap<String, ChannelDecl> {
        [
            ("di0", Direction::In, ValueKind::Bool),
            ("di1", Direction::In, ValueKind::Bool),
            ("ai0", Direction::In, ValueKind::Int),
            ("do0", Direction::Out, ValueKind::Bool),
            ("do1", Direction::Out, ValueKind::Bool),
        ]
        .into_iter()
        .map(|(name, direction, kind)| (name.to_string(), ChannelDecl { direction, kind }))
        .collect()
    }

    fn parse(parameters: &BTreeMap<String, serde_json::Value>) -> Result<DeviceParameters, String> {
        DeviceParameters::parse(parameters, &channels())
    }

    /// Replaces `key` with `value` in the well-formed declaration and
    /// parses it.
    fn with(key: &str, value: serde_json::Value) -> Result<DeviceParameters, String> {
        let mut parameters = declaration();
        parameters.insert(key.to_string(), value);
        parse(&parameters)
    }

    /// Replaces `path` (dot-separated object keys) with `value`.
    fn with_at(path: &str, value: serde_json::Value) -> Result<DeviceParameters, String> {
        let mut parameters = declaration();
        let mut cursor = parameters
            .get_mut(path.split('.').next().unwrap())
            .unwrap()
            .as_object_mut()
            .unwrap();
        let parts: Vec<&str> = path.split('.').collect();
        for key in &parts[1..parts.len() - 1] {
            cursor = cursor.get_mut(*key).unwrap().as_object_mut().unwrap();
        }
        cursor.insert(parts.last().unwrap().to_string(), value);
        parse(&parameters)
    }

    /// Removes `key` from the declaration and parses it.
    fn without(key: &str) -> Result<DeviceParameters, String> {
        let mut parameters = declaration();
        parameters.remove(key);
        parse(&parameters)
    }

    fn assert_err(result: Result<DeviceParameters, String>, needle: &str) {
        let Err(detail) = result else {
            panic!("expected an error containing {needle:?}");
        };
        assert!(detail.contains(needle), "{detail:?} lacks {needle:?}");
    }

    #[test]
    fn well_formed_declaration_parses() {
        let parsed = parse(&declaration()).unwrap();
        assert_eq!(parsed.bus, "ecat0");
        assert_eq!(
            parsed.identity,
            StationIdentity {
                vendor: 21,
                product: 750354,
                revision: 1
            }
        );
        assert_eq!(parsed.exchange_miss_threshold, 3);
        assert_eq!(parsed.startup, StartupPolicy::FailOnMismatch);
        assert_eq!(
            parsed.inputs["di1"],
            ImageOffset {
                bit_offset: 1,
                bits: 1
            }
        );
        assert_eq!(
            parsed.inputs["ai0"],
            ImageOffset {
                bit_offset: 8,
                bits: 16
            }
        );
        assert_eq!(
            parsed.outputs["do0"],
            ImageOffset {
                bit_offset: 0,
                bits: 1
            }
        );
        assert_eq!(parsed.safe_outputs["do1"], Value::Bool(false));
    }

    #[test]
    fn missing_or_malformed_bus_is_named() {
        assert_err(without("bus"), "\"bus\" is required");
        assert_err(with("bus", json!(0)), "\"bus\" must be a string");
        assert_err(with("bus", json!("")), "non-empty");
    }

    #[test]
    fn malformed_identity_is_named() {
        assert_err(without("identity"), "\"identity\" is required");
        assert_err(with("identity", json!("wago")), "must be an object");
        assert_err(
            with("identity", json!({"vendor": 21, "product": 750354})),
            "missing field \"revision\"",
        );
        assert_err(
            with(
                "identity",
                json!({"vendor": "wago", "product": 750354, "revision": 1}),
            ),
            "\"vendor\" must be a non-negative integer",
        );
        assert_err(
            with(
                "identity",
                json!({"vendor": 21, "product": 750354, "revision": 1, "serial": 7}),
            ),
            "unknown \"identity\" field \"serial\"",
        );
    }

    #[test]
    fn malformed_threshold_is_named() {
        assert_err(
            without("exchange_miss_threshold"),
            "\"exchange_miss_threshold\" is required",
        );
        assert_err(
            with("exchange_miss_threshold", json!("3")),
            "must be a positive integer",
        );
        assert_err(
            with("exchange_miss_threshold", json!(0)),
            "must be at least 1",
        );
    }

    #[test]
    fn malformed_startup_policy_is_named() {
        assert_err(without("startup"), "\"startup\" is required");
        assert_err(with("startup", json!("fail")), "must be an object");
        assert_err(
            with("startup", json!({"on_mismatch": "simulate"})),
            "admits \"fail\" only",
        );
        assert_err(with("startup", json!({})), "missing field \"on_mismatch\"");
    }

    #[test]
    fn unknown_parameter_is_named() {
        assert_err(
            with("interface", json!("eth0")),
            "unknown parameter \"interface\"",
        );
    }

    #[test]
    fn missing_or_malformed_mapping_is_named() {
        assert_err(without("mapping"), "\"mapping\" is required");
        assert_err(with("mapping", json!({})), "does not place");
        assert_err(with("mapping", json!([])), "must be an object");
        assert_err(
            with("mapping", json!({"txpdo": {}})),
            "unknown \"mapping\" image",
        );
    }

    #[test]
    fn offset_collision_is_named() {
        assert_err(
            with_at("mapping.inputs.di1", json!({"byte": 0, "bit": 0})),
            "overlap",
        );
        // A byte-aligned field overlapping a bit also collides.
        assert_err(
            with_at("mapping.inputs.ai0", json!({"byte": 0, "bits": 16})),
            "overlap",
        );
    }

    #[test]
    fn wrong_image_or_undeclared_channel_is_named() {
        // An `out` channel placed in the input image is a direction
        // mismatch; that it would leave `outputs` short is diagnosed
        // after the wrong-image entry is named.
        assert_err(
            with_at("mapping.inputs.do0", json!({"byte": 0, "bit": 8})),
            "places Out channel \"do0\"",
        );
        // A channel the device does not declare cannot take an offset.
        assert_err(
            with_at("mapping.inputs.di9", json!({"byte": 0, "bit": 7})),
            "does not declare",
        );
    }

    #[test]
    fn mapping_entries_kind_mismatch_is_named() {
        // An `int` channel declared with a bit offset takes a `bits`
        // width instead.
        assert_err(
            with_at("mapping.inputs.ai0", json!({"byte": 1, "bit": 0})),
            "unknown field \"bit\"",
        );
        // A `bool` channel cannot carry a `bits` width.
        assert_err(
            with_at("mapping.inputs.di0", json!({"byte": 0, "bits": 8})),
            "unknown field \"bits\"",
        );
        // A width outside the kind's admitted set is named.
        assert_err(
            with_at("mapping.inputs.ai0", json!({"byte": 1, "bits": 24})),
            "admits [8, 16, 32, 64] only",
        );
    }

    #[test]
    fn unmapped_channel_is_named() {
        let mut parameters = declaration();
        parameters
            .get_mut("mapping")
            .and_then(|mapping| mapping.get_mut("inputs"))
            .and_then(|inputs| inputs.as_object_mut())
            .unwrap()
            .remove("di1");
        assert_err(
            parse(&parameters),
            "does not place declared channel \"di1\"",
        );
    }

    #[test]
    fn malformed_safe_outputs_is_named() {
        // An `in` channel cannot take a safe state.
        assert_err(
            with(
                "safe_outputs",
                json!({"di0": {"bool": false}, "do0": {"bool": false}, "do1": {"bool": false}}),
            ),
            "safe states apply to out channels",
        );
        // An undeclared channel cannot take one either.
        assert_err(
            with(
                "safe_outputs",
                json!({"do0": {"bool": false}, "do1": {"bool": false}, "do9": {"bool": false}}),
            ),
            "does not declare",
        );
        // A safe state must match the channel's kind.
        assert_err(
            with(
                "safe_outputs",
                json!({"do0": {"int": 0}, "do1": {"bool": false}}),
            ),
            "is kind Int, the channel is Bool",
        );
        // A missing safe state for a declared out channel is named.
        assert_err(
            with("safe_outputs", json!({"do0": {"bool": false}})),
            "must declare a safe state for out channel \"do1\"",
        );
        // A malformed tagged value is named.
        assert_err(
            with("safe_outputs", json!({"do0": true, "do1": {"bool": false}})),
            "is not a tagged Value",
        );
    }

    #[test]
    fn device_without_out_channels_accepts_no_safe_outputs() {
        let mut parameters = declaration();
        parameters
            .get_mut("mapping")
            .and_then(|mapping| mapping.as_object_mut())
            .unwrap()
            .insert("outputs".to_string(), json!({}));
        parameters.remove("safe_outputs");
        let channels: BTreeMap<String, ChannelDecl> = channels()
            .into_iter()
            .filter(|(_, decl)| decl.direction == Direction::In)
            .collect();
        let parsed = DeviceParameters::parse(&parameters, &channels).unwrap();
        assert!(parsed.safe_outputs.is_empty());
    }
}
