//! The `ethercat` device-parameter contract: what a model device
//! declares for the kind — the logical bus it sits on, the station
//! profile the bus must discover, the channel→process-image map, the
//! declared safe state for outputs, and the cyclic contract's
//! `exchange_miss_threshold`.
//!
//! An `ethercat` device's `parameters` carry:
//!
//! - `"bus"` (required string): the logical bus name. The deployment —
//!   not the model — binds logical buses to host interfaces (decision
//!   47); several devices may share one logical bus and its master.
//! - `"exchange_miss_threshold"` (required positive integer): the
//!   cyclic contract's miss count at which reads escalate to
//!   [`dcs_core::IoError::Disconnected`].
//! - `"exchange_deadline_ms"` (optional non-negative number): the
//!   exchange deadline — a completed cycle that took longer counts as a
//!   missed deadline in diagnostics. Absent: no deadline tracking.
//! - `"stations"` (required non-empty array): the expected station
//!   profile in bus position order. Each entry declares `"position"`
//!   (its index in the array), `"vendor_id"`, `"product_id"`,
//!   `"revision"` (unsigned integers, or `"0x…"` hex strings),
//!   `"input_bytes"` and `"output_bytes"` (the station's cyclic
//!   process-data areas), and optionally `"name"`. The discovered bus
//!   must match this profile exactly — station count, identity, and
//!   per-station layout — or the device fails at startup before OP.
//! - `"layout"` (required object): every declared channel's name mapped
//!   to its process-image location — `{"station": <position>,
//!   "offset": <byte within the station's input or output area>}`, plus
//!   `"bit": <0..8>` for `bool` channels or `"width": <bytes>` for
//!   `int` (1, 2, 4, or 8 — little-endian signed) and `float` (4 or 8 —
//!   little-endian) channels. The map must cover the declared channel
//!   set exactly, each channel must fit inside its station's declared
//!   area, and no two channels may share image bits.
//! - `"safe_state"` (optional object): `out` channel name → the value
//!   the staged output image initializes with — the field's state until
//!   the first exchange publishes it. Channels not named initialize to
//!   their kind's neutral value. Values must match the channel's
//!   declared `value_type`.

use dcs_core::{Value, ValueKind};
use dcs_model::Channel;
use std::collections::BTreeMap;
use std::ops::Range;
use std::time::Duration;

/// The device-kind string the EtherCAT integration registers.
pub const DEVICE_KIND: &str = "ethercat";

/// One station in a device-declared expected profile — compared against
/// what the transport discovered before OP entry.
#[derive(Debug, Clone, PartialEq)]
pub struct StationProfile {
    /// The station's expected bus position.
    pub position: usize,
    /// The expected vendor id.
    pub vendor_id: u32,
    /// The expected product code.
    pub product_id: u32,
    /// The expected revision.
    pub revision: u32,
    /// The expected station name, when the declaration pins one.
    pub name: Option<String>,
    /// The station's expected cyclic input area in bytes.
    pub input_bytes: usize,
    /// The station's expected cyclic output area in bytes.
    pub output_bytes: usize,
}

/// One channel's declared location in the bus process image, relative
/// to its station's direction area.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ChannelLayout {
    /// The station position the channel lives on.
    pub station: usize,
    /// The channel's bit range within the station's input or output
    /// area — a single bit for `bool` channels, `width * 8` bits for
    /// `int`/`float` channels.
    pub bits: Range<usize>,
}

/// An `ethercat` device's parsed `parameters`.
#[derive(Debug, Clone, PartialEq)]
pub struct DeviceParameters {
    /// The logical bus name the deployment binds to a host interface.
    pub bus: String,
    /// The declared miss threshold: consecutive failed exchanges at
    /// which point reads escalate to `Disconnected`.
    pub miss_threshold: u64,
    /// The exchange deadline, when declared: a completed cycle past it
    /// counts as a missed deadline.
    pub deadline: Option<Duration>,
    /// The expected station profile, in bus position order.
    pub stations: Vec<StationProfile>,
    /// Channel name → its declared process-image location.
    pub layout: BTreeMap<String, ChannelLayout>,
    /// `out` channel name → its declared safe-state value, seeded into
    /// the staged output image at attach.
    pub safe_state: BTreeMap<String, Value>,
}

impl DeviceParameters {
    /// Parses and validates an `ethercat` device's `parameters` against
    /// its declared `channels` (name → [`Channel`]).
    ///
    /// The `Err` text names the offending parameter, channel, or
    /// station so the caller can wrap it — `DeviceError::parameters` at
    /// assembly.
    pub fn parse(
        parameters: &BTreeMap<String, serde_json::Value>,
        channels: &BTreeMap<String, Channel>,
    ) -> Result<Self, String> {
        for name in parameters.keys() {
            if !matches!(
                name.as_str(),
                "bus"
                    | "exchange_miss_threshold"
                    | "exchange_deadline_ms"
                    | "stations"
                    | "layout"
                    | "safe_state"
            ) {
                return Err(format!(
                    "unknown parameter {name:?}; {DEVICE_KIND:?} takes \"bus\", \"exchange_miss_threshold\", \"exchange_deadline_ms\", \"stations\", \"layout\", and \"safe_state\""
                ));
            }
        }

        let bus = parameters
            .get("bus")
            .ok_or_else(|| "parameter \"bus\" is required".to_string())?;
        let Some(bus) = bus.as_str().filter(|bus| !bus.is_empty()) else {
            return Err(format!(
                "parameter \"bus\" must be a non-empty string, found {bus}"
            ));
        };

        let miss_threshold = match parameters.get("exchange_miss_threshold") {
            None => {
                return Err("parameter \"exchange_miss_threshold\" is required".to_string());
            }
            Some(value) => {
                let Some(threshold) = value.as_u64().filter(|&threshold| threshold >= 1) else {
                    return Err(format!(
                        "parameter \"exchange_miss_threshold\" must be a positive integer, found {value}"
                    ));
                };
                threshold
            }
        };

        let deadline = match parameters.get("exchange_deadline_ms") {
            None => None,
            Some(value) => {
                let Some(ms) = value.as_f64().filter(|ms| ms.is_finite() && *ms >= 0.0) else {
                    return Err(format!(
                        "parameter \"exchange_deadline_ms\" must be a non-negative number of milliseconds, found {value}"
                    ));
                };
                Some(Duration::from_secs_f64(ms / 1000.0))
            }
        };

        let stations = parse_stations(parameters.get("stations"))?;
        let layout = parse_layout(parameters.get("layout"), &stations, channels)?;
        let safe_state = parse_safe_state(parameters.get("safe_state"), channels)?;

        Ok(Self {
            bus: bus.to_string(),
            miss_threshold,
            deadline,
            stations,
            layout,
            safe_state,
        })
    }
}

/// An unsigned integer field: a non-negative JSON integer, or a
/// `"0x…"` hex string for the hexadecimal spellings vendor and product
/// ids are conventionally written in.
fn unsigned(entry: &serde_json::Value) -> Option<u64> {
    if let Some(value) = entry.as_u64() {
        return Some(value);
    }
    entry
        .as_str()
        .and_then(|hex| hex.strip_prefix("0x"))
        .and_then(|digits| u64::from_str_radix(digits, 16).ok())
}

/// The `"stations"` array: position-ordered expected profiles, each
/// entry's `"position"` required to equal its index.
fn parse_stations(value: Option<&serde_json::Value>) -> Result<Vec<StationProfile>, String> {
    let Some(value) = value else {
        return Err("parameter \"stations\" is required".to_string());
    };
    let Some(entries) = value.as_array() else {
        return Err(format!(
            "parameter \"stations\" must be an array of station profiles, found {value}"
        ));
    };
    if entries.is_empty() {
        return Err("parameter \"stations\" must declare at least one station".to_string());
    }
    entries
        .iter()
        .enumerate()
        .map(|(index, entry)| {
            let invalid = |detail: String| format!("stations entry {index}: {detail}");
            let Some(object) = entry.as_object() else {
                return Err(invalid(format!("must be an object, found {entry}")));
            };
            for key in object.keys() {
                if !matches!(
                    key.as_str(),
                    "position" | "vendor_id" | "product_id" | "revision" | "name"
                        | "input_bytes" | "output_bytes"
                ) {
                    return Err(invalid(format!("unknown key {key:?}")));
                }
            }
            let field = |name: &str| -> Result<u64, String> {
                let Some(value) = object.get(name) else {
                    return Err(invalid(format!("missing {name:?}")));
                };
                unsigned(value)
                    .ok_or_else(|| invalid(format!("{name:?} must be an unsigned integer or \"0x…\" string, found {value}")))
            };
            let position = field("position")?;
            if position != index as u64 {
                return Err(invalid(format!(
                    "\"position\" {position} must equal the station's bus position {index}"
                )));
            }
            let name = match object.get("name") {
                None => None,
                Some(value) => match value.as_str() {
                    Some(name) => Some(name.to_string()),
                    None => {
                        return Err(invalid(format!(
                            "\"name\" must be a string, found {value}"
                        )));
                    }
                },
            };
            Ok(StationProfile {
                position: index,
                vendor_id: u32::try_from(field("vendor_id")?)
                    .map_err(|_| invalid("\"vendor_id\" exceeds u32".to_string()))?,
                product_id: u32::try_from(field("product_id")?)
                    .map_err(|_| invalid("\"product_id\" exceeds u32".to_string()))?,
                revision: u32::try_from(field("revision")?)
                    .map_err(|_| invalid("\"revision\" exceeds u32".to_string()))?,
                name,
                input_bytes: usize::try_from(field("input_bytes")?)
                    .map_err(|_| invalid("\"input_bytes\" exceeds usize".to_string()))?,
                output_bytes: usize::try_from(field("output_bytes")?)
                    .map_err(|_| invalid("\"output_bytes\" exceeds usize".to_string()))?,
            })
        })
        .collect()
}

/// The `"layout"` object: channel name → process-image location, exactly
/// covering the declared channel set.
fn parse_layout(
    value: Option<&serde_json::Value>,
    stations: &[StationProfile],
    channels: &BTreeMap<String, Channel>,
) -> Result<BTreeMap<String, ChannelLayout>, String> {
    let Some(value) = value else {
        return Err("parameter \"layout\" is required".to_string());
    };
    let Some(object) = value.as_object() else {
        return Err(format!(
            "parameter \"layout\" must be an object mapping channel names to image locations, found {value}"
        ));
    };
    let mut layout = BTreeMap::new();
    // Claimed bit ranges per direction, so two channels cannot share
    // image bits — the same check the register map makes per register.
    let mut claimed: [Vec<Range<usize>>; 2] = [Vec::new(), Vec::new()];
    for (channel, entry) in object {
        let Some(&declared) = channels.get(channel.as_str()) else {
            return Err(format!(
                "layout names channel {channel:?} the device does not declare"
            ));
        };
        let parsed = parse_layout_entry(channel, declared, entry)?;
        let area = match declared.direction {
            dcs_model::Direction::In => stations[parsed.station].input_bytes,
            dcs_model::Direction::Out => stations[parsed.station].output_bytes,
        };
        if parsed.bits.end > area * 8 {
            return Err(format!(
                "layout for channel {channel:?} exceeds station {}'s declared {:?} area of {area} bytes",
                parsed.station, declared.direction
            ));
        }
        let claimed = &mut claimed[match declared.direction {
            dcs_model::Direction::In => 0,
            dcs_model::Direction::Out => 1,
        }];
        if let Some(other) = claimed
            .iter()
            .find(|range| range.start < parsed.bits.end && parsed.bits.start < range.end)
        {
            return Err(format!(
                "layout for channel {channel:?} overlaps bits {}..{} already claimed",
                other.start, other.end
            ));
        }
        claimed.push(parsed.bits.clone());
        layout.insert(channel.clone(), parsed);
    }
    for channel in channels.keys() {
        if !layout.contains_key(channel) {
            return Err(format!("layout does not cover declared channel {channel:?}"));
        }
    }
    Ok(layout)
}

/// One `"layout"` entry: `{"station", "offset"}` plus `"bit"` for
/// `bool` channels or `"width"` for `int`/`float` channels.
fn parse_layout_entry(
    channel: &str,
    declared: Channel,
    entry: &serde_json::Value,
) -> Result<ChannelLayout, String> {
    let invalid = |detail: String| format!("layout for channel {channel:?}: {detail}");
    let Some(object) = entry.as_object() else {
        return Err(invalid(format!("must be an object, found {entry}")));
    };
    for key in object.keys() {
        if !matches!(key.as_str(), "station" | "offset" | "bit" | "width") {
            return Err(invalid(format!("unknown key {key:?}")));
        }
    }
    let integer = |name: &str| -> Result<u64, String> {
        let Some(value) = object.get(name) else {
            return Err(invalid(format!("missing {name:?}")));
        };
        value.as_u64().ok_or_else(|| {
            invalid(format!("{name:?} must be a non-negative integer, found {value}"))
        })
    };
    let station = usize::try_from(integer("station")?)
        .map_err(|_| invalid("\"station\" exceeds usize".to_string()))?;
    let offset = usize::try_from(integer("offset")?)
        .map_err(|_| invalid("\"offset\" exceeds usize".to_string()))?;
    let bit = match object.get("bit") {
        None => None,
        Some(value) => {
            let Some(bit) = value.as_u64().filter(|&bit| bit < 8) else {
                return Err(invalid(format!(
                    "\"bit\" must be in 0..8, found {value}"
                )));
            };
            Some(bit as usize)
        }
    };
    let width = match object.get("width") {
        None => None,
        Some(value) => {
            let Some(width) = value.as_u64() else {
                return Err(invalid(format!(
                    "\"width\" must be a positive integer, found {value}"
                )));
            };
            Some(usize::try_from(width)
                .map_err(|_| invalid("\"width\" exceeds usize".to_string()))?)
        }
    };
    let bits = match declared.value_type {
        ValueKind::Bool => {
            if width.is_some() {
                return Err(invalid(
                    "\"width\" is not valid on a bool channel — use \"bit\"".to_string(),
                ));
            }
            let Some(bit) = bit else {
                return Err(invalid("a bool channel requires \"bit\"".to_string()));
            };
            offset * 8 + bit..offset * 8 + bit + 1
        }
        ValueKind::Int | ValueKind::Float => {
            if bit.is_some() {
                return Err(invalid(
                    "\"bit\" is not valid on an int or float channel — use \"width\"".to_string(),
                ));
            }
            let allowed: &[usize] = match declared.value_type {
                ValueKind::Int => &[1, 2, 4, 8],
                ValueKind::Float => &[4, 8],
                ValueKind::Bool => unreachable!(),
            };
            let Some(width) = width.filter(|width| allowed.contains(width)) else {
                return Err(invalid(format!(
                    "\"width\" must be one of {allowed:?} bytes for a {:?} channel",
                    declared.value_type
                )));
            };
            offset * 8..(offset + width) * 8
        }
    };
    Ok(ChannelLayout { station, bits })
}

/// The `"safe_state"` object: `out` channel name → declared safe value.
fn parse_safe_state(
    value: Option<&serde_json::Value>,
    channels: &BTreeMap<String, Channel>,
) -> Result<BTreeMap<String, Value>, String> {
    let Some(value) = value else {
        return Ok(BTreeMap::new());
    };
    let Some(object) = value.as_object() else {
        return Err(format!(
            "parameter \"safe_state\" must be an object mapping out channel names to values, found {value}"
        ));
    };
    let mut safe = BTreeMap::new();
    for (channel, entry) in object {
        let Some(&declared) = channels.get(channel.as_str()) else {
            return Err(format!(
                "safe_state names channel {channel:?} the device does not declare"
            ));
        };
        if declared.direction != dcs_model::Direction::Out {
            return Err(format!(
                "safe_state names {channel:?}, an in channel; safe state applies to outputs"
            ));
        }
        let value: Value = serde_json::from_value(entry.clone()).map_err(|error| {
            format!("safe_state for channel {channel:?} is not a signal value: {error}")
        })?;
        if value.kind() != declared.value_type {
            return Err(format!(
                "safe_state for channel {channel:?} must match its {:?} kind, found {entry}",
                declared.value_type
            ));
        }
        safe.insert(channel.clone(), value);
    }
    Ok(safe)
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_model::Direction;
    use serde_json::json;

    fn channels() -> BTreeMap<String, Channel> {
        [
            (
                "di-1".to_string(),
                Channel {
                    direction: Direction::In,
                    value_type: ValueKind::Bool,
                },
            ),
            (
                "do-1".to_string(),
                Channel {
                    direction: Direction::Out,
                    value_type: ValueKind::Bool,
                },
            ),
            (
                "ai-1".to_string(),
                Channel {
                    direction: Direction::In,
                    value_type: ValueKind::Float,
                },
            ),
        ]
        .into_iter()
        .collect()
    }

    fn complete() -> serde_json::Value {
        json!({
            "bus": "fieldbus0",
            "exchange_miss_threshold": 3,
            "stations": [{
                "position": 0,
                "vendor_id": "0x000000ad",
                "product_id": 950,
                "revision": 2,
                "name": "750-354",
                "input_bytes": 4,
                "output_bytes": 1
            }],
            "layout": {
                "di-1": {"station": 0, "offset": 0, "bit": 0},
                "ai-1": {"station": 0, "offset": 0, "width": 4},
                "do-1": {"station": 0, "offset": 0, "bit": 0}
            },
            "safe_state": {"do-1": {"bool": false}}
        })
    }

    #[test]
    fn a_complete_declaration_parses() {
        let parameters: BTreeMap<String, serde_json::Value> =
            serde_json::from_value(complete()).unwrap();
        let parsed = DeviceParameters::parse(&parameters, &channels()).unwrap();
        assert_eq!(parsed.bus, "fieldbus0");
        assert_eq!(parsed.miss_threshold, 3);
        assert_eq!(parsed.stations.len(), 1);
        assert_eq!(parsed.stations[0].vendor_id, 0xad);
        assert_eq!(parsed.stations[0].input_bytes, 4);
        assert_eq!(parsed.layout["di-1"].bits, 0..1);
        assert_eq!(parsed.layout["ai-1"].bits, 0..32);
        assert_eq!(parsed.layout["do-1"].bits, 0..1);
        assert_eq!(parsed.safe_state["do-1"], Value::Bool(false));
    }

    #[test]
    fn malformed_declarations_are_named_errors() {
        let cases = [
            // No bus.
            json!({"exchange_miss_threshold": 1, "stations": [], "layout": {}}),
            // Missing the miss threshold.
            json!({"bus": "b", "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":0,"output_bytes":0}], "layout": {}}),
            // A miss threshold of zero would escalate every read.
            json!({"bus": "b", "exchange_miss_threshold": 0, "stations": [], "layout": {}}),
            // Station position out of order.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":1,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":0,"output_bytes":0}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0}}}),
            // A channel the device does not declare.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0},"xx":{"station":0,"offset":1,"bit":0}}}),
            // A declared channel left unmapped.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4}}}),
            // A channel exceeding its station's declared area.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":1,"output_bytes":1}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0}}}),
            // Two channels sharing bits.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0}}, "safe_state": {"di-1": {"bool": true}}}),
            // A bool channel without a bit.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0}}}),
            // A float channel at width 8 is fine; width 2 is not.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":2},"do-1":{"station":0,"offset":0,"bit":0}}}),
            // An unknown top-level parameter.
            json!({"bus": "b", "exchange_miss_threshold": 1, "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}], "layout": {"di-1":{"station":0,"offset":0,"bit":0},"ai-1":{"station":0,"offset":0,"width":4},"do-1":{"station":0,"offset":0,"bit":0}}, "bogus": 1}),
        ];
        for parameters in cases {
            let map: BTreeMap<String, serde_json::Value> =
                serde_json::from_value(parameters.clone()).unwrap();
            assert!(
                DeviceParameters::parse(&map, &channels()).is_err(),
                "{parameters}"
            );
        }
    }

    #[test]
    fn shared_bits_across_channels_are_rejected() {
        let parameters = json!({
            "bus": "b",
            "exchange_miss_threshold": 1,
            "stations": [{"position":0,"vendor_id":1,"product_id":1,"revision":1,"input_bytes":8,"output_bytes":8}],
            "layout": {
                "di-1": {"station":0,"offset":0,"bit":0},
                "ai-1": {"station":0,"offset":0,"width":4},
                "do-1": {"station":0,"offset":0,"bit":0}
            }
        });
        // di-1 and ai-1 overlap at input bits 0..1.
        let map: BTreeMap<String, serde_json::Value> =
            serde_json::from_value(parameters).unwrap();
        let error = DeviceParameters::parse(&map, &channels()).unwrap_err();
        assert!(error.contains("overlap"), "{error}");
    }
}
